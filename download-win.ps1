param(
    [string]$DestinationRoot = $(if (Test-Path -LiteralPath 'D:\') { 'D:\OB' } else { 'C:\OB' })
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repository = 'montewaltrip188-hash/obsidian-wiki-setup'
$StableUrl = "https://raw.githubusercontent.com/$Repository/main/release/stable.json"
$ExpectedKeyId = 'c1f596094a9a54ada888502a2ab7ef6bc5fecf82d4281dd4bbae2ae7bc9d9938'
$ExpectedXmlSha256 = '3ab5cb740f3e92d3230561fe231f0f761e5fa1c3c058483a2ed2a48071b4245b'
$ExpectedPemSha256 = '3cb1a3fec3d028d57735bb576939bd0c49f7589aa2edd8ff61ac41f3d5cd0802'

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-ReleaseUrl([string]$Tag, [string]$Name) {
    return "https://github.com/$Repository/releases/download/$Tag/$Name"
}

function Save-HttpsFile([string]$Url, [string]$Path) {
    $uri = [Uri]$Url
    if ($uri.Scheme -ne 'https' -or $uri.Host -notin @('github.com', 'raw.githubusercontent.com')) {
        throw "拒绝非 GitHub HTTPS 下载地址：$Url"
    }
    Invoke-WebRequest -UseBasicParsing -Uri $uri -OutFile $Path
}

function Assert-FileRecord([string]$Path, $Record, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label 不存在"
    }
    $actualSize = (Get-Item -LiteralPath $Path).Length
    $actualSha256 = Get-Sha256 $Path
    if ($actualSize -ne [int64]$Record.size -or $actualSha256 -ne ([string]$Record.sha256).ToLowerInvariant()) {
        throw "$Label 的长度或 SHA-256 不匹配"
    }
}

$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$work = Join-Path $tempRoot ('obsidian-wiki-download-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($work) | Out-Null

try {
    $stablePath = Join-Path $work 'stable.json'
    Save-HttpsFile $StableUrl $stablePath
    $stable = Get-Content -LiteralPath $stablePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        $stable.pointer_format -ne 1 -or
        $stable.channel -ne 'stable' -or
        $stable.release_state -ne 'stable' -or
        $stable.repository -ne $Repository -or
        $stable.tag -ne ('v' + $stable.bundle_version) -or
        $stable.trust.key_id -ne $ExpectedKeyId -or
        ([string]$stable.trust.xml.sha256).ToLowerInvariant() -ne $ExpectedXmlSha256 -or
        ([string]$stable.trust.pem.sha256).ToLowerInvariant() -ne $ExpectedPemSha256
    ) {
        throw 'stable.json 合同或固定信任根不匹配'
    }

    $tag = [string]$stable.tag
    $asset = $stable.assets.'windows-x64'
    $expectedUrls = @{
        manifest = Get-ReleaseUrl $tag ([string]$stable.manifest.name)
        signature = Get-ReleaseUrl $tag ([string]$stable.signature.name)
        asset = Get-ReleaseUrl $tag ([string]$asset.name)
        xml = "https://raw.githubusercontent.com/$Repository/$tag/release/release-signing-public-key.xml"
    }
    if (
        $stable.manifest.url -ne $expectedUrls.manifest -or
        $stable.signature.url -ne $expectedUrls.signature -or
        $asset.url -ne $expectedUrls.asset -or
        $stable.trust.xml.url -ne $expectedUrls.xml
    ) {
        throw 'stable.json 含非预期不可变下载地址'
    }

    $manifestPath = Join-Path $work 'release-manifest.json'
    $signaturePath = Join-Path $work 'release-manifest.sig'
    $publicKeyPath = Join-Path $work 'release-signing-public-key.xml'
    $assetPath = Join-Path $work ([string]$asset.name)
    Save-HttpsFile $stable.manifest.url $manifestPath
    Save-HttpsFile $stable.signature.url $signaturePath
    Save-HttpsFile $stable.trust.xml.url $publicKeyPath
    Save-HttpsFile $asset.url $assetPath
    Assert-FileRecord $manifestPath $stable.manifest 'release manifest'
    Assert-FileRecord $signaturePath $stable.signature 'release signature'
    Assert-FileRecord $publicKeyPath $stable.trust.xml 'release public key'
    Assert-FileRecord $assetPath $asset 'Windows 安装资产'

    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        $manifest.release_state -ne 'stable' -or
        $manifest.bundle_version -ne $stable.bundle_version -or
        $manifest.required_signature.algorithm -ne 'RSA-SHA256-PKCS1-v1_5' -or
        $manifest.required_signature.key_id -ne $ExpectedKeyId
    ) {
        throw 'release manifest 的稳定版本或签名合同不匹配'
    }
    $assetRecord = $manifest.files | Where-Object { $_.path -eq ('assets/' + $asset.name) }
    if (
        @($assetRecord).Count -ne 1 -or
        $assetRecord.sha256 -ne $asset.sha256 -or
        [int64]$assetRecord.size -ne [int64]$asset.size
    ) {
        throw 'Windows 安装资产未被签名 manifest 唯一绑定'
    }

    [xml]$keyXml = Get-Content -LiteralPath $publicKeyPath -Raw -Encoding UTF8
    $parameters = New-Object Security.Cryptography.RSAParameters
    $parameters.Modulus = [Convert]::FromBase64String($keyXml.RSAKeyValue.Modulus)
    $parameters.Exponent = [Convert]::FromBase64String($keyXml.RSAKeyValue.Exponent)
    $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider
    try {
        $rsa.ImportParameters($parameters)
        $signatureValid = $rsa.VerifyData(
            [IO.File]::ReadAllBytes($manifestPath),
            'SHA256',
            [IO.File]::ReadAllBytes($signaturePath)
        )
    } finally {
        $rsa.Dispose()
    }
    if (-not $signatureValid) {
        throw 'release manifest 的 RSA 签名无效'
    }

    $destinationRootFull = [IO.Path]::GetFullPath($DestinationRoot)
    [IO.Directory]::CreateDirectory($destinationRootFull) | Out-Null
    $destination = Join-Path $destinationRootFull ("Obsidian-LLM-Wiki-" + $stable.bundle_version)
    if (Test-Path -LiteralPath $destination) {
        throw "目标已存在，拒绝覆盖：$destination"
    }
    $staging = Join-Path $destinationRootFull ('.obsidian-wiki-staging-' + [Guid]::NewGuid().ToString('N'))
    try {
        Expand-Archive -LiteralPath $assetPath -DestinationPath $staging
        if (-not (Test-Path -LiteralPath (Join-Path $staging 'setup-win.ps1') -PathType Leaf)) {
            throw '解压后的安装入口缺失'
        }
        Move-Item -LiteralPath $staging -Destination $destination
    } finally {
        if (Test-Path -LiteralPath $staging) {
            $stagingFull = [IO.Path]::GetFullPath($staging)
            if (-not $stagingFull.StartsWith($destinationRootFull.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
                throw '拒绝清理目标根目录之外的 staging'
            }
            Remove-Item -LiteralPath $stagingFull -Recurse -Force
        }
    }

    Write-Host "下载、SHA-256 和 RSA 验签完成：$destination" -ForegroundColor Green
    Write-Host "下一步：powershell -ExecutionPolicy Bypass -File `"$destination\setup-win.ps1`"" -ForegroundColor Yellow
} finally {
    $workFull = [IO.Path]::GetFullPath($work)
    if (Test-Path -LiteralPath $workFull) {
        if (-not $workFull.StartsWith($tempRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw '拒绝清理系统临时目录之外的下载缓存'
        }
        Remove-Item -LiteralPath $workFull -Recurse -Force
    }
}
