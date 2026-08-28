$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$root = Split-Path -Parent $PSScriptRoot
$python = Get-Command python -ErrorAction Stop
& $python.Source (Join-Path $root "tools\vault_update.py") @args
exit $LASTEXITCODE
