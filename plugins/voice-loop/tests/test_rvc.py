"""The RVC recolor stage — the client half of the fast-TTS-then-convert pipeline.

The converter itself is a service on somebody's GPU and is faked at the opener seam (see
`FakeConverter` in conftest), so everything this repo actually owns runs for real: the URL check,
the POST that is built, the bounded read of the answer, the degrade-to-base-voice rule, and the two
places the recolored audio leaves from — the `/tts` blob and each `/tts/stream` chunk.

The rule under all of it is the one the engine fallback already follows: a broken converter degrades
the voice, it never silences it.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request

import pytest

import voice_server
from conftest import RECOLORED_WAV, FakeConverter

LONG_TEXT = " ".join(f"Предложение номер {i}." for i in range(120))  # several sentence chunks


def events(body: str) -> list[tuple[str, dict]]:
    parsed: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        event_line, data_line = block.split("\n")
        parsed.append((event_line.removeprefix("event: "), json.loads(data_line.removeprefix("data: "))))
    return parsed


# --- which URLs the stage will open -----------------------------------------------------------------


def test_the_stage_is_off_when_no_url_is_configured():
    assert voice_server.rvc_endpoint() == ""


@pytest.mark.parametrize("url", ["http://127.0.0.1:7865/convert", "https://gpu.example/convert"])
def test_http_and_https_urls_are_accepted(monkeypatch, url):
    monkeypatch.setattr(voice_server, "RVC_URL", url)

    assert voice_server.rvc_endpoint() == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",  # urlopen would read it happily and we would play the bytes
        "ftp://host/clip.wav",
        "/var/run/rvc.sock",  # no scheme at all
        "http://[::1",  # malformed enough that urlsplit itself refuses to parse it
    ],
)
def test_a_url_that_is_not_http_leaves_the_stage_off(monkeypatch, caplog, url):
    monkeypatch.setattr(voice_server, "RVC_URL", url)

    with caplog.at_level("WARNING"):
        assert voice_server.rvc_endpoint() == ""

    assert "VOICE_LOOP_RVC_URL" in caplog.text


# --- the request that is actually posted ------------------------------------------------------------


def test_the_converter_gets_a_posted_wav_and_the_configured_timeout(monkeypatch, rvc_service):
    monkeypatch.setattr(voice_server, "RVC_TIMEOUT", 3.5)

    audio, recolored = voice_server.recolor(b"RIFFbase")

    assert (audio, recolored) == (RECOLORED_WAV, True)
    assert rvc_service.posts == [
        {
            "url": "http://127.0.0.1:7865/convert",
            "method": "POST",
            "content_type": "audio/wav",
            "body": b"RIFFbase",
            "timeout": 3.5,
        }
    ]


def test_the_default_opener_bypasses_the_system_proxy(monkeypatch):
    """A loopback or LAN converter is reached directly — a proxy has no business in the middle.

    Asserted against the stdlib default built in the same environment, which is what makes the
    difference visible: with `http_proxy` set, urllib's own opener grows a handler that would route
    the audio through it, and ours does not.
    """
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")

    def proxies(opener) -> list[dict]:
        return [h.proxies for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]

    assert proxies(urllib.request.build_opener()) == [{"http": "http://proxy.invalid:3128"}]
    assert proxies(voice_server._default_opener()) == []


def test_post_wav_reads_one_byte_past_the_upload_cap(monkeypatch):
    """The bound the oversize check below reads: a peer's answer is not a promise about its size."""
    monkeypatch.setattr(voice_server, "MAX_UPLOAD_BYTES", 8)
    converter = FakeConverter(answer=b"RIFF" + b"x" * 100)

    body = voice_server.post_wav("http://127.0.0.1:7865/convert", b"RIFFbase", 1.0, opener=converter)

    assert body == b"RIFF" + b"x" * 5  # 8 + 1, and no more


def test_a_converter_that_breaches_the_wall_clock_budget_raises_timeout():
    """The breach arm of the deadline: a converter still mid-exchange when the budget elapses makes
    `post_wav` raise TimeoutError.

    Every other test drives an instant fake, so the worker has always finished by the time `join`
    returns — the `worker.is_alive()` True arc and the raise itself never ran. Here the fake hangs
    past the budget. The hang is gated by a threading.Event, not a bare sleep, so the worker it
    orphans unblocks the moment the test releases it (no thread parked for the rest of the session),
    and a wait-backstop caps it even if that release somehow never ran. The worker can never finish
    on its own, so `is_alive()` is True at the deadline on every runner — the raise is deterministic,
    not a race against the clock.
    """
    release = threading.Event()
    completed = threading.Event()

    class HangingConverter(FakeConverter):
        def open(self, request, timeout=None):
            release.wait(5.0)  # bounded backstop — never parks the worker indefinitely
            response = super().open(request, timeout)
            completed.set()  # the orphaned worker ran to completion — nothing left parked
            return response

    converter = HangingConverter()
    try:
        with pytest.raises(TimeoutError):
            voice_server.post_wav(
                "http://127.0.0.1:7865/convert", b"RIFFbase", 0.1, opener=converter
            )
    finally:
        release.set()

    assert completed.wait(2.0)  # the worker we breached on unblocked and finished
    assert len(converter.posts) == 1


# --- degrade, never silence -------------------------------------------------------------------------


def test_the_stage_off_hands_the_base_audio_straight_back():
    assert voice_server.recolor(b"RIFFbase") == (b"RIFFbase", False)
    assert voice_server._rvc_recolored == voice_server._rvc_failures == 0


def test_a_converter_that_is_down_leaves_the_base_voice_untouched(monkeypatch, caplog):
    secret = "http://gpu.internal:7865/convert refused the connection"
    monkeypatch.setattr(voice_server, "RVC_URL", "http://127.0.0.1:7865/convert")
    converter = FakeConverter(error=urllib.error.URLError(secret))
    monkeypatch.setattr(voice_server, "_default_opener", lambda: converter)

    with caplog.at_level("WARNING"):
        audio, recolored = voice_server.recolor(b"RIFFbase")

    assert (audio, recolored) == (b"RIFFbase", False)
    assert voice_server._rvc_failures == 1
    # The class name is the whole of what is logged: str(exc) on a URLError names the endpoint.
    assert "URLError" in caplog.text and secret not in caplog.text


def test_an_answer_that_is_not_a_wav_is_refused(monkeypatch, caplog):
    """An error page, a JSON body, an empty 200 — none of it may reach a player as audio."""
    monkeypatch.setattr(voice_server, "RVC_URL", "http://127.0.0.1:7865/convert")
    monkeypatch.setattr(voice_server, "_default_opener", lambda: FakeConverter(answer=b"<html>502</html>"))

    with caplog.at_level("WARNING"):
        assert voice_server.recolor(b"RIFFbase") == (b"RIFFbase", False)

    assert voice_server._rvc_failures == 1
    assert "not a WAV" in caplog.text


def test_an_answer_larger_than_the_upload_cap_is_refused(monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_UPLOAD_BYTES", 8)
    monkeypatch.setattr(voice_server, "RVC_URL", "http://127.0.0.1:7865/convert")
    monkeypatch.setattr(voice_server, "_default_opener", lambda: FakeConverter(answer=b"RIFF" + b"x" * 100))

    assert voice_server.recolor(b"RIFFbase") == (b"RIFFbase", False)
    assert voice_server._rvc_failures == 1


# --- /tts ---------------------------------------------------------------------------------------


def test_tts_returns_the_recolored_blob_and_says_so(client, fake_silero, rvc_service):
    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    assert response.content == RECOLORED_WAV
    assert response.headers[voice_server.ENGINE_HEADER] == "silero + rvc"
    assert rvc_service.posts[0]["body"].startswith(b"RIFF")  # the base voice went up as a WAV


def test_tts_keeps_speaking_in_the_base_voice_when_the_converter_is_down(client, fake_silero, monkeypatch):
    monkeypatch.setattr(voice_server, "RVC_URL", "http://127.0.0.1:7865/convert")
    monkeypatch.setattr(voice_server, "_default_opener", lambda: FakeConverter(error=OSError("down")))

    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    assert response.content.startswith(b"RIFF")  # real audio, just not repainted
    assert response.headers[voice_server.ENGINE_HEADER] == "silero"


def test_tts_recolors_whichever_voice_ended_up_speaking(client, monkeypatch, coqui_installed, fake_silero, rvc_service):
    """The stage sits after the engine question is settled — a fallback voice is recolored too."""
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")  # no reference wav: unusable
    monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", "silero")

    response = client.post("/tts", json={"text": "Привет."})

    assert response.headers[voice_server.ENGINE_HEADER] == "silero (fallback) + rvc"
    assert response.content == RECOLORED_WAV


# --- /tts/stream --------------------------------------------------------------------------------


def test_stream_recolors_every_chunk_on_its_way_out(client, fake_silero, rvc_service):
    response = client.post("/tts/stream", json={"text": LONG_TEXT})

    parsed = events(response.text)
    chunks, terminal = parsed[:-1], parsed[-1]
    assert len(chunks) > 1
    assert all(base64.b64decode(data["audio"]) == RECOLORED_WAV for _, data in chunks)
    assert terminal == ("end", {"chunks": len(chunks), "engine": "silero + rvc", "recolored": len(chunks)})
    assert len(rvc_service.posts) == len(chunks)


def test_stream_reports_the_chunks_a_dying_converter_left_in_the_base_voice(client, fake_silero, monkeypatch):
    """Unlike an engine fallback, the stage CAN change the voice mid-stream — the alternative is
    silence from that chunk on. So the count is reported, because it is the one thing a client
    cannot hear its way to."""
    converter = FakeConverter()

    def die_after_the_first(request: object, timeout: float | None = None):
        if converter.posts:
            raise OSError("the converter fell over")
        return FakeConverter.open(converter, request, timeout)

    monkeypatch.setattr(voice_server, "RVC_URL", "http://127.0.0.1:7865/convert")
    converter.open = die_after_the_first
    monkeypatch.setattr(voice_server, "_default_opener", lambda: converter)

    response = client.post("/tts/stream", json={"text": LONG_TEXT})

    parsed = events(response.text)
    chunks, terminal = parsed[:-1], parsed[-1]
    assert base64.b64decode(chunks[0][1]["audio"]) == RECOLORED_WAV
    assert base64.b64decode(chunks[1][1]["audio"]).startswith(b"RIFF")  # base voice, still audio
    assert terminal == ("end", {"chunks": len(chunks), "engine": "silero", "recolored": 1})


def test_stream_events_are_unchanged_when_the_stage_is_off(client, fake_silero):
    """A server without the stage emits exactly what it emitted before it existed — no new field."""
    response = client.post("/tts/stream", json={"text": "Привет."})

    assert events(response.text)[-1] == ("end", {"chunks": 1, "engine": "silero"})


# --- /health ------------------------------------------------------------------------------------


def test_health_reports_the_stage_off_by_default(client):
    body = client.get("/health").json()

    assert body["rvc"] is False
    assert body["rvc_recolored"] == body["rvc_failures"] == 0


def test_health_counts_what_the_stage_did_without_naming_the_endpoint(client, fake_silero, rvc_service):
    client.post("/tts", json={"text": "Привет."})
    voice_server.count_rvc(False)

    body = client.get("/health").json()

    assert body["rvc"] is True
    assert (body["rvc_recolored"], body["rvc_failures"]) == (1, 1)
    assert "7865" not in json.dumps(body)  # /health has no authentication; the URL stays private
