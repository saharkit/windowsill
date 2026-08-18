"""preview.py — the live dictation preview overlay renderer (windowsill#115).

The dictation-side preview wiring (the ``dictate.preview`` setting key, ``_write_preview`` /
``_clear_preview``, the ``on_interim`` seam, the ``_start_preview`` Popen spawn) is covered in
``tests/test_dictate.py``. This file covers the renderer side — the script that is spawned as a
detached child to actually paint the overlay window.

Stdlib-only ``tkinter`` GUI. Three documented degrade paths return 0 silently: no ``tkinter`` at
all, no display surface (``TclError`` on ``Tk()``), no state-file path on argv. The polling loop,
when reached, reads the JSON state file the worker writes and updates the two labels; on a
missing or unreadable state file the overlay calls ``root.destroy()`` and exits so it does not
orphan itself on screen.

The window-paint, label-layout and event-loop behaviours are display concerns — they are reached
through real invocation in the loopback job (where a real ``Xvfb`` display is available), not unit
tests, because the headless suite cannot exercise a real display surface.
"""

from __future__ import annotations

import importlib.util
import json
import runpy
import sys
import types
from pathlib import Path

import pytest

_PREVIEW_PATH = Path(__file__).resolve().parents[1] / "scripts" / "preview.py"
_preview_spec = importlib.util.spec_from_file_location("_preview_under_test", _PREVIEW_PATH)
preview = importlib.util.module_from_spec(_preview_spec)
_preview_spec.loader.exec_module(preview)


# --- the tkinter fake -----------------------------------------------------------------------------


class _FakeRoot:
    """Records the window properties ``preview.py`` configures on ``Tk()`` and the ``after``
    callbacks ``poll()`` schedules. ``mainloop()`` is a no-op so ``main()`` returns, leaving the
    most recent ``after`` callable (which is the ``poll`` closure) reachable from the test.
    """

    def __init__(self) -> None:
        self.title_value = ""
        self.overrideredirect_value: bool | None = None
        self.attributes_calls: list[tuple[object, ...]] = []
        self.bg = ""
        self.alpha: float | None = None
        self.geometry_value = ""
        self.screenwidth = 1920
        self.screenheight = 1080
        self.after_calls: list[tuple[int, object]] = []
        self.mainloop_called = False
        self.destroy_called = False

    def title(self, t: str) -> None:
        self.title_value = t

    def overrideredirect(self, value: bool) -> None:
        self.overrideredirect_value = value

    def attributes(self, *args: object, **kwargs: object) -> None:
        self.attributes_calls.append(args)
        if args and args[0] == "-alpha":
            self.alpha = args[1]

    def configure(self, bg: str | None = None, **kwargs: object) -> None:
        if bg is not None:
            self.bg = bg

    def winfo_screenwidth(self) -> int:
        return self.screenwidth

    def winfo_screenheight(self) -> int:
        return self.screenheight

    def geometry(self, value: str) -> None:
        self.geometry_value = value

    def after(self, ms: int, func: object) -> str:
        self.after_calls.append((ms, func))
        return f"after_id_{len(self.after_calls)}"

    def mainloop(self) -> None:
        self.mainloop_called = True

    def destroy(self) -> None:
        self.destroy_called = True


class _FakeLabel:
    """Records ``pack()`` and ``config(text=...)`` calls. Two of these are created per run —
    the assembled-finals label and the interim-guess label — and the tests assert against their
    text + the order they were packed into their master.
    """

    def __init__(self, master: object, **kwargs: object) -> None:
        self.master = master
        self.kwargs = dict(kwargs)
        self._text = str(kwargs.get("text", ""))
        self.config_calls: list[dict[str, object]] = []
        self.packed = False

    def pack(self, **kwargs: object) -> None:
        self.packed = True

    def cget(self, key: str) -> object:
        if key == "text":
            return self._text
        return self.kwargs.get(key)

    def config(self, **kwargs: object) -> None:
        self.config_calls.append(dict(kwargs))
        if "text" in kwargs:
            self._text = str(kwargs["text"])

    @property
    def text(self) -> str:
        return self._text


class _FakeTkinter(types.ModuleType):
    """A tkinter-shaped module. ``TclError`` is an exception class; ``Tk()`` returns a fresh
    ``_FakeRoot``; ``Label(master, **kw)`` returns a ``_FakeLabel`` registered against its master.
    """

    def __init__(self) -> None:
        super().__init__("tkinter")
        self.TclError = type("TclError", (Exception,), {})
        self.roots: list[_FakeRoot] = []
        self.labels: list[_FakeLabel] = []

    def Tk(self) -> _FakeRoot:
        root = _FakeRoot()
        self.roots.append(root)
        return root

    def Label(self, master: object, **kwargs: object) -> _FakeLabel:
        label = _FakeLabel(master, **kwargs)
        self.labels.append(label)
        return label


def _install_fake_tkinter(monkeypatch, *, tk_raises: bool = False) -> _FakeTkinter:
    """Install a fresh tkinter fake. If ``tk_raises=True``, ``Tk()`` raises ``TclError`` so the
    "no display" branch in ``main()`` runs."""
    mod = _FakeTkinter()
    if tk_raises:

        def _raise() -> object:
            raise mod.TclError("no display name and no $DISPLAY environment variable")

        mod.Tk = _raise  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "tkinter", mod)
    return mod


# --- contract test for the fake itself (L4: one per Fake*) ----------------------------------------


def test_fake_root_records_window_properties() -> None:
    """The fake promises it records what ``preview.py`` configures on it. If this drifts, every
    test in this file is testing the fake instead of the code."""
    root = _FakeRoot()
    root.title("voice-loop preview")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.88)
    root.configure(bg="#1e1e1e")
    root.geometry("620x80+650+950")
    assert root.title_value == "voice-loop preview"
    assert root.overrideredirect_value is True
    assert root.attributes_calls == [("-topmost", True), ("-alpha", 0.88)]
    assert root.bg == "#1e1e1e"
    assert root.geometry_value == "620x80+650+950"
    assert root.alpha == 0.88


# --- argv handling ---------------------------------------------------------------------------------


def test_main_returns_one_when_state_path_missing() -> None:
    """The contract: argv[1] is the path the worker writes. Without it, ``main()`` returns 1
    immediately. Catches: a regression that lets ``main()`` fall through with an empty path and
    either crash on the first ``open()`` or hang reading from stdin.
    """
    assert preview.main(["preview.py"]) == 1


# --- silent-degrade paths -------------------------------------------------------------------------


def test_main_returns_zero_when_tkinter_is_missing(monkeypatch, import_raises) -> None:
    """The documented degrade: no tkinter → silently off, exit 0. Catches: a regression that
    raises ``ImportError`` instead of catching it, which would crash the dictation session on
    any system where tkinter is not present.
    """
    import_raises("tkinter", ImportError("No module named 'tkinter'"))
    assert preview.main(["preview.py", "/tmp/never-read.json"]) == 0


def test_main_returns_zero_when_display_is_unavailable(monkeypatch) -> None:
    """The documented degrade: no display → silently off, exit 0. Catches: a regression that
    prints a traceback on a headless machine (SSH session, CI runner), breaking dictation
    for the user even though the dictation itself does not need a display.
    """
    fake = _install_fake_tkinter(monkeypatch, tk_raises=True)
    assert preview.main(["preview.py", "/tmp/never-read.json"]) == 0
    # The fake's Tk() raised before any label was created.
    assert fake.labels == []


# --- the polling loop ------------------------------------------------------------------------------


def test_poll_reads_state_file_and_populates_both_labels(tmp_path, monkeypatch) -> None:
    """The first ``poll()`` reads the JSON state file and writes its two keys onto the assembled
    and interim labels in order. Catches: a regression that swallows one of the keys (say, only
    the interim) and silently drops half the overlay.
    """
    fake = _install_fake_tkinter(monkeypatch)
    state = tmp_path / "preview.json"
    state.write_text(json.dumps({"interim": "Hel", "assembled": "Hello world"}), encoding="utf-8")

    preview.main(["preview.py", str(state)])

    assert len(fake.labels) == 2
    assembled, interim = fake.labels
    assert assembled.text == "Hello world"
    assert interim.text == "Hel"
    # The first poll scheduled itself — the captured closure is reachable from the root.
    assert len(fake.roots[0].after_calls) == 1
    assert fake.roots[0].after_calls[0][0] == 100


def test_poll_destroys_root_when_state_file_is_missing(tmp_path, monkeypatch) -> None:
    """On ``FileNotFoundError`` (the worker cleared the file on stop), ``poll()`` destroys the
    root and exits without rescheduling. Catches: an overlay that lingers on screen after
    dictation stops, blocking the user's view.
    """
    fake = _install_fake_tkinter(monkeypatch)
    state = tmp_path / "preview.json"
    state.write_text(json.dumps({"interim": "x", "assembled": "y"}), encoding="utf-8")

    preview.main(["preview.py", str(state)])
    root = fake.roots[0]
    # Drain the first-poll side effects so we can observe the second poll in isolation.
    root.destroy_called = False
    _, poll = root.after_calls[0]
    root.after_calls.clear()

    state.unlink()
    poll()

    assert root.destroy_called is True
    assert root.after_calls == []  # did not reschedule itself


def test_poll_destroys_root_when_state_file_is_corrupt_json(tmp_path, monkeypatch) -> None:
    """On ``ValueError`` from ``json.load`` (worker wrote a truncated or malformed state file),
    ``poll()`` destroys the root and exits. Catches: an overlay that survives a corrupted state
    file and either renders garbage or freezes on the last good values.
    """
    fake = _install_fake_tkinter(monkeypatch)
    state = tmp_path / "preview.json"
    state.write_text("{not json", encoding="utf-8")

    preview.main(["preview.py", str(state)])

    # First poll hit the corrupt JSON: destroyed and never scheduled a second poll.
    assert fake.roots[0].destroy_called is True
    assert fake.roots[0].after_calls == []


def test_poll_reconfigures_label_when_text_changes(tmp_path, monkeypatch) -> None:
    """When the worker's next interim or final differs from what is on screen, ``poll()``
    reconfigures the label. Catches: a regression that compares by reference instead of by
    value and never updates on changed-but-equal-identity strings.
    """
    fake = _install_fake_tkinter(monkeypatch)
    state = tmp_path / "preview.json"
    state.write_text(json.dumps({"interim": "Hel", "assembled": "Hello"}), encoding="utf-8")

    preview.main(["preview.py", str(state)])
    root = fake.roots[0]
    _, poll = root.after_calls[0]
    assembled, interim = fake.labels
    # Drop the noise from the first poll.
    assembled.config_calls.clear()
    interim.config_calls.clear()

    state.write_text(
        json.dumps({"interim": "Hello world", "assembled": "Hello there"}), encoding="utf-8"
    )
    poll()

    assert [c.get("text") for c in assembled.config_calls] == ["Hello there"]
    assert [c.get("text") for c in interim.config_calls] == ["Hello world"]


def test_poll_does_not_reconfigure_label_when_text_is_unchanged(tmp_path, monkeypatch) -> None:
    """When the worker's next write carries the same text (most writes do — the server writes
    after every message, not only transcripts), ``poll()`` skips the ``config()`` call. Catches:
    a regression that unconditionally reconfigures and causes tkinter to flicker the label on
    every message, which is exactly what this guard exists to prevent.
    """
    fake = _install_fake_tkinter(monkeypatch)
    state = tmp_path / "preview.json"
    payload = {"interim": "Hel", "assembled": "Hello"}
    state.write_text(json.dumps(payload), encoding="utf-8")

    preview.main(["preview.py", str(state)])
    root = fake.roots[0]
    _, poll = root.after_calls[0]
    assembled, interim = fake.labels
    assembled.config_calls.clear()
    interim.config_calls.clear()

    poll()  # same content as before

    assert assembled.config_calls == []
    assert interim.config_calls == []


def test_poll_treats_missing_keys_as_empty_string(tmp_path, monkeypatch) -> None:
    """If the state file is well-formed JSON but one of the two keys is absent, ``poll()``
    treats that key as the empty string rather than raising. Catches: a regression that lets a
    ``KeyError`` propagate out of the polling loop and crashes the overlay mid-session.
    """
    fake = _install_fake_tkinter(monkeypatch)
    state = tmp_path / "preview.json"
    state.write_text(json.dumps({"assembled": "Only one key present"}), encoding="utf-8")

    preview.main(["preview.py", str(state)])

    assembled, interim = fake.labels
    assert assembled.text == "Only one key present"
    assert interim.text == ""


def test_window_is_configured_topmost_and_almost_opaque(monkeypatch) -> None:
    """The overlay is documented as always-on-top, non-resizable (``overrideredirect``), with a
    semi-transparent dark background. Catches: a regression that drops ``-topmost`` and lets
    other windows cover the live transcript mid-dictation.
    """
    fake = _install_fake_tkinter(monkeypatch)

    preview.main(["preview.py", "/tmp/never-read.json"])

    root = fake.roots[0]
    assert root.title_value == "voice-loop preview"
    assert root.overrideredirect_value is True
    assert root.attributes_calls[0] == ("-topmost", True)
    assert root.attributes_calls[1] == ("-alpha", 0.88)
    assert root.bg == "#1e1e1e"
    # mainloop was reached — the event loop entered, however briefly in the no-state-file path.
    assert root.mainloop_called is True


def test_alpha_failure_is_best_effort(monkeypatch, tmp_path) -> None:
    """A window manager that rejects alpha transparency must not take the overlay down.

    L1 gap: this is the documented best-effort seam; losing the optional translucency cannot be
    allowed to prevent the preview from polling its state file.
    """
    fake = _install_fake_tkinter(monkeypatch)
    state = tmp_path / "preview.json"
    state.write_text("{}", encoding="utf-8")
    root = _FakeRoot()
    original_attributes = root.attributes

    def attributes(*args, **kwargs):
        if args and args[0] == "-alpha":
            root.attributes_calls.append(args)
            raise fake.TclError("alpha unavailable")
        return original_attributes(*args, **kwargs)

    root.attributes = attributes
    fake.Tk = lambda: root
    assert preview.main(["preview.py", str(state)]) == 0
    assert root.mainloop_called is True
    assert ("-alpha", 0.88) in root.attributes_calls


def test_script_entrypoint_returns_the_missing_path_error(monkeypatch):
    """Executing the file itself still carries the required state-path refusal."""
    monkeypatch.setattr(preview.sys, "argv", ["preview.py"])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(_PREVIEW_PATH), run_name="__main__")

    assert raised.value.code == 1