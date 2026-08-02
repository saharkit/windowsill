"""Engine fallback — a broken primary degrades the voice instead of silencing it.

The live case this exists for: XTTS down for hours while a client-side fallback quietly kept the
speech going. Here it is a server option, so every client gets it. What the tests pin: WHO spoke is
always on the response (`X-Voice-Loop-Engine`, or the stream's terminal `end` event), a fallback
only ever happens when the ENGINE broke (never for a client error) — though a language refusal does
name the engine that could serve it — and the failed primary's model slot is back in the gate before
the fallback takes one, on BOTH endpoints: the one-slot gate is never held twice for one request.
The stream additionally falls back only before its first chunk.

Those two gate tests are the ones that would hang rather than fail on a regression, so they run
against `one_slot_gate` (which raises instead of blocking forever) under a timeout marker.
"""

from __future__ import annotations

import importlib
import io
import threading

import pytest
import soundfile as sf

import voice_server
from conftest import FakeSilero, FakeXtts
from test_streaming import events, wav_of

LONG_TEXT = " ".join(f"Предложение номер {i}." for i in range(120))  # several sentence chunks


class BrokenXtts(FakeXtts):
    """A loaded cloner whose synthesis always raises — the failure this feature answers."""

    def __init__(self, in_flight: list[int] | None = None) -> None:
        super().__init__()
        self.in_flight = in_flight if in_flight is not None else []

    def tts(self, text: str, speaker_wav: str, language: str) -> list[float]:
        self.in_flight.append(voice_server._model_in_flight)
        super().tts(text, speaker_wav, language)
        raise RuntimeError("xtts said no")


class RecordingSilero(FakeSilero):
    """A working voice that also notes how many model slots were held while it synthesized."""

    def __init__(self, in_flight: list[int]) -> None:
        super().__init__()
        self.in_flight = in_flight

    def apply_tts(self, text: str, speaker: str, sample_rate: int):
        self.in_flight.append(voice_server._model_in_flight)
        return super().apply_tts(text, speaker, sample_rate)


class WatchedLock:
    """`_fallbacks_lock`, instrumented so a test can hold its critical section OPEN.

    Entering announces itself and then waits for `let_go` before the guarded code runs, which puts a
    second caller where the property actually lives: queued on the real lock underneath. Counting
    threads cannot see that — the GIL happens not to preempt a tight `+= 1`, so a thread-count test
    passes with the lock removed (measured: 20/20 green against that mutation). This one does not.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.entered = threading.Event()
        self.let_go = threading.Event()

    def __enter__(self) -> "WatchedLock":
        self._lock.acquire()
        self.entered.set()
        self.let_go.wait(10)
        return self

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


@pytest.fixture
def silero_fallback(monkeypatch, xtts_engine):
    """The shipped pairing: an xtts primary (configured, reference on disk), silero behind it."""
    monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", "silero")
    return xtts_engine


@pytest.fixture
def broken_xtts(monkeypatch, silero_fallback):
    model = BrokenXtts()
    monkeypatch.setattr(voice_server, "xtts", lambda: model)
    return model


@pytest.fixture
def missing_coqui(monkeypatch, tmp_path, import_raises):
    """The same pairing with coqui-tts ABSENT — the primary is refused by its own request check.

    Deliberately not built on `xtts_engine`: that fixture installs the fake `TTS.api`, which the
    import probe would then find no matter what the import seam says.
    """
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFFfake")
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")
    monkeypatch.setattr(voice_server, "XTTS_REFERENCE", str(reference))
    monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", "silero")
    import_raises("TTS", ModuleNotFoundError("No module named 'TTS'", name="TTS"))
    return reference


# --- configuration ---------------------------------------------------------------------------------


def test_the_fallback_default_follows_the_primary_engine(monkeypatch):
    """silero behind xtts, nothing behind silero — a silero-primary box has nothing lighter."""
    monkeypatch.setenv("VOICE_LOOP_TTS_ENGINE", "xtts")
    assert importlib.reload(voice_server).TTS_FALLBACK_ENGINE == "silero"

    monkeypatch.setenv("VOICE_LOOP_TTS_ENGINE", "silero")
    assert importlib.reload(voice_server).TTS_FALLBACK_ENGINE == "none"

    monkeypatch.setenv("VOICE_LOOP_TTS_FALLBACK_ENGINE", "XTTS")  # explicit, and case-insensitive
    assert importlib.reload(voice_server).TTS_FALLBACK_ENGINE == "xtts"

    monkeypatch.delenv("VOICE_LOOP_TTS_ENGINE")
    monkeypatch.delenv("VOICE_LOOP_TTS_FALLBACK_ENGINE")
    importlib.reload(voice_server)  # back to the shipped defaults for the rest of the suite


@pytest.mark.parametrize("configured", ["none", "silero", "espeak", ""])
def test_a_useless_fallback_setting_simply_means_no_fallback(monkeypatch, configured):
    """"none", the primary itself and nonsense are all the same answer: there is nothing to fall to.

    A safety net must never be the thing that breaks a working primary."""
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "silero")
    monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", configured)
    assert voice_server.fallback_engine() == ""


def test_health_reports_the_effective_fallback_and_the_counter(client, monkeypatch):
    assert client.get("/health").json()["tts_fallback_engine"] == "none"

    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")
    monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", "silero")
    body = client.get("/health").json()
    assert body["tts_fallback_engine"] == "silero"
    assert body["tts_fallbacks"] == 0

    voice_server.count_fallback()
    assert client.get("/health").json()["tts_fallbacks"] == 1


def test_health_reports_none_when_the_fallback_is_the_primary_itself(client, monkeypatch):
    monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", "silero")  # == TTS_ENGINE
    assert client.get("/health").json()["tts_fallback_engine"] == "none"


def test_reset_caches_zeroes_the_fallback_counter():
    voice_server.count_fallback()
    assert voice_server._tts_fallbacks == 1
    voice_server.reset_caches()
    assert voice_server._tts_fallbacks == 0


@pytest.mark.timeout(15)
def test_a_second_handover_waits_for_the_counter_section(monkeypatch):
    """The counter is bumped under mutual exclusion — forced here, not hoped for.

    `+= 1` is a read-modify-write and the handovers do not share a thread: /tts counts on the event
    loop, every /tts/stream restart counts from the worker thread its generator is iterated on. So
    the section has to EXCLUDE, and the only way to see exclusion is to hold it open: while the
    first count_fallback sits inside, the second is provably still blocked and nothing has been
    counted yet; released, both land."""
    watched = WatchedLock()
    monkeypatch.setattr(voice_server, "_fallbacks_lock", watched)
    first = threading.Thread(target=voice_server.count_fallback)
    second = threading.Thread(target=voice_server.count_fallback)

    try:
        first.start()
        assert watched.entered.wait(5), "count_fallback never entered the counter's lock"

        second.start()
        second.join(0.5)
        assert second.is_alive(), "a second count_fallback ran while the section was held"
        assert voice_server._tts_fallbacks == 0  # the first is inside, before its own increment
    finally:
        # Tolerant of a failed assertion above: `second` may never have been started, and the
        # diagnostic that failed must reach the report instead of a join error on top of it.
        watched.let_go.set()
        for worker in (first, second):
            if worker.is_alive():
                worker.join(5)

    assert not (first.is_alive() or second.is_alive())
    assert voice_server._tts_fallbacks == 2  # both handovers, neither lost to the other


@pytest.mark.timeout(20)
def test_the_fallback_counter_survives_a_crowd_of_handovers():
    """A SMOKE, not the proof — the proof of exclusion is the test above.

    What this still buys: the locked counter does not deadlock or lose its lock under real
    concurrency, and the total is exact at a density no single-threaded run reaches. What it cannot
    do is fail when the lock is gone (CPython does not preempt the tight loop), which is exactly why
    it is not left to stand alone."""
    threads, per_thread = 8, 500
    start = threading.Barrier(threads)

    def hand_over() -> None:
        start.wait(10)
        for _ in range(per_thread):
            voice_server.count_fallback()

    workers = [threading.Thread(target=hand_over) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)

    assert not any(worker.is_alive() for worker in workers)
    assert voice_server._tts_fallbacks == threads * per_thread


# --- /tts: the engine header -----------------------------------------------------------------------


def test_tts_names_the_engine_that_spoke(client, fake_silero):
    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    assert response.headers["x-voice-loop-engine"] == "silero"


def test_tts_names_xtts_when_xtts_spoke(client, fake_xtts):
    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    assert response.headers["x-voice-loop-engine"] == "xtts"


# --- /tts: falling back ----------------------------------------------------------------------------


def test_tts_falls_back_when_the_primary_synthesis_raises(client, broken_xtts, fake_silero, caplog):
    with caplog.at_level("ERROR"):
        response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    assert response.headers["x-voice-loop-engine"] == "silero (fallback)"
    assert response.content[:4] == b"RIFF"
    _wav, sample_rate = sf.read(io.BytesIO(response.content))
    assert sample_rate == voice_server.TTS_SR  # the FALLBACK engine's rate, not XTTS's 24 kHz
    assert len(broken_xtts.calls) == 1 and len(fake_silero.calls) == 1
    assert voice_server._tts_fallbacks == 1
    assert "retrying this request on silero" in caplog.text
    assert "RuntimeError: xtts said no" in caplog.text  # the cause survives, in the log


def test_tts_falls_back_when_the_primary_fails_to_LOAD(client, monkeypatch, import_fake, fake_silero, tmp_path):
    """A load that dies even after the CPU retry is just as fatal to the voice as a bad synthesis."""

    class OomEverywhere(FakeXtts):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

        def to(self, device: object) -> "OomEverywhere":
            raise voice_server.torch.cuda.OutOfMemoryError("CUDA out of memory")

    api = import_fake("TTS.api", TTS=OomEverywhere)
    import_fake("TTS", api=api)
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFFfake")
    monkeypatch.setattr(voice_server, "DEVICE", "cuda")
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")
    monkeypatch.setattr(voice_server, "XTTS_REFERENCE", str(reference))
    monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", "silero")

    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    assert response.headers["x-voice-loop-engine"] == "silero (fallback)"
    assert len(fake_silero.calls) == 1
    assert voice_server._tts_fallbacks == 1


def test_tts_falls_back_when_the_primary_engine_is_not_installed(client, missing_coqui, fake_silero, caplog):
    """Missing coqui-tts with a fallback configured is degraded, not dead — the refusal never ships."""
    with caplog.at_level("WARNING"):
        response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    assert response.headers["x-voice-loop-engine"] == "silero (fallback)"
    assert len(fake_silero.calls) == 1
    assert voice_server._tts_fallbacks == 1
    assert "the xtts engine cannot serve requests — falling back to silero" in caplog.text


def test_tts_without_a_fallback_keeps_todays_error_path(client, monkeypatch, fake_silero):
    def explode(self, text: str, speaker: str, sample_rate: int):
        raise RuntimeError("silero said no")

    monkeypatch.setattr(FakeSilero, "apply_tts", explode)

    with pytest.raises(RuntimeError, match="silero said no"):
        client.post("/tts", json={"text": "Привет."})
    assert voice_server._tts_fallbacks == 0


def test_tts_never_falls_back_to_the_primary_engine_itself(client, monkeypatch, fake_silero):
    """A fallback equal to the primary is not a fallback: retrying the same broken engine is noise."""
    monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", "silero")
    calls: list[str] = []

    def explode(self, text: str, speaker: str, sample_rate: int):
        calls.append(text)
        raise RuntimeError("silero said no")

    monkeypatch.setattr(FakeSilero, "apply_tts", explode)

    with pytest.raises(RuntimeError, match="silero said no"):
        client.post("/tts", json={"text": "Привет."})
    assert len(calls) == 1  # one attempt, not two
    assert voice_server._tts_fallbacks == 0


def test_tts_a_failing_fallback_is_the_ordinary_error_path(client, monkeypatch, broken_xtts, fake_silero):
    def explode(self, text: str, speaker: str, sample_rate: int):
        raise RuntimeError("silero said no too")

    monkeypatch.setattr(FakeSilero, "apply_tts", explode)

    with pytest.raises(RuntimeError, match="silero said no too"):
        client.post("/tts", json={"text": "Привет."})
    assert len(broken_xtts.calls) == 1
    assert voice_server._tts_fallbacks == 1  # the handover happened; it just did not save the request


def test_tts_a_fallback_that_took_over_up_front_is_not_retried_again(client, monkeypatch, missing_coqui):
    """The primary was already refused before synthesis — the fallback's own failure has nowhere to go."""
    model = FakeSilero()

    def explode(self, text: str, speaker: str, sample_rate: int):
        raise RuntimeError("silero said no")

    monkeypatch.setattr(FakeSilero, "apply_tts", explode)
    monkeypatch.setattr(voice_server, "tts", lambda language: model)

    with pytest.raises(RuntimeError, match="silero said no"):
        client.post("/tts", json={"text": "Привет."})
    assert voice_server._tts_fallbacks == 1


def test_tts_a_fallback_that_cannot_speak_the_language_returns_the_ordinary_error(
    client, broken_xtts, fake_silero
):
    """The engines' language sets differ: XTTS-v2 speaks ja, Silero has no model for it."""
    response = client.post("/tts", json={"text": "こんにちは。", "language": "ja"})

    assert response.status_code == 400
    body = response.json()
    assert "ja" in body["error"] and "ru" in body["supported"]
    assert len(broken_xtts.calls) == 1 and fake_silero.calls == []
    assert voice_server._tts_fallbacks == 0  # nothing was handed over


def test_tts_client_errors_never_reach_the_fallback(client, silero_fallback, fake_silero):
    """A 400 is the request's problem, not the engine's — no other engine can fix it.

    `uk` is the sharp case: Silero speaks it and XTTS-v2 does not, so a fallback WOULD have a voice
    for it. The refusal still stands — engine fallback is for a broken engine, not for routing
    around a language the configured engine was never asked to speak."""
    unsupported = client.post("/tts", json={"text": "Привіт.", "language": "uk"})
    assert unsupported.status_code == 400
    assert "uk" in unsupported.json()["error"]

    empty = client.post("/tts", json={"text": "   "})
    assert (empty.status_code, empty.json()["error"]) == (400, "empty text")

    assert fake_silero.calls == []
    assert voice_server._tts_fallbacks == 0


# --- the refusal points at the engine that CAN speak -----------------------------------------------
# A language 400 is final (fallback is not a router), but it should not be a dead end: when the OTHER
# known engine has a voice for the requested language, the refusal names the setting that serves it.


def test_a_language_refusal_names_the_engine_that_speaks_it(client, fake_silero):
    """Silero has no Japanese and XTTS-v2 does — the box can speak this, one setting away."""
    response = client.post("/tts", json={"text": "こんにちは。", "language": "ja"})

    assert response.status_code == 400
    body = response.json()
    assert "switch VOICE_LOOP_TTS_ENGINE=xtts to serve 'ja'" in body["hint"]
    assert "cloud TTS backend" in body["hint"]  # the standing advice survives alongside it
    assert fake_silero.calls == []


def test_the_xtts_language_refusal_names_silero(client, fake_xtts):
    """The other direction — `uk` is the language an xtts box keeps Silero around for."""
    response = client.post("/tts", json={"text": "Привіт.", "language": "uk"})

    assert response.status_code == 400
    body = response.json()
    assert body["hint"] == "switch VOICE_LOOP_TTS_ENGINE=silero to serve 'uk'"
    assert "uk" in body["error"] and "ru" in body["supported"]  # the refusal itself is unchanged
    assert fake_xtts.calls == []


def test_a_language_no_engine_speaks_gets_no_false_hint(client, fake_silero):
    """Never a lead that goes nowhere: neither engine has a Kazakh voice, so none is suggested."""
    body = client.post("/tts", json={"text": "Сәлем.", "language": "kk"}).json()

    assert "VOICE_LOOP_TTS_ENGINE" not in body["hint"]
    assert "cloud TTS backend" in body["hint"]


def test_the_xtts_refusal_stays_hintless_when_no_engine_speaks_the_language(client, fake_xtts):
    body = client.post("/tts", json={"text": "Сәлем.", "language": "kk"}).json()

    assert "kk" in body["error"] and "hint" not in body


def test_the_refusal_never_points_back_at_the_configured_engine(client, broken_xtts, fake_silero):
    """The fallback's refusal is still the fallback's: `ja` is a language the BROKEN primary speaks,
    and "switch to xtts" would be advice to switch to what is already configured and just failed."""
    body = client.post("/tts", json={"text": "こんにちは。", "language": "ja"}).json()

    assert "ja" in body["error"]
    assert "VOICE_LOOP_TTS_ENGINE" not in body["hint"]


# --- the one model slot ----------------------------------------------------------------------------


@pytest.mark.timeout(15)
def test_the_failed_primary_releases_its_slot_before_the_fallback_takes_one(
    client, monkeypatch, silero_fallback, one_slot_gate
):
    """A one-slot gate is never held twice for one request — the failed engine's slot goes back first.

    Held twice, a plain BoundedSemaphore would not fail this test — it would HANG on the second
    acquire. Two guards make the regression report itself instead: `one_slot_gate` raises
    GateHeldTwice rather than blocking forever (the failure the endpoint would then surface), and
    the timeout marker bounds the case even if a future shape finds another way to wedge."""
    in_flight: list[int] = []
    xtts_model, silero_model = BrokenXtts(in_flight), RecordingSilero(in_flight)
    monkeypatch.setattr(voice_server, "xtts", lambda: xtts_model)
    monkeypatch.setattr(voice_server, "tts", lambda language: silero_model)

    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    assert in_flight == [1, 1]  # one slot during the primary, one during the fallback — never two
    assert voice_server._model_in_flight == 0
    assert one_slot_gate.acquire(timeout=1)  # and the gate is genuinely free afterwards
    one_slot_gate.release()


# --- /tts/stream -----------------------------------------------------------------------------------


def test_stream_falls_back_before_the_first_chunk(client, broken_xtts, fake_silero, caplog):
    with caplog.at_level("ERROR"):
        response = client.post("/tts/stream", json={"text": LONG_TEXT})

    parsed = events(response.text)
    chunks, terminal = parsed[:-1], parsed[-1]
    assert response.status_code == 200
    assert len(chunks) > 1
    assert [name for name, _ in chunks] == ["chunk"] * len(chunks)
    assert terminal == ("end", {"chunks": len(chunks), "engine": "silero (fallback)"})
    assert len(broken_xtts.calls) == 1  # the primary died on its first chunk
    assert len(fake_silero.calls) == len(chunks)  # and the WHOLE text was re-synthesized
    assert voice_server._tts_fallbacks == 1
    assert "restarting on silero" in caplog.text


def test_stream_fallback_chunks_carry_the_fallback_engines_sample_rate(client, broken_xtts, fake_silero):
    response = client.post("/tts/stream", json={"text": "Привет."})

    _wav, sample_rate = wav_of(events(response.text)[0][1])
    assert sample_rate == voice_server.TTS_SR


def test_stream_takes_the_fallback_over_before_it_starts_when_the_primary_is_unusable(
    client, missing_coqui, fake_silero
):
    response = client.post("/tts/stream", json={"text": "Привет."})

    assert response.status_code == 200
    assert events(response.text)[-1] == ("end", {"chunks": 1, "engine": "silero (fallback)"})


@pytest.mark.timeout(15)
def test_the_failed_stream_releases_its_slot_before_the_restart_takes_one(
    client, monkeypatch, silero_fallback, one_slot_gate
):
    """The same property as on /tts, on the path that restarts INSIDE the response: the broken
    generator is finished and its slot returned before the restart's first chunk acquires one.

    The stream's shape makes this the easier one to get wrong — the restart happens in the middle
    of a live response, from an exception handler that could plausibly still hold the gate. Held
    twice, `one_slot_gate` raises instead of hanging, and the restart is then reported as a
    terminal error event rather than the `end` this asserts."""
    in_flight: list[int] = []
    xtts_model, silero_model = BrokenXtts(in_flight), RecordingSilero(in_flight)
    monkeypatch.setattr(voice_server, "xtts", lambda: xtts_model)
    monkeypatch.setattr(voice_server, "tts", lambda language: silero_model)

    response = client.post("/tts/stream", json={"text": "Привет."})

    assert response.status_code == 200
    assert events(response.text)[-1] == ("end", {"chunks": 1, "engine": "silero (fallback)"})
    assert in_flight == [1, 1]  # one slot for the dead primary chunk, one for the restart — never two
    assert voice_server._model_in_flight == 0
    assert one_slot_gate.acquire(timeout=1)
    one_slot_gate.release()


def test_stream_never_falls_back_mid_stream(client, monkeypatch, silero_fallback, fake_silero, caplog):
    """Today's terminal error event, unchanged: a client that already played chunk 0 is not handed a
    different voice mid-sentence, and the 200 left with those bytes long ago."""
    model = FakeXtts()
    original = FakeXtts.tts

    def explode_on_the_second_chunk(self, text: str, speaker_wav: str, language: str):
        if len(self.calls) == 1:
            raise RuntimeError("xtts said no")
        return original(self, text, speaker_wav, language)

    monkeypatch.setattr(FakeXtts, "tts", explode_on_the_second_chunk)
    monkeypatch.setattr(voice_server, "xtts", lambda: model)

    with caplog.at_level("ERROR"):
        response = client.post("/tts/stream", json={"text": LONG_TEXT})

    parsed = events(response.text)
    assert parsed[0][0] == "chunk"
    assert parsed[-1] == ("error", {"error": "synthesis failed (RuntimeError)", "chunks": 1})
    assert fake_silero.calls == []  # the fallback was never consulted
    assert voice_server._tts_fallbacks == 0
    assert "streaming synthesis failed after 1 chunk(s)" in caplog.text


def test_stream_a_failing_fallback_ends_with_the_terminal_error(client, monkeypatch, broken_xtts):
    """One restart, never a loop: the fallback's own failure is the ordinary terminal error event."""
    model = FakeSilero()

    def explode(self, text: str, speaker: str, sample_rate: int):
        raise RuntimeError("silero said no too")

    monkeypatch.setattr(FakeSilero, "apply_tts", explode)
    monkeypatch.setattr(voice_server, "tts", lambda language: model)

    response = client.post("/tts/stream", json={"text": "Привет."})

    assert events(response.text) == [("error", {"error": "synthesis failed (RuntimeError)", "chunks": 0})]
    assert voice_server._tts_fallbacks == 1


def test_stream_refusals_that_no_engine_can_fix_stay_plain_json(client, silero_fallback, fake_silero):
    response = client.post("/tts/stream", json={"text": "Привіт.", "language": "uk"})

    assert response.status_code == 400
    assert "uk" in response.json()["error"]
    assert fake_silero.calls == []
