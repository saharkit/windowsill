"""The HTTP contract — the shape the plugin scripts and the selftest depend on.

The models are faked; the request handling, the language routing and the WAV encoding are real. A
`/tts` response really is a RIFF file produced by soundfile, which is exactly what `selftest.sh`
checks before it hands the bytes back to recognition.
"""

from __future__ import annotations

import io
import json
import struct
import wave

import pytest

import voice_server


def wav_upload(seconds: float, rate: int = 8000) -> bytes:
    """A real, minimal PCM WAV of the requested duration — what the duration guard parses."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def preparse_request(
    method: str = "POST",
    path: str = "/stt",
    headers: list[tuple[bytes, bytes]] | None = None,
):
    """A bare Starlette Request carrying only what the pre-parse gate reads — no body, no receive."""
    return voice_server.Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers or [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
            "root_path": "",
        }
    )


def test_health_reports_capabilities(client, monkeypatch):
    monkeypatch.setattr(voice_server, "DEVICE", "cpu")
    monkeypatch.setattr(voice_server, "STT_MODEL", "small")

    body = client.get("/health").json()

    assert body["ok"] is True
    assert body["version"] == voice_server.SERVER_VERSION == "0.5.0"
    assert body["model_concurrency"] == voice_server.MODEL_CONCURRENCY >= 1
    assert body["model_in_flight"] == 0
    assert body["model_waiting"] == 0
    assert sorted(body["model_queues"]) == ["cpu", "gpu"]
    assert body["device"] == "cpu"
    assert body["language"] == "ru"
    assert body["stt_model"] == "small"
    assert "en" in body["tts_languages"]
    assert body["accentuated_languages"] == ["ru", "uk"]
    assert body["stt_loaded"] is False
    assert body["tts_loaded"] == []


def test_health_reports_a_loaded_voice(client, monkeypatch):
    monkeypatch.setattr(voice_server.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(voice_server, "DEVICE", "auto")
    voice_server._tts["en"] = object()

    body = client.get("/health").json()

    assert body["cuda"] is True
    assert body["device"] == "cuda"
    assert body["tts_loaded"] == ["en"]


# --- /health: the hook heartbeat ---------------------------------------------------------------------


def test_health_reports_the_hook_heartbeat(client, monkeypatch, tmp_path):
    stamp = tmp_path / "hook-last-fired"
    stamp.write_text("1754157721.500\n", encoding="utf-8")
    monkeypatch.setattr(voice_server, "HOOK_STAMP_FILE", stamp)
    monkeypatch.setattr(voice_server.time, "time", lambda: 1754157721.5 + 90.0)

    body = client.get("/health").json()

    assert body["hook_last_fired"] == "2025-08-02T18:02:01.500+00:00"
    assert body["hook_last_fired_age_s"] == 90.0


def test_health_reports_a_negative_age_when_the_stamp_is_ahead(client, monkeypatch, tmp_path):
    # Signed on purpose: the stamp ahead of this clock means two machines disagree about the time.
    stamp = tmp_path / "hook-last-fired"
    stamp.write_text("1754157721.500\n", encoding="utf-8")
    monkeypatch.setattr(voice_server, "HOOK_STAMP_FILE", stamp)
    monkeypatch.setattr(voice_server.time, "time", lambda: 1754157721.5 - 90.0)

    body = client.get("/health").json()

    assert body["hook_last_fired_age_s"] == -90.0


@pytest.mark.parametrize(
    "stamp",
    [
        None,  # no stamp at all: the hook never fired on this machine
        "not-a-number\n",  # a corrupt stamp reads the same as none
        "1e400\n",  # parses as inf but no date exists for it — still none, never a 500
    ],
)
def test_health_reports_nulls_without_a_readable_stamp(client, monkeypatch, tmp_path, stamp):
    path = tmp_path / "hook-last-fired"
    if stamp is not None:
        path.write_text(stamp, encoding="utf-8")
    monkeypatch.setattr(voice_server, "HOOK_STAMP_FILE", path)

    body = client.get("/health").json()

    assert body["hook_last_fired"] is None
    assert body["hook_last_fired_age_s"] is None


def test_health_survives_an_unreadable_stamp(client, monkeypatch, tmp_path):
    monkeypatch.setattr(voice_server, "HOOK_STAMP_FILE", tmp_path)  # a directory: read fails

    body = client.get("/health").json()

    assert body["hook_last_fired"] is None
    assert body["hook_last_fired_age_s"] is None


# --- /health: the heartbeat, when the bind address breaks the connection to the client's stamp --


def test_health_reports_null_heartbeat_when_bound_to_a_non_loopback_address(
    client, monkeypatch, tmp_path
):
    """L3 (two-way falsification): with HOST bound beyond loopback, the readable stamp is on the
    SERVER's machine — not the client's hook. Reporting that stamp's age as the client's heartbeat
    is the WSL2-into-LAN confabulation #179 retired; the server now says ``null`` instead.

    The shape is the same as the unreadable-stamp cases above: null both fields. The reason
    differs (non-loopback bind, vs no readable file) but the contract the client sees is one
    shape — it cannot read the heartbeat it does not have.
    """
    stamp = tmp_path / "hook-last-fired"
    stamp.write_text("1754157721.500\n", encoding="utf-8")
    monkeypatch.setattr(voice_server, "HOOK_STAMP_FILE", stamp)
    monkeypatch.setattr(voice_server.time, "time", lambda: 1754157721.5 + 90.0)
    monkeypatch.setattr(voice_server, "HOST", "0.0.0.0")  # the wide bind — anyone on the LAN

    body = client.get("/health").json()

    assert body["hook_last_fired"] is None
    assert body["hook_last_fired_age_s"] is None


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.1.42", "::", "::ffff:192.168.1.42"],
)
def test_health_null_heartbeat_for_any_non_loopback_bind(
    client, monkeypatch, tmp_path, host
):
    """L2: cover the addresses a remote-bind configuration actually uses, not just ``0.0.0.0``.

    The existence of a readable stamp on this server's state dir must never be reported as the
    client's heartbeat — that stamp belongs to a hook on THIS machine, which is not necessarily
    whose hook calls the server.
    """
    stamp = tmp_path / "hook-last-fired"
    stamp.write_text("1754157721.500\n", encoding="utf-8")
    monkeypatch.setattr(voice_server, "HOOK_STAMP_FILE", stamp)
    monkeypatch.setattr(voice_server, "HOST", host)

    body = client.get("/health").json()

    assert body["hook_last_fired"] is None
    assert body["hook_last_fired_age_s"] is None


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.0.0.42", "::1"])
def test_health_keeps_the_heartbeat_on_loopback_binds(client, monkeypatch, tmp_path, host):
    """L3 companion: the LAN carve-out (#179, defect 2) must NOT regress the loopback case.

    A local server reading its local stamp is exactly what /health was designed for; the WSL2
    fix narrowed the contract, it did not widen the loopback path to silence.
    """
    stamp = tmp_path / "hook-last-fired"
    stamp.write_text("1754157721.500\n", encoding="utf-8")
    monkeypatch.setattr(voice_server, "HOOK_STAMP_FILE", stamp)
    monkeypatch.setattr(voice_server, "HOST", host)
    monkeypatch.setattr(voice_server.time, "time", lambda: 1754157721.5 + 90.0)

    body = client.get("/health").json()

    assert body["hook_last_fired"] == "2025-08-02T18:02:01.500+00:00"
    assert body["hook_last_fired_age_s"] == 90.0


def test_is_loopback_bind_classifies_addresses_for_wsl2_carveout():
    """L1: the classification helper is the answer the heartbeat will trust next to HOOK_STAMP_FILE.
    Tests for its branches keep the carve-out exact — a typo in the prefix string would re-open
    the LAN confabulation or silence every local install.
    """
    assert voice_server._is_loopback_bind("127.0.0.1") is True
    assert voice_server._is_loopback_bind("localhost") is True
    assert voice_server._is_loopback_bind("127.0.0.42") is True
    assert voice_server._is_loopback_bind("::1") is True
    assert voice_server._is_loopback_bind("0.0.0.0") is False
    assert voice_server._is_loopback_bind("192.168.1.42") is False
    assert voice_server._is_loopback_bind("::") is False
    assert voice_server._is_loopback_bind("") is False


# --- /stt ------------------------------------------------------------------------------------------


def test_stt_joins_and_strips_segments(client, fake_whisper):
    response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})

    assert response.status_code == 200
    assert response.json() == {"text": "hello world", "language": "ru", "duration": 1.25}


def test_stt_defaults_to_the_configured_language(client, fake_whisper, monkeypatch):
    monkeypatch.setattr(voice_server, "LANGUAGE", "en")
    client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert fake_whisper.calls[0]["language"] == "en"


def test_stt_takes_the_language_from_the_query_and_lowercases_it(client, fake_whisper):
    client.post("/stt?language=EN", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert fake_whisper.calls[0]["language"] == "en"


def test_stt_passes_the_lexicon_hint_and_uses_vad(client, fake_whisper, monkeypatch):
    monkeypatch.setattr(voice_server, "STT_HINT", "Acme, kubectl")
    client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert fake_whisper.calls[0]["initial_prompt"] == "Acme, kubectl"
    assert fake_whisper.calls[0]["vad_filter"] is True


def test_stt_takes_the_prompt_from_the_query_and_it_wins_over_the_server_wide_hint(client, fake_whisper, monkeypatch):
    """windowsill#162, server half — the unification. A client's ``stt.prompt`` arrives as ``?prompt=``
    and feeds faster-whisper's initial_prompt, WINNING over the server-wide ``VOICE_LOOP_STT_HINT`` so a
    local user's config key reaches the recogniser without editing the systemd unit. The other
    direction (no query param -> the env default) is the test just above; together they two-way pin the
    precedence. A regression that ignored ``?prompt=`` and always used ``STT_HINT`` would silently break
    local prompting, and nothing existing would notice."""
    monkeypatch.setattr(voice_server, "STT_HINT", "server-wide default")
    client.post("/stt?prompt=kubectl%2C%20Acme", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert fake_whisper.calls[0]["initial_prompt"] == "kubectl, Acme"  # the request won, not the env


# --- /tts ------------------------------------------------------------------------------------------


def test_tts_returns_a_real_wav(client, fake_silero):
    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content[:4] == b"RIFF"
    assert fake_silero.calls[0]["speaker"] == "baya"
    assert fake_silero.calls[0]["sample_rate"] == voice_server.TTS_SR


def test_tts_english_uses_the_english_voice_and_skips_stress_marking(client, fake_silero):
    response = client.post("/tts", json={"text": "Build finished. Two tests failed.", "language": "en"})

    assert response.status_code == 200
    assert response.content[:4] == b"RIFF"
    assert fake_silero.calls[0]["speaker"] == "en_0"
    assert fake_silero.calls[0]["text"] == "Build finished. Two tests failed."


def test_tts_applies_the_stress_pipeline_before_synthesis(client, fake_silero, stress_file, monkeypatch):
    stress_file.write_text(json.dumps({r"\bАкме\b": "+Акме"}), encoding="utf-8")
    monkeypatch.setattr(voice_server, "USE_ACCENT", False)

    client.post("/tts", json={"text": "Акме готово."})

    assert fake_silero.calls[0]["text"] == "+Акме готово."


def test_tts_honours_an_explicit_speaker(client, fake_silero):
    client.post("/tts", json={"text": "Привет.", "speaker": "xenia"})
    assert fake_silero.calls[0]["speaker"] == "xenia"


# --- field type checks: a non-string field is the client's 400, never an AttributeError 500 (#219) --


@pytest.mark.parametrize("field", ["text", "language", "speaker"])
def test_tts_rejects_a_non_string_field(client, fake_silero, field):
    """Each of the three fields is type-checked, so a non-string one returns the house 400 rather than
    crashing in .strip()/.lower() or reaching the model. A regression that checked only `text` would
    let a non-string `language` or `speaker` through to a 500."""
    response = client.post("/tts", json={field: 123})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == f"{field} must be a string"
    assert set(body) == {"error", "hint"}
    assert fake_silero.calls == []


def test_tts_stream_rejects_a_non_string_field_like_tts(client, fake_silero):
    response = client.post("/tts/stream", json={"text": ["not", "a", "string"]})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "text must be a string"
    assert set(body) == {"error", "hint"}
    assert fake_silero.calls == []


def test_tts_chunks_a_long_text_into_several_calls(client, fake_silero):
    text = " ".join(f"Предложение номер {i}." for i in range(120))
    client.post("/tts", json={"text": text})
    assert len(fake_silero.calls) > 1


def test_tts_rejects_an_unsupported_language(client, fake_silero):
    response = client.post("/tts", json={"text": "Merhaba.", "language": "tr"})

    assert response.status_code == 400
    body = response.json()
    assert "tr" in body["error"]
    assert "en" in body["supported"] and "ru" in body["supported"]
    assert fake_silero.calls == []


def test_tts_rejects_empty_text(client, fake_silero):
    response = client.post("/tts", json={"text": "   "})

    assert response.status_code == 400
    assert response.json()["error"] == "empty text"
    assert fake_silero.calls == []


def test_tts_language_is_case_insensitive(client, fake_silero):
    response = client.post("/tts", json={"text": "Hello.", "language": "EN"})
    assert response.status_code == 200
    assert fake_silero.calls[0]["speaker"] == "en_0"


# --- speakable-characters validation ----------------------------------------------------------------


def test_tts_rejects_pure_latin_for_cyrillic_voice(client, fake_silero):
    """A Russian voice fed pure Latin text returns 400 naming the voice — never a 500."""
    response = client.post(
        "/tts", json={"text": "Voice contour: rvc is serving on cpu, expected gpu", "language": "ru"}
    )

    assert response.status_code == 400
    body = response.json()
    assert "no speakable characters" in body["error"]
    assert "ru" in body["error"]
    assert "baya" in body["error"]  # the default speaker for ru
    assert fake_silero.calls == []


def test_tts_rejects_pure_cyrillic_for_latin_voice(client, fake_silero):
    """An English voice fed pure Cyrillic text returns 400 — the reverse direction."""
    response = client.post("/tts", json={"text": "Привет мир", "language": "en"})

    assert response.status_code == 400
    body = response.json()
    assert "no speakable characters" in body["error"]
    assert "en" in body["error"]
    assert "en_0" in body["error"]  # the default speaker for en
    assert fake_silero.calls == []


def test_tts_accepts_mixed_latin_cyrillic_for_russian_voice(client, fake_silero):
    """One Cyrillic character is enough — the voice has something to pronounce."""
    response = client.post("/tts", json={"text": "Status: ок", "language": "ru"})
    assert response.status_code == 200
    assert response.content[:4] == b"RIFF"
    assert len(fake_silero.calls) >= 1


def test_tts_accepts_cyrillic_text_for_russian_voice(client, fake_silero):
    """The ordinary happy case: Russian text on a Russian voice. The check must not false-positive."""
    response = client.post("/tts", json={"text": "Привет, мир!", "language": "ru"})
    assert response.status_code == 200
    assert response.content[:4] == b"RIFF"


# --- request-level caps ----------------------------------------------------------------------------


def test_tts_blob_carries_the_small_cap_and_points_at_the_stream(client, fake_silero, monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_TTS_TEXT_BLOB", 50)

    response = client.post("/tts", json={"text": "Слово. " * 20})

    assert response.status_code == 400
    assert "the limit is 50" in response.json()["error"]
    assert "/tts/stream" in response.json()["hint"]
    assert "VOICE_LOOP_MAX_TTS_TEXT_BLOB" in response.json()["hint"]
    assert fake_silero.calls == []


def test_tts_stream_carries_its_own_larger_cap(client, fake_silero, monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_TTS_TEXT", 50)
    monkeypatch.setattr(voice_server, "MAX_TTS_TEXT_BLOB", 10)  # must NOT apply to the stream

    over_stream_cap = client.post("/tts/stream", json={"text": "Слово. " * 20})
    assert over_stream_cap.status_code == 400
    assert "the limit is 50" in over_stream_cap.json()["error"]
    assert "VOICE_LOOP_MAX_TTS_TEXT" in over_stream_cap.json()["hint"]

    over_blob_only = client.post("/tts/stream", json={"text": "Тридцать три символа ровно тут."})
    assert over_blob_only.status_code == 200  # longer than the blob cap, fine on the stream
    assert fake_silero.calls != []


def test_tts_text_cap_defaults_are_the_documented_ones(client, fake_silero):
    assert voice_server.MAX_TTS_TEXT == 20000
    assert voice_server.MAX_TTS_TEXT_BLOB == 3000
    response = client.post("/tts", json={"text": "Ok.", "language": "en"})
    assert response.status_code == 200


def test_tts_refuses_a_json_body_over_one_mib_wherever_the_bytes_live(client, fake_silero):
    """The 1 MiB raw-body cap counts the WHOLE JSON body, not the text field: a huge `speaker` trips
    it exactly like a huge `text` would, and before any synthesis starts."""
    response = client.post("/tts", json={"text": "ok", "speaker": "x" * (1024 * 1024 + 1)})

    assert response.status_code == 413
    assert "1048576 byte limit" in response.json()["error"]
    assert fake_silero.calls == []


def test_stt_rejects_an_upload_over_the_size_cap(client, fake_whisper, monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_UPLOAD_BYTES", 16)

    response = client.post("/stt", files={"audio": ("clip.wav", b"R" * 64, "audio/wav")})

    assert response.status_code == 413
    assert "16 byte limit" in response.json()["error"]
    assert "VOICE_LOOP_MAX_UPLOAD_BYTES" in response.json()["hint"]
    assert fake_whisper.calls == []


def test_stt_size_cap_counts_the_whole_body_not_just_the_file(client, fake_whisper, monkeypatch):
    """The cap moved from the handler's file read to the pre-parse Content-Length gate (#219), so the
    multipart framing counts too: a file exactly at the cap is over it once the wrapper is added. A
    regression that put the check back in the handler (measuring only the file) would return 200 here."""
    monkeypatch.setattr(voice_server, "MAX_UPLOAD_BYTES", 8)

    response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})

    assert response.status_code == 413
    assert "8 byte limit" in response.json()["error"]
    assert fake_whisper.calls == []


def test_stt_rejects_a_wav_longer_than_the_duration_cap(client, fake_whisper, monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_STT_SECONDS", 1.0)

    response = client.post("/stt", files={"audio": ("clip.wav", wav_upload(2.0), "audio/wav")})

    assert response.status_code == 413
    assert response.json()["error"] == "audio too long: 2 seconds, the limit is 1"
    assert "VOICE_LOOP_MAX_STT_SECONDS" in response.json()["hint"]
    assert fake_whisper.calls == []


def test_stt_accepts_a_wav_under_the_duration_cap(client, fake_whisper):
    assert voice_server.MAX_STT_SECONDS == 600.0  # the documented default
    response = client.post("/stt", files={"audio": ("clip.wav", wav_upload(0.5), "audio/wav")})
    assert response.status_code == 200
    assert len(fake_whisper.calls) == 1


def test_stt_duration_cap_passes_non_wav_codecs_on_the_byte_cap_alone(client, fake_whisper, monkeypatch):
    """Compressed audio reveals duration only by decoding — the very work the cap avoids."""
    monkeypatch.setattr(voice_server, "MAX_STT_SECONDS", 0.0)  # any measured duration would refuse
    response = client.post("/stt", files={"audio": ("clip.ogg", b"OggS" + b"\x00" * 64, "audio/ogg")})
    assert response.status_code == 200
    assert len(fake_whisper.calls) == 1


def test_stt_duration_cap_passes_an_unparseable_riff(client, fake_whisper, monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_STT_SECONDS", 0.0)
    response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert response.status_code == 200
    assert len(fake_whisper.calls) == 1


# --- the pre-parse upload size gate (#219) ----------------------------------------------------------
# Content-Length is the only size signal available ABOVE the body parser — by the time an endpoint or
# dependency runs, Starlette has already spooled the multipart/JSON body. So the cap fires in
# middleware (preparse_upload_refusal), before a byte is parsed. The helper is unit-tested here for
# the shapes httpx cannot produce (a lengthless/chunked body, an unreadable length, the exact boundary).


def test_preparse_gate_refuses_a_lengthless_post():
    refusal = voice_server.preparse_upload_refusal(preparse_request(headers=[]))
    assert refusal is not None
    assert refusal.status_code == 413
    assert "chunked" in refusal.body.decode()


def test_preparse_gate_refuses_an_unreadable_content_length():
    refusal = voice_server.preparse_upload_refusal(preparse_request(headers=[(b"content-length", b"nope")]))
    assert refusal.status_code == 413


def test_preparse_gate_lets_non_post_methods_through():
    assert voice_server.preparse_upload_refusal(preparse_request(method="GET", path="/health")) is None


def test_preparse_gate_accepts_a_body_at_the_cap(monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_UPLOAD_BYTES", 8)
    refusal = voice_server.preparse_upload_refusal(preparse_request(headers=[(b"content-length", b"8")]))
    assert refusal is None


def test_preparse_gate_refuses_a_body_over_the_cap(monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_UPLOAD_BYTES", 8)
    refusal = voice_server.preparse_upload_refusal(preparse_request(headers=[(b"content-length", b"9")]))
    assert refusal.status_code == 413
    assert "audio upload larger than the 8 byte limit" in refusal.body.decode()


@pytest.mark.parametrize("path", ["/tts", "/tts/stream"])
def test_preparse_gate_caps_the_two_tts_bodies_at_one_mib(monkeypatch, path):
    monkeypatch.setattr(voice_server, "MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
    over = str(voice_server.MAX_TTS_BODY_BYTES + 1).encode()

    refusal = voice_server.preparse_upload_refusal(
        preparse_request(path=path, headers=[(b"content-length", over)])
    )

    assert refusal.status_code == 413
    assert "TTS request body larger than the 1048576 byte limit" in refusal.body.decode()


def test_preparse_gate_accepts_a_tts_body_under_one_mib():
    refusal = voice_server.preparse_upload_refusal(
        preparse_request(path="/tts", headers=[(b"content-length", b"1024")])
    )
    assert refusal is None


# --- the wall-clock transcription budget (the cap the duration guard cannot enforce) ---------------


class FakeClock:
    """A clock that only moves when a test says so — elapsed time as an input, never a sleep."""

    def __init__(self, steps: list[float]) -> None:
        self.steps = list(steps)
        self.now = 0.0

    def __call__(self) -> float:
        if self.steps:
            self.now += self.steps.pop(0)
        return self.now


def test_bounded_segments_passes_a_transcription_that_finishes_in_time():
    clock = FakeClock([0.0, 1.0, 1.0])  # start, then a second per segment, against a budget of 10
    kept = list(voice_server.bounded_segments(iter("abc"), 10.0, clock))
    assert kept == ["a", "b", "c"]


def test_bounded_segments_gives_up_once_the_budget_is_spent():
    clock = FakeClock([0.0, 1.0, 99.0])
    with pytest.raises(voice_server.TranscriptionTimeout) as raised:
        list(voice_server.bounded_segments(iter("abc"), 10.0, clock))
    assert "10 second budget" in str(raised.value)


def test_bounded_segments_does_not_check_after_the_last_segment():
    """The exhaustion probe is not a place to give up — a stream that is simply over is not failed."""
    clock = FakeClock([0.0, 1.0, 1.0, 9_000.0])
    assert list(voice_server.bounded_segments(iter("ab"), 10.0, clock)) == ["a", "b"]
    assert clock.steps == [9_000.0]  # the clock was never read again once the segments ran out


def test_bounded_segments_budget_of_zero_means_no_bound():
    clock = FakeClock([0.0, 10_000.0])
    assert list(voice_server.bounded_segments(iter("ab"), 0.0, clock)) == ["a", "b"]


def test_stt_abandons_a_transcription_that_outruns_its_budget(client, monkeypatch, fake_whisper):
    """The format-agnostic half of the holding-time bound: no header is consulted at all."""

    def outran(segments, budget, **_):
        raise voice_server.TranscriptionTimeout(f"outran its {budget:.0f} second budget")

    monkeypatch.setattr(voice_server, "STT_TIMEOUT", 30.0)
    monkeypatch.setattr(voice_server, "bounded_segments", outran)

    response = client.post("/stt", files={"audio": ("clip.ogg", b"OggS" + b"\x00" * 64, "audio/ogg")})

    assert response.status_code == 503
    assert response.json()["error"] == "transcription outran the 30 second time budget and was abandoned"
    assert "VOICE_LOOP_STT_TIMEOUT" in response.json()["hint"]
    # And the slot went back with the failed call — the queue behind it is free, which is the point.
    assert voice_server.model_in_flight() == 0
    assert voice_server.queue_depths()["cpu"]["waiting"] == 0


def test_stt_deadline_refuses_before_loading_or_transcribing(client, monkeypatch, one_slot_gate):
    """L2 GAP: deleting this test lets a queued request load and invoke Whisper before its deadline.

    The request bound owns both model loading and transcription, so neither may start after the gate
    wait has already consumed the budget.
    """
    calls: list[str] = []
    monkeypatch.setattr(voice_server, "STT_TIMEOUT", 0.01)
    monkeypatch.setattr(voice_server, "resolve_device", lambda: "cpu")
    monkeypatch.setattr(voice_server, "whisper", lambda: calls.append("load"))
    clock_values = iter((100.0, 100.02))
    with one_slot_gate:
        with pytest.raises(voice_server.TranscriptionTimeout):
            voice_server.transcribe_upload(b"RIFFfake", "ru", clock=lambda: next(clock_values))
    assert calls == []


def test_stt_deadline_refuses_before_loading_after_elapsed_budget(monkeypatch, one_slot_gate):
    """A request whose budget expires before model loading must not load Whisper."""
    calls: list[str] = []
    monkeypatch.setattr(voice_server, "STT_TIMEOUT", 0.01)
    monkeypatch.setattr(voice_server, "resolve_device", lambda: "cpu")
    monkeypatch.setattr(voice_server, "whisper", lambda: calls.append("load"))
    clock_values = iter((100.0, 100.0, 100.02))

    with pytest.raises(voice_server.TranscriptionTimeout, match="0 second budget"):
        voice_server.transcribe_upload(b"RIFFfake", "ru", clock=lambda: next(clock_values))
    assert calls == []


def test_stt_deadline_refuses_before_transcribing_after_loading(monkeypatch, one_slot_gate):
    """A model load that consumes the deadline must not start transcription."""
    calls: list[str] = []
    monkeypatch.setattr(voice_server, "STT_TIMEOUT", 0.01)
    monkeypatch.setattr(voice_server, "resolve_device", lambda: "cpu")
    monkeypatch.setattr(voice_server, "whisper", lambda: calls.append("load") or object())
    clock_values = iter((100.0, 100.0, 100.005, 100.02))

    with pytest.raises(voice_server.TranscriptionTimeout, match="0 second budget"):
        voice_server.transcribe_upload(b"RIFFfake", "ru", clock=lambda: next(clock_values))
    assert calls == ["load"]


def test_stt_timeout_default_is_the_documented_one(client, fake_whisper):
    assert voice_server.STT_TIMEOUT == 900.0
    response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert response.status_code == 200  # an ordinary clip is nowhere near it


def test_wav_duration_skips_a_zero_rate_header():
    """A crafted header must not divide by zero — it degrades to 'unmeasurable', not an error."""
    data = bytearray(wav_upload(1.0))
    struct.pack_into("<I", data, 24, 0)  # the fmt chunk's sample-rate field
    assert voice_server.wav_duration_seconds(bytes(data)) is None


# --- an upload the recognizer cannot decode is a 400, never a 500 (#219) ---------------------------


def test_transcribe_upload_flags_an_undecodable_upload(monkeypatch):
    """The decode step — not the model load, not the slot — maps a decode failure to the named error."""
    class UndecodableModel:
        def transcribe(self, path, **kwargs):
            raise RuntimeError("cannot decode")

    monkeypatch.setattr(voice_server, "whisper", lambda: UndecodableModel())

    with pytest.raises(voice_server.UndecodableUpload):
        voice_server.transcribe_upload(b"RIFFfake", "ru")


def test_stt_refuses_an_upload_the_decoder_cannot_read(client, monkeypatch):
    """The handler turns UndecodableUpload into the house-shaped 400 rather than letting it 500."""

    def undecodable(*args, **kwargs):
        raise voice_server.UndecodableUpload("the recognizer could not decode the upload")

    monkeypatch.setattr(voice_server, "transcribe_upload", undecodable)

    response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "audio could not be decoded"
    assert set(body) == {"error", "hint"}


# --- cross-site browser guard ----------------------------------------------------------------------
# Multipart is a CORS-"simple" body, so a malicious page can fire real cross-origin POSTs at a
# loopback server. Browser-labelled cross-site requests get a plain 403; header-less clients pass.


@pytest.mark.parametrize("path", ["/stt", "/tts", "/tts/stream"])
def test_post_endpoints_refuse_a_cross_site_browser_request(client, fake_whisper, fake_silero, path):
    response = client.post(
        path,
        files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")} if path == "/stt" else None,
        json=None if path == "/stt" else {"text": "Привет."},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403
    assert response.json() == {"error": "cross-site browser requests are not accepted"}
    assert fake_whisper.calls == [] and fake_silero.calls == []


def test_stt_refuses_a_foreign_origin(client, fake_whisper):
    response = client.post(
        "/stt",
        files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json() == {"error": "cross-origin browser requests are not accepted"}
    assert fake_whisper.calls == []


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:8355", "http://localhost:3000", "http://[::1]:8355", "null"]
)
def test_stt_accepts_loopback_and_null_origins(client, fake_whisper, origin):
    response = client.post(
        "/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")}, headers={"Origin": origin}
    )
    assert response.status_code == 200


def test_stt_refuses_a_malformed_origin(client, fake_whisper):
    response = client.post(
        "/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")}, headers={"Origin": "http://[::1"}
    )
    assert response.status_code == 403


def test_same_origin_sec_fetch_site_passes(client, fake_silero):
    response = client.post("/tts", json={"text": "Привет."}, headers={"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200
