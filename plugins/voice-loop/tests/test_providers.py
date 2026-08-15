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


# The forbidden shape: a provider name compared against a literal, which is how a dispatch path
# sneaks back in. Every spelling matters — the two dispatch sites read ``s["provider"]`` /
# ``s["stt_provider"]``, and the idiomatic ``.get()`` spelling is the one a bare ``provider ==``
# grep misses (the ``)`` before the ``==`` defeats it), which is exactly how #54 shipped its body
# without its comment.
_PROVIDER_BRANCH = re.compile(
    r"""
    \[["'](?:stt_)?provider["']\]\s*==             # s["provider"] ==  /  s['stt_provider'] ==
    |\.get\(\s*["'](?:stt_)?provider["']\s*\)\s*==  # s.get("provider") ==  /  s.get('stt_provider') ==
    |\b\w*provider\s*==                             # provider ==  /  stt_provider ==
    """,
    re.VERBOSE,
)


def test_no_dispatch_path_compares_a_provider_against_a_literal():
    """windowsill#94's first acceptance criterion, as a test rather than a reviewer's grep.

    The form matters: the two DISPATCH sites read ``s["provider"]`` / ``s["stt_provider"]``, so a
    check written as ``grep 'provider =='`` matches the three cosmetic sites and misses both of the
    ones that decide where a request goes. The idiomatic ``.get()`` spelling is the other one a
    naive grep misses, and is covered by ``_PROVIDER_BRANCH`` (see the self-test below).

    This file is exempt from its own rule: the pattern below is data, and the string it looks for
    has to be written down somewhere.
    """
    offenders = []
    for path in sorted(_SCRIPTS.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _PROVIDER_BRANCH.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "a provider is an entry, never a branch — but: " + "; ".join(offenders)


def test_the_guard_catches_the_get_spelling_of_a_provider_branch():
    """The acceptance criterion: the forbidden-shape guard must FAIL when the shape it forbids is
    planted in its most idiomatic ``.get()`` spelling. A ``grep 'provider =='`` does not see
    ``s.get("provider") == "openai"`` — the ``")`` before the ``==`` defeats it — which is how a
    dispatch branch ships while the guard stays green."""
    for planted in (
        'entry = providers.TTS_PROVIDERS[s.get("provider") == "openai"]',
        'if s.get("stt_provider") == "deepgram":',
        "branch = s['stt_provider'] == 'elevenlabs'",
        'if stt_provider == "openai":',
    ):
        assert _PROVIDER_BRANCH.search(planted), f"the guard missed: {planted!r}"


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
    assert providers.text_field({"detail": "no text key"}) is None
    assert providers.text_field(["not", "a", "dict"]) is None
    assert providers.text_field(None) is None


def test_an_empty_transcript_is_a_transcript_and_a_missing_one_is_not():
    """The distinction windowsill#93 turns on: `{"text": ""}` is what a silent clip transcribes to
    and it is a SUCCESS, while a document with no `text` field at all is the degrade signal. Read
    as one value ('' for both), a silent toggle logged a cloud error and posted the clip twice."""
    assert providers.text_field({"text": ""}) == ""
    assert providers.text_field({"text": "   "}) == ""
    assert providers.text_field({}) is None


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
        assert entry.transcript(body) is None


def test_a_deepgram_walk_that_succeeds_on_an_empty_transcript_is_a_silent_clip():
    """Same distinction as text_field's, at the other parser: the walk COMPLETED, so this is a
    clip with no speech in it — not a document Deepgram failed to answer with."""
    entry = providers.STT_PROVIDERS["deepgram"]
    assert entry.transcript({"results": {"channels": [{"alternatives": [{"transcript": ""}]}]}}) == ""


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


def test_the_deepgram_entry_carries_turkish_to_the_cloud_provider():
    """L2 GAP: deleting this language-axis assertion would let Turkish fall back to auto-detect or
    another language while the provider request still looked structurally valid."""
    entry = providers.STT_PROVIDERS["deepgram"]
    request = entry.request(
        {"cloud_endpoint": "", "endpoint": "", "stt_model": "nova-3", "language": "tr"},
        "dg-secret",
        b"RIFFfakewav",
        "BOUND",
    )
    assert "language=tr" in request.url


def test_the_scribe_request_carries_the_configured_language_as_language_code():
    """windowsill#93: the language axis is a per-provider SPELLING, not a per-provider silence.
    OpenAI takes `language`, Scribe takes `language_code`, Deepgram takes a query parameter — and
    a user who configured `stt.language: "ru"` gets the hint through all three."""
    entry = providers.STT_PROVIDERS["elevenlabs"]
    s = {"cloud_endpoint": "", "endpoint": "", "stt_model": "scribe_v1", "language": "ru"}
    request = entry.request(s, "xi-secret", b"RIFFfakewav", "BOUND")
    assert request.url == "https://api.elevenlabs.io/v1/speech-to-text"
    assert b'name="language_code"\r\n\r\nru\r\n' in request.body
    assert b'name="model_id"\r\n\r\nscribe_v1\r\n' in request.body


def test_an_empty_language_leaves_scribe_to_auto_detect():
    """The escape hatch the old unconditional silence gave everybody for free: no language_code
    field at all, which is what asks Scribe to detect the language itself."""
    entry = providers.STT_PROVIDERS["elevenlabs"]
    s = {"cloud_endpoint": "", "endpoint": "", "stt_model": "scribe_v1", "language": ""}
    assert b"language_code" not in entry.request(s, "xi-secret", b"RIFFfakewav", "BOUND").body


# --- stt.prompt: the jargon-priming lever (windowsill#162) ---------------------------------------
#
# One config key, two paths: on the cloud OpenAI request it is the API's `prompt` form field; on the
# local server it is faster-whisper's initial_prompt (pinned in test_dictate.py / test_api.py). The
# OpenAI builder is the ONLY path that truncates — the 224-token cap is an OpenAI-API constraint — so
# the truncation decision and the include/omit decision are pinned HERE, at the request-build tier;
# the wiring from a real config dict is pinned through resolve_settings in test_dictate.py.


def test_a_prompt_under_the_budget_is_returned_trimmed_and_untouched():
    """L2 GAP: a regression that always truncated (or skipped the trim) would still execute this
    line for coverage and catch nothing. A short lexicon is passed through verbatim; surrounding
    whitespace is trimmed because the value is a multipart form field, not free-form config prose."""
    assert providers.truncate_stt_prompt("kubectl, Acme") == "kubectl, Acme"
    assert providers.truncate_stt_prompt("  kubectl, Acme\n") == "kubectl, Acme"


def test_a_prompt_over_the_budget_is_cut_after_the_last_whole_term():
    """L2 GAP: a hard ``text[:limit]`` cut would split a term mid-word (and the API would cut again
    at its own boundary). The mutant passes line coverage and survives every test that only checks
    ``len <= limit``. This one proves the cut lands ON a comma boundary — the character right after
    the kept text is the comma that began the dropped term — so no term is ever split, and the
    leading (most-relevant) terms are kept because the cut text is a verbatim prefix."""
    over = ", ".join(f"term{i}" for i in range(200))  # ~1300 chars, far over the 448 budget
    cut = providers.truncate_stt_prompt(over)
    assert len(cut) <= providers.STT_PROMPT_MAX_CHARS
    assert over[: len(cut)] == cut, "the kept text must be a verbatim prefix (leading terms kept)"
    assert over[len(cut)] == ",", "the cut must land on a term boundary, never mid-word"
    assert cut, "truncation keeps the leading terms, it does not empty the prompt"


def test_a_prompt_with_no_commas_lands_on_the_last_space():
    """L2 GAP: the comma-first boundary has a space fallback for prose (or a lexicon a user wrote
    with spaces). A mutant that handled commas only would hard-cut a comma-less string mid-word and
    no comma-oriented test would notice."""
    prose = " ".join(f"word{i}" for i in range(200))
    cut = providers.truncate_stt_prompt(prose)
    assert len(cut) <= providers.STT_PROMPT_MAX_CHARS
    assert prose[: len(cut)] == cut
    assert prose[len(cut)] == " ", "with no comma, the cut lands on the last space"


def test_a_prompt_with_no_separator_at_all_is_hard_cut_to_the_limit():
    """L2 GAP: the ``boundary <= 0`` guard. A string whose first ``limit`` chars hold NEITHER comma
    nor space (one very long token) makes ``rfind`` return -1, and without this branch
    ``head[:boundary]`` would slice to ``head[:-1]`` (dropping a char) — or to "" at a leading
    separator, emptying the prompt. That mutant passes the comma and space tests above and survives;
    only a separator-less input forces ``boundary`` negative and lands here."""
    token = "x" * (providers.STT_PROMPT_MAX_CHARS + 50)
    cut = providers.truncate_stt_prompt(token)
    assert cut == "x" * providers.STT_PROMPT_MAX_CHARS  # a hard cut at the limit, nothing dropped past it


def test_the_openai_builder_carries_a_set_prompt_as_a_form_field():
    """L2 GAP: this is the bug the ticket fixes. A regression that dropped the `prompt` field from
    the OpenAI body would transcribe English technical terms wrong again (Sighted, Little Indian) and
    pass silently — the request would still be well-formed multipart. Without this assertion the
    lever's presence is unverified."""
    entry = providers.STT_PROVIDERS["openai"]
    s = {"cloud_endpoint": "", "endpoint": "", "stt_model": "whisper-1", "language": "ru",
         "stt_prompt": "kubectl, Acme"}
    assert b'name="prompt"\r\n\r\nkubectl, Acme\r\n' in entry.request(
        s, "sk-secret", b"RIFFfakewav", "BOUND"
    ).body


def test_the_openai_builder_omits_the_prompt_field_when_it_is_empty():
    """L2 GAP: omit-when-empty (mirroring Scribe's language_code) keeps the request shape stable for
    the users who set none. An always-send mutant would add an empty `prompt` part to EVERY OpenAI
    request and break the `== 3` parts assertion in test_dictate's documented-shape test — but only
    that one fragile test would notice. This pins the decision at the builder's own tier."""
    entry = providers.STT_PROVIDERS["openai"]
    s = {"cloud_endpoint": "", "endpoint": "", "stt_model": "whisper-1", "language": "en",
         "stt_prompt": ""}
    assert b'name="prompt"' not in entry.request(s, "sk-secret", b"RIFFfakewav", "BOUND").body


def test_the_openai_builder_truncates_an_over_budget_prompt_before_sending():
    """L2 GAP: the composition claim that the builder sends the prompt THROUGH truncate_stt_prompt,
    not raw. A mutant `fields["prompt"] = s["stt_prompt"]` (skipping the call) passes the include
    test above (short prompt) and the truncation tests in isolation (pure function) while shipping an
    over-budget prompt for the API to cut. Only this test — an over-budget value end to end — kills it."""
    entry = providers.STT_PROVIDERS["openai"]
    over = ", ".join(f"term{i}" for i in range(200))
    s = {"cloud_endpoint": "", "endpoint": "", "stt_model": "whisper-1", "language": "en",
         "stt_prompt": over}
    body = entry.request(s, "sk-secret", b"RIFFfakewav", "BOUND").body
    sent = providers.truncate_stt_prompt(over)
    assert f'name="prompt"\r\n\r\n{sent}\r\n'.encode() in body
    assert over.encode() not in body, "the full over-budget string must not travel to the API"


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


def test_elevenlabs_tts_refuses_an_unset_voice():
    """The sibling of _openai_tts's `voice_id or "alloy"` defence: ElevenLabs has no default voice,
    so an unset one must not interpolate an empty path segment (``/v1/text-to-speech//…``) that
    comes back as an opaque 404. Refusing at build time is the "you have not configured a voice"
    the caller logs instead of a request."""
    entry = providers.TTS_PROVIDERS["elevenlabs"]
    s = {"cloud_model": "eleven_multilingual_v2", "voice_id": "", "voice_settings": None,
         "output_format": entry.default_output_format, "endpoint": ""}
    with pytest.raises(ValueError, match="voice"):
        entry.request(s, "xi-secret", "hello")


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
    assert "Russian" in doc and "Ukrainian" in doc and "Turkish" in doc


def test_the_fixture_says_out_loud_that_it_is_not_a_live_capture_yet():
    """The one thing a fixture must never do is quietly claim provenance it does not have. When the
    human live run happens, this assertion is what makes updating PROVENANCE.md unavoidable."""
    provenance = (_FIXTURES / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "deepgram_listen_response.json" in provenance
    # the fixture is valid JSON of the shape the parser reads, whatever its provenance
    assert json.loads((_FIXTURES / "deepgram_listen_response.json").read_bytes())["results"]["channels"]


# --- the streaming variant (windowsill#99) -------------------------------------------------------


class TestTheStreamingVariantIsAnEntryToo:
    """A live socket is an eighth axis on the SAME entry, not a second table and not a branch."""

    def test_a_provider_without_one_carries_none_rather_than_a_guess(self):
        assert providers.STT_PROVIDERS["openai"].streaming is None
        assert providers.STT_PROVIDERS["elevenlabs"].streaming is None
        assert providers.STT_PROVIDERS["deepgram"].streaming is not None

    def test_the_variant_carries_every_axis_a_socket_needs(self):
        """Same bar as the batch rows: a variant that leaves one axis to its caller has put the
        branch back somewhere else, out of sight of the grep above."""
        for entry in providers.STT_PROVIDERS.values():
            if entry.streaming is None:
                continue
            assert callable(entry.streaming.url)
            assert callable(entry.streaming.headers)
            assert callable(entry.streaming.result)
            assert json.loads(entry.streaming.close_message)
            assert json.loads(entry.streaming.keepalive_message)

    def test_the_deepgram_live_url_declares_the_audio_it_is_about_to_send(self):
        """There is no container to read the PCM's shape out of — the URL says it, or the server
        transcribes the samples as whatever it assumed."""
        entry = providers.STT_PROVIDERS["deepgram"]
        s = {"cloud_endpoint": "", "endpoint": "", "stt_model": "nova-2", "language": "ru", "stream_rate": 16000}
        url = entry.streaming.url(entry.streaming, entry, s)
        assert url.startswith("wss://api.deepgram.com/v1/listen?")
        for expected in ("model=nova-2", "language=ru", "encoding=linear16", "sample_rate=16000", "channels=1"):
            assert expected in url
        assert "interim_results=true" in url and "smart_format=true" in url
        assert entry.streaming.headers("dg-secret") == {"Authorization": "Token dg-secret"}

    def test_a_self_hosted_endpoint_becomes_a_websocket_of_the_same_scheme(self):
        """The batch host is an https URL and a self-hosted one is whatever the operator wrote;
        both name the same server, and guessing the scheme wrong is a socket that never opens."""
        assert providers.websocket_scheme("https://deepgram.internal") == "wss://deepgram.internal"
        assert providers.websocket_scheme("http://127.0.0.1:8355") == "ws://127.0.0.1:8355"
        assert providers.websocket_scheme("wss://already.ws") == "wss://already.ws"
        entry = providers.STT_PROVIDERS["deepgram"]
        s = {"cloud_endpoint": "http://127.0.0.1:9999", "stt_model": "nova-2", "language": "en", "stream_rate": 16000}
        assert entry.streaming.url(entry.streaming, entry, s).startswith("ws://127.0.0.1:9999/v1/listen?")

    def test_the_live_parser_reads_the_streaming_nesting_not_the_batch_one(self):
        """One level shallower than the pre-recorded response, which is exactly the kind of drift a
        shared parser would hide until a user's dictation went quiet."""
        entry = providers.STT_PROVIDERS["deepgram"]
        message = {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": " Привет "}]}}
        assert entry.streaming.result(message) == providers.StreamResult("Привет", True)
        interim = dict(message, is_final=False)
        assert entry.streaming.result(interim).is_final is False

    @pytest.mark.parametrize(
        "message",
        [
            {"type": "Metadata", "duration": 1.5},
            {"type": "SpeechStarted"},
            {"type": "UtteranceEnd", "last_word_end": 2.0},
            {"type": "Results", "channel": {}},
            {"type": "Results", "channel": {"alternatives": []}},
            {"err_code": "INVALID_AUTH"},
            ["not", "a", "message"],
            None,
        ],
    )
    def test_everything_that_is_not_a_transcript_reads_as_none(self, message):
        """A live socket says a great deal besides words, and every one of those must not be
        appended to somebody's dictation."""
        assert providers.STT_PROVIDERS["deepgram"].streaming.result(message) is None

    def test_a_silent_span_is_an_empty_final_and_not_a_failure(self):
        entry = providers.STT_PROVIDERS["deepgram"]
        empty = {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": ""}]}}
        assert entry.streaming.result(empty) == providers.StreamResult("", True)


class TestTheStreamingVariantValidatesItself:
    """The same import-time bar the batch rows meet: a broken variant is a socket that will not
    open or a message nobody parses, in a path whose failure mode is a quiet degrade."""

    def _variant(self, **overrides) -> providers.SttStreaming:
        base = providers.DEEPGRAM_STREAMING
        fields = {
            "url": base.url,
            "headers": base.headers,
            "result": base.result,
            "close_message": base.close_message,
            "keepalive_message": base.keepalive_message,
        }
        fields.update(overrides)
        return providers.SttStreaming(**fields)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"url": "not callable"},
            {"headers": None},
            {"result": 42},
            {"close_message": "not json"},
            {"keepalive_message": ""},
        ],
    )
    def test_a_broken_variant_is_refused_at_import(self, monkeypatch, overrides):
        table = dict(providers.STT_PROVIDERS)
        table["deepgram"] = _replace_streaming(table["deepgram"], self._variant(**overrides))
        monkeypatch.setattr(providers, "STT_PROVIDERS", table)
        with pytest.raises(ValueError):
            providers._validate_registry()

    def test_a_variant_that_is_absent_is_not_an_error(self, monkeypatch):
        """Most providers have none, and that is a row like any other — not a broken one."""
        table = dict(providers.STT_PROVIDERS)
        table["deepgram"] = _replace_streaming(table["deepgram"], None)
        monkeypatch.setattr(providers, "STT_PROVIDERS", table)
        providers._validate_registry()


def _replace_streaming(entry: providers.SttProvider, streaming) -> providers.SttProvider:
    return providers.SttProvider(
        name=entry.name,
        default_model=entry.default_model,
        default_host=entry.default_host,
        key_env_fallbacks=entry.key_env_fallbacks,
        build=entry.build,
        transcript=entry.transcript,
        error_summary=entry.error_summary,
        comparison=entry.comparison,
        streaming=streaming,
    )


class TestTheStreamingVariantIsDocumented:
    """PROVIDERS.md is the human half of the registry, and it drifts the moment nothing checks."""

    def test_every_provider_says_whether_it_streams(self):
        doc = (Path(__file__).resolve().parents[1] / "PROVIDERS.md").read_text(encoding="utf-8")
        assert "`stt.cloud.streaming`" in doc
        for name, entry in providers.STT_PROVIDERS.items():
            if entry.streaming is not None:
                assert f"`{name}` (STT) | **yes**" in doc, f"{name} streams but the table does not say so"

    def test_and_nothing_claims_to_stream_that_does_not(self):
        """The other direction, the LOG_RULES way. A row promising a live socket that the registry
        cannot open is worse than a missing row: the first sends a user to change a setting that
        does nothing, the second only fails to advertise. Both directions or neither."""
        doc = (Path(__file__).resolve().parents[1] / "PROVIDERS.md").read_text(encoding="utf-8")
        claimed = set(re.findall(r"`([a-z0-9-]+)` \(STT\) \| \*\*yes\*\*", doc))
        streams = {name for name, entry in providers.STT_PROVIDERS.items() if entry.streaming is not None}
        assert claimed == streams, f"PROVIDERS.md claims {sorted(claimed)} stream; the registry says {sorted(streams)}"


# --- the TTS streaming variant (windowsill#113) --------------------------------------------------


class TestTheTtsStreamingVariantIsAnEntryToo:
    """The voice-back counterpart of #99: a live socket is an axis on the SAME TTS entry, not a
    second table and not a branch."""

    def test_a_provider_without_one_carries_none_rather_than_a_guess(self):
        assert providers.TTS_PROVIDERS["openai"].streaming is None
        assert providers.TTS_PROVIDERS["deepgram"].streaming is None
        assert providers.TTS_PROVIDERS["elevenlabs"].streaming is not None

    def test_the_variant_carries_every_axis_a_socket_needs(self):
        for entry in providers.TTS_PROVIDERS.values():
            if entry.streaming is None:
                continue
            assert callable(entry.streaming.url)
            assert callable(entry.streaming.headers)
            assert callable(entry.streaming.bos)
            assert callable(entry.streaming.text_message)
            assert callable(entry.streaming.result)
            assert json.loads(entry.streaming.flush_message)
            assert json.loads(entry.streaming.keepalive_message)
            assert entry.streaming.default_output_format

    def test_the_elevenlabs_live_url_carries_the_model_and_the_pcm_output_format(self):
        """There is no container coming back — only raw PCM frames — so the format the player must
        handle is declared in the URL (pcm_22050 = raw s16le, no decoder in the critical path)."""
        entry = providers.TTS_PROVIDERS["elevenlabs"]
        s = {"endpoint": "", "voice_id": "vX", "cloud_model": "eleven_flash_v2_5",
             "stream_output_format": "pcm_22050", "voice_settings": None, "speed": 1.0}
        url = entry.streaming.url(entry.streaming, entry, s)
        assert url.startswith("wss://api.elevenlabs.io/v1/text-to-speech/vX/stream-input?")
        assert "model_id=eleven_flash_v2_5" in url
        assert "output_format=pcm_22050" in url
        assert entry.streaming.headers("xi-secret") == {"xi-api-key": "xi-secret"}

    def test_a_self_hosted_endpoint_becomes_a_websocket_of_the_same_scheme(self):
        entry = providers.TTS_PROVIDERS["elevenlabs"]
        s = {"endpoint": "http://127.0.0.1:9999", "voice_id": "v", "cloud_model": "eleven_flash_v2_5",
             "stream_output_format": "pcm_22050", "voice_settings": None, "speed": 1.0}
        assert entry.streaming.url(entry.streaming, entry, s).startswith(
            "ws://127.0.0.1:9999/v1/text-to-speech/v/stream-input?"
        )

    def test_the_bos_carries_voice_settings_and_a_space(self):
        entry = providers.TTS_PROVIDERS["elevenlabs"]
        bos = json.loads(entry.streaming.bos({"voice_settings": {"speed": 0.9, "stability": 0.5}}))
        assert bos["text"] == " "
        assert bos["voice_settings"]["speed"] == 0.9
        bare = json.loads(entry.streaming.bos({"voice_settings": None}))
        assert bare == {"text": " "}

    def test_a_text_frame_carries_the_line_with_a_trailing_separator(self):
        entry = providers.TTS_PROVIDERS["elevenlabs"]
        assert json.loads(entry.streaming.text_message("hello")) == {"text": "hello "}

    def test_the_flush_forces_audio_and_the_keepalive_does_not(self):
        """The distinction that lets a held socket sit idle: flush emits audio per line, keepalive
        is a whitespace text frame (no flush) that only resets the vendor's idle close."""
        entry = providers.TTS_PROVIDERS["elevenlabs"]
        assert json.loads(entry.streaming.flush_message) == {"flush": True}
        assert "flush" not in json.loads(entry.streaming.keepalive_message)

    def test_the_live_parser_reads_audio_fragments_and_the_final_marker(self):
        entry = providers.TTS_PROVIDERS["elevenlabs"]
        assert entry.streaming.result({"audio": "AAEC", "isFinal": False}) == providers.StreamAudio("AAEC", False)
        # the terminal marker (audio null, isFinal true) is NOT an error — the holder waits on it
        assert entry.streaming.result({"isFinal": True}) == providers.StreamAudio("", True)
        assert entry.streaming.result({"audio": None, "isFinal": True}) == providers.StreamAudio("", True)

    @pytest.mark.parametrize(
        "message",
        [
            {"type": "Metadata"},
            {"detail": "quota_exceeded"},
            {"audio": 123, "isFinal": False},  # a non-string audio is not a fragment
            ["not", "a", "dict"],
            None,
            {},
        ],
    )
    def test_everything_that_is_not_audio_reads_as_none(self, message):
        assert providers.TTS_PROVIDERS["elevenlabs"].streaming.result(message) is None


class TestTheTtsStreamingVariantValidatesItself:
    """Same import-time bar as the batch rows and the STT variant: a broken TTS variant is a socket
    that will not open or a frame nobody parses, in a path whose failure mode is a quiet degrade."""

    def _variant(self, **overrides) -> providers.TtsStreaming:
        base = providers.ELEVENLABS_STREAMING
        fields = {
            "url": base.url,
            "headers": base.headers,
            "bos": base.bos,
            "text_message": base.text_message,
            "flush_message": base.flush_message,
            "result": base.result,
            "keepalive_message": base.keepalive_message,
            "default_output_format": base.default_output_format,
        }
        fields.update(overrides)
        return providers.TtsStreaming(**fields)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"url": "not callable"},
            {"headers": None},
            {"bos": 42},
            {"text_message": "not callable"},
            {"result": "x"},
            {"flush_message": "not json"},
            {"keepalive_message": ""},
            {"default_output_format": ""},
        ],
    )
    def test_a_broken_variant_is_refused_at_import(self, monkeypatch, overrides):
        table = dict(providers.TTS_PROVIDERS)
        table["elevenlabs"] = _replace_tts_streaming(table["elevenlabs"], self._variant(**overrides))
        monkeypatch.setattr(providers, "TTS_PROVIDERS", table)
        with pytest.raises(ValueError):
            providers._validate_registry()

    def test_a_variant_that_is_absent_is_not_an_error(self, monkeypatch):
        table = dict(providers.TTS_PROVIDERS)
        table["elevenlabs"] = _replace_tts_streaming(table["elevenlabs"], None)
        monkeypatch.setattr(providers, "TTS_PROVIDERS", table)
        providers._validate_registry()


def _replace_tts_streaming(entry: providers.TtsProvider, streaming) -> providers.TtsProvider:
    return providers.TtsProvider(
        name=entry.name,
        default_model=entry.default_model,
        default_host=entry.default_host,
        default_output_format=entry.default_output_format,
        build=entry.build,
        comparison=entry.comparison,
        streaming=streaming,
    )


class TestTheTtsStreamingVariantIsDocumented:
    """PROVIDERS.md drifts the moment nothing checks — the TTS side gets the same bidirectional
    guard the STT side has."""

    def test_every_provider_says_whether_it_streams(self):
        doc = (Path(__file__).resolve().parents[1] / "PROVIDERS.md").read_text(encoding="utf-8")
        assert "`tts.cloud.streaming`" in doc
        for name, entry in providers.TTS_PROVIDERS.items():
            if entry.streaming is not None:
                assert f"`{name}` (TTS) | **yes**" in doc, f"{name} streams but the table does not say so"

    def test_and_nothing_claims_to_stream_that_does_not(self):
        doc = (Path(__file__).resolve().parents[1] / "PROVIDERS.md").read_text(encoding="utf-8")
        claimed = set(re.findall(r"`([a-z0-9-]+)` \(TTS\) \| \*\*yes\*\*", doc))
        streams = {name for name, entry in providers.TTS_PROVIDERS.items() if entry.streaming is not None}
        assert claimed == streams, f"PROVIDERS.md claims {sorted(claimed)} stream; the registry says {sorted(streams)}"
