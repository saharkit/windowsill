"""The bounded model executor — one shared card, one queue.

Every model call site (/stt transcription, /tts blob rendering, each /tts/stream chunk) takes one
of MODEL_CONCURRENCY slots from a single global gate; anyio's default threadpool merely queues
behind it. These tests drive the gate directly, through the chunk iterator, and via /health.
"""

from __future__ import annotations

import importlib
import threading

import voice_server


# --- the default size ------------------------------------------------------------------------------


def test_default_model_concurrency_is_one_on_cuda(monkeypatch):
    """Concurrent calls stack activation memory on the one VRAM budget — a second slot buys an OOM."""
    monkeypatch.setattr(voice_server, "DEVICE", "cuda")
    assert voice_server.default_model_concurrency() == 1


def test_default_model_concurrency_uses_half_the_cores_on_cpu_capped_at_two(monkeypatch):
    monkeypatch.setattr(voice_server, "DEVICE", "cpu")
    monkeypatch.setattr(voice_server.os, "cpu_count", lambda: 8)
    assert voice_server.default_model_concurrency() == 2


def test_default_model_concurrency_never_drops_below_one(monkeypatch):
    monkeypatch.setattr(voice_server, "DEVICE", "cpu")
    monkeypatch.setattr(voice_server.os, "cpu_count", lambda: 1)
    assert voice_server.default_model_concurrency() == 1


def test_model_concurrency_env_override(monkeypatch):
    monkeypatch.setenv("VOICE_LOOP_MODEL_CONCURRENCY", "3")
    module = importlib.reload(voice_server)
    assert module.MODEL_CONCURRENCY == 3
    monkeypatch.delenv("VOICE_LOOP_MODEL_CONCURRENCY")
    importlib.reload(voice_server)  # back to the shipped default for the rest of the suite


# --- the gate itself -------------------------------------------------------------------------------


def test_model_slot_counts_in_flight_work():
    assert voice_server._model_in_flight == 0
    with voice_server.model_slot():
        assert voice_server._model_in_flight == 1
    assert voice_server._model_in_flight == 0


def test_model_slot_bounds_concurrent_model_work(monkeypatch):
    """Two threads, one slot: the second must WAIT, not run alongside."""
    monkeypatch.setattr(voice_server, "_model_gate", threading.BoundedSemaphore(1))
    first_inside = threading.Event()
    release_first = threading.Event()
    second_inside = threading.Event()

    def first():
        with voice_server.model_slot():
            first_inside.set()
            release_first.wait(5)

    def second():
        with voice_server.model_slot():
            second_inside.set()

    workers = [threading.Thread(target=first), threading.Thread(target=second)]
    workers[0].start()
    assert first_inside.wait(5)
    workers[1].start()
    assert not second_inside.wait(0.2)  # blocked behind the single slot
    assert voice_server._model_in_flight == 1
    release_first.set()
    assert second_inside.wait(5)
    for worker in workers:
        worker.join(5)
    assert voice_server._model_in_flight == 0


def test_gated_pieces_releases_the_slot_between_chunks(monkeypatch):
    """The property the /tts/stream cap depends on: a long stream queues fairly, chunk by chunk."""
    gate = threading.BoundedSemaphore(1)
    monkeypatch.setattr(voice_server, "_model_gate", gate)
    in_flight_during_synthesis: list[int] = []

    def pieces():
        in_flight_during_synthesis.append(voice_server._model_in_flight)
        yield "first"
        in_flight_during_synthesis.append(voice_server._model_in_flight)
        yield "second"

    shipped: list[str] = []
    for piece in voice_server.gated_pieces(pieces()):
        shipped.append(piece)
        assert voice_server._model_in_flight == 0  # released while the chunk is on the wire
        assert gate.acquire(blocking=False)  # genuinely free for another request
        gate.release()

    assert shipped == ["first", "second"]
    assert in_flight_during_synthesis == [1, 1]  # and held while synthesizing


# --- surfaced in /health ---------------------------------------------------------------------------


def test_health_exposes_the_executor(client):
    body = client.get("/health").json()
    assert body["model_concurrency"] == voice_server.MODEL_CONCURRENCY >= 1
    assert body["model_in_flight"] == 0
