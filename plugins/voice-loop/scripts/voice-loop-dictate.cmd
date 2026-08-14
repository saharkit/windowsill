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
where python3 >nul 2>nul
if errorlevel 1 exit /b 0
python3 "%~dp0voice-loop-dictate" %*
