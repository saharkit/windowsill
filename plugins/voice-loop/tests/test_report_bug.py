"""The bug-report collector's contract: what may travel, and what may never.

Two of these tests are the feature, not decoration:

* ``test_a_healthy_run_carries_no_secret_and_no_spoken_word`` — the grep audit. A whole fake install
  is planted with keys, a username, a LAN host and real transcript text in every place the collector
  reads, and every one of those strings is then hunted in the rendered bundle. That is the acceptance
  criterion stated as code.
* ``test_log_rules_cover_every_log_call_in_the_scripts`` — the drift guard. ``LOG_RULES`` describes
  log lines written by two OTHER files in this plugin, so it can fall behind them silently. This test
  reads those files and fails the moment a log call appears that the table does not classify.

Everything here runs without network, models or audio hardware: the one HTTP call (``/health``) is
faked at the urllib seam, the one subprocess (``gh``) at the injected-runner seam, and every path the
collector reads is under tmp_path — never the live config or state dir.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import os
import re
import runpy
import stat
import subprocess
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location("report_bug", _SCRIPTS / "report_bug.py")
report_bug = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report_bug)


def clock_at(stamp: str = "2026-08-03T09:00:00+00:00"):
    moment = datetime.fromisoformat(stamp)
    return lambda: moment


def runner_returning(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """The injected subprocess seam: records the argv it was given, runs nothing."""
    calls: list[tuple[list[str], float]] = []

    def run(argv, timeout):
        calls.append((list(argv), timeout))
        return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)

    run.calls = calls
    return run


# --- the planted install ---------------------------------------------------------------------------

# Every one of these is a string a bug report must never carry. They are planted in the config, the
# environment, both logs and the health payload below, and hunted in the bundle afterwards.
SECRETS = {
    "openai_key": "sk-liveKEY0987654321abcdefghij",
    "eleven_key": "abcd1234efgh5678ijkl9012mnop3456",
    "gh_token": "ghp_0123456789abcdefghijklmnopqrstuv",
    "transcript_ru": "привет это секретное сообщение",
    "spoken_line": "Готово, я закончила работу",
    "dedup_line": "и это тоже говорили вслух",
    "server_echo": "boom while synthesizing тайный текст",
    "tool_stderr_transcript": "whisper output: тайная расшифровка",
    "lan_host": "192.168.7.31",
    "username": "vasilisa",
}


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A whole fake voice-loop install — config, state dir, both logs — with secrets planted in it."""
    home = tmp_path / "home" / SECRETS["username"]
    state = home / ".local/state/voice-loop"
    config_dir = home / ".config/voice-loop"
    state.mkdir(parents=True)
    config_dir.mkdir(parents=True)

    config = {
        "language": "ru",
        "stt": {
            "backend": "lan",
            "endpoint": f"http://{SECRETS['lan_host']}:8355",
            "cloud": {"api_key_env": "VOICE_LOOP_STT_API_KEY", "key_file": f"{config_dir}/openai.key"},
        },
        "tts": {
            "backend": "cloud",
            "endpoint": "http://127.0.0.1:8355",
            "cloud": {"provider": "elevenlabs", "api_key": SECRETS["eleven_key"], "voice_id": "v1"},
        },
        "speak": {"marker": "🔊", "player": "aplay -q"},
    }
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    (state / "dictate.log").write_text(
        "\n".join(
            [
                "2026-08-03T08:00:00 recording via pw-record pid=4242",
                f"2026-08-03T08:00:04 transcript: {SECRETS['transcript_ru']}",
                "2026-08-03T08:00:04 auto-pasted (mode=send key=ctrl+shift+v)",
                f"{SECRETS['tool_stderr_transcript']}",
                f"2026-08-03T08:00:09 stt unreachable: [Errno 111] Connection refused to {SECRETS['lan_host']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (state / "speak.log").write_text(
        "\n".join(
            [
                f"2026-08-03T08:01:00 text: {SECRETS['spoken_line']}",
                "2026-08-03T08:01:03 played rc=0 bytes=88200 chunks=3 via=stream",
                "2026-08-03T08:01:03 timings extract_ms=12 first_audio_ms=430 total_ms=2100",
                f'2026-08-03T08:01:05 stream refused (500): {{"detail": "{SECRETS["server_echo"]}"}}',
                f"2026-08-03T08:01:06 stop: dropped a read identical to the last spoken line (dedup): {SECRETS['dedup_line']}",
                f"2026-08-03T08:01:07 config ignored ({config_dir}/config.json): ValueError: nope",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (state / "last-spoken").write_text(SECRETS["spoken_line"], encoding="utf-8")
    (state / "spoken.ledger").write_text("a1b2c3d4e5f60718\n", encoding="utf-8")

    monkeypatch.setattr(report_bug, "_HOME", str(home))
    monkeypatch.setenv("USER", SECRETS["username"])
    monkeypatch.setenv("LOGNAME", SECRETS["username"])
    monkeypatch.setenv("USERNAME", SECRETS["username"])
    monkeypatch.setenv("VOICE_LOOP_TTS_API_KEY", SECRETS["openai_key"])
    monkeypatch.setenv("VOICE_LOOP_STT_MODEL", "small")
    monkeypatch.delenv("VOICE_LOOP_REPORT_MAILBOX", raising=False)
    monkeypatch.setattr(report_bug, "_STATE_DIR", str(state))
    monkeypatch.setattr(report_bug, "_CONFIG_PATH", str(config_dir / "config.json"))
    return {"home": home, "state": state, "config": config_dir / "config.json"}


@pytest.fixture
def offline(monkeypatch):
    """Every /health probe fails to connect — the collector must report that, not raise."""

    class DeadOpener:
        def open(self, request, timeout=None):
            raise urllib.error.URLError("no route")

    monkeypatch.setattr(report_bug.urllib.request, "build_opener", lambda *handlers: DeadOpener())


def bundle_for(install, **kwargs) -> dict:
    return report_bug.build_bundle(
        state_dir=str(install["state"]),
        config_path=str(install["config"]),
        clock=clock_at(),
        **kwargs,
    )


# --- the acceptance test: zero secrets, zero spoken words -----------------------------------------


def test_a_healthy_run_carries_no_secret_and_no_spoken_word(install, offline):
    digest = report_bug.render_digest(bundle_for(install, summary="dictation pastes nothing"))

    for name, planted in SECRETS.items():
        if name == "username":
            continue  # asserted separately: it must be gone as a WORD, and the home path with it
        assert planted not in digest, f"{name} survived into the bundle"
    assert not re.search(rf"\b{SECRETS['username']}\b", digest)
    assert str(install["home"]) not in digest

    # And the report is still a report: the events survived their payloads.
    assert "transcript: <redacted 30 chars>" in digest
    assert "played rc=0 bytes=88200 chunks=3 via=stream" in digest
    assert "dictation pastes nothing" in digest
    assert "<redacted-key>" in digest


def test_the_bundle_never_reads_last_spoken(install, offline):
    """`last-spoken` holds the previous utterance verbatim: it may contribute a size and nothing else."""
    bundle = bundle_for(install)
    row = next(r for r in bundle["state"] if r["name"] == "last-spoken")
    assert row["present"] and row["bytes"] == len(SECRETS["spoken_line"].encode("utf-8"))
    assert SECRETS["spoken_line"] not in json.dumps(bundle, ensure_ascii=False)


def test_secrets_named_by_their_config_key_are_dropped_but_pointers_are_kept(install, offline):
    config = bundle_for(install)["config"]["config"]
    assert config["tts"]["cloud"]["api_key"] == "<redacted-key>"
    # the pointers to a secret are not the secret, and a report without them cannot diagnose "no key"
    assert config["stt"]["cloud"]["api_key_env"] == "VOICE_LOOP_STT_API_KEY"
    # The pointer survives redacted: home collapsed to ~, the file name still readable. The
    # separator spelling is the platform's and is deliberately not asserted.
    key_file = config["stt"]["cloud"]["key_file"]
    assert key_file.startswith("~") and key_file.endswith("openai.key")
    assert str(install["home"]) not in key_file


def test_environment_reports_a_credential_variable_as_set_never_as_its_value(install, offline):
    environment = bundle_for(install)["environment"]
    assert environment["VOICE_LOOP_TTS_API_KEY"] == "<set>"
    assert environment["VOICE_LOOP_STT_MODEL"] == "small"


def test_unreachable_endpoints_are_a_reported_fact_not_an_exception(install, offline):
    servers = bundle_for(install)["servers"]
    assert [s["reachable"] for s in servers] == [False, False]
    assert servers[0]["endpoint"] == "http://<host>:8355/health"  # the LAN host is not in the report
    assert servers[1]["endpoint"] == "http://127.0.0.1:8355/health"  # loopback IS the fact worth having


def test_health_payload_travels_when_the_server_answers(install, monkeypatch):
    class Response:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Opener:
        def open(self, request, timeout=None):
            return Response(json.dumps({"ok": True, "version": "0.3.2", "cuda": False}).encode())

    monkeypatch.setattr(report_bug.urllib.request, "build_opener", lambda *handlers: Opener())
    servers = bundle_for(install)["servers"]
    assert servers[0]["health"]["version"] == "0.3.2"


# --- redaction ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "planted",
    [
        "sk-liveKEY0987654321abcdefghij",
        "ghp_0123456789abcdefghijklmnopqrstuv",
        "xoxb-1234567890-abcdefghij",
        "AIzaSyA0123456789abcdefghijklmnopqrstuv",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.signature",
        "abcd1234efgh5678ijkl9012mnop3456",
    ],
)
def test_key_shapes_are_redacted_wherever_they_appear(planted):
    assert planted not in report_bug.redact(f"something went wrong with {planted} today")


def test_labelled_credentials_are_redacted_even_in_an_unfamiliar_shape():
    assert report_bug.redact("Authorization: hunter2") == "Authorization: <redacted-key>"
    assert report_bug.redact('password="hunter2"') == 'password="<redacted-key>"'


def test_home_and_other_users_homes_collapse(monkeypatch):
    monkeypatch.setattr(report_bug, "_HOME", "/home/vasilisa")
    monkeypatch.setenv("USER", "vasilisa")
    monkeypatch.setenv("LOGNAME", "vasilisa")
    assert report_bug.redact("/home/vasilisa/.config/voice-loop") == "~/.config/voice-loop"
    assert report_bug.redact("/Users/someoneelse/Library") == "/Users/<user>/Library"
    assert report_bug.redact("owned by vasilisa") == "owned by <user>"


def test_a_short_login_is_left_alone(monkeypatch):
    """Rewriting a two-letter login everywhere would mangle the diagnostics, and names nobody."""
    monkeypatch.setattr(report_bug, "_HOME", "/home/ex")
    monkeypatch.setenv("USER", "ex")
    monkeypatch.setenv("LOGNAME", "ex")
    assert "extract_ms" in report_bug.redact("timings extract_ms=12 total_ms=90")


def test_usernames_returns_only_the_fixture_set(monkeypatch):
    """The fixture must pin every name source so `usernames()` is deterministic on any host."""
    monkeypatch.setattr(report_bug, "_HOME", "/home/vasilisa")
    monkeypatch.setenv("USER", "vasilisa")
    monkeypatch.setenv("LOGNAME", "vasilisa")
    monkeypatch.setenv("USERNAME", "vasilisa")
    # Also mock getpass.getuser to ensure determinism
    monkeypatch.setattr(report_bug.getpass, "getuser", lambda: "vasilisa")
    assert report_bug.usernames() == ("vasilisa",)


# --- the host grammar -------------------------------------------------------------------------------

# One class, because these are one decision made in one place: what a host is, and which of them is
# the loopback a report may keep. Four review rounds patched two divergent grammars against each
# other; each test below pins one thing that divergence cost.


class TestHostRedaction:
    """`redact` over hosts: with a scheme, bare in prose, loopback, and the tokens that are neither."""

    def test_a_hosts_name_leaves_a_url_while_its_scheme_port_and_path_stay(self):
        assert report_bug.redact("http://voicebox.lan:8355/tts") == "http://<host>:8355/tts"
        assert report_bug.redact("https://api.elevenlabs.io/v1") == "https://<host>/v1"
        assert report_bug.redact("ws://voicebox.lan:8355/socket") == "ws://<host>:8355/socket"
        assert report_bug.redact("wss://secure.example.com:443/socket") == "wss://<host>:443/socket"

    def test_loopback_survives_a_url_because_it_names_nobody(self):
        assert report_bug.redact("http://127.0.0.1:8355/tts") == "http://127.0.0.1:8355/tts"
        assert report_bug.redact("http://localhost:8355/tts") == "http://localhost:8355/tts"

    def test_a_bare_address_in_a_log_line_is_a_host_too(self):
        """urllib reasons name the address with no scheme in front of it — the URL rule never sees it."""
        assert report_bug.redact("[Errno 111] Connection refused to 192.168.7.31") == (
            "[Errno 111] Connection refused to <host>"
        )
        assert report_bug.redact("bound to 127.0.0.1 as always") == "bound to 127.0.0.1 as always"
        assert report_bug.redact("plugin 0.3.2 python 3.10.12") == "plugin 0.3.2 python 3.10.12"

    def test_a_bare_dotted_hostname_with_a_port_is_redacted(self):
        """A private name in a failure reason is exactly what must not reach a public issue."""
        assert report_bug.redact("Connection refused to deepgram.corp.internal:443") == (
            "Connection refused to <host>:443"
        )
        assert report_bug.redact("TLS failed for deepgram.corp.internal:443") == (
            "TLS failed for <host>:443"
        )
        assert report_bug.redact("Connection refused to voicebox.lan:8355") == (
            "Connection refused to <host>:8355"
        )

    def test_a_private_ipv4_address_is_never_shown_with_or_without_a_port(self):
        """The dotted quad is redacted in both spellings — no branch anywhere shows the IP."""
        assert report_bug.redact("Connection refused to 192.168.7.31:8355") == (
            "Connection refused to <host>:8355"
        )
        assert report_bug.redact("Connection at 10.0.0.1:443") == "Connection at <host>:443"
        assert report_bug.redact("Connection refused to 192.168.7.31") == (
            "Connection refused to <host>"
        )

    def test_loopback_survives_the_bare_rule_in_every_spelling(self):
        assert report_bug.redact("Connection refused to 127.0.0.1:8355") == (
            "Connection refused to 127.0.0.1:8355"
        )
        assert report_bug.redact("Connection refused to localhost:8355") == (
            "Connection refused to localhost:8355"
        )
        assert report_bug.redact("Connection refused to [::1]:8080") == (
            "Connection refused to [::1]:8080"
        )
        assert report_bug.redact("bound to ::1 as always") == "bound to ::1 as always"

    def test_a_bracketed_ipv6_endpoint_is_redacted(self):
        assert report_bug.redact("Connection refused to [fd00::1234]:443") == (
            "Connection refused to <host>:443"
        )
        assert report_bug.redact("TLS failed for [2001:db8::1]:443") == "TLS failed for <host>:443"
        assert report_bug.redact("Connection refused to [fe80::1]:8080") == (
            "Connection refused to <host>:8080"
        )

    def test_a_bracketed_ipv6_with_no_port_is_still_a_host(self):
        """The port is optional for an IP literal: `[fd00::1234]` names the network on its own."""
        assert report_bug.redact("Connection refused to [fd00::1234]") == (
            "Connection refused to <host>"
        )
        assert report_bug.redact("http://[fd00::1234]/tts") == "http://<host>/tts"

    def test_an_unbracketed_ipv6_in_prose_is_a_host(self):
        """A reason string has no brackets to offer: "Connection refused to fd00::1234"."""
        assert report_bug.redact("Connection refused to fd00::1234") == (
            "Connection refused to <host>"
        )
        assert report_bug.redact("bound to fd00::1%eth0") == "bound to <host>"
        assert report_bug.redact("stt unreachable: 2001:db8::8a2e:370:7334") == (
            "stt unreachable: <host>"
        )

    def test_a_zone_id_may_carry_dots_and_underscores(self):
        """A zone id is an interface NAME — `eth0.100` and `br_lan` are ordinary ones."""
        assert report_bug.redact("Connection refused to [fd00::1%eth0_0]:8443") == (
            "Connection refused to <host>:8443"
        )
        assert report_bug.redact("Connection refused to [fd00::1%eth0.100]:8443") == (
            "Connection refused to <host>:8443"
        )
        assert report_bug.redact("bound to fe80::1%br_lan") == "bound to <host>"

    def test_a_zone_id_does_not_hide_that_an_address_is_loopback(self):
        """A zone id names the interface, not the network — loopback stays visible with one on."""
        assert report_bug.redact("http://[::1%eth0]:8355/tts") == "http://[::1%eth0]:8355/tts"
        assert report_bug.redact("Connection refused to [::1%lo]:443") == (
            "Connection refused to [::1%lo]:443"
        )
        assert report_bug.redact("bound to ::1%eth0") == "bound to ::1%eth0"

    def test_a_zone_id_does_not_launder_a_private_address_past_the_redactor(self):
        assert report_bug.redact("Connection refused to [fe80::1%eth0]:8080") == (
            "Connection refused to <host>:8080"
        )
        assert report_bug.redact("http://[fe80::1%eth0]:8080/tts") == "http://<host>:8080/tts"

    def test_the_expanded_ipv6_loopback_is_preserved_by_both_passes(self):
        """`0:0:0:0:0:0:0:1` is `::1` written out. The URL pass kept it and the bare pass then
        redacted it, because each carried its own idea of loopback."""
        assert report_bug.redact("http://[0:0:0:0:0:0:0:1]:8355/tts") == (
            "http://[0:0:0:0:0:0:0:1]:8355/tts"
        )
        assert report_bug.redact("Connection refused to [0:0:0:0:0:0:0:1]:443") == (
            "Connection refused to [0:0:0:0:0:0:0:1]:443"
        )
        assert report_bug.redact("bound to 0:0:0:0:0:0:0:1") == "bound to 0:0:0:0:0:0:0:1"
        assert report_bug.redact("bound to 0:0:0:0:0:0:0:1%lo") == "bound to 0:0:0:0:0:0:0:1%lo"
        assert report_bug.redact("Connection refused to [0:0:0:0:0:0:0:1%eth0]:443") == (
            "Connection refused to [0:0:0:0:0:0:0:1%eth0]:443"
        )

    def test_one_loopback_set_answers_for_every_pass(self, monkeypatch):
        """The set is the single source: a spelling added to it is loopback EVERYWHERE at once.

        A rule that kept its own private tuple would ignore this and redact the planted spelling.
        """
        monkeypatch.setattr(
            report_bug, "_LOOPBACK_HOSTS", frozenset(report_bug._LOOPBACK_HOSTS | {"fc00::dead"})
        )
        assert report_bug.redact("http://[fc00::dead]:8355/tts") == "http://[fc00::dead]:8355/tts"
        assert report_bug.redact("Connection refused to [fc00::dead]:443") == (
            "Connection refused to [fc00::dead]:443"
        )
        assert report_bug.redact("bound to fc00::dead%eth0") == "bound to fc00::dead%eth0"

    def test_the_loopback_set_carries_no_bracketed_spelling(self):
        """Brackets are punctuation the predicate strips, so `[::1]` must not be an entry — and the
        preservation of `[::1]:8080` above must therefore rest on the normalisation, not on a
        duplicate entry that happens to match."""
        assert "[::1]" not in report_bug._LOOPBACK_HOSTS
        assert report_bug._is_loopback("[::1]")
        assert report_bug._is_loopback("[::1%lo]")
        assert report_bug._is_loopback("0:0:0:0:0:0:0:1")
        assert not report_bug._is_loopback("[fd00::1]")

    def test_source_locations_and_key_value_tokens_are_not_hosts(self):
        """The bare rule runs over log prose. These are the report's diagnostic value; it may not
        eat them to look thorough."""
        assert report_bug.redact("voice_loop/session.py:117: warning") == (
            "voice_loop/session.py:117: warning"
        )
        assert report_bug.redact("File app.py:42") == "File app.py:42"
        assert report_bug.redact("websocket closed code:1006") == "websocket closed code:1006"
        assert report_bug.redact("worker pid:4321 task:42") == "worker pid:4321 task:42"
        assert report_bug.redact("config ignored (~/.config/voice-loop/config.json): nope") == (
            "config ignored (~/.config/voice-loop/config.json): nope"
        )
        assert report_bug.redact("dictate.debounce_ms is not a usable number") == (
            "dictate.debounce_ms is not a usable number"
        )

    def test_timestamps_and_clock_times_are_not_hosts(self):
        assert report_bug.redact("2026-08-07T05:17:33 stt unreachable") == (
            "2026-08-07T05:17:33 stt unreachable"
        )
        assert report_bug.redact("Log started at 05:17") == "Log started at 05:17"
        assert report_bug.redact("Meeting at 09:30") == "Meeting at 09:30"
        assert report_bug.redact("Start time: 12:00") == "Start time: 12:00"
        assert report_bug.redact("14:30:45") == "14:30:45"

    def test_a_known_single_label_host_is_redacted(self):
        """`voicebox` is in the known set — a dotless LAN service name, caught and redacted.
        `code:1006` and `pid:4321` are not in the set, so they survive (pinned below)."""
        assert report_bug.redact("Connection refused to voicebox:8355") == (
            "Connection refused to <host>:8355"
        )

    def test_an_unknown_single_label_with_a_port_is_left_alone(self):
        """A `word:digits` token whose label is not in the known set is left alone — these are
        the report's diagnostic value (close codes, pids, task ids, config keys)."""
        assert report_bug.redact("websocket closed code:1006") == "websocket closed code:1006"
        assert report_bug.redact("worker pid:4321 task:42") == "worker pid:4321 task:42"
        assert report_bug.redact("item1:8080 is ready") == "item1:8080 is ready"

    def test_every_single_label_loopback_name_matches_the_bare_regex(self):
        """A loopback name added to `_LOOPBACK_HOSTS` must be matched by the bare-host regex
        so it is recognised as a host. The regex is built from the loopback set by construction —
        this test pins that the derivation holds after any edit."""
        for name in report_bug._LOOPBACK_NAMES:
            assert report_bug._BARE_HOST_PORT_RE.search(f"Connection refused to {name}:8355"), (
                f"{name!r} is in _LOOPBACK_HOSTS but _BARE_HOST_PORT_RE does not match it"
            )

    def test_the_port_grammar_is_the_same_for_every_host_shape(self):
        """One port fragment, so a one-digit port is not a host on one shape and prose on another."""
        assert report_bug.redact("Connection refused to deepgram.corp.internal:8") == (
            "Connection refused to <host>:8"
        )
        assert report_bug.redact("Connection refused to [fd00::1234]:8") == (
            "Connection refused to <host>:8"
        )

    def test_inline_credentials_leave_with_the_host_they_name(self):
        """`user:pass@host` is a password in a URL; the host rule owns it, in all spellings."""
        assert report_bug.redact("http://user:pw@voicebox.lan:8355/tts") == (
            "http://<user>@<host>:8355/tts"
        )
        assert report_bug.redact("Connection refused to user:pw@deepgram.corp.internal:443") == (
            "Connection refused to <user>@<host>:443"
        )
        # Known single-label hosts carry credentials too.
        assert report_bug.redact("Connection refused to user:pw@voicebox:8355") == (
            "Connection refused to <user>@<host>:8355"
        )


# --- round-6 residue (#127): edge host spellings the rebuilt grammar left out ----------------------
#
# Each test below pins ONE edge spelling the host grammar missed before #127 (the non-blocking
# basket from PR #121's round-6 review: no realistic log line was shown to leak, but the grammar's
# own shape left these out). The fail-closed allowlist flip recorded on #121 is the standing
# disposition if any of these ever grows a real leak repro; these are the narrower grammar fixes.


class TestRedactionRoundSixResidue:
    """#127: one edge host spelling per test, each closing a gap the rebuilt grammar left open."""

    def test_a_dotted_host_separated_from_its_port_by_whitespace_is_still_a_host(self):
        # F1: the port "spelled other than :port" — whitespace on either side of the colon. Without
        # the tolerance a private name plus its port in pasted prose or a looser log format leaks.
        assert report_bug.redact("refused to deepgram.corp.internal :443") == "refused to <host>:443"
        assert report_bug.redact("refused to deepgram.corp.internal: 443") == "refused to <host>:443"

    def test_a_fully_qualified_dotted_host_with_a_root_dot_is_still_a_host(self):
        # F1: `host.` (a trailing dot is the DNS root label, as `dig`/`nslookup` print it) then a
        # port — the trailing dot used to break the `name:port` join and the host leaked.
        assert report_bug.redact("refused to deepgram.corp.internal.:443") == "refused to <host>:443"

    def test_a_unicode_idn_host_is_redacted(self):
        # F2/F8: a host typed in its native script is invisible to an ASCII-only label charset, so
        # the most identifying form of a private host survived verbatim.
        assert report_bug.redact("refused to café.example.com:443") == "refused to <host>:443"
        assert report_bug.redact("refused to тест.рф:443") == "refused to <host>:443"

    def test_a_punycode_host_is_redacted(self):
        # F2/F8: the ASCII punycode form is the same host — pinning both spellings keeps a future
        # charset change from fixing one and re-breaking the other.
        assert report_bug.redact("refused to xn--80ak6aa92e.com:443") == "refused to <host>:443"

    def test_a_subdomained_host_whose_suffix_is_also_a_tld_is_redacted(self):
        # F3/F9: `api.evil.py:443` is a host on the .py TLD (Paraguay), not a Python file. Two or
        # more labels before a file-suffix-colliding TLD is a domain; only one label stays ambiguous.
        assert report_bug.redact("refused to api.evil.py:443") == "refused to <host>:443"
        assert report_bug.redact("refused to relay.corp.sh:443") == "refused to <host>:443"

    def test_a_single_label_file_shaped_token_with_a_port_stays_ambiguous_and_kept(self):
        # F3 boundary: `evil.py:443` is shape-identical to the source location `app.py:42`. No shape
        # rule can tell them apart, so the single-label form is kept (the safe direction) — this
        # pins that the wider suffix rule did NOT reach back and start eating real source locations.
        assert report_bug.redact("refused to evil.py:443") == "refused to evil.py:443"
        assert report_bug.redact("File app.py:42") == "File app.py:42"

    def test_a_label_then_a_bare_ipv6_is_a_host(self):
        # F4/F10: the bare-IPv6 lookbehind treated a preceding colon as part of a longer address, so
        # `gateway:fd00::1234` (a config-style label:address spelling) was never examined.
        assert report_bug.redact("default route via gateway:fd00::1234") == (
            "default route via gateway:<host>"
        )

    def test_a_bare_ipv6_does_not_swallow_a_clock_after_a_label_colon(self):
        # F4 two-way falsification: the loosened lookbehind lets `gateway:` through, but it must not
        # then turn a clock tail into a host — `12:34:56` is three groups with no `::`, so the IPv6
        # grammar still refuses it even though the colon in front is now allowed.
        assert report_bug.redact("ping at time:12:34:56 elapsed") == "ping at time:12:34:56 elapsed"
        assert report_bug.redact("mac 00:1a:2b:3c:4d:5e seen") == "mac 00:1a:2b:3c:4d:5e seen"

    def test_a_full_form_ipv6_with_an_ipv4_tail_is_redacted_whole(self):
        # F5: `0:0:0:0:0:ffff:10.0.0.1` — the IPv4 rule caught the dotted quad but left the six hex
        # groups in front of it standing, so the address prefix leaked even as its tail did not.
        assert report_bug.redact("refused to 0:0:0:0:0:ffff:10.0.0.1") == "refused to <host>"
        assert report_bug.redact("refused to 1:2:3:4:5:6:10.0.0.1") == "refused to <host>"

    def test_an_uppercase_scheme_url_is_redacted(self):
        # F6/F11: the scheme alternation was case-sensitive lowercase, so `HTTP://host` bypassed the
        # URL pass and (with no port to rescue it through the bare pass) the host leaked outright.
        assert report_bug.redact("HTTP://deepgram.corp.internal:443") == "HTTP://<host>:443"
        assert report_bug.redact("HTTPS://api.example.com/v1") == "HTTPS://<host>/v1"

    def test_a_non_http_scheme_url_is_redacted_with_its_credentials(self):
        # F6: `ftp://`, `ssh://`, `postgres://` and the like bypassed the URL pass, leaking the
        # scheme and — worse — the `user:pass@` credentials riding in front of the host.
        assert report_bug.redact("ftp://user:pw@deepgram.corp.internal:21") == (
            "ftp://<user>@<host>:21"
        )
        assert report_bug.redact("postgres://user:pw@db.example.com:5432/x") == (
            "postgres://<user>@<host>:5432/x"
        )

    def test_looks_like_a_file_is_exactly_one_label_then_a_suffix(self):
        # F7/F9 unit (the decision lives here, so it is pinned here, once): the predicate keeps a
        # source file (`name.ext`) and lets through both a bare loopback and a subdomained host.
        assert report_bug._looks_like_a_file("session.py") is True
        assert report_bug._looks_like_a_file("api.evil.py") is False
        assert report_bug._looks_like_a_file("localhost") is False


# --- the log vocabulary ------------------------------------------------------------------------------


def test_speech_bearing_lines_keep_their_event_and_lose_their_words():
    assert report_bug.scrub_message("transcript: привет мир") == ("transcript: <redacted 10 chars>", True)
    assert report_bug.scrub_message("text: Готово") == ("text: <redacted 6 chars>", True)
    assert report_bug.scrub_message('stream refused (500): {"detail": "x"}') == (
        "stream refused (500): <redacted 15 chars>",
        True,
    )
    assert report_bug.scrub_message("stream error after 3 chunk(s): boom") == (
        "stream error after 3 chunk(s): <redacted 4 chars>",
        True,
    )


def test_the_contour_page_travels_as_its_count_and_never_as_the_operators_service_names():
    """The drift guard proves every log call HAS a row; it cannot prove the row's cut marker still
    matches the line. This one does, against the real format string: the count is metadata a
    maintainer needs, and everything from "alert(s): " on is alert text — which is built out of the
    operator's own service names, i.e. the host:port of machines that are not in this repository.
    A cut marker that no longer occurs in the line would send the whole tail through untouched.
    """
    line = "contour: voicing 2 alert(s): Voice contour: rvc-gpu-01.internal:8358 is serving on cpu, expected gpu"
    scrubbed, classified = report_bug.scrub_message(line)
    assert classified is True
    assert scrubbed.startswith("contour: voicing 2 alert(s): ")
    assert "rvc-gpu-01" not in scrubbed and "8358" not in scrubbed
    assert scrubbed.endswith(" chars>")
    # the two contour lines that are metadata whole, and the dedup's own marker among them
    for message in (
        "contour: another firing is speaking — alert left unannounced for the next firing",
        "contour: the page reached no player — left unannounced, to be retried",
        "contour: already announced — nothing to voice (1 alert(s) still active)",
    ):
        assert report_bug.scrub_message(message) == (message, True)


def test_metadata_lines_travel_whole():
    for message in (
        "played rc=0 bytes=88200 chunks=3 via=stream",
        "timings extract_ms=12 first_audio_ms=430 total_ms=2100",
        "clip too short (1200 bytes ≈ 0.04s) — stt skipped",
        "no recorder available",
    ):
        assert report_bug.scrub_message(message) == (message, True)


def test_an_unknown_line_is_cut_not_trusted():
    scrubbed, classified = report_bug.scrub_message("brand new event: с текстом внутри")
    assert scrubbed == "brand new event: <redacted 16 chars>"
    assert classified is False


def test_an_unknown_line_without_a_delimiter_travels_as_its_size_alone():
    scrubbed, classified = report_bug.scrub_message("spoke привет мир")
    assert scrubbed == "<redacted 16 chars>"
    assert classified is False


def test_a_blank_line_stays_blank():
    assert report_bug.scrub_log_line("   ") == ("", True)


def test_third_party_stderr_is_withheld_whole():
    """A whisper.cpp stt.command prints its transcript to stderr, and stderr is appended to the log."""
    scrubbed, classified = report_bug.scrub_log_line("whisper output: тайная расшифровка")
    assert scrubbed == "<tool output, 34 chars, withheld>"
    assert classified is True


def test_log_rules_cover_every_log_call_in_the_scripts():
    """The drift guard: LOG_RULES describes lines written by two other files in this plugin.

    Both scripts write `log(f"...")` / `log("...")`. The literal head of each format string — up to
    its first interpolation — is the row id in LOG_RULES. A log call this table does not know would
    be redacted at runtime (safe), but its diagnostics would be lost silently, which is why the miss
    is a failing test rather than a quiet degradation.
    """
    call = re.compile(r"""\blog\(\s*f?(['"])(.+?)\1""", re.S)
    known = {prefix for prefix, _ in report_bug.LOG_RULES}
    unclassified: list[str] = []
    matched: set[str] = set()
    # A _NoLock reason reaches log() as a variable, so the regex above cannot see its literal.
    # Collected separately so the table still covers every string that can be written.
    reason = re.compile(r"""_NoLock\(\s*reason=f?(['"])(.+?)\1""", re.S)
    for name in ("speak.py", "dictate.py"):
        source = (_SCRIPTS / name).read_text(encoding="utf-8")
        for pattern in (call, reason):
            for _quote, literal in pattern.findall(source):
                head = literal.split("{")[0]
                rows = [prefix for prefix in known if head.startswith(prefix)]
                if rows:
                    matched.add(max(rows, key=len))
                else:
                    unclassified.append(f"{name}: {literal[:60]}")
    assert not unclassified, "log calls with no LOG_RULES row: " + "; ".join(unclassified)
    # …and the other direction: a row nothing writes any more is a row nobody will notice is wrong.
    assert not known - matched, "LOG_RULES rows no script writes: " + "; ".join(sorted(known - matched))


def test_the_rule_table_refuses_to_be_ambiguous(monkeypatch):
    for broken in (
        (("", None),),
        (("a: ", None), ("a: ", "a: ")),
        (("stop: ", None), ("stop: nothing new", None)),
        (("x: ", ""),),
    ):
        monkeypatch.setattr(report_bug, "LOG_RULES", broken)
        with pytest.raises(ValueError):
            report_bug._validate_log_rules()


def test_jobs_are_the_recent_outcomes_and_nothing_else(install, offline):
    jobs = bundle_for(install)["jobs"]
    assert any(job.endswith("played rc=0 bytes=88200 chunks=3 via=stream") for job in jobs)
    assert any(job.endswith("transcript: <redacted 30 chars>") for job in jobs)
    assert not any("stt unreachable" in job for job in jobs)  # a failure to connect is not a job state


def test_a_missing_log_is_a_verified_missing_source(tmp_path):
    tail = report_bug.read_log_tail(str(tmp_path / "nope.log"))
    assert tail == {"present": False, "status": "missing", "lines": [], "unclassified": 0}


def test_the_tail_is_the_last_n_lines(tmp_path):
    path = tmp_path / "speak.log"
    path.write_text("".join(f"2026-08-03T08:00:{i:02d} played rc=0 bytes={i} chunks=1 via=blob\n" for i in range(30)))
    tail = report_bug.read_log_tail(str(path), lines=5)
    assert tail["total_lines"] == 30 and len(tail["lines"]) == 5
    assert tail["lines"][-1].endswith("bytes=29 chunks=1 via=blob")


# --- rendering and the bundle file ---------------------------------------------------------------------


def test_a_fence_inside_collected_text_cannot_break_out_of_its_block(install, offline, tmp_path):
    (install["state"] / "speak.log").write_text(
        "2026-08-03T08:00:00 player failed: ```\n## not a heading\n```\n", encoding="utf-8"
    )
    digest = report_bug.render_digest(bundle_for(install))
    assert "```\n## not a heading" not in digest
    assert "'''" in digest


def test_render_is_deterministic(install, offline):
    assert report_bug.render_digest(bundle_for(install)) == report_bug.render_digest(bundle_for(install))


def test_an_unclassified_line_is_announced_in_the_bundle(install, offline):
    (install["state"] / "speak.log").write_text("2026-08-03T08:00:00 brand new: text\n", encoding="utf-8")
    assert "were not recognised by this collector's table" in report_bug.render_digest(bundle_for(install))


def test_the_bundle_file_is_written_atomically_and_privately(tmp_path):
    path = tmp_path / "nested" / "bug-report.md"
    report_bug.write_bundle(str(path), "hello\n")
    assert path.read_text() == "hello\n"
    assert [p.name for p in path.parent.iterdir()] == ["bug-report.md"]  # no temp file left behind
    # mkstemp is the privacy mechanism (write_bundle adds no chmod of its own): 0o600 is the
    # POSIX creation mode. Windows has no group/other bits to read here — its boundary is the
    # ACL on the user's own directories — so the mode assertion is a POSIX-only fact.
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(report_bug.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        report_bug.write_bundle(str(tmp_path / "bug-report.md"), "hello\n")
    assert list(tmp_path.iterdir()) == []


def test_collect_prints_byte_exactly_what_it_wrote(install, offline, tmp_path, capsys):
    out = tmp_path / "bundle.md"
    assert report_bug.main(["collect", "--out", str(out), "--summary", "no sound"]) == 0
    captured = capsys.readouterr()
    assert captured.out == out.read_text(encoding="utf-8")
    assert str(out) in captured.err  # where it landed goes to stderr, so stdout stays the bundle


def test_the_default_bundle_path_is_timestamped_in_the_state_dir():
    # Path-to-Path: the separator between the state dir and the file name is the platform's,
    # and a str-to-str compare would pin this test to the POSIX spelling of it.
    path = report_bug.default_bundle_path("/state", clock_at())
    assert Path(path) == Path("/state") / "bug-report-20260803T090000Z.md"


# --- the three transports --------------------------------------------------------------------------------


def test_gh_is_unavailable_without_the_cli(monkeypatch):
    monkeypatch.setattr(report_bug.shutil, "which", lambda name: None)
    ready, why = report_bug.gh_ready(runner_returning())
    assert ready is False and "not installed" in why


def test_gh_is_unavailable_when_it_is_not_authenticated(monkeypatch):
    monkeypatch.setattr(report_bug.shutil, "which", lambda name: "/usr/bin/gh")
    ready, why = report_bug.gh_ready(runner_returning(returncode=1))
    assert ready is False and "not authenticated" in why


def test_gh_is_available_when_authenticated(monkeypatch):
    monkeypatch.setattr(report_bug.shutil, "which", lambda name: "/usr/bin/gh")
    runner = runner_returning()
    assert report_bug.gh_ready(runner)[0] is True
    argv, timeout = runner.calls[0]
    assert argv == ["gh", "auth", "status"] and timeout > 0  # argv list, never a shell string


def test_artifact_redacts_title_and_rejects_unknown_bundle_fields(install, offline):
    """L2 gap: catches a new outbound field bypassing the single redaction/schema seam."""
    bundle = bundle_for(install)
    artifact = report_bug.make_artifact("account vasilisa at /home/vasilisa", bundle)
    assert "vasilisa" not in artifact.title
    with pytest.raises(ValueError, match="unknown field"):
        report_bug.make_artifact("t", {**bundle, "new_field": "must not travel"})


def test_transport_artifact_body_matches_the_bytes_it_sends(tmp_path):
    """L2 gap: catches consent drift when a transport reads a different body than displayed."""
    path = tmp_path / "bundle.md"
    # CRLF on purpose: the reader must not re-normalise the file's bytes or the byte check below fails.
    path.write_bytes(b"displayed bytes\r\n")
    artifact = report_bug._load_artifact("private /home/vasilisa", str(path))
    runner = runner_returning(stdout="https://github.com/saharkit/windowsill/issues/99\n")
    ok, message = report_bug.create_issue(artifact, str(path), runner=runner)
    assert ok and message.endswith("/99")
    assert runner.calls[0][0][5:7] == ["--title", "private /home/<user>"]


def test_creating_an_issue_passes_the_body_by_path_and_returns_the_url():
    runner = runner_returning(stdout="https://github.com/saharkit/windowsill/issues/99\n")
    ok, message = report_bug.create_issue("voice-loop: no sound", "/tmp/bundle.md", runner=runner)
    assert ok and message == "https://github.com/saharkit/windowsill/issues/99"
    argv, timeout = runner.calls[0]
    assert argv == [
        "gh", "issue", "create",
        "--repo", "saharkit/windowsill",
        "--title", "voice-loop: no sound",
        "--body-file", "/tmp/bundle.md",
    ]
    assert timeout == report_bug.GH_TIMEOUT  # a bounded wall clock, always


def test_a_failing_gh_reports_its_own_stderr():
    ok, message = report_bug.create_issue("t", "/tmp/b.md", runner=runner_returning(1, "", "HTTP 403\n"))
    assert ok is False and message == "HTTP 403"


def test_a_hanging_gh_is_given_up_on():
    def hang(argv, timeout):
        raise subprocess.TimeoutExpired(list(argv), timeout)

    ok, message = report_bug.create_issue("t", "/tmp/b.md", runner=hang)
    assert ok is False and "timed out" in message


def test_the_prefilled_url_is_well_formed_and_round_trips():
    url = report_bug.issue_url("voice-loop: no sound", "## bundle\n\nbody & more")
    parsed = urllib.parse.urlsplit(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "github.com", "/saharkit/windowsill/issues/new")
    fields = urllib.parse.parse_qs(parsed.query)
    assert fields["title"] == ["voice-loop: no sound"]
    assert fields["body"] == ["## bundle\n\nbody & more"]


def test_the_prefilled_url_is_trimmed_to_fit_and_says_so():
    url = report_bug.issue_url("t", "x" * 40_000)
    assert len(url) <= report_bug.ISSUE_URL_LIMIT
    body = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["body"][0]
    assert body.startswith("xxxx") and "truncated" in body


def test_the_mailto_is_well_formed_and_round_trips():
    url = report_bug.mailto_url("bugs@example.invalid", "voice-loop: no sound", "## bundle\n\nbody")
    scheme, _, rest = url.partition(":")
    address, _, query = rest.partition("?")
    assert scheme == "mailto" and urllib.parse.unquote(address) == "bugs@example.invalid"
    fields = urllib.parse.parse_qs(query)
    assert fields["subject"] == ["voice-loop: no sound"]
    assert fields["body"] == ["## bundle\n\nbody"]


def test_the_mailto_is_trimmed_harder_than_the_url():
    url = report_bug.mailto_url("bugs@example.invalid", "t", "x" * 40_000)
    assert len(url) <= report_bug.MAILTO_LIMIT
    assert "truncated" in urllib.parse.unquote(url)


def test_the_mailto_tier_is_available_by_default(monkeypatch):
    monkeypatch.delenv("VOICE_LOOP_REPORT_MAILBOX", raising=False)
    monkeypatch.setattr(report_bug.shutil, "which", lambda name: None)
    tiers = {row["name"]: row for row in report_bug.transports(runner_returning())}
    assert tiers["mailto"]["available"] is True
    assert "reports@saharkit.com" in tiers["mailto"]["destination"]
    assert tiers["url"]["available"] is True  # the tier that always works needs nothing installed


def test_a_configured_mailbox_turns_the_tier_on(monkeypatch):
    monkeypatch.setenv("VOICE_LOOP_REPORT_MAILBOX", "bugs@example.invalid")
    monkeypatch.setattr(report_bug.shutil, "which", lambda name: None)
    tiers = {row["name"]: row for row in report_bug.transports(runner_returning())}
    assert tiers["mailto"]["available"] is True
    assert "bugs@example.invalid" in tiers["mailto"]["destination"]


def test_every_transport_names_its_destination(monkeypatch):
    monkeypatch.setattr(report_bug.shutil, "which", lambda name: None)
    for row in report_bug.transports(runner_returning()):
        assert row["destination"]
        assert row["reason"]


# --- the command line -------------------------------------------------------------------------------------


def test_the_mailto_command_sends_to_the_default_address(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("VOICE_LOOP_REPORT_MAILBOX", raising=False)
    bundle = tmp_path / "b.md"
    bundle.write_text("## bundle\n")
    assert report_bug.main(["mailto", "--title", "t", "--bundle", str(bundle)]) == 0
    out = capsys.readouterr().out
    assert "mailto:reports@saharkit.com" in urllib.parse.unquote(out)


def test_the_gh_command_does_not_send_when_gh_is_not_ready(monkeypatch, capsys):
    monkeypatch.setattr(report_bug, "gh_ready", lambda: (False, "the gh CLI is not installed"))
    monkeypatch.setattr(report_bug, "create_issue", lambda *a, **k: pytest.fail("it sent anyway"))
    assert report_bug.main(["gh", "--title", "t", "--bundle", "/tmp/b.md"]) == 1
    assert "not installed" in capsys.readouterr().err


def test_the_gh_command_prints_the_created_issue(monkeypatch, capsys):
    monkeypatch.setattr(report_bug, "gh_ready", lambda: (True, "ok"))
    monkeypatch.setattr(report_bug, "create_issue", lambda *a, **k: (True, "https://example.invalid/issues/1"))
    assert report_bug.main(["gh", "--title", "t", "--bundle", "/tmp/b.md"]) == 0
    assert "https://example.invalid/issues/1" in capsys.readouterr().out


def test_a_missing_bundle_file_is_a_named_error_not_a_traceback(capsys, tmp_path):
    assert report_bug.main(["url", "--title", "t", "--bundle", str(tmp_path / "gone.md")]) == 1
    assert "FileNotFoundError" in capsys.readouterr().err


def test_transports_prints_a_line_per_tier(monkeypatch, capsys):
    monkeypatch.setattr(report_bug.shutil, "which", lambda name: None)
    assert report_bug.main(["transports"]) == 0
    printed = capsys.readouterr().out.strip().splitlines()
    assert [line.split()[0] for line in printed] == ["gh", "url", "mailto"]


def test_transports_json_is_machine_readable(monkeypatch, capsys):
    monkeypatch.setattr(report_bug.shutil, "which", lambda name: None)
    assert report_bug.main(["transports", "--json"]) == 0
    assert [row["name"] for row in json.loads(capsys.readouterr().out)] == ["gh", "url", "mailto"]


def test_collect_json_carries_the_same_bytes_as_the_file(install, offline, tmp_path, capsys):
    out = tmp_path / "bundle.md"
    assert report_bug.main(["collect", "--out", str(out), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == str(out)
    assert payload["digest"] == out.read_text(encoding="utf-8")


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit):
        report_bug.main([])


def test_the_clock_is_injected_not_read_from_the_wall(install, offline):
    bundle = bundle_for(install)
    assert bundle["generated_at"] == "2026-08-03T09:00:00Z"


def test_state_rows_report_size_and_age_only(install, offline):
    rows = {row["name"]: row for row in bundle_for(install)["state"]}
    assert rows["dictate.log"]["present"] is True
    assert set(rows["dictate.log"]) == {"name", "what", "present", "bytes", "age_seconds"}
    assert rows["playing.pid"] == {"name": "playing.pid", "what": rows["playing.pid"]["what"], "present": False}


def test_a_config_that_is_not_json_is_reported_as_such(tmp_path, offline, monkeypatch):
    broken = tmp_path / "config.json"
    broken.write_text("{ not json", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    bundle = report_bug.build_bundle(state_dir=str(state), config_path=str(broken), clock=clock_at())
    assert bundle["config"]["status"].startswith("malformed")
    assert bundle["servers"] == []  # no config, no endpoints to probe — not a crash


def test_command_only_installs_render_without_a_server_section(tmp_path, offline):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"tts": {"command": "say -v Milena"}}), encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    digest = report_bug.render_digest(
        report_bug.build_bundle(state_dir=str(state), config_path=str(config), clock=clock_at())
    )
    assert "no HTTP endpoint configured" in digest


def test_the_plugin_version_is_reported_from_the_manifest():
    versions = report_bug.collect_versions()
    assert versions["plugin_manifest"] == "ok"
    assert re.fullmatch(r"\d+\.\d+\.\d+", versions["plugin"])


def test_an_absent_manifest_is_named_not_guessed(monkeypatch, tmp_path):
    monkeypatch.setattr(report_bug, "_PLUGIN_MANIFEST", str(tmp_path / "plugin.json"))
    versions = report_bug.collect_versions()
    assert versions == {"plugin": "unknown", "plugin_manifest": "absent", "python": report_bug.platform.python_version()}


def test_default_runner_is_bounded_and_argv_only(monkeypatch):
    """The default runner must retain the spawn policy at its own seam.

    L1 gap: this catches a refactor that invokes a shell string, drops the wall-clock bound, or lets
    ``check=True`` turn an ordinary command result into an unhandled exception.
    """
    expected = subprocess.CompletedProcess(["probe"], 0, stdout="", stderr="")
    calls = {}

    def fake_run(argv, **kwargs):
        calls.update(argv=argv, **kwargs)
        return expected

    monkeypatch.setattr(report_bug.subprocess, "run", fake_run)
    assert report_bug._default_runner(["probe"], 2.5) is expected
    assert calls == {
        "argv": ["probe"],
        "capture_output": True,
        "text": True,
        "timeout": 2.5,
        "check": False,
    }


def test_usernames_ignore_a_failed_passwd_lookup(monkeypatch):
    """A machine without a passwd entry still gets redacted names from the available inputs."""
    monkeypatch.setattr(report_bug, "_HOME", "/home/tester")
    for name in ("USER", "LOGNAME", "USERNAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        report_bug.getpass,
        "getuser",
        lambda: (_ for _ in ()).throw(RuntimeError("no passwd entry")),
    )

    assert report_bug.usernames() == ("tester",)


def test_redact_does_not_replace_a_root_home(monkeypatch):
    """The root home is not a user's identity and must not enter the home-collapse path."""
    monkeypatch.setattr(report_bug, "_HOME", "/")
    for name in ("USER", "LOGNAME", "USERNAME"):
        monkeypatch.delenv(name, raising=False)

    assert report_bug.redact("ordinary text") == "ordinary text"


def test_a_cut_message_with_no_payload_redacts_only_its_head():
    """A known log marker at EOF is still metadata, not an empty speech payload."""
    assert report_bug.scrub_message("text: ") == ("text: ", True)


def test_read_log_tail_reports_an_unreadable_source_directory(tmp_path):
    """A directory at the log path is a verified unreadable source, not a missing log."""
    result = report_bug.read_log_tail(str(tmp_path))

    assert result["status"] == "unreadable"
    assert result["read_detail"]
    assert result["lines"] == []
    assert result["unclassified"] == 0


def test_read_log_tail_records_malformed_utf8(tmp_path):
    """Replacement-character recovery must be followed by a strict decodability check."""
    path = tmp_path / "speak.log"
    path.write_bytes(b"2026-08-03T08:00:00 played rc=0\xff")

    result = report_bug.read_log_tail(str(path))

    assert result["status"] == "malformed"
    assert "UnicodeDecodeError" in result["read_detail"]


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (FileNotFoundError, "missing", "disappeared"),
        (OSError, "unreadable", "OSError"),
    ],
)
def test_read_log_tail_distinguishes_a_second_read_failure(error, status, detail, monkeypatch):
    """A second strict read must not replace the original failure with an incidental cleanup error."""
    class _Handle:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return "2026-08-03T08:00:00 played rc=0\n"

    calls = 0

    def fake_open(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Handle()
        raise error(path)

    monkeypatch.setattr(builtins, "open", fake_open)
    result = report_bug.read_log_tail("speak.log")

    assert result["status"] == status
    assert detail in result["read_detail"]


def test_read_json_keeps_a_directory_unreadable_distinct(tmp_path):
    """Config readers name an unreadable path rather than treating it as malformed JSON."""
    path = tmp_path / "config.json"
    path.mkdir()

    loaded, note = report_bug.read_json(str(path))
    assert loaded == {}
    assert note.startswith("unreadable (")


def test_fetch_health_names_a_missing_endpoint(monkeypatch):
    """An absent endpoint is a named no-op and must not attempt a network request."""
    def never(*args, **kwargs):
        raise AssertionError("a missing endpoint must not be opened")

    monkeypatch.setattr(report_bug.urllib.request, "build_opener", never)
    assert report_bug.fetch_health("") == {
        "endpoint": "",
        "reachable": False,
        "error": "no endpoint configured",
    }


def test_fetch_health_treats_http_error_as_a_reachable_certificate(monkeypatch):
    """A 403 proves the handshake completed even though the endpoint was not healthy."""
    class _Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError("https://service.example/", 403, "Forbidden", {}, None)

    monkeypatch.setattr(report_bug.urllib.request, "build_opener", lambda handlers: _Opener())

    result = report_bug.fetch_health("https://service.example", 1.0)

    assert result["reachable"] is True
    assert result["error"] == "HTTP 403"


def test_state_inventory_ignores_a_directory_listing_failure(monkeypatch):
    """A state directory that cannot be listed still yields the explicit missing stream row."""
    def unreadable(path):
        raise PermissionError("state directory")

    monkeypatch.setattr(report_bug.os, "listdir", unreadable)

    inventory = report_bug._state_inventory("/state")

    assert inventory[-1] == (
        report_bug._STREAM_RESULT_ABSENT_NAME,
        report_bug._STREAM_RESULT_WHAT,
    )


def test_sanitize_bundle_reports_missing_fields_without_a_false_unknown_field():
    """Schema drift with only a missing field must name that half of the contract."""
    with pytest.raises(ValueError, match="missing field"):
        report_bug.sanitize_bundle({"generated_at": "now"})


def test_write_bundle_swallow_cleanup_errors_after_replace_failure(tmp_path, monkeypatch):
    """A cleanup failure must not replace the original disk-full error."""
    monkeypatch.setattr(report_bug.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(report_bug.os, "unlink", lambda path: (_ for _ in ()).throw(OSError("cleanup failed")))

    with pytest.raises(OSError, match="disk full"):
        report_bug.write_bundle(str(tmp_path / "bundle.md"), "body")


def test_gh_ready_reports_a_runner_error(monkeypatch):
    """An unspawnable gh probe is a normal unavailable tier, not a traceback."""
    monkeypatch.setattr(report_bug.shutil, "which", lambda name: "/usr/bin/gh")

    def runner(argv, timeout):
        raise OSError("permission denied")

    ready, why = report_bug.gh_ready(runner)

    assert ready is False
    assert "gh could not be run" in why


def test_create_issue_rejects_bytes_that_differ_from_the_artifact(tmp_path):
    """The transport must refuse to send a body other than the one the user saw."""
    path = tmp_path / "bundle.md"
    path.write_text("displayed", encoding="utf-8")
    artifact = report_bug.ReportArtifact("title", "other bytes")

    assert report_bug.create_issue(artifact, str(path), runner=lambda *args: pytest.fail("must not send")) == (
        False,
        "report artifact does not match the bundle file",
    )


def test_create_issue_passes_an_optional_label(tmp_path):
    """A consent-approved label is passed as an argv token, never shell syntax."""
    path = tmp_path / "bundle.md"
    path.write_bytes(b"displayed bytes")
    artifact = report_bug.ReportArtifact("title", "displayed bytes")
    calls = []

    def runner(argv, timeout):
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, stdout="https://example.invalid/1\n", stderr="")

    assert report_bug.create_issue(artifact, str(path), label="voice-loop", runner=runner) == (
        True,
        "https://example.invalid/1",
    )
    assert calls[0][0][-2:] == ["--label", "voice-loop"]


def test_create_issue_reports_a_runner_oserror():
    """A failed issue-create spawn is an ordinary transport result."""
    def runner(argv, timeout):
        raise OSError("permission denied")

    assert report_bug.create_issue("title", "bundle.md", runner=runner) == (
        False,
        "gh could not be run (OSError)",
    )


def test_issue_url_accepts_an_existing_artifact():
    """An artifact already bound to its displayed body must not be rerendered."""
    artifact = report_bug.ReportArtifact("redacted title", "redacted body")
    url = report_bug.issue_url(artifact)

    parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert parsed["title"] == ["redacted title"]
    assert parsed["body"] == ["redacted body"]


def test_issue_url_requires_a_body_for_a_plain_title():
    """A plain title cannot silently become an issue containing only a title."""
    with pytest.raises(TypeError, match="body is required"):
        report_bug.issue_url("title")


def test_mailto_url_requires_a_body_for_a_plain_subject():
    """A plain subject cannot silently become a mail body containing no report."""
    with pytest.raises(TypeError, match="body is required"):
        report_bug.mailto_url("bugs@example.invalid", "subject")


def test_url_command_prints_a_prefilled_url_for_a_readable_bundle(tmp_path, capsys):
    """The URL command's success return is part of its CLI contract."""
    path = tmp_path / "bundle.md"
    path.write_text("bundle", encoding="utf-8")

    assert report_bug.main(["url", "--title", "title", "--bundle", str(path)]) == 0
    assert "https://github.com/" in capsys.readouterr().out


def test_mailto_command_refuses_an_empty_mailbox(monkeypatch, tmp_path, capsys):
    """An empty configured mailbox is a user-facing refusal, not an implicit default."""
    monkeypatch.setenv(report_bug._MAILBOX_ENV, "")

    assert report_bug.main(["mailto", "--title", "title", "--bundle", str(tmp_path / "missing.md")]) == 1
    assert "no service mailbox" in capsys.readouterr().err


def test_script_entrypoint_requires_a_command(monkeypatch):
    """The executable entry point must not silently accept an empty argv."""
    monkeypatch.setattr(report_bug.sys, "argv", ["report_bug.py"])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(_SCRIPTS / "report_bug.py"), run_name="__main__")

    assert raised.value.code == 2
