$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$failed = 0
$passed = 0

function Test-Case {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Body
    )

    try {
        & $Body
        Write-Host "[PASS] $Name" -ForegroundColor Green
        $script:passed++
    } catch {
        Write-Host "[FAIL] $Name`n       $($_.Exception.Message)" -ForegroundColor Red
        $script:failed++
    }
}

Test-Case "公开下载脚本不携带客户端凭据" {
    foreach ($name in @("download-win.ps1", "download-mac.sh")) {
        $content = Get-Content -LiteralPath (Join-Path $repoRoot $name) -Raw
        if ($content -match '(?im)access_token|^\s*\$?token\s*=') {
            throw "$name 仍包含令牌变量或 access_token 查询参数"
        }
        if ($content -match '(?i)https?://[^\s"'']+\?[^\s"'']*(token|key|signature)=') {
            throw "$name 仍包含带凭据的下载 URL"
        }
    }
}

function ConvertTo-Base64Url {
    param([Parameter(Mandatory)][byte[]]$Bytes)
    return [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-TestActivationCode {
    param(
        [Parameter(Mandatory)][System.Security.Cryptography.RSA]$Rsa,
        [Parameter(Mandatory)][string]$ActivationId,
        [Parameter(Mandatory)][string]$ExpiresAt,
        [string]$Product = "obsidian-llm-wiki",
        [string]$Version = "2.1"
    )

    $payload = [ordered]@{
        activation_id = $ActivationId
        product = $Product
        version = $Version
        expires_at = $ExpiresAt
    } | ConvertTo-Json -Compress
    $payloadSegment = ConvertTo-Base64Url ([Text.Encoding]::UTF8.GetBytes($payload))
    $signature = $Rsa.SignData(
        [Text.Encoding]::ASCII.GetBytes($payloadSegment),
        [Security.Cryptography.HashAlgorithmName]::SHA256,
        [Security.Cryptography.RSASignaturePadding]::Pkcs1
    )
    return "WIKI2.$payloadSegment.$(ConvertTo-Base64Url $signature)"
}

function Invoke-WindowsActivationValidation {
    param(
        [Parameter(Mandatory)][string]$Code,
        [Parameter(Mandatory)][string]$PublicKeyPath,
        [Parameter(Mandatory)][string]$RevokedIdsPath
    )

    $pwsh = (Get-Process -Id $PID).Path
    $output = & $pwsh -NoProfile -File (Join-Path $repoRoot "setup-win.ps1") `
        -ValidateActivationOnly `
        -ActivationCode $Code `
        -ActivationPublicKeyPath $PublicKeyPath `
        -RevokedActivationIdsPath $RevokedIdsPath `
        -NowUtc "2026-08-28T00:00:00Z" 2>&1
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($output -join "`n") }
}

function Invoke-MacActivationValidation {
    param(
        [Parameter(Mandatory)][string]$Code,
        [Parameter(Mandatory)][string]$PublicKeyPath,
        [Parameter(Mandatory)][string]$RevokedIdsPath
    )

    $bash = "D:\Git\bin\bash.exe"
    if (-not (Test-Path -LiteralPath $bash)) {
        $bash = (Get-Command bash -ErrorAction Stop).Source
    }
    $scriptPath = (Join-Path $repoRoot "setup-mac.sh").Replace('\', '/')
    $publicPath = $PublicKeyPath.Replace('\', '/')
    $revokedPath = $RevokedIdsPath.Replace('\', '/')
    $output = & $bash $scriptPath `
        --validate-activation-only `
        --activation-code $Code `
        --public-key $publicPath `
        --revoked-ids $revokedPath `
        --now-utc "2026-08-28T00:00:00Z" 2>&1
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($output -join "`n") }
}

function Assert-RejectedOnBothPlatforms {
    param(
        [Parameter(Mandatory)][string]$Code,
        [Parameter(Mandatory)][string]$PublicKeyPath,
        [Parameter(Mandatory)][string]$RevokedIdsPath
    )

    $windows = Invoke-WindowsActivationValidation -Code $Code -PublicKeyPath $PublicKeyPath -RevokedIdsPath $RevokedIdsPath
    $mac = Invoke-MacActivationValidation -Code $Code -PublicKeyPath $PublicKeyPath -RevokedIdsPath $RevokedIdsPath
    if ($windows.ExitCode -eq 0 -or $mac.ExitCode -eq 0) {
        throw "至少一个平台错误接受了应拒绝的激活码"
    }
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("obsidian-wiki-security-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$rsa = [Security.Cryptography.RSA]::Create(2048)
$parameters = $rsa.ExportParameters($false)
$testPublicKeyPath = Join-Path $testRoot "activation-public-key.xml"
$testRevokedIdsPath = Join-Path $testRoot "revoked-activation-ids.txt"
$publicXml = "<RSAKeyValue><Modulus>$([Convert]::ToBase64String($parameters.Modulus))</Modulus><Exponent>$([Convert]::ToBase64String($parameters.Exponent))</Exponent></RSAKeyValue>"
[IO.File]::WriteAllText($testPublicKeyPath, $publicXml, [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText($testRevokedIdsPath, "", [Text.UTF8Encoding]::new($false))

Test-Case "Windows 接受有效的 WIKI2 RSA 激活码" {
    $code = New-TestActivationCode -Rsa $rsa -ActivationId "TEST-VALID-001" -ExpiresAt "2027-08-28T00:00:00Z"
    $result = Invoke-WindowsActivationValidation -Code $code -PublicKeyPath $testPublicKeyPath -RevokedIdsPath $testRevokedIdsPath
    if ($result.ExitCode -ne 0) {
        throw "有效激活码被拒绝（退出码 $($result.ExitCode)）：$($result.Output)"
    }
}

Test-Case "macOS 接受有效的 WIKI2 RSA 激活码" {
    $code = New-TestActivationCode -Rsa $rsa -ActivationId "TEST-VALID-002" -ExpiresAt "2027-08-28T00:00:00Z"
    $result = Invoke-MacActivationValidation -Code $code -PublicKeyPath $testPublicKeyPath -RevokedIdsPath $testRevokedIdsPath
    if ($result.ExitCode -ne 0) {
        throw "有效激活码被拒绝（退出码 $($result.ExitCode)）"
    }
}

Test-Case "Windows 和 macOS 拒绝旧版共享秘密格式" {
    Assert-RejectedOnBothPlatforms -Code "WIKI-ABCD-EFGH-1234" -PublicKeyPath $testPublicKeyPath -RevokedIdsPath $testRevokedIdsPath
}

Test-Case "Windows 和 macOS 拒绝签名被篡改的 WIKI2 激活码" {
    $code = New-TestActivationCode -Rsa $rsa -ActivationId "TEST-TAMPERED-001" -ExpiresAt "2027-08-28T00:00:00Z"
    $segments = $code.Split('.')
    $replacement = if ($segments[2][0] -eq 'A') { 'B' } else { 'A' }
    $segments[2] = $replacement + $segments[2].Substring(1)
    Assert-RejectedOnBothPlatforms -Code ($segments -join '.') -PublicKeyPath $testPublicKeyPath -RevokedIdsPath $testRevokedIdsPath
}

Test-Case "Windows 和 macOS 拒绝过期的 WIKI2 激活码" {
    $code = New-TestActivationCode -Rsa $rsa -ActivationId "TEST-EXPIRED-001" -ExpiresAt "2026-08-27T23:59:59Z"
    Assert-RejectedOnBothPlatforms -Code $code -PublicKeyPath $testPublicKeyPath -RevokedIdsPath $testRevokedIdsPath
}

Test-Case "Windows 和 macOS 拒绝已撤销的激活 ID" {
    [IO.File]::WriteAllText($testRevokedIdsPath, "TEST-REVOKED-001`n", [Text.UTF8Encoding]::new($false))
    try {
        $code = New-TestActivationCode -Rsa $rsa -ActivationId "TEST-REVOKED-001" -ExpiresAt "2027-08-28T00:00:00Z"
        Assert-RejectedOnBothPlatforms -Code $code -PublicKeyPath $testPublicKeyPath -RevokedIdsPath $testRevokedIdsPath
    } finally {
        [IO.File]::WriteAllText($testRevokedIdsPath, "", [Text.UTF8Encoding]::new($false))
    }
}

Test-Case "Windows 和 macOS 拒绝不带时区的 expires_at" {
    $code = New-TestActivationCode -Rsa $rsa -ActivationId "TEST-NAIVE-TIME-001" -ExpiresAt "2027-08-28T00:00:00"
    Assert-RejectedOnBothPlatforms -Code $code -PublicKeyPath $testPublicKeyPath -RevokedIdsPath $testRevokedIdsPath
}

Test-Case "Windows 和 macOS 在撤销清单缺失时拒绝激活" {
    $missingRevokedIdsPath = Join-Path $testRoot "missing-revoked-activation-ids.txt"
    $code = New-TestActivationCode -Rsa $rsa -ActivationId "TEST-MISSING-REVOCATION-001" -ExpiresAt "2027-08-28T00:00:00Z"
    Assert-RejectedOnBothPlatforms -Code $code -PublicKeyPath $testPublicKeyPath -RevokedIdsPath $missingRevokedIdsPath
}

Test-Case "Windows 和 macOS 拒绝产品不匹配的 WIKI2 激活码" {
    $code = New-TestActivationCode -Rsa $rsa -ActivationId "TEST-PRODUCT-001" -ExpiresAt "2027-08-28T00:00:00Z" -Product "other-product"
    Assert-RejectedOnBothPlatforms -Code $code -PublicKeyPath $testPublicKeyPath -RevokedIdsPath $testRevokedIdsPath
}

Test-Case "Windows 和 macOS 拒绝版本不匹配的 WIKI2 激活码" {
    $code = New-TestActivationCode -Rsa $rsa -ActivationId "TEST-VERSION-001" -ExpiresAt "2027-08-28T00:00:00Z" -Version "1.0"
    Assert-RejectedOnBothPlatforms -Code $code -PublicKeyPath $testPublicKeyPath -RevokedIdsPath $testRevokedIdsPath
}

Test-Case "仓库只交付公钥并阻断签发侧私密产物" {
    $publicKey = Join-Path $repoRoot "activation-public-key.xml"
    $revokedIds = Join-Path $repoRoot "revoked-activation-ids.txt"
    if (-not (Test-Path -LiteralPath $publicKey -PathType Leaf)) { throw "缺少客户端激活公钥" }
    if (-not (Test-Path -LiteralPath $revokedIds -PathType Leaf)) { throw "缺少撤销 ID 清单" }

    $ignore = Get-Content -LiteralPath (Join-Path $repoRoot ".gitignore") -Raw
    $ignoreLines = @($ignore -split '\r?\n')
    foreach ($pattern in @('activation-private-key*.xml', 'activation-private-key*.pem', 'activation-private-key*.pfx', 'activation-private-key*.key', 'issuer-private/', 'activation-codes.*', 'generate-secure-code.ps1', 'generate-short-code.ps1', 'staging/', 'build/', 'dist/')) {
        if ($ignoreLines -notcontains $pattern) {
            throw ".gitignore 缺少 $pattern"
        }
    }

    $privateFiles = Get-ChildItem -LiteralPath $repoRoot -Recurse -File -ErrorAction Stop |
        Where-Object { $_.Name -match '^activation-private-key' -or $_.FullName -match '[\\/]issuer-private[\\/]' }
    if ($privateFiles) { throw "工作树中出现签发侧私钥文件" }

    $publicIgnored = & git -C $repoRoot check-ignore activation-public-key.xml 2>$null
    if ($LASTEXITCODE -eq 0 -or $publicIgnored) { throw "客户端公钥不应被 .gitignore 排除" }

    $tracked = @(& git -C $repoRoot ls-files)
    if ($tracked -notcontains 'activation-public-key.xml') { throw "客户端公钥尚未进入版本控制" }
    if ($tracked | Where-Object { $_ -match '(^|/)activation-private-key|(^|/)issuer-private/' }) {
        throw "版本控制中出现签发侧私钥文件"
    }
}

Test-Case "激活入口不再保留旧共享秘密并隐藏人工输入" {
    $windows = Get-Content -LiteralPath (Join-Path $repoRoot "setup-win.ps1") -Raw
    $mac = Get-Content -LiteralPath (Join-Path $repoRoot "setup-mac.sh") -Raw
    if (($windows + $mac) -match 'wiki2026salt|\^WIKI-\(') {
        throw "安装入口仍包含旧共享秘密或旧激活格式"
    }
    if ($windows -notmatch 'Read-Host[^\r\n]*-AsSecureString') {
        throw "Windows 激活码输入未隐藏"
    }
    if ($mac -notmatch 'read -r -s ACTIVATION_CODE') {
        throw "macOS 激活码输入未隐藏"
    }
}

Test-Case "Windows 和 macOS 均隐藏 API Key 输入" {
    $windows = Get-Content -LiteralPath (Join-Path $repoRoot "setup-win.ps1") -Raw
    $mac = Get-Content -LiteralPath (Join-Path $repoRoot "setup-mac.sh") -Raw
    if ($windows -notmatch 'Read-Host\s+"\s*API Key"\s+-AsSecureString') {
        throw "Windows API Key 输入未隐藏"
    }
    if ($mac -notmatch 'read -r -s API_KEY') {
        throw "macOS API Key 输入未隐藏"
    }
}

Test-Case "Windows 和 macOS 的改模型入口均隐藏 API Key 输入" {
    $windows = Get-Content -LiteralPath (Join-Path $repoRoot "change-model.ps1") -Raw
    $mac = Get-Content -LiteralPath (Join-Path $repoRoot "change-model.sh") -Raw
    if ($windows -notmatch 'Read-Host\s+"\s*API Key"\s+-AsSecureString') {
        throw "Windows 改模型入口的 API Key 输入未隐藏"
    }
    if ($windows -notmatch 'SecureStringToBSTR' -or $windows -notmatch 'ZeroFreeBSTR') {
        throw "Windows 改模型入口未安全释放 API Key 的明文桥接缓冲区"
    }
    if ($mac -notmatch 'read -r -s API_KEY') {
        throw "macOS 改模型入口的 API Key 输入未隐藏"
    }
}

$rsa.Dispose()
Remove-Item -LiteralPath $testRoot -Recurse -Force

Write-Host "`n安全回归：$passed 通过，$failed 失败"
if ($failed -gt 0) { exit 1 }
