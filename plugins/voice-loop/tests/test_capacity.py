"""The bounded model executor — one queue per piece of hardware.

Every model call site (/stt transcription, /tts blob rendering, each /tts/stream chunk) takes a slot
from the gate belonging to the device it actually runs on: whisper follows the GPU, Silero is pinned
to the CPU, XTTS goes wherever it managed to load. anyio's default threadpool merely queues behind
them. These tests drive the gates directly, through the chunk iterator, and via /health.
"""

from __future__ import annotations

import importlib
import threading
import time

import pytest

import voice_server
from conftest import GateHeldTwice


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


def test_default_model_concurrency_can_be_asked_about_a_named_device(monkeypatch):
    """The CPU queue on a GPU box: its size is the CPU's, not the card's, whatever DEVICE says."""
    monkeypatch.setattr(voice_server, "DEVICE", "cuda")
    monkeypatch.setattr(voice_server.os, "cpu_count", lambda: 8)
    assert voice_server.default_model_concurrency("cpu") == 2
    assert voice_server.default_model_concurrency("gpu") == 1


def test_model_concurrency_env_override(monkeypatch):
    monkeypatch.setenv("VOICE_LOOP_MODEL_CONCURRENCY", "3")
    module = importlib.reload(voice_server)
    assert module.MODEL_CONCURRENCY == 3
    # It sets the queue the operator meant — the device the server runs its models on — and leaves
    # the incidental other queue at its own default.
    primary = module.model_device(module.resolve_device())
    assert module.MODEL_CONCURRENCY_BY_DEVICE[primary] == 3
    other = next(device for device in module.MODEL_DEVICES if device != primary)
    assert module.MODEL_CONCURRENCY_BY_DEVICE[other] == module.default_model_concurrency(other)
    monkeypatch.delenv("VOICE_LOOP_MODEL_CONCURRENCY")
    importlib.reload(voice_server)  # back to the shipped default for the rest of the suite


# --- which queue a call belongs in -----------------------------------------------------------------


def test_model_device_folds_every_accelerator_onto_one_queue():
    """There is one accelerator budget to protect, and its torch name is not what makes it scarce."""
    assert voice_server.model_device("cpu") == "cpu"
    assert voice_server.model_device("cuda") == "gpu"
    assert voice_server.model_device("cuda:1") == "gpu"
    assert voice_server.model_device("mps") == "gpu"


def test_silero_synthesis_stays_on_the_cpu_queue_even_on_a_gpu_box(monkeypatch):
    """The whole point of pinning Silero to the CPU: it must not queue behind GPU recognition."""
    monkeypatch.setattr(voice_server, "DEVICE", "cuda")
    assert voice_server.synthesis_device("silero") == "cpu"
    assert voice_server.model_device(voice_server.synthesis_device("silero")) == "cpu"


def test_xtts_synthesis_queues_on_the_resolved_device_until_it_has_loaded(monkeypatch):
    monkeypatch.setattr(voice_server, "DEVICE", "cuda")
    assert voice_server._xtts_device == ""
    assert voice_server.synthesis_device("xtts") == "cuda"


def test_xtts_synthesis_follows_where_the_model_actually_landed(monkeypatch):
    """A card too small drops XTTS to the CPU — so its slot must come from the CPU queue, not the GPU's.

    That the loader RECORDS the device it fell back to is pinned next to the loader itself, in
    test_xtts.py; this is the reading half.
    """
    monkeypatch.setattr(voice_server, "DEVICE", "cuda")
    monkeypatch.setattr(voice_server, "_xtts_device", "cpu")
    assert voice_server.synthesis_device("xtts") == "cpu"


def test_synthesis_device_reads_the_configured_engine_when_unnamed(monkeypatch):
    monkeypatch.setattr(voice_server, "DEVICE", "cuda")
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")
    assert voice_server.synthesis_device() == "cuda"
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "silero")
    assert voice_server.synthesis_device() == "cpu"


@pytest.mark.timeout(10)
def test_the_two_queues_do_not_block_each_other(monkeypatch):
    """A GPU transcription must not stall CPU playback — the reason the gate is per device."""
    monkeypatch.setattr(
        voice_server,
        "_model_gates",
        {device: threading.BoundedSemaphore(1) for device in voice_server.MODEL_DEVICES},
    )
    with voice_server.model_slot("cuda"):
        assert voice_server.model_in_flight("gpu") == 1
        with voice_server.model_slot("cpu"):  # would deadlock behind one shared gate
            assert voice_server.model_in_flight("cpu") == 1
            assert voice_server.model_in_flight() == 2
    assert voice_server.model_in_flight() == 0


# --- the gate itself -------------------------------------------------------------------------------


def test_model_slot_counts_in_flight_work():
    assert voice_server.model_in_flight() == 0
    with voice_server.model_slot():
        assert voice_server.model_in_flight() == 1
    assert voice_server.model_in_flight() == 0


@pytest.mark.timeout(15)
def test_model_slot_bounds_concurrent_model_work(monkeypatch):
    """Two threads, one slot on the SAME device: the second must WAIT, not run alongside."""
    monkeypatch.setitem(voice_server._model_gates, "cpu", threading.BoundedSemaphore(1))
    first_inside = threading.Event()
    release_first = threading.Event()
    second_inside = threading.Event()

    def first():
        with voice_server.model_slot("cpu"):
            first_inside.set()
            release_first.wait(5)

    def second():
        with voice_server.model_slot("cpu"):
            second_inside.set()

    workers = [threading.Thread(target=first), threading.Thread(target=second)]
    workers[0].start()
    assert first_inside.wait(5)
    workers[1].start()
    assert not second_inside.wait(0.2)  # blocked behind the single slot
    assert voice_server.model_in_flight("cpu") == 1
    assert voice_server.queue_depths()["cpu"]["waiting"] == 1  # and visibly queued, not just busy
    release_first.set()
    assert second_inside.wait(5)
    for worker in workers:
        worker.join(5)
    assert voice_server.model_in_flight() == 0
    assert voice_server.queue_depths()["cpu"]["waiting"] == 0


def test_a_slot_that_is_never_granted_leaves_nobody_waiting(one_slot_gate):
    """The counter is a gauge, not a tally: an acquire that never returns leaks no phantom waiter.

    `__enter__` rather than a `with` block on purpose — the acquire is what raises, so there is no
    body to enter and nothing to unwind.
    """
    with voice_server.model_slot("cpu"):
        with pytest.raises(GateHeldTwice):
            voice_server.model_slot("cpu").__enter__()  # the one shared slot is already held
        assert voice_server.queue_depths()["cpu"]["waiting"] == 0
    assert voice_server.model_in_flight() == 0


def test_gated_pieces_releases_the_slot_between_chunks(monkeypatch):
    """The property the /tts/stream cap depends on: a long stream queues fairly, chunk by chunk."""
    gate = threading.BoundedSemaphore(1)
    monkeypatch.setitem(voice_server._model_gates, "cpu", gate)
    in_flight_during_synthesis: list[int] = []

    def pieces():
        in_flight_during_synthesis.append(voice_server.model_in_flight("cpu"))
        yield "first"
        in_flight_during_synthesis.append(voice_server.model_in_flight("cpu"))
        yield "second"

    shipped: list[str] = []
    for piece in voice_server.gated_pieces(pieces(), "cpu"):
        shipped.append(piece)
        assert voice_server.model_in_flight() == 0  # released while the chunk is on the wire
        assert gate.acquire(blocking=False)  # genuinely free for another request
        gate.release()

    assert shipped == ["first", "second"]
    assert in_flight_during_synthesis == [1, 1]  # and held while synthesizing


# --- surfaced in /health ---------------------------------------------------------------------------


def test_health_exposes_the_executor(client):
    body = client.get("/health").json()
    assert body["model_concurrency"] == voice_server.MODEL_CONCURRENCY >= 1
    assert body["model_in_flight"] == 0
    assert body["model_waiting"] == 0


@pytest.mark.timeout(15)
def test_health_reports_the_queue_behind_each_device(client, monkeypatch):
    """Busy and saturated look identical without this — `waiting` is the whole difference.

    One slot, one holder and one caller queued behind it: `model_in_flight` reads 1 in BOTH states,
    so it is `model_waiting` that has to say somebody is paying for the wait.
    """
    monkeypatch.setitem(voice_server._model_gates, "cpu", threading.BoundedSemaphore(1))
    monkeypatch.setitem(voice_server.MODEL_CONCURRENCY_BY_DEVICE, "cpu", 1)  # what /health reports
    holding = threading.Event()
    let_go = threading.Event()
    passed_through = threading.Event()

    def holder():
        with voice_server.model_slot("cpu"):
            holding.set()
            let_go.wait(10)

    def queuer():
        with voice_server.model_slot("cpu"):  # blocks until the holder lets go
            passed_through.set()

    workers = [threading.Thread(target=holder), threading.Thread(target=queuer)]
    workers[0].start()
    assert holding.wait(5)
    workers[1].start()
    deadline = time.monotonic() + 5
    while not voice_server.queue_depths()["cpu"]["waiting"] and time.monotonic() < deadline:
        time.sleep(0.01)

    body = client.get("/health").json()
    assert body["model_in_flight"] == 1
    assert body["model_waiting"] == 1
    assert body["model_queues"]["cpu"] == {"limit": 1, "in_flight": 1, "waiting": 1}  # saturated
    assert body["model_queues"]["gpu"] == {  # a disjoint queue, untouched by the CPU's backlog
        "limit": voice_server.MODEL_CONCURRENCY_BY_DEVICE["gpu"],
        "in_flight": 0,
        "waiting": 0,
    }

    let_go.set()
    assert passed_through.wait(5)
    for worker in workers:
        worker.join(5)
    assert client.get("/health").json()["model_waiting"] == 0
