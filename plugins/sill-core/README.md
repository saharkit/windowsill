# sill-core

Shared core modules for windowsill plugins. The first module shipped is the **assistant state
store** — a plugin-agnostic, schema-versioned, atomic-write JSON persistence layer that keeps
assistant memory (reminders, throttles, checkpoints) separate from user configuration.

## assistant state store

One JSON file per plugin under `$XDG_STATE_HOME/<plugin>/assistant-state.json`:

- **Schema versioned** — `schema` key stamped on every write; unknown keys preserved (forward compat).
- **Atomic writes** — temp file, `fsync`, `os.replace`; a crash or `kill -9` mid-write leaves old
  state or new, never a truncated partial file.
- **Content never stored** — only flags, counters and timestamps. The text of a reminder or the body
  of a diagnostic lives in code, not on disk.
- **Injected clock** — timestamps come through an injected `Callable[[], datetime]`, defaulting to
  `datetime.now(UTC)`, so tests can pin time without sleeping.

### remind-once etiquette

```python
from sill_core.assistant_state import AssistantState

store = AssistantState("voice-loop")

if store.reminder_should_show("paste-auto"):
    show_reminder("You chose manual paste — want to switch to auto?")
    store.reminder_mark_shown("paste-auto")

# ... weeks later, user explicitly asks ...
store.reminder_unmute("paste-auto")
```

A reminder is shown **once** and then muted. It stays muted until the user explicitly requests
it be re-allowed — never automatically.

### generic merge for other consumers

```python
# /doctor throttle counters
store.merge({"doctor": {"throttle_count": 3}})

# install lifecycle checkpoints (#48)
store.merge({"step_ledger": {"install": "checkpoint-done"}})
```

Each consumer owns a top-level namespace in the state dict.

## Layout

```
plugins/sill-core/
  .claude-plugin/plugin.json   plugin manifest (version lives here)
  README.md                    this file
  sill_core/                   the Python package
    __init__.py
    assistant_state.py         the state store
  tests/                       its own suite, invoked from here
    __init__.py
    test_assistant_state.py
  pytest.ini                   runner config (paths relative to this dir)
  .coveragerc                  coverage gate config
```

## Tests

No network, no models, no hardware. Run from the plugin directory:

```sh
cd plugins/sill-core
pytest --cov=sill_core --cov-report=term-missing --cov-fail-under=100
```

## License

MIT — see the root [LICENSE](../../LICENSE).
