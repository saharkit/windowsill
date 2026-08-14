@echo off
rem voice-loop — push-to-talk toggle LAUNCHER (Windows native).
rem
rem Bash analog of dictate-toggle.sh: a thin wrapper that runs dictate.py through Python. The
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
rem INTERPRETER RESOLUTION (see doctor.py, "python3_is_store_stub"): the python.org Windows
rem installer ships python.exe and NOT python3.exe, and a python3 found under WindowsApps is
rem the Microsoft Store stub — a real exe that opens the Store instead of running Python. So
rem the shim probes in order (py -3, python, python3) and picks the first that actually runs
rem `import sys`, not the first name that merely resolves.
rem
rem A node where no probe answers does nothing rather than half-recording — the same
rem fail-silent contract the bash launcher keeps.
setlocal
set "VLPY="
py -3 -c "import sys" >nul 2>nul && set "VLPY=py -3"
if not defined VLPY python -c "import sys" >nul 2>nul && set "VLPY=python"
if not defined VLPY python3 -c "import sys" >nul 2>nul && set "VLPY=python3"
if not defined VLPY exit /b 0
%VLPY% "%~dp0dictate.py" %*
