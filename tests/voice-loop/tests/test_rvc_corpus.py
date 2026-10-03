"""Tests for `scripts/rvc_corpus.py` — "is there enough of the target voice to train a converter?".

No network, no models, no audio hardware. The corpus is a directory of real (silent) PCM WAV files
written into ``tmp_path``; the script reads their headers, which is all it ever reads.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import wave
from pathlib import Path

import pytest

# The module under test lives in scripts/; add it to the path.
_scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import rvc_corpus


def write_clip(root: Path, language: str, name: str, seconds: float, text: str | None = None) -> Path:
    """One recorded sentence in the layout the server writes: <corpus>/<language>/<name>.wav."""
    directory = root / language
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * int(24000 * seconds))
    if text is not None:
        path.with_suffix(".txt").write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def corpus(tmp_path) -> Path:
    """Two languages, one clip too short to use and one too long — the interesting shape."""
    root = tmp_path / "corpus"
    write_clip(root, "ru", "a", 3.0, "Первое предложение.")
    write_clip(root, "ru", "b", 5.0, "Второе предложение.")
    write_clip(root, "ru", "tiny", 0.4, "Да.")
    write_clip(root, "ru", "huge", 25.0, "Очень длинный кусок.")
    write_clip(root, "en", "c", 4.0)  # no transcript beside it
    return root


# --- reading the corpus -------------------------------------------------------------------------


def test_every_clip_is_read_with_its_language_and_transcript(corpus):
    clips = rvc_corpus.read_corpus(corpus)

    assert [(clip.language, round(clip.seconds, 1)) for clip in clips] == [
        ("en", 4.0),
        ("ru", 3.0),
        ("ru", 5.0),
        ("ru", 25.0),
        ("ru", 0.4),
    ]
    assert [clip.text for clip in clips if clip.language == "en"] == [""]


def test_a_file_that_is_not_a_readable_wav_is_skipped(corpus):
    (corpus / "ru" / "half-written.wav").write_bytes(b"RIFF truncated")
    (corpus / "ru" / "adirectory.wav").mkdir()

    assert len(rvc_corpus.read_corpus(corpus)) == 5


def test_an_unreadable_transcript_leaves_the_clip_usable(corpus):
    (corpus / "ru" / "a.txt").write_bytes(b"\xff\xfe not utf-8 \xff")

    clip = next(clip for clip in rvc_corpus.read_corpus(corpus) if clip.path.stem == "a")
    assert (clip.text, clip.usable) == ("", True)


def test_clip_seconds_is_none_for_a_header_with_no_rate(tmp_path):
    """A rate of zero would divide by it; the corpus is the operator's and may hold anything."""
    path = tmp_path / "broken.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(1)
        handle.writeframes(b"\x00\x00")
    path.write_bytes(path.read_bytes().replace((1).to_bytes(4, "little"), (0).to_bytes(4, "little")))

    assert rvc_corpus.clip_seconds(path) is None


# --- the summary --------------------------------------------------------------------------------


def test_readiness_counts_usable_audio_not_everything_on_disk(corpus):
    """The 0.4 s fragment and the 25 s artefact are on disk but would make a converter worse."""
    summary = rvc_corpus.summarize(rvc_corpus.read_corpus(corpus), min_minutes=0.2)

    assert (summary["clips"], summary["usable_clips"], summary["skipped_clips"]) == (5, 3, 2)
    assert summary["usable_seconds"] == pytest.approx(12.0)
    assert summary["seconds"] == pytest.approx(37.4)
    assert summary["ready"] is True  # 12 s of usable audio, against a 12 s mark


def test_the_mark_is_missed_on_usable_audio_even_when_the_directory_looks_full(corpus):
    summary = rvc_corpus.summarize(rvc_corpus.read_corpus(corpus), min_minutes=0.5)

    assert summary["ready"] is False


def test_each_language_is_counted_on_its_own(corpus):
    summary = rvc_corpus.summarize(rvc_corpus.read_corpus(corpus), min_minutes=10)

    assert summary["languages"]["en"] == {"clips": 1, "seconds": 4.0, "shortest": 4.0, "longest": 4.0}
    assert summary["languages"]["ru"]["shortest"] == pytest.approx(0.4)
    assert summary["languages"]["ru"]["longest"] == pytest.approx(25.0)


def test_duration_is_stated_in_the_minutes_the_target_is_stated_in():
    assert rvc_corpus.duration(0) == "0m 00s"
    assert rvc_corpus.duration(67.9) == "1m 07s"
    assert rvc_corpus.duration(1200) == "20m 00s"


# --- the manifest -------------------------------------------------------------------------------


def test_the_manifest_holds_one_json_object_per_usable_clip(corpus, tmp_path):
    destination = tmp_path / "train.jsonl"

    rows = rvc_corpus.write_manifest(str(destination), rvc_corpus.read_corpus(corpus))

    written = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert rows == len(written) == 3
    assert {row["language"] for row in written} == {"ru", "en"}
    assert written[0] == {
        "audio": str(corpus / "en" / "c.wav"),
        "language": "en",
        "seconds": 4.0,
        "text": "",
    }
    assert all(rvc_corpus.MIN_CLIP_SECONDS <= row["seconds"] <= rvc_corpus.MAX_CLIP_SECONDS for row in written)


def test_a_manifest_of_dash_goes_to_stdout(corpus, capsys):
    rvc_corpus.write_manifest("-", rvc_corpus.read_corpus(corpus))

    assert len(capsys.readouterr().out.strip().splitlines()) == 3


# --- the CLI ------------------------------------------------------------------------------------


def run(*args: str) -> int:
    return rvc_corpus.main(["rvc_corpus.py", *args])


def test_the_report_names_the_languages_and_the_verdict(corpus, capsys):
    assert run("--corpus", str(corpus), "--min-minutes", "0.2") == 0

    out = capsys.readouterr().out
    assert str(corpus) in out
    assert "ru" in out and "en" in out
    assert "READY" in out


def test_not_enough_yet_says_how_much_is_missing_and_exits_1(corpus, capsys):
    assert run("--corpus", str(corpus)) == 1

    out = capsys.readouterr().out
    assert "NOT YET" in out
    assert "9m 48s short" in out  # 10m00s - 12s of usable audio


def test_an_empty_corpus_says_so_rather_than_reading_as_broken(tmp_path, capsys):
    (tmp_path / "corpus").mkdir()

    assert run("--corpus", str(tmp_path / "corpus")) == 1
    assert "empty" in capsys.readouterr().out


def test_a_corpus_path_that_is_not_a_directory_is_not_the_same_as_an_empty_one(tmp_path, capsys):
    assert run("--corpus", str(tmp_path / "typo")) == 1
    assert "not a directory" in capsys.readouterr().err


def test_json_output_carries_the_whole_summary(corpus, capsys):
    assert run("--corpus", str(corpus), "--min-minutes", "0.2", "--json") == 0

    body = json.loads(capsys.readouterr().out)
    assert body["corpus"] == str(corpus)
    assert body["ready"] is True
    assert body["usable_clips"] == 3


def test_the_manifest_is_written_and_reported(corpus, tmp_path, capsys):
    destination = tmp_path / "train.jsonl"

    assert run("--corpus", str(corpus), "--min-minutes", "0.2", "--manifest", str(destination)) == 0

    assert f"3 clips -> {destination}" in capsys.readouterr().out
    assert len(destination.read_text(encoding="utf-8").splitlines()) == 3


def test_the_manifest_row_count_rides_along_in_json(corpus, tmp_path, capsys):
    destination = tmp_path / "train.jsonl"

    run("--corpus", str(corpus), "--json", "--manifest", str(destination))

    assert json.loads(capsys.readouterr().out)["manifest_rows"] == 3


def test_the_corpus_directory_comes_from_the_servers_own_variable(corpus, monkeypatch, capsys):
    monkeypatch.setenv("VOICE_LOOP_CORPUS_DIR", str(corpus))

    assert run("--min-minutes", "0.2") == 0
    assert str(corpus) in capsys.readouterr().out


def test_no_corpus_anywhere_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.delenv("VOICE_LOOP_CORPUS_DIR", raising=False)

    assert run() == 2
    assert "VOICE_LOOP_CORPUS_DIR" in capsys.readouterr().err


def test_two_writers_of_stdout_are_refused_rather_than_interleaved(corpus, capsys):
    assert run("--corpus", str(corpus), "--json", "--manifest", "-") == 2
    assert "pick one" in capsys.readouterr().err


def test_min_minutes_wants_a_number(corpus, capsys):
    assert run("--corpus", str(corpus), "--min-minutes", "soon") == 2
    assert "wants a number" in capsys.readouterr().err


def test_help_and_unknown_arguments(corpus, capsys):
    assert run("--help") == 0
    assert "usage:" in capsys.readouterr().err

    assert run("--corpus", str(corpus), "--frobnicate") == 2
    assert "unknown argument: --frobnicate" in capsys.readouterr().err


def test_argv_defaults_to_the_real_one(monkeypatch, corpus, capsys):
    monkeypatch.setattr(sys, "argv", ["rvc_corpus.py", "--corpus", str(corpus), "--min-minutes", "0.2"])

    assert rvc_corpus.main() == 0
    assert "READY" in capsys.readouterr().out


def test_script_entrypoint_reports_a_missing_corpus(monkeypatch):
    """The executable entry point keeps the documented usage error for a missing corpus."""
    monkeypatch.setattr(sys, "argv", ["rvc_corpus.py"])
    monkeypatch.delenv("VOICE_LOOP_CORPUS_DIR", raising=False)
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(Path(_scripts_dir) / "rvc_corpus.py"), run_name="__main__")

    assert raised.value.code == 2
