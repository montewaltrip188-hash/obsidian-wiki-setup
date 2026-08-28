# ============================================================
# Obsidian LLM Wiki - Windows 一键安装脚本
# ============================================================
# 用法: 右键 -> 使用 PowerShell 运行
#       或在 PowerShell 中执行: powershell -ExecutionPolicy Bypass -File setup-win.ps1
# ============================================================

[CmdletBinding()]
param(
    [switch]$ValidateActivationOnly,
    [string]$ActivationCode,
    [string]$ActivationPublicKeyPath,
    [string]$RevokedActivationIdsPath,
    [string]$ExpectedProduct = "obsidian-llm-wiki",
    [string]$ExpectedVersion = "2.1",
    [string]$NowUtc
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

if ([string]::IsNullOrWhiteSpace($ActivationPublicKeyPath)) {
    $ActivationPublicKeyPath = Join-Path $repoRoot "activation-public-key.xml"
}
if ([string]::IsNullOrWhiteSpace($RevokedActivationIdsPath)) {
    $RevokedActivationIdsPath = Join-Path $repoRoot "revoked-activation-ids.txt"
}
if ([string]::IsNullOrWhiteSpace($NowUtc)) {
    $validationTime = [DateTimeOffset]::UtcNow
} else {
    $validationTime = [DateTimeOffset]::Parse(
        $NowUtc,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal
    ).ToUniversalTime()
}

function ConvertFrom-Base64Url {
    param([Parameter(Mandatory)][string]$Value)
    if ($Value -notmatch '^[A-Za-z0-9_-]+$') { throw "Base64Url 字段格式错误" }
    $normalized = $Value.Replace('-', '+').Replace('_', '/')
    switch ($normalized.Length % 4) {
        0 { }
        2 { $normalized += '==' }
        3 { $normalized += '=' }
        default { throw "Base64Url 字段长度错误" }
    }
    return [Convert]::FromBase64String($normalized)
}

function Test-Wiki2Activation {
    param(
        [Parameter(Mandatory)][string]$Code,
        [Parameter(Mandatory)][string]$PublicKeyPath,
        [Parameter(Mandatory)][string]$RevokedIdsPath,
        [Parameter(Mandatory)][string]$Product,
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][DateTimeOffset]$AtUtc
    )

    $segments = $Code.Split('.')
    if ($segments.Count -ne 3 -or $segments[0] -cne 'WIKI2') {
        throw "激活码格式错误"
    }
    if (-not (Test-Path -LiteralPath $PublicKeyPath -PathType Leaf)) {
        throw "激活公钥缺失"
    }

    $payloadSegment = $segments[1]
    $signature = ConvertFrom-Base64Url $segments[2]
    [xml]$publicKey = Get-Content -LiteralPath $PublicKeyPath -Raw
    $rsaParameters = [Security.Cryptography.RSAParameters]::new()
    $rsaParameters.Modulus = [Convert]::FromBase64String([string]$publicKey.RSAKeyValue.Modulus)
    $rsaParameters.Exponent = [Convert]::FromBase64String([string]$publicKey.RSAKeyValue.Exponent)
    $rsa = [Security.Cryptography.RSA]::Create()
    try {
        $rsa.ImportParameters($rsaParameters)
        $verified = $rsa.VerifyData(
            [Text.Encoding]::ASCII.GetBytes($payloadSegment),
            $signature,
            [Security.Cryptography.HashAlgorithmName]::SHA256,
            [Security.Cryptography.RSASignaturePadding]::Pkcs1
        )
    } finally {
        $rsa.Dispose()
    }
    if (-not $verified) { throw "激活码签名无效" }

    $payloadJson = [Text.Encoding]::UTF8.GetString((ConvertFrom-Base64Url $payloadSegment))
    $payload = $payloadJson | ConvertFrom-Json
    foreach ($required in @('activation_id', 'product', 'version', 'expires_at')) {
        if (-not $payload.PSObject.Properties[$required] -or [string]::IsNullOrWhiteSpace([string]$payload.$required)) {
            throw "激活码缺少必需字段"
        }
    }
    if ([string]$payload.product -cne $Product) { throw "激活码产品不匹配" }
    if ([string]$payload.version -cne $Version) { throw "激活码版本不匹配" }

    $expiresAt = [DateTimeOffset]::Parse(
        [string]$payload.expires_at,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal
    ).ToUniversalTime()
    if ($expiresAt -le $AtUtc.ToUniversalTime()) { throw "激活码已过期" }

    if (Test-Path -LiteralPath $RevokedIdsPath -PathType Leaf) {
        $revokedIds = Get-Content -LiteralPath $RevokedIdsPath | ForEach-Object { $_.Trim() } |
            Where-Object { $_ -and -not $_.StartsWith('#') }
        if ($revokedIds -ccontains [string]$payload.activation_id) { throw "激活码已撤销" }
    }
    return $true
}

function Write-Step { param($msg) Write-Host "`n[$script:step] $msg" -ForegroundColor Cyan; $script:step++ }
$script:step = 1

Write-Host "============================================" -ForegroundColor Green
Write-Host "  Obsidian LLM Wiki 一键安装" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green

# ----------------------------------------------------------
# 0. 激活码验证
# ----------------------------------------------------------
Write-Step "验证激活码..."
if ([string]::IsNullOrWhiteSpace($ActivationCode)) {
    $secureCode = Read-Host "  请输入激活码" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureCode)
    try {
        $ActivationCode = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

try {
    $null = Test-Wiki2Activation -Code $ActivationCode -PublicKeyPath $ActivationPublicKeyPath `
        -RevokedIdsPath $RevokedActivationIdsPath -Product $ExpectedProduct -Version $ExpectedVersion `
        -AtUtc $validationTime
    Write-Host "  激活码验证通过" -ForegroundColor Green
} catch {
    Write-Host "  [!] 激活码无效，请联系服务提供者获取新的 WIKI2 激活码" -ForegroundColor Red
    exit 1
}

if ($ValidateActivationOnly) { exit 0 }

# ----------------------------------------------------------
# 1. 检查管理员权限
# ----------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "`n[!] 建议以管理员身份运行以确保安装正常。" -ForegroundColor Yellow
    Write-Host "    右键此脚本 -> 使用 PowerShell 运行（管理员）`n" -ForegroundColor Yellow
}

# ----------------------------------------------------------
# 1. 安装 Obsidian
# ----------------------------------------------------------
Write-Step "检查 Obsidian..."
$obsidianInstalled = (Test-Path "$env:LOCALAPPDATA\Obsidian\Obsidian.exe") -or (Test-Path "$env:LOCALAPPDATA\Programs\Obsidian\Obsidian.exe") -or (Test-Path "$env:ProgramFiles\Obsidian\Obsidian.exe")
if ($obsidianInstalled) {
    Write-Host "  Obsidian 已安装" -ForegroundColor Green
} else {
    # 检查本地安装包（完整文件或分片文件）
    $localObsidian = Join-Path $repoRoot "installers\Obsidian-1.12.7.exe"
    if (-not (Test-Path $localObsidian)) {
        # 尝试从分片合并
        $part1 = Join-Path $repoRoot "installers\Obsidian-win-part1.bin"
        if (Test-Path $part1) {
            Write-Host "  正在合并 Obsidian 安装包分片..." -ForegroundColor Yellow
            $parts = Get-ChildItem (Join-Path $repoRoot "installers\Obsidian-win-part*.bin") | Sort-Object Name
            $outStream = [System.IO.File]::Create($localObsidian)
            foreach ($part in $parts) {
                $bytes = [System.IO.File]::ReadAllBytes($part.FullName)
                $outStream.Write($bytes, 0, $bytes.Length)
            }
            $outStream.Close()
            Write-Host "  合并完成" -ForegroundColor Green
        }
    }
    if (Test-Path $localObsidian) {
        Write-Host "  正在安装 Obsidian..." -ForegroundColor Yellow
        Start-Process -FilePath $localObsidian -ArgumentList "/S" -Wait
        Write-Host "  Obsidian 安装完成" -ForegroundColor Green
    } else {
        Write-Host "  [!] 未找到 Obsidian 安装包，请手动下载安装: https://obsidian.md" -ForegroundColor Yellow
    }
}

# ----------------------------------------------------------
# 2. 安装 Git（优先使用本地安装包）
# ----------------------------------------------------------
Write-Step "检查 Git..."
$gitPath = Get-Command git -ErrorAction SilentlyContinue
if ($gitPath) {
    Write-Host "  Git 已安装: $(git --version)" -ForegroundColor Green
} else {
    $localGit = Join-Path $repoRoot "installers\Git-Setup.exe"
    if (Test-Path $localGit) {
        Write-Host "  使用本地安装包安装 Git..." -ForegroundColor Yellow
        Start-Process -FilePath $localGit -ArgumentList "/VERYSILENT", "/NORESTART", "/NOCANCEL", "/SP-", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS" -Wait
    } else {
        Write-Host "  本地安装包未找到，尝试 winget 在线安装..." -ForegroundColor Yellow
        winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
    }
    # 刷新 PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Host "  Git 安装成功: $(git --version)" -ForegroundColor Green
    } else {
        Write-Host "  [!] Git 安装后未找到，请手动安装 installers\Git-Setup.exe" -ForegroundColor Red
        exit 1
    }
}

# ----------------------------------------------------------
# 2. 安装 Claude Code（优先使用本地安装包）
# ----------------------------------------------------------
Write-Step "检查 Claude Code..."
$claudePath = Get-Command claude -ErrorAction SilentlyContinue
if ($claudePath) {
    Write-Host "  Claude Code 已安装: $(claude --version)" -ForegroundColor Green
} else {
    $localClaude = Join-Path $repoRoot "installers\claude.exe"
    if (Test-Path $localClaude) {
        Write-Host "  使用本地安装包部署 Claude Code..." -ForegroundColor Yellow
        # 复制到用户本地目录并加入 PATH
        $claudeInstallDir = "$env:LOCALAPPDATA\ClaudeCode"
        New-Item -ItemType Directory -Force -Path $claudeInstallDir | Out-Null
        Copy-Item $localClaude "$claudeInstallDir\claude.exe" -Force
        # 将目录加入用户 PATH
        $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
        if ($userPath -notlike "*$claudeInstallDir*") {
            [System.Environment]::SetEnvironmentVariable("Path", "$userPath;$claudeInstallDir", "User")
        }
        $env:Path = "$env:Path;$claudeInstallDir"
        Write-Host "  Claude Code 已部署到: $claudeInstallDir" -ForegroundColor Green
    } else {
        Write-Host "  本地安装包未找到，尝试 winget 在线安装..." -ForegroundColor Yellow
        winget install Anthropic.ClaudeCode --accept-source-agreements --accept-package-agreements
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    }
    if (Get-Command claude -ErrorAction SilentlyContinue) {
        Write-Host "  Claude Code 安装成功" -ForegroundColor Green
    } else {
        Write-Host "  [!] Claude Code 安装失败，请手动运行: winget install Anthropic.ClaudeCode" -ForegroundColor Red
        exit 1
    }
}

# ----------------------------------------------------------
# 3. 配置 AI 模型
# ----------------------------------------------------------
Write-Step "配置 AI 模型..."
$claudeDir = "$env:USERPROFILE\.claude"
$settingsPath = "$claudeDir\settings.json"

if (Test-Path $settingsPath) {
    $existing = Get-Content $settingsPath -Raw | ConvertFrom-Json
    if ($existing.env.ANTHROPIC_AUTH_TOKEN -and $existing.env.ANTHROPIC_AUTH_TOKEN -ne "sk-YOUR-API-KEY") {
        Write-Host "  API 已配置，跳过" -ForegroundColor Green
        $skipApi = $true
    }
}

if (-not $skipApi) {
    Write-Host ""
    Write-Host "  请选择 AI 模型:" -ForegroundColor Yellow
    Write-Host "    1. DeepSeek (推荐，国内直连，无需 VPN)" -ForegroundColor White
    Write-Host "    2. Claude 原版 (需要 VPN 或海外网络)" -ForegroundColor White
    Write-Host "    3. OpenAI (需要 VPN 或海外网络)" -ForegroundColor White
    Write-Host "    4. 自定义 API" -ForegroundColor White
    $modelChoice = Read-Host "  请输入编号 (默认 1)"
    if ([string]::IsNullOrWhiteSpace($modelChoice)) { $modelChoice = "1" }

    switch ($modelChoice) {
        "1" {
            $baseUrl = "https://api.deepseek.com/anthropic"
            $mainModel = "deepseek-v4-pro"
            $fastModel = "deepseek-v4-flash"
            $apiUrl = "https://platform.deepseek.com"
            $providerName = "DeepSeek"
        }
        "2" {
            $baseUrl = "https://api.anthropic.com"
            $mainModel = "claude-sonnet-4-6"
            $fastModel = "claude-haiku-4-5-20251001"
            $apiUrl = "https://console.anthropic.com"
            $providerName = "Claude"
        }
        "3" {
            $baseUrl = "https://api.openai.com/v1"
            $mainModel = "gpt-4.1"
            $fastModel = "gpt-4.1-mini"
            $apiUrl = "https://platform.openai.com"
            $providerName = "OpenAI"
        }
        "4" {
            Write-Host "  请输入 API Base URL (如 https://api.example.com/v1):" -ForegroundColor Yellow
            $baseUrl = Read-Host "  Base URL"
            Write-Host "  请输入主模型名称:" -ForegroundColor Yellow
            $mainModel = Read-Host "  主模型"
            Write-Host "  请输入快速模型名称 (可与主模型相同):" -ForegroundColor Yellow
            $fastModel = Read-Host "  快速模型"
            $apiUrl = $baseUrl
            $providerName = "自定义"
        }
        default {
            $baseUrl = "https://api.deepseek.com/anthropic"
            $mainModel = "deepseek-v4-pro"
            $fastModel = "deepseek-v4-flash"
            $apiUrl = "https://platform.deepseek.com"
            $providerName = "DeepSeek"
        }
    }

    Write-Host ""
    Write-Host "  已选择: $providerName" -ForegroundColor Green
    Write-Host "  请输入你的 API Key" -ForegroundColor Yellow
    Write-Host "  (获取地址: $apiUrl)" -ForegroundColor Yellow
    $apiKey = Read-Host "  API Key"

    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        Write-Host "  [!] 未输入 API Key，跳过配置。稍后可手动编辑: $settingsPath" -ForegroundColor Yellow
        $apiKey = "sk-YOUR-API-KEY"
    }

    New-Item -ItemType Directory -Force -Path $claudeDir | Out-Null
    $settings = @"
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "$apiKey",
    "ANTHROPIC_BASE_URL": "$baseUrl",
    "ANTHROPIC_MODEL": "$mainModel",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "$mainModel",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "$mainModel",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "$fastModel",
    "CLAUDE_CODE_SUBAGENT_MODEL": "$fastModel"
  }
}
"@
    $settings | Out-File -FilePath $settingsPath -Encoding utf8 -Force
    Write-Host "  API 配置完成 (${providerName}: ${mainModel})" -ForegroundColor Green
}

# ----------------------------------------------------------
# 4. 部署 Obsidian Vault
# ----------------------------------------------------------
Write-Step "部署知识库..."
$defaultVaultPath = "$env:USERPROFILE\Documents\ObsidianVault"
Write-Host "  知识库将部署到: $defaultVaultPath"
Write-Host "  (按回车确认，或输入自定义路径)" -ForegroundColor Yellow
$customPath = Read-Host "  路径"
if (-not [string]::IsNullOrWhiteSpace($customPath)) {
    $defaultVaultPath = $customPath
}

$vaultZip = Join-Path $repoRoot "vault.zip"
$vaultManifest = Join-Path $repoRoot "install-manifest.json"
$vaultDeployer = Join-Path $repoRoot "extract-vault.py"
foreach ($requiredFile in @($vaultZip, $vaultManifest, $vaultDeployer)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        Write-Host "  [!] 缺少安全部署文件: $requiredFile" -ForegroundColor Red
        exit 1
    }
}
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    Write-Host "  [!] 安全部署需要 Python 3" -ForegroundColor Red
    exit 1
}

if (Test-Path -LiteralPath $defaultVaultPath) {
    Write-Host "  [!] 目录已存在: $defaultVaultPath" -ForegroundColor Yellow
    $overwrite = Read-Host "  是否先保留同级备份再升级？(y/N)"
    if ($overwrite -ne 'y' -and $overwrite -ne 'Y') {
        Write-Host "  跳过部署" -ForegroundColor Yellow
        $skipDeploy = $true
    } else {
        $allowExistingVault = $true
    }
}

if (-not $skipDeploy) {
    Write-Host "  正在验证候选包并安全部署知识库..." -ForegroundColor Yellow
    $deployArgs = @(
        $vaultDeployer, "deploy",
        "--archive", $vaultZip,
        "--manifest", $vaultManifest,
        "--target", $defaultVaultPath
    )
    if ($allowExistingVault) {
        $deployArgs += "--allow-existing"
    }
    & $pythonCommand.Source @deployArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "  知识库已部署到: $defaultVaultPath" -ForegroundColor Green
}

# ----------------------------------------------------------
# 5. 配置 Claudian 插件的 Claude CLI 路径
# ----------------------------------------------------------
Write-Step "配置 Claudian 插件..."
$claudeExe = (Get-Command claude -ErrorAction SilentlyContinue).Source
if ($claudeExe) {
    $claudianData = @{ claudePath = $claudeExe } | ConvertTo-Json
    $claudianDataPath = Join-Path $defaultVaultPath ".obsidian\plugins\claudian\data.json"
    if (Test-Path (Split-Path $claudianDataPath -Parent)) {
        $claudianData | Out-File -FilePath $claudianDataPath -Encoding utf8 -Force
        Write-Host "  Claudian 已配置 CLI 路径: $claudeExe" -ForegroundColor Green
    }
}

# ----------------------------------------------------------
# 6. 验证安装
# ----------------------------------------------------------
Write-Step "验证安装..."
Write-Host ""
$checks = @(
    @{ Name = "Git";          Cmd = "git --version" },
    @{ Name = "Claude Code";  Cmd = "claude --version" },
    @{ Name = "API 配置";     Path = $settingsPath },
    @{ Name = "知识库";       Path = $defaultVaultPath }
)

foreach ($check in $checks) {
    if ($check.Cmd) {
        try {
            $result = Invoke-Expression $check.Cmd 2>&1
            Write-Host "  [OK] $($check.Name): $result" -ForegroundColor Green
        } catch {
            Write-Host "  [X]  $($check.Name): 未安装" -ForegroundColor Red
        }
    } elseif ($check.Path) {
        if (Test-Path $check.Path) {
            Write-Host "  [OK] $($check.Name): $($check.Path)" -ForegroundColor Green
        } else {
            Write-Host "  [X]  $($check.Name): 未找到" -ForegroundColor Red
        }
    }
}

# ----------------------------------------------------------
# 完成
# ----------------------------------------------------------
Write-Host "`n============================================" -ForegroundColor Green
Write-Host "  安装完成！" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "下一步：" -ForegroundColor Yellow
Write-Host "  1. 打开 Obsidian -> 打开文件夹作为库 -> 选择 $defaultVaultPath"
Write-Host "  2. 信任插件并启用"
Write-Host "  3. 阅读「使用指南」文件夹中的文档"
Write-Host ""
Read-Host "按回车退出"
