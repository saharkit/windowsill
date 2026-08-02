"""The /tts/stream contract — the framing the streaming client parses.

Server-sent events, one complete standalone WAV segment per `chunk` event (base64), then a terminal
`end` or `error` event. The framing asserted here is byte-exact: `event: <name>\\ndata: <json>\\n\\n`,
data always a single line — that is what a stdlib line-by-line reader consumes.
"""

from __future__ import annotations

import base64
import io
import json

import soundfile as sf

import voice_server
from conftest import FakeSilero

LONG_TEXT = " ".join(f"Предложение номер {i}." for i in range(120))  # > MAX_TTS_CHARS: several chunks


def events(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, data) pairs — the strict two-line framing, nothing looser."""
    parsed: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        event_line, data_line = block.split("\n")
        assert event_line.startswith("event: ") and data_line.startswith("data: ")
        parsed.append((event_line.removeprefix("event: "), json.loads(data_line.removeprefix("data: "))))
    return parsed


def wav_of(data: dict) -> tuple[object, int]:
    return sf.read(io.BytesIO(base64.b64decode(data["audio"])))


def test_stream_emits_ordered_chunks_and_a_terminal_end(client, fake_silero):
    response = client.post("/tts/stream", json={"text": LONG_TEXT})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    parsed = events(response.text)
    chunks, terminal = parsed[:-1], parsed[-1]
    assert len(chunks) > 1
    assert [name for name, _ in chunks] == ["chunk"] * len(chunks)
    assert [data["index"] for _, data in chunks] == list(range(len(chunks)))
    assert terminal == ("end", {"chunks": len(chunks), "engine": "silero"})
    assert len(fake_silero.calls) == len(chunks)


def test_stream_chunks_are_self_contained_wavs(client, fake_silero):
    response = client.post("/tts/stream", json={"text": "Привет."})

    parsed = events(response.text)
    assert [name for name, _ in parsed] == ["chunk", "end"]
    wav, sample_rate = wav_of(parsed[0][1])
    assert sample_rate == voice_server.TTS_SR
    assert len(wav) > 0


def test_stream_paces_like_tts_with_leading_silence_from_the_second_chunk(client, fake_silero):
    """Pacing parity with /tts: every chunk after the FIRST carries PAUSE_SECONDS of silence at its
    head — the same audible gap /tts inserts between sentence chunks. The first chunk stays bare so
    streamed playback starts as early as before."""
    response = client.post("/tts/stream", json={"text": LONG_TEXT})

    parsed = events(response.text)
    chunks = parsed[:-1]
    assert len(chunks) > 1
    piece_frames = int(voice_server.TTS_SR * 0.05)  # what FakeSilero synthesizes per call
    pause_frames = int(voice_server.TTS_SR * voice_server.PAUSE_SECONDS)
    first, _ = wav_of(chunks[0][1])
    assert len(first) == piece_frames
    for _, data in chunks[1:]:
        wav, _ = wav_of(data)
        assert len(wav) == pause_frames + piece_frames
        assert not wav[:pause_frames].any()  # the head really is silence


def test_stream_xtts_streams_per_sentence_chunk_at_its_own_rate(client, fake_xtts):
    response = client.post("/tts/stream", json={"text": LONG_TEXT, "language": "en"})

    parsed = events(response.text)
    chunks, terminal = parsed[:-1], parsed[-1]
    assert len(chunks) > 1
    assert terminal == ("end", {"chunks": len(chunks), "engine": "xtts"})
    _wav, sample_rate = wav_of(chunks[0][1])
    assert sample_rate == voice_server.XTTS_SR
    assert len(fake_xtts.calls) == len(chunks)


def test_stream_mid_stream_failure_ends_with_a_terminal_error_event(client, monkeypatch, caplog):
    model = FakeSilero()
    original = FakeSilero.apply_tts

    def explode_on_the_second_chunk(self, text: str, speaker: str, sample_rate: int):
        if len(self.calls) == 1:
            raise RuntimeError("model said no")
        return original(self, text, speaker, sample_rate)

    monkeypatch.setattr(FakeSilero, "apply_tts", explode_on_the_second_chunk)
    monkeypatch.setattr(voice_server, "tts", lambda language: model)

    with caplog.at_level("ERROR"):
        response = client.post("/tts/stream", json={"text": LONG_TEXT})

    assert response.status_code == 200  # the status left with the first bytes; the failure is an event
    parsed = events(response.text)
    assert parsed[0][0] == "chunk"
    assert parsed[-1] == ("error", {"error": "synthesis failed (RuntimeError)", "chunks": 1})
    assert "streaming synthesis failed after 1 chunk(s)" in caplog.text
    # the app survives: the very next request is served normally
    assert client.get("/health").json()["ok"] is True


def test_stream_error_event_never_carries_exception_internals(client, monkeypatch):
    """What str(exc) says stays in the server log — the wire gets the class name at most."""
    secret = "/home/user/.secret/reference.wav is unreadable"
    model = FakeSilero()

    def explode(self, text: str, speaker: str, sample_rate: int):
        raise RuntimeError(secret)

    monkeypatch.setattr(FakeSilero, "apply_tts", explode)
    monkeypatch.setattr(voice_server, "tts", lambda language: model)

    response = client.post("/tts/stream", json={"text": "Привет."})

    assert secret not in response.text
    assert events(response.text)[-1] == ("error", {"error": "synthesis failed (RuntimeError)", "chunks": 0})


def test_stream_refuses_bad_requests_with_plain_json_like_tts(client, fake_silero):
    empty = client.post("/tts/stream", json={"text": "   "})
    assert (empty.status_code, empty.json()["error"]) == (400, "empty text")

    unsupported = client.post("/tts/stream", json={"text": "Merhaba.", "language": "tr"})
    assert unsupported.status_code == 400
    assert "tr" in unsupported.json()["error"]
    assert fake_silero.calls == []


def test_stream_refuses_a_misconfigured_xtts_before_streaming(client, monkeypatch, coqui_installed):
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")

    response = client.post("/tts/stream", json={"text": "Привет."})

    assert response.status_code == 500
    assert "VOICE_LOOP_XTTS_REFERENCE" in response.json()["error"]


def test_health_advertises_streaming(client):
    assert client.get("/health").json()["streaming"] is True
