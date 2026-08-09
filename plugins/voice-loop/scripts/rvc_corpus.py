#!/usr/bin/env python3
"""voice-loop — the RVC training corpus: how much of it there is, and the manifest to train from.

Why it exists. The speech server's recolor stage (``VOICE_LOOP_RVC_URL``) sends each synthesized
piece through an RVC voice-conversion service, which repaints its timbre into a target voice. That
converter has to be TRAINED first, and RVC wants 10-30 minutes of the target — two orders of
magnitude more than the 6-30 second reference XTTS-v2 clones from. ``VOICE_LOOP_CORPUS_DIR`` is how
the gap closes without anybody recording for half an hour: the cloned voice writes down what it says
as it says it. This script is the other end of that pipe — it answers "is there enough yet?" and
hands a trainer the list of clips worth training on.

What it reads is the layout the server writes:

    <corpus>/<language>/<digest>.wav     one sentence chunk of synthesized audio
    <corpus>/<language>/<digest>.txt     the text that produced it (optional — RVC converts
                                         timbre, not words, so a clip without one is still usable)

Usage:
  python3 rvc_corpus.py [--corpus DIR] [--min-minutes N] [--manifest FILE] [--json]

    --corpus DIR       where the corpus lives (default: $VOICE_LOOP_CORPUS_DIR)
    --min-minutes N    how much counts as enough (default: 10 — RVC's lower bound)
    --manifest FILE    write one JSON object per usable clip to FILE (JSONL), "-" for stdout
    --json             print the summary as JSON instead of a table

Exit codes: 0 when the corpus has reached --min-minutes, 1 when it has not (or cannot be read),
2 on a usage error. So a scripted "train when ready" is one `if` around this.

Stdlib only, Python 3.10+. Reads WAV headers; never decodes audio and never touches the network.
"""

from __future__ import annotations

import json
import os
import sys
import wave
from pathlib import Path
from typing import NamedTuple

# Clip hygiene, and the reason the manifest is not simply "every wav in the directory". A fragment
# under a second carries no usable pitch contour, and a very long one is usually a chunker artefact
# (a pasted URL, a run of CJK) rather than speech — both make a converter worse, not better.
MIN_CLIP_SECONDS = 1.0
MAX_CLIP_SECONDS = 20.0

# RVC's own guidance: 10 minutes is where a converter starts sounding like the target, 30 is where
# more stops helping. The server's VOICE_LOOP_CORPUS_MAX_SECONDS default stops recording at 30.
DEFAULT_MIN_MINUTES = 10.0

CLIP_GLOB = "*/*.wav"


class Clip(NamedTuple):
    """One recorded sentence: where it is, what language it is, how long, and what was said."""

    path: Path
    language: str
    seconds: float
    text: str

    @property
    def usable(self) -> bool:
        return MIN_CLIP_SECONDS <= self.seconds <= MAX_CLIP_SECONDS


def clip_seconds(path: Path) -> float | None:
    """Duration from the WAV header, or None when the file is not a readable PCM WAV.

    Header-only on purpose: a corpus is thousands of files and the answer is in the first 44 bytes.
    A clip the stdlib parser rejects is skipped rather than fatal — the directory is the operator's,
    and one bad file from an interrupted write must not stop the report.
    """
    try:
        with wave.open(str(path)) as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else None
    except (OSError, wave.Error, EOFError):
        return None


def clip_text(path: Path) -> str:
    """The transcript beside a clip, or "" when there is none or it cannot be read."""
    try:
        return path.with_suffix(".txt").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def read_corpus(root: Path) -> list[Clip]:
    """Every readable clip under `root`, sorted by language then name — a stable report every run."""
    clips = []
    for path in sorted(root.glob(CLIP_GLOB)):
        seconds = clip_seconds(path)
        if seconds is None:
            continue
        clips.append(Clip(path=path, language=path.parent.name, seconds=seconds, text=clip_text(path)))
    return clips


def summarize(clips: list[Clip], min_minutes: float) -> dict:
    """The whole report as data — what the table prints and what --json emits, from one place."""
    usable = [clip for clip in clips if clip.usable]
    languages = {}
    for clip in clips:
        row = languages.setdefault(clip.language, {"clips": 0, "seconds": 0.0, "shortest": None, "longest": None})
        row["clips"] += 1
        row["seconds"] += clip.seconds
        row["shortest"] = clip.seconds if row["shortest"] is None else min(row["shortest"], clip.seconds)
        row["longest"] = clip.seconds if row["longest"] is None else max(row["longest"], clip.seconds)
    usable_seconds = sum(clip.seconds for clip in usable)
    return {
        "clips": len(clips),
        "seconds": sum(clip.seconds for clip in clips),
        "usable_clips": len(usable),
        "usable_seconds": usable_seconds,
        "skipped_clips": len(clips) - len(usable),
        "languages": languages,
        "min_seconds": min_minutes * 60,
        # Readiness is measured in USABLE audio: the clips a trainer would actually be handed.
        "ready": usable_seconds >= min_minutes * 60,
    }


def duration(seconds: float) -> str:
    """Seconds as `18m 07s` — minutes are the unit the 10-30 minute target is stated in."""
    return f"{int(seconds) // 60}m {int(seconds) % 60:02d}s"


def format_report(root: Path, summary: dict) -> str:
    lines = [f"corpus: {root}", "", f"  {'language':<10}{'clips':>7}{'duration':>12}   per clip"]
    if not summary["clips"]:
        lines.append("  (empty — set VOICE_LOOP_CORPUS_DIR on the server and let the xtts voice speak)")
    for language, row in sorted(summary["languages"].items()):
        lines.append(
            f"  {language:<10}{row['clips']:>7}{duration(row['seconds']):>12}"
            f"   {row['shortest']:.1f}s - {row['longest']:.1f}s"
        )
    lines += [
        "",
        f"  {'total':<10}{summary['clips']:>7}{duration(summary['seconds']):>12}",
        f"  {'usable':<10}{summary['usable_clips']:>7}{duration(summary['usable_seconds']):>12}"
        f"   ({summary['skipped_clips']} outside {MIN_CLIP_SECONDS:.0f}s - {MAX_CLIP_SECONDS:.0f}s)",
        "",
    ]
    target = duration(summary["min_seconds"])
    if summary["ready"]:
        lines.append(f"READY — {duration(summary['usable_seconds'])} of usable audio, past the {target} mark.")
    else:
        short = summary["min_seconds"] - summary["usable_seconds"]
        lines.append(f"NOT YET — {target} of usable audio is the mark; {duration(short)} short.")
    return "\n".join(lines)


def write_manifest(destination: str, clips: list[Clip], out=None) -> int:
    """Write one JSON object per usable clip — JSONL, which every trainer's loader already reads.

    Returns how many rows were written. "-" writes to stdout so the manifest can be piped straight
    into a training job without landing on disk at all.
    """
    rows = [
        json.dumps(
            {
                "audio": str(clip.path),
                "language": clip.language,
                "seconds": round(clip.seconds, 3),
                "text": clip.text,
            },
            ensure_ascii=False,
        )
        for clip in clips
        if clip.usable
    ]
    body = "".join(f"{row}\n" for row in rows)
    if destination == "-":
        (out or sys.stdout).write(body)
    else:
        Path(destination).write_text(body, encoding="utf-8")
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    corpus = os.environ.get("VOICE_LOOP_CORPUS_DIR", "")
    min_minutes = DEFAULT_MIN_MINUTES
    manifest = ""
    as_json = False

    args = argv[1:]
    while args:
        if args[0] == "--corpus" and len(args) > 1:
            corpus, args = args[1], args[2:]
        elif args[0] == "--manifest" and len(args) > 1:
            manifest, args = args[1], args[2:]
        elif args[0] == "--min-minutes" and len(args) > 1:
            try:
                min_minutes = float(args[1])
            except ValueError:
                print(f"--min-minutes wants a number, got: {args[1]}", file=sys.stderr)
                return 2
            args = args[2:]
        elif args[0] == "--json":
            as_json, args = True, args[1:]
        elif args[0] in ("-h", "--help"):
            print(
                f"usage: {argv[0]} [--corpus DIR] [--min-minutes N] [--manifest FILE] [--json]",
                file=sys.stderr,
            )
            return 0
        else:
            print(f"unknown argument: {args[0]}", file=sys.stderr)
            return 2

    if not corpus:
        print(
            "no corpus directory: pass --corpus DIR, or set VOICE_LOOP_CORPUS_DIR to the one the "
            "server records into",
            file=sys.stderr,
        )
        return 2
    if as_json and manifest == "-":
        print("--manifest - and --json both write to stdout; pick one", file=sys.stderr)
        return 2

    root = Path(corpus)
    if not root.is_dir():
        # Distinct from "empty": a typo in the path and a corpus that has not started are the same
        # zero, and only one of them is fixed by waiting.
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    clips = read_corpus(root)
    summary = summarize(clips, min_minutes)

    if manifest:
        summary["manifest"] = manifest
        summary["manifest_rows"] = write_manifest(manifest, clips)

    if as_json:
        print(json.dumps({"corpus": str(root), **summary}, indent=2, ensure_ascii=False))
    else:
        print(format_report(root, summary))
        if manifest and manifest != "-":
            print(f"\nmanifest: {summary['manifest_rows']} clips -> {manifest}")

    return 0 if summary["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
