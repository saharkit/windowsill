@echo off
rem voice-loop — push-to-talk toggle LAUNCHER (Windows native).
rem
rem Bash analog of dictate-toggle.sh: a thin wrapper that runs dict.py through Python. The
rem hotkey binding on Windows should target this file (the durable version is one file over —
rem voice-loop-dictate.cmd — and queries Claude Code's registry on each invocation, so a plugin
rem update never strands a bound hotkey with the wrong path, the #151 prior art).
rem
rem Bind it to a global hotkey (Windows-key combinations reach here via the OS shortcut UI;
rem arbitrary key combos need a small helper like AutoHotkey — this file is the target the
rem helper launches). Hold the key is not a stream of toggles: dictate.py ignores a re-fire
rem within dictate.debounce_ms (750 ms by default) of the previous one, and each ignored fire
rem restarts that window — so a key held down is ONE toggle however long it is held.
rem
rem A node that has no python3 (or a missing dictate.py) does nothing rather than
rem half-recording — the same fail-silent contract the bash launcher keeps.
where python3 >nul 2>nul
if errorlevel 1 exit /b 0
python3 "%~dp0dictate.py" %*
