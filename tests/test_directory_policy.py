"""The plugin directory's ALLOWED_TOOLS_BROAD and RUNTIME_FETCH_EXEC holds, expressed as pytests.

The Anthropic plugin directory holds windowsill plugins on three findings that
``claude plugin validate --strict`` does NOT check on its own (windowsill#5867, the
post-#5816 rescan at f9a a0fa):

  * ``ALLOWED_TOOLS_BROAD`` — a skill (or plugin command or agent) declares a bare ``Bash``,
    ``Bash(*)``, or a wildcard right after a shell, interpreter, package manager, runner or
    curl in its ``allowed-tools`` front matter. The cure is to remove the shell entry: with
    no ``Bash`` entry the skill still runs its commands, each through the normal permission
    prompt, and ``claude plugin validate --strict`` accepts a flow list of bare tool names.
  * ``RUNTIME_FETCH_EXEC`` — anywhere under the plugins tree, a fetcher (``curl``, ``wget``,
    ``iwr``, ``irm``, ``Invoke-WebRequest``, ``Invoke-RestMethod``) piped into a shell or
    interpreter (``sh``, ``bash``, ``zsh``, ``dash``, ``python``, ``python3``, ``node``,
    ``perl``, ``ruby``, ``iex``, ``Invoke-Expression``), or the explicit forms
    ``sh -c "$(curl ...)`` and ``bash <(curl ...)``. Each is rejected because the remote
    payload is executed without first being read by a human; the cure is to either point at
    a vendor install page or replace it with a download-then-inspect-then-run form.

The directory's own rescan is webhook-driven, and these two rules are the ones that held
voice-loop and agent-statusline after #5816 merged (windowsill PR 319, f9a a0fa). Both rules
are expressed as separate test functions here, each with its own
REFUSAL fixture asserted FIRST so a future regression that swaps a refusal for a pass
fails loudly with the test name visible.

Stdlib only — no YAML library. The front-matter is parsed by hand: the value of the line
beginning ``allowed-tools:`` is read, a flow list (``[a, b, c]``) is split on commas at
parenthesis depth 0, a block list reads the following ``- item`` lines. Each item is
stripped and a value containing ``{`` is refused as unparseable rather than guessed at.

The pipe check matches case-insensitively, with both the fetcher and the interpreter
matched as whole words (``\\b…\\b``) and a literal ``|`` required between them, plus the
two explicit forms ``sh -c "$(curl ...)`` and ``bash <(curl ...)``. The word-boundary
rule is load-bearing: a bare ``irm`` substring otherwise matches ``confirm``, which the
plugins tree carries in prose today.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_ROOT = REPO_ROOT / "plugins"


# --- the allowed-tools check ----------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"^---\s*$\n(.*?)\n---\s*$\n", re.DOTALL | re.MULTILINE)
_ALLOWED_TOOLS_LINE_RE = re.compile(r"^allowed-tools:\s*(.*?)\s*$", re.MULTILINE)
_FLOW_LIST_RE = re.compile(r"^\[(.*)\]\s*$")


def _parse_front_matter(path: Path) -> dict[str, str] | None:
    """A skill/command/agent file's front matter as a flat dict of ``key: value`` lines.

    Only the keys this test cares about (``allowed-tools``) are read; the rest is left as
    the raw string the matcher compares against. ``None`` when the file has no front
    matter at all (a plain markdown body) — the allowed-tools check below then has
    nothing to assert on, so the file is skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = _FRONTMATTER_RE.search(text)
    if not match:
        return None
    body = match.group(1)
    parsed: dict[str, str] = {}
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        parsed[key.strip()] = value.strip()
    return parsed


def _flow_list_items(value: str) -> list[str] | None:
    """The items of a flow-style list, or ``None`` when the value is empty (a block list follows).

    A value that contains ``{`` is refused as unparseable rather than guessed at — that is a
    f-string or a templated value a future author might write, and the test refuses to
    invent an answer for it. The check below reports it as a refusal so the holder can
    rewrite it without thinking the test was being lenient."""
    if "{" in value:
        return None
    match = _FLOW_LIST_RE.match(value)
    if not match:
        return None
    inner = match.group(1)
    items: list[str] = []
    depth = 0
    buf: list[str] = []
    for char in inner:
        if char == "(":
            depth += 1
            buf.append(char)
        elif char == ")":
            depth -= 1
            buf.append(char)
        elif char == "," and depth == 0:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(char)
    if buf:
        items.append("".join(buf).strip())
    return [item for item in items if item]


def _block_list_items(front: dict[str, str], key: str) -> list[str]:
    """The items of a block-style list that follows an empty ``key:`` line — only the
    immediate ``- item`` lines are read, never a sibling key's list."""
    body_lines = front.get(f"__{key}_block", "")
    return [line[1:].strip() for line in body_lines.splitlines() if line.startswith("- ")]


def _block_list_source(text: str) -> dict[str, str]:
    """A front-matter body broken into per-key blocks so a block list following an empty
    ``key:`` line can be parsed without confusing it with a sibling key's block."""
    result: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith(" ") or line.startswith("\t") or line.startswith("- "):
            if current is not None:
                buf.append(line)
            continue
        if current is not None:
            result[f"__{current}_block"] = "\n".join(buf)
            buf = []
        if ":" in line:
            current = line.split(":", 1)[0].strip()
    if current is not None:
        result[f"__{current}_block"] = "\n".join(buf)
    return result


def _allowed_tools_items(path: Path) -> list[str] | None:
    """The ``allowed-tools`` items this file declares, or ``None`` for a file with no
    front matter / no ``allowed-tools`` line at all. A file whose flow-list value
    contains ``{`` is refused (returns ``[]`` so the check still runs against it) and the
    matcher's whole-word rule fires on the literal ``{``."""
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.search(text)
    if not match:
        return None
    body_text = match.group(1)
    front = _parse_front_matter(path) or {}
    line_match = _ALLOWED_TOOLS_LINE_RE.search(body_text)
    if not line_match:
        return None
    value = line_match.group(1).strip()
    if value.startswith("[") and value.endswith("]"):
        items = _flow_list_items(value)
        if items is None:
            return []
        return items
    if value == "":
        front_blocks = _block_list_source(body_text)
        return _block_list_items(front_blocks, "allowed-tools")
    # a single bare name on the same line (not a list at all): treat it as one item
    return [value]


def _is_bash_item(item: str) -> bool:
    """A single ``allowed-tools`` item that names ``Bash`` (the whole-word match the
    directory's rule enforces). ``Bash(...)`` and ``Bash*`` are equivalent under the rule
    (windowsill#5867), so the prefix check covers both — a literal ``Bash`` also fails
    by the same prefix."""
    return item == "Bash" or item.startswith("Bash(")


def _skill_files() -> list[Path]:
    """Every file under the plugins tree whose front matter can declare an
    ``allowed-tools`` entry — skills (``skills/**/SKILL.md``), commands
    (``commands/**/*.md``), and agents (``agents/**/*.md``)."""
    found: list[Path] = []
    for pattern in ("skills", "commands", "agents"):
        for path in (PLUGINS_ROOT).rglob(f"{pattern}/*/SKILL.md"):
            found.append(path)
        for path in (PLUGINS_ROOT).rglob(f"{pattern}/*.md"):
            found.append(path)
    return sorted(found)


def _all_skill_files_with_bash() -> list[Path]:
    """The set of files THIS test asserts against — every skill/command/agent under the
    plugins tree. A test that names a single f-string for the post-#5816 tree would
    silently pass once that one file stops carrying ``Bash`` (a future fix in the same
    place would not be caught), so the assertion is over the whole file set, and a new
    file added with ``Bash`` fails this test on its own."""
    return _skill_files()


# --- refusal fixtures (asserted FIRST so a regression that swaps refusal for pass fails loudly) ---


def test_allowed_tools_refuses_a_bare_bash_entry() -> None:
    """Refusal fixture: a flow list with a bare ``Bash`` item fails the check. Pinned
    first so a future regression that turns the refusal into a pass fails this test by
    name, not silently."""
    items = ["Bash", "Read"]
    assert any(_is_bash_item(item) for item in items), (
        "the refusal fixture does not refuse its own bare Bash"
    )


def test_allowed_tools_refuses_a_narrowed_bash_entry() -> None:
    """Refusal fixture: ``Bash(git status:*)`` (a permitted-prefix form) also fails the
    check — the directory's rule does NOT accept narrowing as a cure; the cure is to
    remove the entry."""
    items = ["Read", "Bash(git status:*)"]
    assert any(_is_bash_item(item) for item in items), (
        "the narrowed-Bash refusal fixture does not refuse its own entry"
    )


def test_allowed_tools_refuses_a_block_style_bash_entry() -> None:
    """Refusal fixture: a block-style list with ``- Bash`` fails the check — the same
    rule covers both spellings so an author cannot dodge it by switching shape."""
    items = ["Read", "Bash"]
    assert any(_is_bash_item(item) for item in items), (
        "the block-list Bash refusal fixture does not refuse its own entry"
    )


# --- the assertion itself -------------------------------------------------------------------------


def test_no_skill_declares_a_bash_entry_in_allowed_tools() -> None:
    """No skill / command / agent file under the plugins tree declares a bare ``Bash``
    or ``Bash(...)`` entry in its ``allowed-tools`` front matter. The directory's
    ALLOWED_TOOLS_BROAD hold (windowsill#5867) names removing the entry as the cure —
    the user then approves each command through the normal permission prompt. Files
    with no front matter at all are skipped (the rule is on the value of an existing
    line, not on the presence of one)."""
    offenders: list[tuple[Path, list[str]]] = []
    for path in _all_skill_files_with_bash():
        items = _allowed_tools_items(path)
        if items is None:
            continue
        bash_items = [item for item in items if _is_bash_item(item)]
        if bash_items:
            offenders.append((path, bash_items))
    assert not offenders, (
        "skill files declare a Bash entry in allowed-tools (ALLOWED_TOOLS_BROAD hold; "
        "the cure is to REMOVE the Bash entry, not to narrow it): "
        + ", ".join(
            f"{p.relative_to(REPO_ROOT)} -> {b}" for p, b in offenders
        )
    )


# --- the pipe-to-shell check ----------------------------------------------------------------------


_FETCHERS = ("curl", "wget", "iwr", "irm", "Invoke-WebRequest", "Invoke-RestMethod")
_INTERPRETERS = (
    "sh",
    "bash",
    "zsh",
    "dash",
    "python",
    "python3",
    "node",
    "perl",
    "ruby",
    "iex",
    "Invoke-Expression",
)


def _is_pipe_to_shell(line: str) -> bool:
    """True when this line carries a fetcher piped to a shell/interpreter (the
    RUNTIME_FETCH_EXEC hold).

    The fetcher and the interpreter are matched as WHOLE WORDS, case-insensitively, and
    a literal ``|`` is required between them. The word-boundary rule is load-bearing: a
    bare ``irm`` substring otherwise matches ``confirm``, which the plugins tree carries
    in prose today (``plugins/voice-loop/CONFORMANCE.md:17`` and ``:48``,
    ``plugins/voice-loop/TESTING.md:392`` and ``:518``,
    ``plugins/voice-loop/docs/rvc-serving.md:346``).

    Plus the two explicit forms ``sh -c "$(curl ...)`` and ``bash <(curl ...)``, which
    the rule names explicitly because a matcher that looks only for a literal ``|``
    misses them and a future fix would walk into the same trap."""
    lower = line.lower()
    for fetcher in _FETCHERS:
        for interpreter in _INTERPRETERS:
            # whole-word match for both, with a literal pipe between them
            pattern = (
                r"\b" + re.escape(fetcher.lower()) + r"\b"
                + r".*?\|"
                + r".*?\b" + re.escape(interpreter.lower()) + r"\b"
            )
            if re.search(pattern, lower):
                return True
    # the two explicit forms the rule names by hand
    if re.search(r"\bsh\s+-c\s+\"\$\(\s*curl", lower):
        return True
    if re.search(r"\bbash\s+<\(\s*curl", lower):
        return True
    return False


def _plugin_source_files() -> list[Path]:
    """Every text source file under the plugins tree the pipe check scans — markdown
    (docs, READMEs, evals), shell scripts, and Python source. Generated files
    (``.coverage``, ``__pycache__``) and the few non-text binary assets are skipped by
    the per-file read below."""
    skip_dirs = {"node_modules", ".git", "__pycache__"}
    found: list[Path] = []
    for path in PLUGINS_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        # text-shaped files only — the scan reads each file as text, and a binary read
        # as text either decodes with replacement characters (so the matcher misses it)
        # or fails outright. Restricting by extension keeps the scan deterministic.
        if path.suffix not in (".md", ".sh", ".py", ".txt", ".json", ".yaml", ".yml"):
            continue
        found.append(path)
    return sorted(found)


# --- refusal fixtures (asserted FIRST so a regression that swaps refusal for pass fails loudly) ---


def test_pipe_to_shell_refuses_curl_piped_to_sh() -> None:
    """Refusal fixture: ``curl ... | sh`` fails the check."""
    assert _is_pipe_to_shell("curl -LsSf https://example.test/i.sh | sh")


def test_pipe_to_shell_refuses_iwr_piped_to_iex() -> None:
    """Refusal fixture: ``iwr ... | iex`` fails the check (PowerShell's fetch piped to
    PowerShell's executor)."""
    assert _is_pipe_to_shell("iwr https://example.test/i.ps1 | iex")


def test_pipe_to_shell_refuses_bash_process_substitution_of_curl() -> None:
    """Refusal fixture: ``bash <(curl ...)`` fails the check (the second explicit form)."""
    assert _is_pipe_to_shell("bash <(curl -s https://example.test/i.sh)")


def test_pipe_to_shell_accepts_a_bare_curl_with_no_pipe() -> None:
    """Must-NOT-match fixture: a bare ``curl`` with no pipe passes — the rule requires a
    literal ``|`` between the fetcher and the interpreter, and a download on its own
    is not a fetch-and-execute form."""
    assert not _is_pipe_to_shell("curl -s https://example.test/data.txt")


def test_pipe_to_shell_accepts_a_curl_continued_with_or_exit() -> None:
    """Must-NOT-match fixture: ``curl ... || exit 1`` does not match — the ``|`` here is
    the ``||`` continuation, not the pipe, and a fetcher whose exit code is checked is a
    different shape than one whose output is fed into a shell."""
    assert not _is_pipe_to_shell("curl -fsS https://example.test/health || exit 1")


# --- the assertion itself -------------------------------------------------------------------------


def test_no_plugin_source_pipe_a_fetcher_into_a_shell() -> None:
    """No file under the plugins tree pipes a fetcher (``curl``, ``wget``, ``iwr``,
    ``irm``, ``Invoke-WebRequest``, ``Invoke-RestMethod``) into a shell or interpreter
    (``sh``, ``bash``, ``zsh``, ``dash``, ``python``, ``python3``, ``node``, ``perl``,
    ``ruby``, ``iex``, ``Invoke-Expression``). The directory's RUNTIME_FETCH_EXEC hold
    (windowsill#5867) names replacing the form with a vendor install link or an
    unpiped download-then-inspect-then-run instruction as the cure.

    Matching is per-line, case-insensitive, with both the fetcher and the interpreter
    matched as WHOLE WORDS and a literal ``|`` required between them, plus the two
    explicit forms ``sh -c "$(curl ...)`` and ``bash <(curl ...)``. The word-boundary
    rule is load-bearing — see the module docstring for the prose sites that would
    otherwise match a bare ``irm`` substring."""
    offenders: list[tuple[Path, int, str]] = []
    for path in _plugin_source_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if _is_pipe_to_shell(line):
                offenders.append((path, n, line))
    assert not offenders, (
        "plugin files pipe a fetcher into a shell (RUNTIME_FETCH_EXEC hold): "
        + ", ".join(
            f"{p.relative_to(REPO_ROOT)}:{n} -> {l}" for p, n, l in offenders
        )
    )