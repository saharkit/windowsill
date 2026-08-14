@echo off
rem voice-loop — durable dictation launcher (Windows native).
rem
rem Bash analog of voice-loop-dictate (the Python script the Linux desktop binds to).
rem Each invocation re-resolves Claude Code's installed-plugins registry so a plugin update
rem never strands the bound hotkey with a stale path (#151 prior art — the version-scoped
rem path that was bound silently died on every plugin update). On Windows the durable
rem surface is a .cmd shim beside the Python script; a global-hotkey helper (AutoHotkey,
rem PowerShell + WshHotKey, etc.) is what fires this file.
rem
rem Args are passed through verbatim to voice-loop-dictate (the .py beside it), which then
rem execs the current dictate.py.
rem
rem INTERPRETER RESOLUTION (see doctor.py, "python3_is_store_stub"): the python.org Windows
rem installer ships python.exe and NOT python3.exe, and a python3 found under WindowsApps is
rem the Microsoft Store stub — a real exe that opens the Store instead of running Python. So
rem the shim probes in order (py -3, python, python3) and picks the first that actually runs
rem `import sys`, not the first name that merely resolves.
setlocal
set "VLPY="
py -3 -c "import sys" >nul 2>nul && set "VLPY=py -3"
if not defined VLPY python -c "import sys" >nul 2>nul && set "VLPY=python"
if not defined VLPY python3 -c "import sys" >nul 2>nul && set "VLPY=python3"
if not defined VLPY exit /b 0
%VLPY% "%~dp0voice-loop-dictate" %*
