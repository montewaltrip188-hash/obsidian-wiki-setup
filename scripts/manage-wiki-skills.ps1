[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$LifecycleArguments
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$ToolPath = Join-Path $Root "tools\manage_wiki_skills.py"
$PythonPath = Join-Path $Root "runtime\targets\windows-x64\python\python.exe"
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "缺少候选包内 Windows x64 离线运行时"
}

& $PythonPath $ToolPath @LifecycleArguments
exit $LASTEXITCODE
