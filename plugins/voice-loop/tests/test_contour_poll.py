"""The contour poller's pure parts: config normalization, the two probes at their seams, the
alert rules, the p95-by-device SLI, and the atomic status file — plus the CLI's exit-code
contract (0 quiet, 1 page, 64 called wrong).

Nothing here touches the network, a GPU, or the real state dir: the health fetch is one injected
callable (``fetch``), the nvidia-smi spawn is another (``runner``), and the clock is a third — the
same seam-and-real-body shape the rest of the suite uses. The real invocation — a poll against
the live server the loopback job starts, including a REAL demotion alert on a GPU-less runner —
is in CI (see TESTING.md); what a fake structurally cannot prove is not claimed here.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

_POLL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "contour_poll.py"
_spec = importlib.util.spec_from_file_location("contour_poll", _POLL_PATH)
contour_poll = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contour_poll)

# The fixed clock every poll in this file runs on: time is an input, never an ambient read.
_T0 = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


# --- normalize_service / resolve_settings: the config surface -----------------------------------


def test_a_bare_url_string_becomes_a_service_named_by_where_it_lives():
    service = contour_poll.normalize_service("http://127.0.0.1:8358/health")
    assert service == {
        "name": "127.0.0.1:8358",
        "health": "http://127.0.0.1:8358/health",
        "expect_device": "",
        "latency_fields": [],
    }


def test_a_full_entry_keeps_its_name_expectation_and_latency_fields():
    service = contour_poll.normalize_service(
        {"name": "rvc", "health": "http://127.0.0.1:8358/health", "expect_device": "gpu", "latency_fields": ["rtf"]}
    )
    assert service["name"] == "rvc"
    assert service["expect_device"] == "gpu"
    assert service["latency_fields"] == ["rtf"]


def test_an_entry_without_a_usable_url_is_not_a_service():
    # A configured service that can never be polled is a monitoring hole that looks like coverage
    # — normalize to None and let the caller be loud about it.
    assert contour_poll.normalize_service("not-a-url") is None
    assert contour_poll.normalize_service({"name": "rvc"}) is None
    assert contour_poll.normalize_service({"health": "ftp://example/health"}) is None
    assert contour_poll.normalize_service(8358) is None


def test_the_default_contour_is_the_local_speech_server_and_nothing_else():
    settings = contour_poll.resolve_settings({})
    assert [s["health"] for s in settings["services"]] == ["http://127.0.0.1:8355/health"]
    # loopback only — no real host is ever baked into the shipped defaults
    assert settings["timeout"] == 5.0
    assert settings["vram_min_free_mib"] == 200
    assert settings["history"] == 2016


def test_contour_knobs_come_from_the_config():
    settings = contour_poll.resolve_settings(
        {"contour": {"timeout": 2, "vram": {"min_free_mib": 512, "command": False}, "history": 10}}
    )
    assert settings["timeout"] == 2.0
    assert settings["vram_min_free_mib"] == 512
    assert settings["vram_command"] == ""  # JSON false disables the probe; "" cannot (cfg parity)
    assert settings["history"] == 10


def test_a_configured_entry_that_can_never_be_polled_is_an_error_not_a_skip():
    # A configured service this poller silently never polls is a monitoring hole wearing the word
    # "ok" — so it stops the run rather than shrinking the contour behind the operator's back.
    with pytest.raises(contour_poll.ConfigError, match="no fetchable"):
        contour_poll.resolve_settings({"contour": {"services": ["not-a-url"]}})


def test_services_configured_says_whether_the_contour_is_the_operators_or_the_default():
    # The flag main() prints: "contour ok" over the shipped default, while the operator believes
    # their own list is being watched, is the coverage lie this whole poller exists to prevent.
    assert contour_poll.resolve_settings({})["services_configured"] is False
    configured = contour_poll.resolve_settings({"contour": {"services": ["http://a/health"]}})
    assert configured["services_configured"] is True


def test_a_services_value_that_is_not_a_list_is_an_error_not_a_silent_default():
    # {"services": {...}} is the natural typo, and it used to fall back to DEFAULT_SERVICES: the
    # poller then polled loopback, printed "contour ok", and none of the three configured
    # services was ever looked at.
    with pytest.raises(contour_poll.ConfigError, match="must be a list"):
        contour_poll.resolve_settings({"contour": {"services": {"speech": "http://127.0.0.1:8355/health"}}})


def test_two_entries_resolving_to_one_name_are_an_error_not_a_silent_merge():
    # Same host:port, different paths: both normalize to the same name, so one overwrote the
    # other's slot, the page said "X; X", and one history sample was lost every poll.
    with pytest.raises(contour_poll.ConfigError, match="two entries named"):
        contour_poll.resolve_settings(
            {"contour": {"services": ["http://127.0.0.1:8355/health", "http://127.0.0.1:8355/healthz"]}}
        )
    # naming one of them is the fix the message asks for, and it resolves
    settings = contour_poll.resolve_settings(
        {"contour": {"services": ["http://127.0.0.1:8355/health", {"name": "spare", "health": "http://127.0.0.1:8355/healthz"}]}}
    )
    assert [s["name"] for s in settings["services"]] == ["127.0.0.1:8355", "spare"]


@pytest.mark.parametrize(
    ("dotted", "config"),
    [
        ("contour.timeout", {"contour": {"timeout": "5s"}}),
        ("contour.history", {"contour": {"history": "lots"}}),
        ("contour.vram.min_free_mib", {"contour": {"vram": {"min_free_mib": "200 MiB"}}}),
        ("contour.max_age", {"contour": {"max_age": "10m"}}),
    ],
)
def test_a_mistyped_scalar_knob_is_a_named_config_error_not_a_bare_valueerror(dotted, config):
    # A bare ValueError out of here reaches Python's own handler, which exits 1 — the PAGE code.
    with pytest.raises(contour_poll.ConfigError, match=dotted.replace(".", r"\.")):
        contour_poll.resolve_settings(config)


def test_a_url_urllib_could_never_fetch_is_rejected_at_the_config_not_at_the_fetch():
    # Both shapes parse fine and then raise out of the OPENER — a UnicodeError and a ValueError
    # that used to leave main() as exit 1, the page code, for what is plainly a typo.
    assert contour_poll.normalize_service("http://a..b/health") is None
    assert contour_poll.normalize_service("http://x:notaport/health") is None
    assert contour_poll.normalize_service("http://:8355/health") is None  # a port and no host
    assert contour_poll.normalize_service("http://[::1/health") is None
    assert contour_poll.normalize_service("http://" + "x" * 300 + ".example/health") is None
    # …and the ordinary ones still resolve
    assert contour_poll.normalize_service("http://[::1]:8355/health")["name"] == "[::1]:8355"


# --- fetch_health: the bounded probe --------------------------------------------------------------


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self, n: int) -> bytes:
        return self._body[:n]


def _opener(body: bytes):
    return lambda url, timeout: _Resp(body)


def test_a_health_body_parses_to_a_dict():
    assert contour_poll.fetch_health("http://x/health", 1.0, _opener(b'{"ok": true}')) == {"ok": True}


def test_an_http_error_status_is_an_availability_answer_not_a_crash():
    def raising(url, timeout):
        raise urllib.error.HTTPError(url, 500, "broken", {}, None)

    with pytest.raises(contour_poll.FetchError, match="http 500"):
        contour_poll.fetch_health("http://x/health", 1.0, raising)


def test_a_refused_connection_is_a_fetch_error():
    def raising(url, timeout):
        raise urllib.error.URLError(OSError("refused"))

    with pytest.raises(contour_poll.FetchError):
        contour_poll.fetch_health("http://x/health", 1.0, raising)


def test_the_body_is_capped():
    over = b" " * (contour_poll.BODY_CAP + 1)
    with pytest.raises(contour_poll.FetchError, match="body over"):
        contour_poll.fetch_health("http://x/health", 1.0, _opener(over))


def test_a_body_that_is_not_a_json_object_is_a_fetch_error():
    with pytest.raises(contour_poll.FetchError):
        contour_poll.fetch_health("http://x/health", 1.0, _opener(b"not json"))
    with pytest.raises(contour_poll.FetchError):
        contour_poll.fetch_health("http://x/health", 1.0, _opener(b"[1, 2]"))


# --- sample_vram: the bounded subprocess ----------------------------------------------------------


class _Done:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def test_free_vram_is_the_tightest_card():
    def runner(argv, timeout):
        assert isinstance(argv, list)  # argv, never a shell string
        return _Done("2206\n1161\n")

    assert contour_poll.sample_vram("nvidia-smi --query-gpu=memory.free", 5.0, runner) == 1161


def test_an_empty_command_disables_the_probe():
    def never(argv, timeout):
        raise AssertionError("a disabled probe spawns nothing")

    assert contour_poll.sample_vram("", 5.0, never) is None


def test_no_answer_worth_having_is_none_not_an_alert():
    assert contour_poll.sample_vram("nvidia-smi", 5.0, lambda argv, timeout: _Done("", 1)) is None
    assert contour_poll.sample_vram("nvidia-smi", 5.0, lambda argv, timeout: _Done("garbage\n")) is None

    def missing(argv, timeout):
        raise FileNotFoundError("nvidia-smi")

    def wedged(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert contour_poll.sample_vram("nvidia-smi", 5.0, missing) is None
    assert contour_poll.sample_vram("nvidia-smi", 5.0, wedged) is None


# --- p95 / latency_p95: the SLI, split by device ----------------------------------------------------


def test_p95_is_nearest_rank():
    assert contour_poll.p95([]) is None
    assert contour_poll.p95([4.0]) == 4.0
    assert contour_poll.p95([float(n) for n in range(1, 21)]) == 19.0


def test_latency_p95_is_computed_per_device_never_across():
    history = [
        {"device": "cuda", "fields": {"rtf": 0.1}},
        {"device": "cuda", "fields": {"rtf": 0.2}},
        {"device": "cpu", "fields": {"rtf": 4.0}},
        {"device": "cpu", "fields": {"rtf": 8.0}},
        {"device": None, "fields": {"rtf": 1.0}},  # no device reported: its own bucket, not noise
        {"device": "cuda", "fields": {}},  # a sample without the field is skipped
        {"device": "cuda", "fields": {"rtf": "fast"}},  # and a non-numeric one
    ]
    result = contour_poll.latency_p95(history, ["rtf"])
    assert result == {"cuda": {"rtf": 0.2}, "cpu": {"rtf": 8.0}, "unknown": {"rtf": 1.0}}


# --- evaluate_service / evaluate_vram: the four rules ----------------------------------------------


def _service(**overrides):
    base = {"name": "rvc", "health": "http://x/health", "expect_device": "", "latency_fields": []}
    base.update(overrides)
    return base


def _sample(**overrides):
    base = {"reachable": True, "ok": True, "device": "cuda", "oom_overflows": 0, "fields": {}, "detail": ""}
    base.update(overrides)
    return base


def test_an_unreachable_service_pages_once_and_nothing_more_can_be_known():
    alerts = contour_poll.evaluate_service(_service(expect_device="gpu"), _sample(reachable=False, detail="refused"), {})
    assert [a["kind"] for a in alerts] == ["unreachable"]
    assert "refused" in alerts[0]["message"]


def test_a_health_answer_of_not_ok_is_an_alert():
    alerts = contour_poll.evaluate_service(_service(), _sample(ok=False), {})
    assert [a["kind"] for a in alerts] == ["not-ok"]
    # …and ONLY an explicit false. A document that does not say is not a document that says no.
    assert contour_poll.evaluate_service(_service(), _sample(ok=None), {}) == []


def test_a_foreign_health_document_is_answering_not_down(tmp_path):
    """The README's own example config, run for real: the "converter" entry at 192.0.2.10:8358
    publishes {"status": "healthy", …} and no ``ok`` key at all. Reading a missing field as
    ok=false made every third-party /health a PERMANENT alert — a monitor crying wolf about a
    service that never had a fault, which is how a page stops being listened to."""
    settings = contour_poll.resolve_settings(
        {
            "contour": {
                "services": [
                    {"name": "speech", "health": "http://127.0.0.1:8355/health"},
                    {"name": "converter", "health": "http://192.0.2.10:8358/health", "expect_device": "gpu"},
                ],
                "vram": {"command": False},
            }
        }
    )
    bodies = {
        "http://127.0.0.1:8355/health": {"ok": True, "device": "cuda"},
        "http://192.0.2.10:8358/health": {"status": "healthy", "device": "gpu"},  # its own vocabulary
    }
    path = str(tmp_path / "contour.json")
    status = contour_poll.poll(settings, path, fetch=lambda url, timeout: bodies[url], clock=lambda: _T0)

    assert status["alerts"] == []
    # and it is recorded DISTINCTLY — None is "this document does not say", never False
    assert status["services"]["converter"]["ok"] is None
    assert status["services"]["converter"]["reachable"] is True
    assert status["history"]["converter"][-1]["ok"] is None
    # a non-boolean ok is the same answer: uninterpretable, not a diagnosis
    assert contour_poll.poll(
        settings, path, fetch=lambda url, timeout: {"ok": "yes", "device": "gpu"}, clock=lambda: _T0
    )["alerts"] == []


def test_the_status_file_carries_its_own_freshness_bound(tmp_path):
    # A reader cannot know this poller's cadence, so the bound travels WITH the file — it is what
    # lets the hook tell "the contour is fine" from "nobody has looked since Tuesday".
    settings = contour_poll.resolve_settings({"contour": {"services": ["http://a/health"], "vram": {"command": False}}})
    status = contour_poll.poll(
        settings, str(tmp_path / "c.json"), fetch=lambda url, timeout: {"ok": True}, clock=lambda: _T0
    )
    assert status["max_age"] == contour_poll.DEFAULT_MAX_AGE
    assert status["at"] == _T0.isoformat()
    tuned = contour_poll.resolve_settings({"contour": {"services": ["http://a/health"], "max_age": 60}})
    assert tuned["max_age"] == 60


def test_the_demotion_alert_fires_only_when_a_client_depends_on_the_fast_path():
    demoted = _sample(device="cpu")
    # expect_device set + a different actual device: THE alert of #40
    alerts = contour_poll.evaluate_service(_service(expect_device="gpu"), demoted, {})
    assert [a["kind"] for a in alerts] == ["device-demoted"]
    assert "cpu" in alerts[0]["message"] and "gpu" in alerts[0]["message"]
    # no expectation declared: recorded, never paged — the dependency is the operator's to declare
    assert contour_poll.evaluate_service(_service(), demoted, {}) == []
    # an expectation met is not an alert
    assert contour_poll.evaluate_service(_service(expect_device="cuda"), _sample(device="cuda"), {}) == []
    # a device the service did not report cannot demote
    assert contour_poll.evaluate_service(_service(expect_device="gpu"), _sample(device=None), {}) == []


def test_oom_overflows_page_on_the_rise_not_on_the_level():
    service = _service()
    # first sight of a non-zero counter: say it once
    assert [a["kind"] for a in contour_poll.evaluate_service(service, _sample(oom_overflows=2), {})] == ["oom-overflow"]
    # the same counter at the next poll: the condition is the RISE, not the number
    assert contour_poll.evaluate_service(service, _sample(oom_overflows=2), {"oom_overflows": 2}) == []
    # a rise pages again
    alerts = contour_poll.evaluate_service(service, _sample(oom_overflows=3), {"oom_overflows": 2})
    assert [a["kind"] for a in alerts] == ["oom-overflow"]
    # a counter of zero, or none reported, is not an alert
    assert contour_poll.evaluate_service(service, _sample(oom_overflows=0), {}) == []
    assert contour_poll.evaluate_service(service, _sample(oom_overflows=None), {}) == []


def test_free_vram_below_the_floor_pages():
    assert contour_poll.evaluate_vram(150, 200)[0]["kind"] == "vram-low"
    assert "150" in contour_poll.evaluate_vram(150, 200)[0]["message"]
    assert contour_poll.evaluate_vram(200, 200) == []
    assert contour_poll.evaluate_vram(None, 200) == []  # no GPU here: no VRAM alert, ever


# --- the status file: atomic write, tolerant read ----------------------------------------------------


def test_write_then_read_is_a_round_trip_and_leaves_no_temp_file(tmp_path):
    path = str(tmp_path / "contour.json")
    contour_poll.write_status(path, {"alerts": [], "at": "t"})
    assert contour_poll.read_status(path) == {"alerts": [], "at": "t"}
    assert [p.name for p in tmp_path.iterdir()] == ["contour.json"]


def test_a_replaced_file_never_reads_as_truncated(tmp_path):
    # the reader's view is the old content or the new — the write is a temp file plus os.replace
    path = str(tmp_path / "contour.json")
    contour_poll.write_status(path, {"v": 1})
    contour_poll.write_status(path, {"v": 2})
    assert contour_poll.read_status(path) == {"v": 2}


def test_read_status_tolerates_a_missing_or_corrupt_file(tmp_path):
    assert contour_poll.read_status(str(tmp_path / "absent.json")) == {}
    bad = tmp_path / "contour.json"
    bad.write_text("{not json", encoding="utf-8")
    assert contour_poll.read_status(str(bad)) == {}
    bad.write_text("[1, 2]", encoding="utf-8")
    assert contour_poll.read_status(str(bad)) == {}


# --- poll: one full pass ------------------------------------------------------------------------------


def _poll_settings(**contour):
    config = {"contour": contour} if contour else {}
    return contour_poll.resolve_settings(config)


def test_one_poll_writes_availability_alerts_history_and_the_split_p95(tmp_path):
    settings = _poll_settings(
        services=[
            {"name": "whisper", "health": "http://a/health", "expect_device": "cuda", "latency_fields": ["rtf"]},
            {"name": "rvc", "health": "http://b/health", "expect_device": "gpu", "latency_fields": ["rtf"]},
        ],
        vram={"command": False, "min_free_mib": 200},
    )

    bodies = {
        "http://a/health": {"ok": True, "device": "cuda", "rtf": 0.3},
        "http://b/health": {"ok": True, "device": "cpu", "oom_overflows": 2, "rtf": 4.1},
    }

    def fetch(url, timeout):
        return bodies[url]

    path = str(tmp_path / "contour.json")
    status = contour_poll.poll(settings, path, fetch=fetch, clock=lambda: _T0)

    assert [a["key"] for a in status["alerts"]] == ["device-demoted:rvc", "oom-overflow:rvc"]
    assert status["services"]["whisper"]["reachable"] is True
    assert status["p95"] == {"whisper": {"cuda": {"rtf": 0.3}}, "rvc": {"cpu": {"rtf": 4.1}}}
    assert len(status["history"]["rvc"]) == 1
    # and it is on disk, atomically, for the hook to read
    assert contour_poll.read_status(path)["alerts"] == status["alerts"]

    # a second poll over the same card: the oom counter held, so its alert does NOT refire —
    # the demotion (a persisting condition) still does
    second = contour_poll.poll(settings, path, fetch=fetch, clock=lambda: _T0)
    assert [a["key"] for a in second["alerts"]] == ["device-demoted:rvc"]
    assert len(second["history"]["rvc"]) == 2


def test_history_is_trimmed_to_the_configured_window(tmp_path):
    settings = _poll_settings(services=["http://a/health"], vram={"command": False}, history=3)
    name = settings["services"][0]["name"]
    previous = {
        "history": {name: [{"at": str(n), "ok": True, "device": "cuda", "fields": {}} for n in range(3)]},
    }
    path = str(tmp_path / "contour.json")
    contour_poll.write_status(path, previous)

    status = contour_poll.poll(settings, path, fetch=lambda url, timeout: {"ok": True, "device": "cuda"}, clock=lambda: _T0)
    assert len(status["history"][name]) == 3  # 3 kept + 1 new, trimmed back to 3
    assert status["history"][name][-1]["at"] == _T0.isoformat()


def test_a_corrupt_previous_status_costs_one_cycles_deltas_not_the_poll(tmp_path):
    path = str(tmp_path / "contour.json")
    Path(path).write_text("{corrupt", encoding="utf-8")
    settings = _poll_settings(services=["http://a/health"], vram={"command": False})
    status = contour_poll.poll(settings, path, fetch=lambda url, timeout: {"ok": True, "device": "cuda"})
    assert status["alerts"] == []
    assert contour_poll.read_status(path)["services"]  # and the file is whole again


def test_vram_sampling_uses_the_runner_seam_and_pages_below_the_floor(tmp_path):
    settings = _poll_settings(services=["http://a/health"], vram={"min_free_mib": 200})
    status = contour_poll.poll(
        settings,
        str(tmp_path / "contour.json"),
        fetch=lambda url, timeout: {"ok": True, "device": "cuda"},
        runner=lambda argv, timeout: _Done("150\n"),
    )
    assert [a["kind"] for a in status["alerts"]] == ["vram-low"]
    assert status["vram"] == {"free_mib": 150, "min_free_mib": 200}


# --- main: the exit-code contract ----------------------------------------------------------------------


def _config_file(tmp_path, monkeypatch, config) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(path))


def test_main_exit_0_when_quiet_1_when_paging_and_the_file_is_written(tmp_path, monkeypatch, capsys):
    _config_file(tmp_path, monkeypatch, {"contour": {"services": ["http://a/health"], "vram": {"command": False}}})
    monkeypatch.setattr(contour_poll, "fetch_health", lambda url, timeout: {"ok": True, "device": "cuda"})
    status_path = str(tmp_path / "contour.json")

    assert contour_poll.main(["--status", status_path]) == 0
    assert "contour ok — 1/1 services answered" in capsys.readouterr().out
    assert contour_poll.read_status(status_path)["services"]

    def demoted(url, timeout):
        return {"ok": True, "device": "cpu"}

    _config_file(
        tmp_path,
        monkeypatch,
        {"contour": {"services": [{"health": "http://a/health", "expect_device": "gpu"}], "vram": {"command": False}}},
    )
    monkeypatch.setattr(contour_poll, "fetch_health", demoted)
    assert contour_poll.main(["--status", status_path]) == 1
    assert "ALERT" in capsys.readouterr().out


def test_the_success_line_names_what_was_actually_polled(tmp_path, monkeypatch, capsys):
    """"contour ok" has to be a claim about the operator's contour, not about whatever this poller
    happened to fall back to — so the summary says when it is watching only the shipped default,
    and says when a service answered without publishing an ``ok`` it could read."""
    _config_file(tmp_path, monkeypatch, {})
    monkeypatch.setattr(contour_poll, "fetch_health", lambda url, timeout: {"status": "healthy"})
    monkeypatch.setattr(contour_poll, "sample_vram", lambda command, timeout, runner: None)
    assert contour_poll.main(["--status", str(tmp_path / "c.json")]) == 0
    out = capsys.readouterr().out
    assert "contour.services unset" in out and "1 publishing no ok field" in out


def test_main_json_prints_the_whole_status(tmp_path, monkeypatch, capsys):
    _config_file(tmp_path, monkeypatch, {"contour": {"services": ["http://a/health"], "vram": {"command": False}}})
    monkeypatch.setattr(contour_poll, "fetch_health", lambda url, timeout: {"ok": True, "device": "cuda"})
    assert contour_poll.main(["--json", "--status", str(tmp_path / "contour.json")]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["services"]["a"]["ok"] is True


def test_main_64_is_for_calling_it_wrong_never_a_diagnosis(tmp_path, monkeypatch, capsys):
    _config_file(tmp_path, monkeypatch, {})
    assert contour_poll.main(["--bogus"]) == 64
    assert contour_poll.main(["--status"]) == 64
    # a config that names services none of which is usable: a config error, not "contour ok"
    _config_file(tmp_path, monkeypatch, {"contour": {"services": ["not-a-url"]}})
    assert contour_poll.main(["--status", str(tmp_path / "c.json")]) == 64
    assert "no fetchable" in capsys.readouterr().err
    # …and an explicitly EMPTY list is a contour nobody watches, which is not "contour ok" either
    _config_file(tmp_path, monkeypatch, {"contour": {"services": []}})
    assert contour_poll.main(["--status", str(tmp_path / "c.json")]) == 64
    assert "no usable service" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("what", "config", "expected"),
    [
        ("a mistyped timeout", {"contour": {"timeout": "5s"}}, "contour.timeout"),
        ("a mistyped history", {"contour": {"history": "lots"}}, "contour.history"),
        ("a mistyped vram floor", {"contour": {"vram": {"min_free_mib": "200 MiB"}}}, "contour.vram.min_free_mib"),
        ("a services value that is not a list", {"contour": {"services": {"a": "http://a/h"}}}, "must be a list"),
        ("a host urllib cannot encode", {"contour": {"services": ["http://a..b/health"]}}, "no fetchable"),
        (
            "two entries with one name",
            {"contour": {"services": ["http://a:1/health", "http://a:1/healthz"]}},
            "two entries named",
        ),
    ],
)
def test_a_config_error_exits_64_never_1_and_still_writes_the_status_file(
    what, config, expected, tmp_path, monkeypatch, capsys
):
    """1 MEANS PAGE, and nothing else. Every one of these used to raise out of resolve_settings,
    hit Python's own handler, and exit 1 — a scheduler branching on the exit code was told an
    alert was active by a typo. And no status file was written, so the hook went on voicing (or
    not voicing) whatever the last successful poll had left behind."""
    status_path = str(tmp_path / "contour.json")
    contour_poll.write_status(status_path, {"at": _T0.isoformat(), "max_age": 900, "alerts": [], "history": {"a": [1]}})

    _config_file(tmp_path, monkeypatch, config)
    assert contour_poll.main(["--status", status_path]) == 64, what
    assert expected in capsys.readouterr().err

    # the status file is refreshed, not left stale: it now carries the poller's own failure, which
    # is what the hook voices instead of reading an "all quiet" nobody has re-checked
    written = contour_poll.read_status(status_path)
    assert [a["key"] for a in written["alerts"]] == ["poller-error"]
    assert written["at"] != _T0.isoformat()
    assert written["history"] == {"a": [1]}  # and the history it did have survives the failure


def test_a_config_that_is_not_readable_json_is_64_not_contour_ok_over_loopback(tmp_path, monkeypatch, capsys):
    """A trailing comma used to be swallowed whole: load_config returned {}, the services fell
    back to DEFAULT_SERVICES, and the poller printed 'contour ok — 1/1 services answered' while
    none of the operator's three services had been looked at."""
    path = tmp_path / "config.json"
    path.write_text('{"contour": {"services": ["http://a/health",]}}', encoding="utf-8")
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(path))

    def never(url, timeout):
        raise AssertionError("a config that will not parse must not be polled against defaults")

    monkeypatch.setattr(contour_poll, "fetch_health", never)
    status_path = str(tmp_path / "contour.json")
    assert contour_poll.main(["--status", status_path]) == 64
    out = capsys.readouterr()
    assert "not valid JSON" in out.err and "contour ok" not in out.out
    assert [a["key"] for a in contour_poll.read_status(status_path)["alerts"]] == ["poller-error"]

    # a config file that is not there at all is still the documented default install, not an error
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(tmp_path / "absent.json"))
    assert contour_poll.load_config(str(tmp_path / "absent.json")) == {}
    # …and one that holds something other than an object is a typo somebody has to be told about
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(contour_poll.ConfigError, match="must hold a JSON object"):
        contour_poll.load_config(str(path))


def test_an_unreadable_config_file_is_64_not_a_default(tmp_path, monkeypatch):
    unreadable = tmp_path / "config.json"
    unreadable.mkdir()  # a directory where a file belongs: open() raises OSError, not ValueError
    with pytest.raises(contour_poll.ConfigError, match="cannot read"):
        contour_poll.load_config(str(unreadable))


def test_a_poll_that_blows_up_internally_exits_64_not_the_page_code(tmp_path, monkeypatch, capsys):
    """The vocabulary is CLOSED: 1 is reachable from exactly one line, the alert count. Anything
    else — including a bug in this file — leaves as 64 with the failure in the status file."""
    _config_file(tmp_path, monkeypatch, {"contour": {"services": ["http://a/health"], "vram": {"command": False}}})

    def broken(settings, status_path, **seams):
        raise RuntimeError("the poll itself is broken")

    monkeypatch.setattr(contour_poll, "poll", broken)
    status_path = str(tmp_path / "contour.json")
    assert contour_poll.main(["--status", status_path]) == 64
    assert "RuntimeError" in capsys.readouterr().err
    assert [a["key"] for a in contour_poll.read_status(status_path)["alerts"]] == ["poller-error"]


def test_a_state_dir_that_will_not_write_costs_the_page_not_a_traceback(tmp_path, monkeypatch):
    # record_poller_error is the last thing standing between a broken poller and silence; when it
    # cannot write either, it must not raise on the way out of an already-failing run.
    def unwritable(path, exist_ok=False):
        raise OSError("read-only state dir")

    monkeypatch.setattr(contour_poll.os, "makedirs", unwritable)
    contour_poll.record_poller_error(str(tmp_path / "nowhere" / "contour.json"), "boom")  # no raise


def test_main_help_is_free_and_exit_0(capsys):
    assert contour_poll.main(["--help"]) == 0
    assert "contour-poll" in capsys.readouterr().out
