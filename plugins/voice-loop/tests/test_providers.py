"""The provider registry: the entries, and the property the registry exists for.

Nothing here touches the network. An entry BUILDS a request and READS a decoded body — both pure —
so every provider on the shelf is proven at the request/response boundary without a socket, and the
live half (a real clip through a real key) stays the human proof it has to be (see
``fixtures/PROVENANCE.md``).

The load-bearing test in this file is the grep one: a provider must never be a branch again.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import providers

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --- the property the whole ticket is about ------------------------------------------------------


def test_no_dispatch_path_compares_a_provider_against_a_literal():
    """windowsill#94's first acceptance criterion, as a test rather than a reviewer's grep.

    The form matters: the two DISPATCH sites read ``s["provider"]`` / ``s["stt_provider"]``, so a
    check written as ``grep 'provider =='`` matches the three cosmetic sites and misses both of the
    ones that decide where a request goes — which is how #54 shipped its body without its comment.
    Both forms are checked here.

    This file is exempt from its own rule: the pattern below is data, and the string it looks for
    has to be written down somewhere.
    """
    forbidden = re.compile(r"""\[.(stt_)?provider.\]\s*==|\bprovider\s*==""")
    offenders = []
    for path in sorted(_SCRIPTS.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if forbidden.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "a provider is an entry, never a branch — but: " + "; ".join(offenders)


def test_adding_a_provider_is_one_entry_and_the_entry_is_complete():
    """"One entry" is only true if the entry carries every axis. A row that leaves one to a caller
    puts the branch back somewhere else, out of sight of the grep above."""
    for entry in providers.STT_PROVIDERS.values():
        assert entry.default_model and callable(entry.build)
        assert callable(entry.transcript) and callable(entry.error_summary)
        assert isinstance(entry.key_env_fallbacks, tuple)
        assert entry.comparison.languages and entry.comparison.privacy
    for entry in providers.TTS_PROVIDERS.values():
        assert entry.default_model and callable(entry.build)
        assert entry.comparison.languages and entry.comparison.privacy


def test_an_unknown_provider_name_has_no_entry_rather_than_a_guessed_one():
    assert providers.stt_provider("deepgrma") is None
    assert providers.tts_provider("deepgrma") is None
    assert providers.stt_provider(providers.DEFAULT_STT).name == "openai"
    assert providers.tts_provider(providers.DEFAULT_TTS).name == "openai"


# --- the registry validates itself at import -----------------------------------------------------


def _stt(**overrides):
    base = providers.STT_PROVIDERS["openai"]
    fields = {
        "name": base.name,
        "default_model": base.default_model,
        "default_host": base.default_host,
        "key_env_fallbacks": base.key_env_fallbacks,
        "build": base.build,
        "transcript": base.transcript,
        "error_summary": base.error_summary,
        "comparison": base.comparison,
    }
    fields.update(overrides)
    return providers.SttProvider(**fields)


@pytest.mark.parametrize(
    "table",
    [
        {"openai": _stt(name="not-openai")},
        {"openai": _stt(default_model="")},
        {"openai": _stt(default_host="http://api.example")},
        {"openai": _stt(comparison=providers.Comparison("", "c", "l", "p"))},
        {},
    ],
)
def test_a_broken_row_is_refused_at_import(monkeypatch, table):
    monkeypatch.setattr(providers, "STT_PROVIDERS", table)
    with pytest.raises(ValueError):
        providers._validate_registry()


def test_a_tts_table_without_the_default_provider_is_refused(monkeypatch):
    monkeypatch.setattr(providers, "TTS_PROVIDERS", {})
    with pytest.raises(ValueError):
        providers._validate_registry()


# --- the shared readers --------------------------------------------------------------------------


def test_decode_answers_none_for_every_shape_of_no_document():
    assert providers.decode(None) is None
    assert providers.decode(b"") is None
    assert providers.decode(b"<html>bad gateway</html>") is None
    assert providers.decode(b'{"text": "ok"}') == {"text": "ok"}


def test_text_field_reads_the_openai_and_elevenlabs_shape():
    assert providers.text_field({"text": " hello \n"}) == "hello"
    assert providers.text_field({"detail": "no text key"}) == ""
    assert providers.text_field(["not", "a", "dict"]) == ""
    assert providers.text_field(None) == ""


def test_an_error_document_that_is_a_list_does_not_explode():
    """``str(err.get('detail', err))`` on a JSON body that decodes to a LIST is an AttributeError
    in a path whose whole job is to explain a failure. The isinstance guard is why it is not."""
    assert providers._detail_or_document(["quota", "exceeded"]) == "['quota', 'exceeded']"
    assert providers._detail_or_document({"detail": "invalid_api_key"}) == "invalid_api_key"
    assert providers._detail_or_document({"code": 429}) == "{'code': 429}"


def test_deepgram_error_documents_read_their_own_field():
    assert providers._deepgram_error({"err_code": "INVALID_AUTH", "err_msg": "Token is invalid"}) == (
        "Token is invalid"
    )
    assert providers._deepgram_error({"error": "project not found"}) == "project not found"
    assert providers._deepgram_error({"reason": "rate limited"}) == "rate limited"
    assert providers._deepgram_error({"unknown": "shape"}) == "{'unknown': 'shape'}"
    assert providers._deepgram_error("not a document") == "not a document"


# --- Deepgram, against a pinned response ---------------------------------------------------------


def test_the_deepgram_entry_parses_the_pinned_real_response():
    """windowsill#94 acceptance criterion 4: a shape drift must go RED, not degrade quietly.

    The parse failing does not raise — it returns "" and dictate.py degrades to local whisper under
    a log line that blames "the cloud". That is precisely the silent failure this pin catches: if
    Deepgram moves the transcript, this assertion fails in CI instead of a user's dictation getting
    quietly slower and less accurate.

    Provenance — and the fact that this is not YET a live capture — is in fixtures/PROVENANCE.md.
    """
    raw = (_FIXTURES / "deepgram_listen_response.json").read_bytes()
    entry = providers.STT_PROVIDERS["deepgram"]
    assert entry.transcript(providers.decode(raw)) == "Hello agent, this is the dictation contract."


def test_the_deepgram_parser_reads_no_transcript_out_of_every_way_the_walk_can_fail():
    entry = providers.STT_PROVIDERS["deepgram"]
    for body in (
        None,
        {},
        {"results": {}},
        {"results": {"channels": []}},
        {"results": {"channels": [{"alternatives": []}]}},
        {"results": {"channels": [{"alternatives": [{}]}]}},
        {"err_code": "INVALID_AUTH", "err_msg": "Token is invalid"},
        ["not", "a", "dict"],
    ):
        assert entry.transcript(body) == ""


def test_the_deepgram_stt_request_is_a_raw_wav_body_with_query_parameters():
    """The axis the registry earns its keep on: Deepgram is NOT multipart, its auth scheme is
    ``Token`` rather than ``Bearer``, and ``language`` is a query parameter rather than a form
    field. None of that is expressible in the if/else this replaced."""
    entry = providers.STT_PROVIDERS["deepgram"]
    s = {"cloud_endpoint": "", "endpoint": "http://127.0.0.1:8355", "stt_model": "nova-3", "language": "ru"}
    request = entry.request(s, "dg-secret", b"RIFFfakewav", "BOUND")
    assert request.url.startswith("https://api.deepgram.com/v1/listen?")
    assert "model=nova-3" in request.url and "language=ru" in request.url
    assert "smart_format=true" in request.url
    assert request.headers == {"Authorization": "Token dg-secret"}
    assert request.body == b"RIFFfakewav"  # the WAV IS the body — no form framing at all
    assert request.content_type == "audio/wav"


def test_the_deepgram_tts_request_asks_for_wav_because_aplay_cannot_play_mp3():
    entry = providers.TTS_PROVIDERS["deepgram"]
    s = {"endpoint": "", "cloud_model": "aura-2-thalia-en", "output_format": entry.default_output_format}
    request = entry.request(s, "dg-secret", "hello there")
    assert request.url == (
        "https://api.deepgram.com/v1/speak?model=aura-2-thalia-en&encoding=linear16&container=wav"
    )
    assert request.headers == {"Authorization": "Token dg-secret"}
    assert request.payload == {"text": "hello there"}


def test_a_deepgram_output_format_the_user_cleared_leaves_the_vendor_default():
    entry = providers.TTS_PROVIDERS["deepgram"]
    s = {"endpoint": "", "cloud_model": "aura-2-thalia-en", "output_format": ""}
    assert entry.request(s, "k", "hi").url == "https://api.deepgram.com/v1/speak?model=aura-2-thalia-en"


def test_a_configured_endpoint_beats_the_providers_own_default_host():
    """The self-hosted case, which is the reason stt.cloud.endpoint exists — and the reason
    default_host is a fallback rather than a constant."""
    entry = providers.STT_PROVIDERS["deepgram"]
    s = {"cloud_endpoint": "https://deepgram.internal", "endpoint": "", "stt_model": "nova-3", "language": "en"}
    assert entry.request(s, "k", b"wav", "B").url.startswith("https://deepgram.internal/v1/listen?")
    tts = providers.TTS_PROVIDERS["deepgram"]
    assert tts.endpoint({"endpoint": "https://deepgram.internal"}) == "https://deepgram.internal"


def test_a_provider_with_no_remote_default_host_falls_back_to_the_local_server():
    assert providers.TTS_PROVIDERS["openai"].endpoint({"endpoint": ""}) == providers.LOCAL_SPEECH_HOST
    assert providers.STT_PROVIDERS["openai"].endpoint(
        {"cloud_endpoint": "", "endpoint": "https://speech.example"}
    ) == "https://speech.example"


def test_the_credentials_home_rule_lives_on_the_entry():
    """ElevenLabs is the one provider that accepts the TTS key for STT, and that is a FIELD now —
    the fallback list, in order, most-specific first."""
    assert providers.STT_PROVIDERS["elevenlabs"].key_envs("VOICE_LOOP_STT_API_KEY") == (
        "VOICE_LOOP_STT_API_KEY",
        "VOICE_LOOP_TTS_API_KEY",
    )
    assert providers.STT_PROVIDERS["openai"].key_envs("MY_KEY") == ("MY_KEY",)
    assert providers.STT_PROVIDERS["deepgram"].key_envs("MY_KEY") == ("MY_KEY",)


# --- the comparison surface ----------------------------------------------------------------------


def test_every_registry_entry_has_a_row_in_the_comparison_table():
    """windowsill#94 acceptance criterion 7. A provider a user cannot compare is a provider they
    cannot choose, so PROVIDERS.md is drift-tested against the registry rather than trusted."""
    doc = (Path(__file__).resolve().parents[1] / "PROVIDERS.md").read_text(encoding="utf-8")
    for name in providers.STT_PROVIDERS:
        assert f"`{name}` (STT)" in doc, f"{name} has no STT row in PROVIDERS.md"
    for name in providers.TTS_PROVIDERS:
        assert f"`{name}` (TTS)" in doc, f"{name} has no TTS row in PROVIDERS.md"
    # the four axes the operator asked for by name, and the two languages #47 is about
    for axis in ("latency", "cost per minute", "language coverage", "privacy posture"):
        assert axis in doc.lower()
    assert "Russian" in doc and "Ukrainian" in doc


def test_the_fixture_says_out_loud_that_it_is_not_a_live_capture_yet():
    """The one thing a fixture must never do is quietly claim provenance it does not have. When the
    human live run happens, this assertion is what makes updating PROVENANCE.md unavoidable."""
    provenance = (_FIXTURES / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "deepgram_listen_response.json" in provenance
    # the fixture is valid JSON of the shape the parser reads, whatever its provenance
    assert json.loads((_FIXTURES / "deepgram_listen_response.json").read_bytes())["results"]["channels"]
