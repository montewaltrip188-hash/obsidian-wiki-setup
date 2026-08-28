[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('plan', 'build', 'verify')]
    [string]$Action,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CandidateArguments
)

$ErrorActionPreference = 'Stop'
$tool = Join-Path (Split-Path -Parent $PSScriptRoot) 'tools\install_candidate.py'

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw '未找到 Python 3；请先安装 Python 3，再运行安装候选组装器。'
}

& $python.Source $tool $Action @CandidateArguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
