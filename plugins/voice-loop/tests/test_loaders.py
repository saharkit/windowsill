"""The lazy loaders, exercised for real against fake packages.

These are the seams: `whisper()` imports `faster_whisper`, `tts()` calls `torch.hub.load`, the
accentuators import their language packages. Each is patchable, so the real function bodies run —
no model is downloaded, nothing touches the network.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

import voice_server
from conftest import FakeSilero

ACUTE = "́"


class FakeWhisperModel:
    instances: list["FakeWhisperModel"] = []

    def __init__(self, name: str, device: str, compute_type: str) -> None:
        self.name, self.device, self.compute_type = name, device, compute_type
        FakeWhisperModel.instances.append(self)


def test_whisper_is_built_once_with_the_resolved_device(monkeypatch, import_fake):
    FakeWhisperModel.instances.clear()
    import_fake("faster_whisper", WhisperModel=FakeWhisperModel)
    monkeypatch.setattr(voice_server, "STT_MODEL", "tiny")
    monkeypatch.setattr(voice_server, "DEVICE", "cpu")
    monkeypatch.setattr(voice_server, "COMPUTE_TYPE", "auto")

    first = voice_server.whisper()
    second = voice_server.whisper()

    assert first is second
    assert len(FakeWhisperModel.instances) == 1
    assert (first.name, first.device, first.compute_type) == ("tiny", "cpu", "int8")


def test_ru_accentuator_loads_and_marks(import_fake):
    class FakeRUAccent:
        def __init__(self) -> None:
            self.options: dict[str, object] = {}

        def load(self, **options: object) -> None:
            self.options = options

        def process_all(self, text: str) -> str:
            return text.replace("тест", "т+ест")

    import_fake("ruaccent", RUAccent=FakeRUAccent)
    process = voice_server._load_ru_accentuator()
    assert process("это тест") == "это т+ест"


class FakeStressSymbol:
    """Mirrors ukrainian_word_stress.StressSymbol — the real values, pinned by the format test below."""

    AcuteAccent = "´"  # U+00B4, the package's DEFAULT and not something Silero can read
    CombiningAcuteAccent = ACUTE


class FakeDisambiguation:
    """Mirrors ukrainian_word_stress.Disambiguation (its Stanza backend downloads ~500 MB of models)."""

    Auto = "auto"
    Stanza = "stanza"
    Dictionary = "dictionary"


def test_uk_accentuator_normalizes_the_acute_it_emits(import_fake):
    built: list[dict[str, object]] = []

    class FakeStressifier:
        def __init__(self, **options: object) -> None:
            built.append(options)

        def __call__(self, text: str) -> str:
            return text.replace("робота", f"робо{ACUTE}та")

    import_fake(
        "ukrainian_word_stress",
        Stressifier=FakeStressifier,
        StressSymbol=FakeStressSymbol,
        Disambiguation=FakeDisambiguation,
    )
    process = voice_server._load_uk_accentuator()

    assert process("робота") == "роб+ота"
    # The two arguments carry the whole contract: ask for the acute we parse, and stay off Stanza.
    assert built == [{"stress_symbol": ACUTE, "disambiguation": "dictionary"}]


def test_real_ukrainian_word_stress_output_matches_what_acute_to_plus_expects():
    """The same contract against the REAL package — a fake can only pin what we already believe.

    Still no network and no model: the loader asks for the dictionary-only backend, which reads the
    trie shipped inside the wheel. Assertions are about the FORMAT (which character, on which side
    of which vowel), never about the syllable the dictionary picks.
    """
    uws = pytest.importorskip("ukrainian_word_stress")

    assert uws.StressSymbol.CombiningAcuteAccent == ACUTE
    assert uws.Disambiguation.Dictionary == FakeDisambiguation.Dictionary

    stressify = uws.Stressifier(
        stress_symbol=uws.StressSymbol.CombiningAcuteAccent,
        disambiguation=uws.Disambiguation.Dictionary,
    )
    process = voice_server._load_uk_accentuator()

    for word in ("привіт", "мова"):  # both are unambiguous dictionary entries
        raw = stressify(word)
        assert ACUTE in raw, f"the package stopped marking {word!r}: {raw!r}"
        assert raw.replace(ACUTE, "") == word, f"the mark is not the only edit to {word!r}: {raw!r}"
        assert raw[raw.index(ACUTE) - 1] in voice_server.STRESSED_VOWELS  # acute FOLLOWS its vowel

        marked = process(word)
        assert ACUTE not in marked, f"an acute survived normalization of {word!r}: {marked!r}"
        assert marked.replace("+", "") == word, f"normalization mangled {word!r}: {marked!r}"
        assert marked[marked.index("+") + 1] in voice_server.STRESSED_VOWELS  # '+' PRECEDES its vowel


def test_accentuator_registry_wires_the_real_loaders():
    assert voice_server.ACCENTUATORS["ru"] is voice_server._load_ru_accentuator
    assert voice_server.ACCENTUATORS["uk"] is voice_server._load_uk_accentuator


def test_missing_language_package_degrades_without_raising(monkeypatch, caplog, accent_enabled):
    monkeypatch.setitem(sys.modules, "ruaccent", None)  # importing None raises ImportError
    with caplog.at_level("WARNING"):
        assert voice_server.accentuator("ru") is None
    assert "accentuation unavailable for ru" in caplog.text


# --- silero voices --------------------------------------------------------------------------------


@pytest.fixture
def hub(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_load(repo: str, entry: str, **kwargs: object):
        calls.append({"repo": repo, "entry": entry, **kwargs})
        return FakeSilero(), "example"

    monkeypatch.setattr(voice_server.torch.hub, "load", fake_load)
    return calls


def test_voice_is_loaded_from_the_language_table_and_cached(hub):
    first = voice_server.tts("ru")
    again = voice_server.tts("ru")

    assert first is again
    assert len(hub) == 1
    assert (hub[0]["language"], hub[0]["speaker"]) == ("ru", "v4_ru")
    assert first.device is not None  # moved to CPU on purpose: the GPU stays free for recognition


def test_ukrainian_uses_the_upstream_language_key(hub):
    voice_server.tts("uk")
    assert (hub[0]["language"], hub[0]["speaker"]) == ("ua", "v4_ua")


def test_english_is_a_first_class_language(hub):
    voice_server.tts("en")
    assert (hub[0]["language"], hub[0]["speaker"]) == ("en", "v3_en")
    assert voice_server.default_speaker("en") == "en_0"


def test_model_override_applies_only_to_the_default_language(hub, monkeypatch):
    monkeypatch.setattr(voice_server, "LANGUAGE", "ru")
    monkeypatch.setattr(voice_server, "TTS_MODEL_OVERRIDE", "v3_1_ru")

    voice_server.tts("ru")
    voice_server.tts("en")

    assert hub[0]["speaker"] == "v3_1_ru"
    assert hub[1]["speaker"] == "v3_en"


def test_speaker_override_applies_only_to_the_default_language(monkeypatch):
    monkeypatch.setattr(voice_server, "LANGUAGE", "ru")
    monkeypatch.setattr(voice_server, "TTS_SPEAKER_OVERRIDE", "xenia")
    assert voice_server.default_speaker("ru") == "xenia"
    assert voice_server.default_speaker("uk") == "mykyta"


# --- process entry points -------------------------------------------------------------------------


def test_require_python_accepts_the_running_interpreter():
    assert voice_server.require_python() is None


def test_require_python_rejects_an_old_interpreter():
    with pytest.raises(SystemExit) as exc:
        voice_server.require_python((3, 9))
    assert "3.10 or newer" in str(exc.value)
    assert "3.9" in str(exc.value)


def test_main_starts_uvicorn_on_the_configured_socket(monkeypatch, caplog):
    started: list[dict[str, object]] = []
    monkeypatch.setattr(voice_server, "HOST", "127.0.0.1")
    monkeypatch.setattr(voice_server, "PORT", 8355)
    monkeypatch.setattr(voice_server.uvicorn, "run", lambda app, host, port: started.append({"host": host, "port": port, "app": app}))

    with caplog.at_level("WARNING"):
        voice_server.main()

    assert started == [{"host": "127.0.0.1", "port": 8355, "app": voice_server.app}]
    assert "0.0.0.0" not in caplog.text  # a loopback bind earns no warning


def test_main_warns_loudly_on_a_wildcard_bind(monkeypatch, caplog):
    started: list[str] = []
    monkeypatch.setattr(voice_server, "HOST", "0.0.0.0")
    monkeypatch.setattr(voice_server.uvicorn, "run", lambda app, host, port: started.append(host))

    with caplog.at_level("WARNING"):
        voice_server.main()

    assert started == ["0.0.0.0"]  # warned, not refused — the Docker default binds wide on purpose
    assert "VOICE_LOOP_HOST=0.0.0.0" in caplog.text
    assert "NO authentication" in caplog.text


def test_default_language_is_english_when_the_env_does_not_choose(monkeypatch):
    """English is a first-class language and the shipped default; setups always write an explicit
    language, so this only shows on a bare `python voice_server.py`."""
    import importlib

    monkeypatch.delenv("VOICE_LOOP_LANGUAGE", raising=False)
    module = importlib.reload(voice_server)
    assert module.LANGUAGE == "en"


# --- the config home: an empty XDG variable is unset, not "here" (#215) --------------------------


def test_config_home_ignores_a_present_but_empty_xdg_variable(monkeypatch, tmp_path):
    """windowsill #215: XDG_CONFIG_HOME="" used to become Path("") — "." — which made the stress
    dictionary a RELATIVE voice-loop/stress.json, an operator-controlled regex file loaded from
    whatever directory the server happened to start in. The spec's default applies when the
    variable is "either not set or empty"; only the first half was honoured."""
    monkeypatch.setattr(voice_server.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", "")

    home = voice_server._config_home()

    assert home == tmp_path / ".config"
    assert home.is_absolute()


def test_config_home_reads_a_set_xdg_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert voice_server._config_home() == tmp_path / "xdg"


def test_config_home_falls_back_to_the_home_default_when_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(voice_server.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert voice_server._config_home() == tmp_path / ".config"


# --- the container's published interface (windowsill #215) ---------------------------------------
#
# The image's own ENV binds 0.0.0.0 ON PURPOSE — inside the container the server must bind every
# interface or the `-p` mapping forwards to nothing. What a stranger can dial is the HOST side of
# the documented `docker run … -p` mapping, so that — never the ENV — is what these tests pin.


def _documented_publishes() -> list[tuple[str, str]]:
    """(file, host-side address) of every documented `docker run` publish; "" where a publish
    carries no host address, which is Docker's "every interface" default."""
    server_dir = Path(__file__).resolve().parents[1] / "server"
    found: list[tuple[str, str]] = []
    for name in ("Dockerfile", "README.md"):
        text = (server_dir / name).read_text(encoding="utf-8")
        for token in re.findall(r"-p[ \t]+(\S+)", text):
            parts = token.split(":")
            if len(parts) == 3:
                found.append((name, parts[0]))
            elif len(parts) == 2:
                found.append((name, ""))
    return found


def test_the_documented_docker_publish_binds_the_host_side_to_loopback():
    """L2: the publish already reads `-p 127.0.0.1:8355:8355`, and nothing but this test notices
    a doc edit back to bare `-p 8355:8355` — which publishes the unauthenticated server on every
    interface the host has, silently."""
    found = _documented_publishes()
    assert found, "the documented `docker run` publish went missing — restore it"
    for name, host in found:
        assert host == "127.0.0.1", (
            f"{name} publishes on {host or 'every interface'} — the server has no authentication"
        )


def test_the_image_env_keeps_binding_wide_on_purpose():
    """The companion guard (#215): the tempting "fix" — VOICE_LOOP_HOST=127.0.0.1 in the image —
    silently breaks the container (the mapping then forwards to a loopback the publishing bridge
    cannot reach) while leaving the real exposure, the publish, untouched. The ENV stays wide and
    the boundary stays on `-p`; this test fails if that decision is quietly reversed."""
    dockerfile = (Path(__file__).resolve().parents[1] / "server" / "Dockerfile").read_text(encoding="utf-8")
    assert "VOICE_LOOP_HOST=0.0.0.0" in dockerfile
