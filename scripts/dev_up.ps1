# Bring the database up reliably from a cold machine.
#
#   .\scripts\dev_up.ps1
#
# Docker Desktop does not always start with Windows, so `docker compose up -d`
# fails with "pipe not found" and every later script fails with connection
# refused. This starts the engine if needed, waits for it, then waits for the
# container healthcheck before returning.

$ErrorActionPreference = 'Stop'

$dockerBin = 'C:\Program Files\Docker\Docker\resources\bin'
if (Test-Path $dockerBin) {
    $env:Path = "$dockerBin;$env:Path"
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Test-DockerEngine {
    $null = & docker version --format '{{.Server.Version}}' 2>$null
    return $LASTEXITCODE -eq 0
}

if (-not (Test-DockerEngine)) {
    $exe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path $exe)) {
        throw "Docker engine is not running and Docker Desktop is not installed at $exe"
    }

    Write-Host 'Docker engine is down, starting Docker Desktop...'
    Start-Process $exe

    $deadline = (Get-Date).AddMinutes(4)
    while (-not (Test-DockerEngine)) {
        if ((Get-Date) -gt $deadline) {
            throw 'Docker engine did not start within 4 minutes'
        }
        Start-Sleep -Seconds 5
    }
    Write-Host 'Docker engine is up.'
}

& docker compose up -d --wait
if ($LASTEXITCODE -ne 0) {
    throw "docker compose up failed with exit code $LASTEXITCODE"
}

& docker compose ps
Write-Host ''
Write-Host 'Database is ready. Next: uv run python scripts/check_db.py'
