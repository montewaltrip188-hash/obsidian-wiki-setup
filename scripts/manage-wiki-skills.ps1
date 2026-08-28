[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$LifecycleArguments
)

$ErrorActionPreference = "Stop"
$ToolPath = Join-Path (Split-Path -Parent $PSScriptRoot) "tools\manage_wiki_skills.py"

python $ToolPath @LifecycleArguments
exit $LASTEXITCODE
