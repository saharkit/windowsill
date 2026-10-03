"""The RVC training corpus — the bootstrap that has to exist before the recolor stage can be trained.

RVC wants 10-30 minutes of the target voice; XTTS-v2 clones from 6-30 seconds. This is how the gap
closes without anybody recording for half an hour: the cloned voice writes down what it says as it
says it, one clip per sentence chunk, and `scripts/rvc_corpus.py` reads the result.

Nothing here touches a model — the xtts engine is the usual fake, and what is asserted is the pair of
files that lands on disk, the cap that stops it growing, and the rule that a corpus which cannot be
written never costs anybody their voice.
"""

from __future__ import annotations

import os

import threading

import pytest

import voice_server
from conftest import FakeXtts, pcm_wav

LONG_TEXT = " ".join(f"Предложение номер {i}." for i in range(120))  # several sentence chunks


class VaryingXtts(FakeXtts):
    """A cloned voice whose sentences differ in length — so their clips differ, as real ones do.

    The plain FakeXtts returns the same 1200 zeros for every sentence, which is exactly the same WAV
    every time; content addressing then stores ONE clip for the whole text (see the dedup test).
    """

    def tts(self, text: str, speaker_wav: str, language: str) -> list[float]:
        super().tts(text=text, speaker_wav=speaker_wav, language=language)
        return [0.0] * (1200 + len(text))


@pytest.fixture
def varying_xtts(monkeypatch, xtts_engine):
    model = VaryingXtts()
    monkeypatch.setattr(voice_server, "xtts", lambda: model)
    return model


def clips(root, language: str = "ru") -> list:
    return sorted((root / language).glob("*.wav"))


# --- what lands on disk -----------------------------------------------------------------------------


def test_an_xtts_render_writes_a_clip_and_its_transcript(client, varying_xtts, corpus_dir):
    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    (clip,) = clips(corpus_dir)
    assert clip.read_bytes().startswith(b"RIFF")
    assert clip.with_suffix(".txt").read_text(encoding="utf-8") == "Привет.\n"


def test_clips_are_filed_by_language(client, varying_xtts, corpus_dir):
    client.post("/tts", json={"text": "Hello there.", "language": "en"})

    assert clips(corpus_dir, "en") and not (corpus_dir / "ru").exists()


def test_a_stream_fills_the_corpus_sentence_by_sentence(client, varying_xtts, corpus_dir):
    client.post("/tts/stream", json={"text": LONG_TEXT})

    assert len(clips(corpus_dir)) == len(varying_xtts.calls) > 1


def test_the_same_sentence_is_stored_once(client, fake_xtts, corpus_dir):
    """Content-addressed: a corpus is worse, not better, for holding the same seconds twice."""
    client.post("/tts", json={"text": LONG_TEXT})  # many chunks, identical audio from FakeXtts

    assert len(clips(corpus_dir)) == 1
    assert voice_server.corpus_totals() == (1, pytest.approx(0.05))


def test_the_silero_voice_is_never_recorded(client, fake_silero, corpus_dir):
    """The corpus trains a converter on the CLONED voice; Silero's stock speakers are not it."""
    client.post("/tts", json={"text": "Привет."})

    assert not corpus_dir.exists()


def test_nothing_is_recorded_without_a_corpus_directory(client, fake_xtts, tmp_path):
    client.post("/tts", json={"text": "Привет."})

    # The engine's own reference wav and nothing else: the corpus is off unless it is asked for.
    assert [path.name for path in tmp_path.iterdir()] == ["reference.wav"]


# --- the cap ----------------------------------------------------------------------------------------


def test_recording_stops_at_the_cap_and_says_so_once(client, varying_xtts, corpus_dir, caplog, monkeypatch):
    monkeypatch.setattr(voice_server, "CORPUS_MAX_SECONDS", 0.2)

    with caplog.at_level("INFO"):
        client.post("/tts/stream", json={"text": LONG_TEXT})

    clip_count, seconds = voice_server.corpus_totals()
    assert clip_count == len(clips(corpus_dir)) <= 5  # ~0.05 s each, so the cap lands in a handful
    assert seconds >= 0.2
    assert caplog.text.count("VOICE_LOOP_CORPUS_MAX_SECONDS cap") == 1


def test_a_corpus_that_is_already_full_records_nothing_more(client, varying_xtts, corpus_dir, monkeypatch):
    """The cap counts what is ALREADY on disk, so a restart does not start the corpus growing again."""
    monkeypatch.setattr(voice_server, "CORPUS_MAX_SECONDS", 0.01)
    seed = corpus_dir / "ru" / "seed.wav"
    seed.parent.mkdir(parents=True)
    seed.write_bytes(pcm_wav(seconds=1.0))

    client.post("/tts", json={"text": "Привет."})

    assert clips(corpus_dir) == [seed]


# --- counting what is already there -----------------------------------------------------------------


def test_the_corpus_on_disk_is_counted_once_and_then_kept_in_step(client, varying_xtts, corpus_dir):
    (corpus_dir / "ru").mkdir(parents=True)
    (corpus_dir / "ru" / "seed.wav").write_bytes(pcm_wav(seconds=2.0))

    assert voice_server.corpus_totals() == (1, pytest.approx(2.0))

    client.post("/tts", json={"text": "Привет."})
    clip_count, seconds = voice_server.corpus_totals()

    assert clip_count == 2
    assert seconds == pytest.approx(2.05, abs=0.01)  # the scan, plus what this process wrote


def test_a_clip_that_cannot_be_read_or_parsed_is_skipped_not_fatal(corpus_dir):
    (corpus_dir / "ru").mkdir(parents=True)
    (corpus_dir / "ru" / "good.wav").write_bytes(pcm_wav(seconds=1.0))
    (corpus_dir / "ru" / "junk.wav").write_bytes(b"not a wav at all")
    (corpus_dir / "ru" / "adirectory.wav").mkdir()  # read_bytes raises IsADirectoryError

    assert voice_server.scan_corpus(corpus_dir) == (1, pytest.approx(1.0))


def test_an_absent_corpus_directory_counts_as_empty(corpus_dir):
    assert voice_server.corpus_totals() == (0, 0.0)


# --- failure never costs the voice ------------------------------------------------------------------


def test_a_corpus_that_cannot_be_written_is_logged_and_the_voice_is_unaffected(
    client, varying_xtts, corpus_dir, caplog
):
    corpus_dir.write_text("this is a file where a directory should be")  # mkdir will fail

    with caplog.at_level("WARNING"):
        response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    assert response.content.startswith(b"RIFF")
    assert "could not record a corpus clip" in caplog.text
    assert voice_server.corpus_totals()[0] == 0


# --- the atomic write -------------------------------------------------------------------------------


def test_a_clip_is_replaced_into_place_never_written_in_place(tmp_path, monkeypatch):
    """The trainer scans this directory while the server writes it: it sees a whole clip or none."""
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def record(src, dst):
        replaced.append((os.path.basename(src), os.path.basename(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(voice_server.os, "replace", record)
    voice_server.atomic_write(tmp_path / "clip.wav", b"RIFFdata")

    assert (tmp_path / "clip.wav").read_bytes() == b"RIFFdata"
    assert [dst for _src, dst in replaced] == ["clip.wav"]
    assert all(src.startswith(".voice-loop-corpus-") for src, _dst in replaced)


def test_a_failed_replace_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_server.os, "replace", _raise(OSError("no space left")))

    with pytest.raises(OSError):
        voice_server.atomic_write(tmp_path / "clip.wav", b"RIFFdata")

    assert list(tmp_path.iterdir()) == []


def test_the_original_failure_survives_a_cleanup_that_also_fails(tmp_path, monkeypatch):
    """Best-effort means best-effort: an unlink that fails must not mask what actually went wrong."""
    monkeypatch.setattr(voice_server.os, "replace", _raise(OSError("no space left")))
    monkeypatch.setattr(voice_server.os, "unlink", _raise(OSError("nor here")))

    with pytest.raises(OSError, match="no space left"):
        voice_server.atomic_write(tmp_path / "clip.wav", b"RIFFdata")


def _raise(error: BaseException):
    def raiser(*args: object, **kwargs: object):
        raise error

    return raiser


# --- /health ----------------------------------------------------------------------------------------


def test_health_reports_nulls_when_no_corpus_is_configured(client):
    body = client.get("/health").json()

    assert body["corpus_clips"] is None
    assert body["corpus_seconds"] is None


def test_health_reports_how_far_the_corpus_has_got(client, varying_xtts, corpus_dir):
    """Nulls until a write has counted something, then the counters that write maintains.

    /health must never be the thing that walks the directory, so a configured-but-unwritten corpus
    reads null — "not counted yet" — and only a WRITE turns that into numbers.
    """
    empty = client.get("/health").json()
    assert (empty["corpus_clips"], empty["corpus_seconds"]) == (None, None)

    client.post("/tts", json={"text": "Привет."})
    body = client.get("/health").json()

    assert body["corpus_clips"] == 1
    assert body["corpus_seconds"] == pytest.approx(0.1, abs=0.05)


class RecordingLock:
    """A real threading.Lock that also counts acquires — the corpus-lock instrumentation.

    Delegates every method to a real lock, so the only new thing is the counter; nothing about
    mutual exclusion is reimplemented.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.acquires = 0

    def acquire(self, *args, **kwargs) -> bool:
        self.acquires += 1
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "RecordingLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def test_health_holds_no_corpus_lock_and_opens_no_wav(client, monkeypatch, corpus_dir):
    """L2 GAP: deleting this lets /health walk the corpus again — the one cheap endpoint becomes
    the one that stalls on every clip on disk. The lock is instrumented and WAV opens are counted,
    so a regression to the scan path fails loudly instead of as a slow /health."""
    (corpus_dir / "ru").mkdir(parents=True)
    for i in range(50):
        (corpus_dir / "ru" / f"clip-{i:02}.wav").write_bytes(pcm_wav(seconds=1.0))

    lock = RecordingLock()
    monkeypatch.setattr(voice_server, "_corpus_lock", lock)

    opened: list[object] = []
    real_read_bytes = voice_server.Path.read_bytes

    def counting_read_bytes(self, *args, **kwargs):
        opened.append(self)
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(voice_server.Path, "read_bytes", counting_read_bytes)

    body = client.get("/health").json()

    assert lock.acquires == 0
    assert opened == []
    # And it still answers honestly: nothing has been written by this process, so nothing counted.
    assert (body["corpus_clips"], body["corpus_seconds"]) == (None, None)
