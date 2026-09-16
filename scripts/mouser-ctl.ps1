<#
.SYNOPSIS
    Thin wrapper: run the installed Mouser.exe lifecycle CLI.

.EXAMPLE
    .\mouser-ctl.ps1 status      # exit 0 = none, 1 = one, 2 = many
    .\mouser-ctl.ps1 restart

.NOTES
    Resolves Mouser.exe from MOUSER_INSTALL_DIR, then the machine-scope
    install (Program Files\Mouser), then the per-user install
    (LocalAppData\Programs\Mouser).  Safe to call from an SSH (session 0)
    shell: `start` launches through a one-shot interactive scheduled task
    for the console user, never Start-Process.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('status', 'stop', 'start', 'restart', 'assert-single')]
    [string]$Verb
)

$ErrorActionPreference = 'Stop'

$candidates = @()
if ($env:MOUSER_INSTALL_DIR) { $candidates += (Join-Path $env:MOUSER_INSTALL_DIR 'Mouser.exe') }
$candidates += (Join-Path $env:ProgramFiles 'Mouser\Mouser.exe')
$candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Mouser\Mouser.exe')

$exe = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $exe) {
    Write-Error "mouser-ctl: installed Mouser.exe not found (tried: $($candidates -join ', '))"
    exit 66
}

# Mouser.exe is a windowed binary, so its stdout is not visible here; the
# exit code is the contract.
$proc = Start-Process -FilePath $exe -ArgumentList @('--ctl', $Verb) -Wait -PassThru -NoNewWindow
exit $proc.ExitCode
