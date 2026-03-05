<#
.SYNOPSIS
    Windows-friendly helper to register the Watercooler MCP server with Claude Code.

.DESCRIPTION
    Mirrors scripts/install-mcp.sh but avoids quoting issues on PowerShell by invoking
    the Claude CLI with an argument array. Supports overriding the Python interpreter
    and key environment variables via parameters.

.PARAMETER Python
    Command used to invoke Python (defaults to "python"). Examples: "python3", "py".

.PARAMETER Agent
    Agent identity advertised to Watercooler threads. Defaults to "Claude@Code".

.PARAMETER ThreadsPattern
    Git remote pattern for paired threads repositories.

.PARAMETER AutoBranch
    Set to $false to disable automatic branch creation. Enabled by default.

.PARAMETER Scope
    Claude configuration scope (user, project, local). Defaults to "user".

.EXAMPLE
    ./scripts/install-mcp.ps1 -Python py -Agent "Claude@Code" -AutoBranch:$true

#>
param(
    [string]$Python = "python",
    [string]$Agent = "Claude@Code",
    [string]$ThreadsPattern = "https://github.com/{org}/{repo}-threads.git",
    [bool]$AutoBranch = $true,
    [ValidateSet("user", "project", "local")][string]$Scope = "user"
)

function Write-Info {
    param([string]$Message)
    Write-Host "[info] $Message" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host "[ok] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[warn] $Message" -ForegroundColor Yellow
}

function Write-ErrorAndExit {
    param([string]$Message, [int]$Code = 1)
    Write-Host "[error] $Message" -ForegroundColor Red
    exit $Code
}

# Ensure Claude CLI is available
if (-not (Get-Command "claude" -ErrorAction SilentlyContinue)) {
    Write-ErrorAndExit "Claude CLI not found on PATH. Install Claude Code and retry."
}

# Ensure Python is available
if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    Write-ErrorAndExit "Python interpreter '$Python' not found on PATH. Override the -Python parameter if needed."
}

# Ensure we are at repo root (pyproject with watercooler-cloud)
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $repoRoot "pyproject.toml"))) {
    Write-ErrorAndExit "Run this script from the watercooler repository root."
}

Push-Location $repoRoot

try {
    Write-Info "Installing watercooler with MCP extras (editable)."
    $pipArgs = @("-m", "pip", "install", "-e", ".[mcp]")
    & $Python @pipArgs
    if ($LASTEXITCODE -ne 0) {
        Write-ErrorAndExit "pip install failed (exit code $LASTEXITCODE)."
    }

    Write-Info "Registering watercooler MCP server with Claude (scope: $Scope)."

    $claudeArgs = @(
        "mcp", "add",
        "--transport", "stdio",
        "watercooler",
        "--scope", $Scope,
        "-e", "WATERCOOLER_AGENT=$Agent",
        "-e", "WATERCOOLER_THREADS_PATTERN=$ThreadsPattern"
    )

    if ($AutoBranch) {
        $claudeArgs += @("-e", "WATERCOOLER_AUTO_BRANCH=1")
    }

    $claudeArgs += @("--", $Python, "-m", "watercooler_mcp")

    $claudeOutput = & "claude" @claudeArgs 2>&1
    $claudeExit = $LASTEXITCODE

    if ($claudeExit -ne 0) {
        $combinedOutput = ($claudeOutput | Out-String)
        if ($combinedOutput -match "already exists") {
            Write-Warn "MCP server already registered. Skipping add step."
        }
        else {
            Write-Host $combinedOutput
            Write-ErrorAndExit "Claude registration failed (exit code $claudeExit)."
        }
    }
    else {
        Write-Host $claudeOutput
    }

    Write-Success "Watercooler MCP server registered successfully."
    Write-Info "Try 'claude mcp list' to verify the watercooler server."
}
finally {
    Pop-Location
}
