# OpenRouter Refresh Script for Rainmeter (Windows)
# Calls openrouter_widget.py (must be accessible via WSL or Windows Python)
# Writes JSON to a shared location that Rainmeter can read
#
# Prerequisites:
#   - Python 3 installed
#   - OPENROUTER_API_KEY set in environment (or edit below)
#   - openrouter_widget.py in same directory or adjust path
#
# Save as: Refresh-OpenRouter.ps1
# Run via Task Scheduler every 5 minutes

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$pythonScript = Join-Path $scriptDir "openrouter_widget.py"

# Output JSON file - adjust for your Rainmeter setup
# Option A: WSL2 path (if using WSL Linux distro)
# $jsonFile = "\\wsl$\Ubuntu\tmp\openrouter_widget.json"
# Option B: Local Windows path
$jsonFile = "$env:TEMP\openrouter_widget.json"

# Ensure OPENROUTER_API_KEY is set (fallback to prompt)
if (-not $env:OPENROUTER_API_KEY) {
    Write-Host "OPENROUTER_API_KEY not found in environment." -ForegroundColor Yellow
    $key = Read-Host "Enter your OpenRouter API key (will be used for this session only)"
    $env:OPENROUTER_API_KEY = $key
}

# Run the Python script
Write-Host "Fetching OpenRouter data..."
try {
    & python $pythonScript
    if (Test-Path $jsonFile) {
        Write-Host "Data written to $jsonFile" -ForegroundColor Green
        # Optionally signal Rainmeter to update (if using a plugin that watches file)
        # For basic WebParser measure, it will pick up on next update cycle
    } else {
        Write-Host "ERROR: JSON file not created at $jsonFile" -ForegroundColor Red
    }
} catch {
    Write-Host "Failed to run python script: $_" -ForegroundColor Red
    Write-Host "Check that python is in PATH and openrouter_widget.py exists." -ForegroundColor Red
}