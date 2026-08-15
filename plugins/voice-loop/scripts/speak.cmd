@echo off
rem voice-loop speaking-hook launcher (Windows native).
rem Probe interpreters by running them: python3 may be the Microsoft Store stub.
setlocal
set "VLPY="
py -3 -c "import sys" >nul 2>nul && set "VLPY=py -3"
if not defined VLPY python -c "import sys" >nul 2>nul && set "VLPY=python"
if not defined VLPY python3 -c "import sys" >nul 2>nul && set "VLPY=python3"
if not defined VLPY exit /b 0
%VLPY% "%~dp0speak.py"
