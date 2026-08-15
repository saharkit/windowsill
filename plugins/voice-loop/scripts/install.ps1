# voice-loop -- Windows native install recipe.
#
# Run from an ADMIN PowerShell (right-click PowerShell -> "Run as administrator").
# This script installs every prerequisite the measured pass (#42) had to discover
# by hand, so a clean Windows guest reaches a working install without a human
# discovering anything the transcript already knew.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# What it installs (in order):
#   1. Python 3.12       -- from python.org (winget is broken on that guest)
#   2. python3.exe        -- python.org ships python.exe but not python3.exe,
#                           and every launcher in this plugin calls python3
#   3. Git for Windows    -- Claude Code needs git on PATH
#   4. Node.js (LTS)      -- Claude Code's runtime
#   5. Claude Code         -- npm install -g @anthropic-ai/claude-code
#   6. The marketplace      -- claude plugin marketplace add saharkit/windowsill
#   7. voice-loop + sill-core -- the two plugins, installed together
#
# What it does NOT install (out of scope for this ticket -- see the tracker):
#   - Dictation (recorder, STT, paste chain -- porting effort, not a config change)
#   - A local speech server (the bundled server is POSIX-only; use a cloud TTS
#     provider, or point the LAN backend at a Linux box on the network)
#
# Elevation: this script detects whether the session is elevated and reports it.
# An install that needs elevation but does not have it will hang on the UAC prompt
# with no console to approve -- so we check first rather than assuming.
#
# Idempotent: every step checks whether its work is already done before acting.
# Re-running a completed install is a fast no-op.

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Elevation check -- report, do not assume
# ---------------------------------------------------------------------------
$isElevated = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")
if ($isElevated) {
    Write-Host "[elevation] running elevated (Administrator)" -ForegroundColor Green
} else {
    Write-Host "[elevation] running UNelevated -- installers that need elevation will show a UAC prompt" -ForegroundColor Yellow
    Write-Host "[elevation] if the prompt appears behind this window and you cannot see it, re-run from an admin PowerShell" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Helper: run a command and report its exit code
# ---------------------------------------------------------------------------
function Invoke-Step {
    param([string]$Description, [ScriptBlock]$Script)
    Write-Host "`n=== $Description ===" -ForegroundColor Cyan
    try {
        & $Script
        Write-Host "    done." -ForegroundColor Green
    } catch {
        Write-Host "    FAILED: $_" -ForegroundColor Red
        throw
    }
}

# ---------------------------------------------------------------------------
# Helper: is a command on PATH right now?
# ---------------------------------------------------------------------------
function Test-Command {
    param([string]$Name)
    $found = Get-Command $Name -ErrorAction SilentlyContinue
    return $found -ne $null
}

# ---------------------------------------------------------------------------
# Helper: is this a real Python interpreter (not a Windows Store App Execution
# Alias stub)?  Runs the interpreter and checks its OUTPUT, never its presence
# on PATH alone.  A real interpreter prints "Python X.Y.Z" on --version and
# exits 0; a Store stub prints nothing (or exits 9009, or opens the Store).
# ---------------------------------------------------------------------------
function Test-RealPython {
    param([string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command -or -not $command.Source) {
        return $false
    }

    # The Microsoft Store alias is a zero-length reparse point.  Do not execute
    # it: on some stock Windows installations that opens the Store or waits for
    # a user decision instead of returning an exit code.
    $item = Get-Item -LiteralPath $command.Source -ErrorAction SilentlyContinue
    if ($item -and (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) -and $item.Length -eq 0) {
        return $false
    }

    $stdoutPath = [IO.Path]::GetTempFileName()
    $stderrPath = "$stdoutPath.err"
    $proc = $null
    try {
        $proc = Start-Process -FilePath $command.Source -ArgumentList @("--version") `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
        if (-not $proc.WaitForExit(5000)) {
            try { $proc.Kill() } catch { }
            return $false
        }
        $output = (Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue) + `
            (Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue)
        return $output -match 'Python \d+\.\d+'
    } catch {
        return $false
    } finally {
        if ($proc) { $proc.Dispose() }
        Remove-Item $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Helper: run an installer and fail on an exit code that is not explicitly
# accepted.  MSI 3010 means success with a reboot pending.
# ---------------------------------------------------------------------------
function Invoke-Installer {
    param(
        [string]$Description,
        [string]$FilePath,
        [string[]]$ArgumentList,
        [int[]]$SuccessExitCodes = @(0),
        [int[]]$RebootExitCodes = @()
    )
    $proc = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -Wait -PassThru -NoNewWindow
    if ($RebootExitCodes -contains $proc.ExitCode) {
        Write-Host "    $Description succeeded; reboot pending (exit code $($proc.ExitCode))." -ForegroundColor Yellow
    } elseif ($SuccessExitCodes -notcontains $proc.ExitCode) {
        throw "$Description failed with exit code $($proc.ExitCode)"
    }
}

function Invoke-Download {
    param([string]$Url, [string]$Destination)
    & curl.exe -L -o $Destination $Url
    if ($LASTEXITCODE -ne 0) {
        throw "download failed with exit code $($LASTEXITCODE): $Url"
    }
}

# ---------------------------------------------------------------------------
# Step 1 -- Python 3.12 (direct installer, no winget)
# ---------------------------------------------------------------------------
Invoke-Step "Python 3.12" {
    if (Test-RealPython python) {
        $ver = & python --version 2>&1
        Write-Host "    already installed: $ver"
        return
    }

    $pythonUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
    $installer = "$env:TEMP\python-3.12.10-amd64.exe"
    Write-Host "    downloading $pythonUrl ..."
    Invoke-Download $pythonUrl $installer
    Write-Host "    running installer (silent, per-machine, with PATH)..."
    Invoke-Installer "Python installer" $installer @("/quiet","InstallAllUsers=1","PrependPath=1","Include_test=0")
    Remove-Item $installer -Force -ErrorAction SilentlyContinue

    # REFRESH PATH for this session so the steps below see python.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    if (Test-RealPython python) {
        $ver = & python --version 2>&1
        Write-Host "    installed: $ver"
    } else {
        throw "Python installer exited successfully, but python.exe is not a working interpreter"
    }
}

# ---------------------------------------------------------------------------
# Step 2 -- python3.exe (python.org ships python.exe but NOT python3.exe)
#
# Every launcher in this plugin calls python3: speak.sh, dictate-toggle.sh,
# selftest.sh, and now hooks.json calls python3 directly. Without this copy,
# python3 resolves to the Microsoft Store stub (App Installer's python3.exe),
# which opens the Store page instead of running the interpreter -- invisible
# failure: it looks like the plugin misbehaving.
# ---------------------------------------------------------------------------
Invoke-Step "python3.exe alias" {
    if (Test-RealPython python3) {
        $ver = & python3 --version 2>&1
        Write-Host "    python3 already resolves: $ver"
        return
    }

    # Refresh PATH again to be sure we see the just-installed python.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $pythonExe) {
        throw "python.exe not found on PATH -- cannot create python3.exe alias"
    }
    $pythonDir = Split-Path $pythonExe -Parent
    $python3Exe = Join-Path $pythonDir "python3.exe"

    if (Test-Path $python3Exe) {
        if (Test-RealPython $python3Exe) {
            Write-Host "    python3.exe already exists at $python3Exe"
        } else {
            Remove-Item $python3Exe -Force
            Copy-Item $pythonExe $python3Exe
            Write-Host "    replaced non-functional python3.exe at $python3Exe"
        }
    } else {
        Copy-Item $pythonExe $python3Exe
        Write-Host "    copied $pythonExe -> $python3Exe"
    }

    # Verify the copy resolves.
    $env:Path = "$pythonDir;$env:Path"
    if (Test-RealPython python3) {
        $ver = & python3 --version 2>&1
        Write-Host "    python3 resolves: $ver"
    } else {
        throw "python3.exe was created, but it is not a working Python interpreter"
    }
}

# ---------------------------------------------------------------------------
# Step 3 -- npm needs PowerShell execution policy at RemoteSigned (CurrentUser)
#
# npm ships as npm.ps1; with Restricted (the Windows default) PowerShell
# refuses to execute it. RemoteSigned at CurrentUser is the narrowest scope
# that fixes it.
# ---------------------------------------------------------------------------
Invoke-Step "PowerShell execution policy" {
    $policy = Get-ExecutionPolicy -Scope CurrentUser -ErrorAction SilentlyContinue
    if ($policy -eq "RemoteSigned" -or $policy -eq "Bypass" -or $policy -eq "Unrestricted") {
        Write-Host "    CurrentUser execution policy is already $policy"
        return
    }
    Write-Host "    CurrentUser policy is $policy -- setting to RemoteSigned..."
    try {
        Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force -ErrorAction Stop
        Write-Host "    set to RemoteSigned (CurrentUser scope -- narrowest that unblocks npm.ps1)"
    } catch {
        if ($_.FullyQualifiedErrorId -like "ExecutionPolicyOverride*") {
            Write-Host "    Process-scope Bypass overrides CurrentUser policy; npm.ps1 is already unblocked"
        } else {
            throw
        }
    }
}

# ---------------------------------------------------------------------------
# Step 4 -- Git for Windows (Claude Code needs git on PATH)
# ---------------------------------------------------------------------------
Invoke-Step "Git for Windows" {
    if (Test-Command git) {
        $ver = & git --version 2>&1
        Write-Host "    already installed: $ver"
        return
    }

    $gitUrl = "https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.2/Git-2.47.1.2-64-bit.exe"
    $installer = "$env:TEMP\git-install.exe"
    Write-Host "    downloading $gitUrl ..."
    Invoke-Download $gitUrl $installer
    Write-Host "    running installer (silent)..."
    Invoke-Installer "Git installer" $installer @("/VERYSILENT","/NORESTART","/NOCANCEL")
    Remove-Item $installer -Force -ErrorAction SilentlyContinue

    # Refresh PATH for this session.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    if (Test-Command git) {
        $ver = & git --version 2>&1
        Write-Host "    installed: $ver"
    } else {
        throw "Git installer exited successfully, but git is not available on PATH"
    }
}

# ---------------------------------------------------------------------------
# Step 5 -- Node.js LTS (Claude Code's runtime)
# ---------------------------------------------------------------------------
Invoke-Step "Node.js LTS" {
    if (Test-Command node) {
        $ver = & node --version 2>&1
        Write-Host "    already installed: $ver"
    } else {
        $nodeUrl = "https://nodejs.org/dist/v22.14.0/node-v22.14.0-x64.msi"
        $installer = "$env:TEMP\node-install.msi"
        Write-Host "    downloading $nodeUrl ..."
        Invoke-Download $nodeUrl $installer
        Write-Host "    running installer (silent)..."
        Invoke-Installer "Node.js installer" "msiexec.exe" @("/i",$installer,"/quiet","/norestart") @(0) @(3010,1641)
        Remove-Item $installer -Force -ErrorAction SilentlyContinue

        # Refresh PATH.
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

        if (Test-Command node) {
            $ver = & node --version 2>&1
            Write-Host "    installed: $ver"
        } else {
            throw "Node.js installer exited successfully, but node is not available on PATH"
        }
    }

    if (Test-Command npm) {
        $ver = & npm --version 2>&1
        Write-Host "    npm version: $ver"
    } else {
        throw "npm not found on PATH after Node.js install -- check the execution policy step above"
    }
}

# ---------------------------------------------------------------------------
# Step 6 -- Claude Code
# ---------------------------------------------------------------------------
Invoke-Step "Claude Code" {
    # Refresh PATH one more time so npm is definitely visible.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    # Resolve the npm global bin directory instead of trusting an arbitrary
    # claude command already present on PATH.
    $npmPrefix = (& npm prefix -g 2>&1 | Out-String).Trim()
    $prefixExitCode = $LASTEXITCODE
    if ($prefixExitCode -ne 0 -or -not $npmPrefix) {
        throw "npm could not report its global prefix"
    }
    $claudeCommand = Get-ChildItem -LiteralPath $npmPrefix -Filter "claude.*" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @(".cmd", ".ps1", ".exe") } |
        Select-Object -First 1
    if ($claudeCommand) {
        $ver = & $claudeCommand.FullName --version 2>&1
        Write-Host "    already installed: $ver"
        return
    }

    Write-Host "    npm install -g @anthropic-ai/claude-code ..."
    & npm install -g @anthropic-ai/claude-code
    $npmExitCode = $LASTEXITCODE
    if ($npmExitCode -ne 0) {
        throw "Claude Code npm install failed with exit code $npmExitCode"
    }

    # Check the global npm bin directory directly.  A stale claude command on
    # PATH must not turn a failed or incomplete install into a false success.
    $claudeCommand = Get-ChildItem -LiteralPath $npmPrefix -Filter "claude.*" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @(".cmd", ".ps1", ".exe") } |
        Select-Object -First 1
    if ($claudeCommand) {
        $ver = & $claudeCommand.FullName --version 2>&1
        Write-Host "    installed: $ver"
    } else {
        throw "Claude Code npm install succeeded, but no claude command was created in $npmPrefix"
    }
}

# ---------------------------------------------------------------------------
# Step 7 -- Marketplace and plugins
#
# The marketplace add is done in the SHELL (claude plugin marketplace add ...)
# because it needs no auth. The plugin install is done from WITHIN a session
# (/plugin install ...) as documented in README.md -- but we print the commands
# because the plugin install needs a running Claude Code session.
# ---------------------------------------------------------------------------
Invoke-Step "Marketplace" {
    # Refresh PATH.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    if (-not (Test-Command claude)) {
        throw "claude not on PATH -- was the Claude Code install successful?"
    }

    # Marketplace add -- idempotent, needs no login (auth gates execution, not install).
    Write-Host "    claude plugin marketplace add saharkit/windowsill ..."
    claude plugin marketplace add saharkit/windowsill
    Write-Host "    marketplace added."
}

Write-Host @"

=== MANUAL STEP -- install the plugins ===

The next step needs a RUNNING Claude Code session. Start one:

    claude

Then type these two commands inside the session:

    /plugin install voice-loop@windowsill
    /plugin install sill-core@windowsill

(voice-loop needs sill-core for /doctor -- the diagnosis engine is a separate plugin.)

=== VERIFY ===

After both plugins are installed, run the doctor inside the session:

    /voice-loop:doctor

Speak-back: end a reply with a line starting with the speak marker (the
default is the loudspeaker emoji). The hook fires and speak.py runs. The
bundled speech server is POSIX-only, so on Windows the line is audible
through a CLOUD TTS provider -- set tts.backend: "cloud", a tts.cloud.provider
and its key (see PROVIDERS.md) -- played by the in-box PowerShell sound
player, or through a LAN server on a Linux box (README: "Running on Windows").

Dictation is NOT part of this pass (see the tracker -- out of scope).

=== FRESH SESSION ===

After each install step that changed PATH (Python, Git, Node.js), a NEW
PowerShell session sees the updated PATH.  If a step reports "visible in a
fresh session", close and re-open PowerShell and re-run this script -- it is
idempotent and will skip already-completed steps.

"@

Write-Host "[install] Windows native install recipe complete." -ForegroundColor Green
