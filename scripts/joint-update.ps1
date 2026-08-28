$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "runtime\targets\windows-x64\python\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "缺少候选包内 Windows x64 离线运行时"
}
& $python (Join-Path $root "tools\joint_update.py") @args
exit $LASTEXITCODE
