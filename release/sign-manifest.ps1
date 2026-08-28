param(
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][string]$SignaturePath,
    [Parameter(Mandatory = $true)][string]$PrivateKeyPath
)

$ErrorActionPreference = 'Stop'

function Write-Blocked([string]$Code) {
    [Console]::Error.WriteLine((@{ status = 'blocked'; error = $Code } | ConvertTo-Json -Compress))
    exit 2
}

try {
    $manifest = (Resolve-Path -LiteralPath $ManifestPath).Path
    $privateKey = (Resolve-Path -LiteralPath $PrivateKeyPath).Path
    $repository = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
    $repositoryPrefix = $repository.TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    ) + [IO.Path]::DirectorySeparatorChar
    if (
        $privateKey.Equals($repository, [StringComparison]::OrdinalIgnoreCase) -or
        $privateKey.StartsWith($repositoryPrefix, [StringComparison]::OrdinalIgnoreCase)
    ) {
        Write-Blocked 'RELEASE_PRIVATE_KEY_INSIDE_REPOSITORY'
    }
    $signature = [IO.Path]::GetFullPath($SignaturePath)
    if ([IO.File]::Exists($signature)) {
        Write-Blocked 'RELEASE_SIGNATURE_OUTPUT_EXISTS'
    }
    $signatureDirectory = [IO.Path]::GetDirectoryName($signature)
    if (-not [IO.Directory]::Exists($signatureDirectory)) {
        [IO.Directory]::CreateDirectory($signatureDirectory) | Out-Null
    }
    $rsa = [Security.Cryptography.RSA]::Create()
    try {
        $rsa.ImportFromPem([IO.File]::ReadAllText($privateKey))
        if ($rsa.KeySize -lt 3072) {
            Write-Blocked 'RELEASE_RSA_KEY_TOO_SMALL'
        }
        $publicDer = $rsa.ExportSubjectPublicKeyInfo()
        $keyId = [Convert]::ToHexString(
            [Security.Cryptography.SHA256]::HashData($publicDer)
        ).ToLowerInvariant()
        $manifestBytes = [IO.File]::ReadAllBytes($manifest)
        $signatureBytes = $rsa.SignData(
            $manifestBytes,
            [Security.Cryptography.HashAlgorithmName]::SHA256,
            [Security.Cryptography.RSASignaturePadding]::Pkcs1
        )
        $temporary = $signature + '.tmp-' + [guid]::NewGuid().ToString('N')
        try {
            [IO.File]::WriteAllBytes($temporary, $signatureBytes)
            [IO.File]::Move($temporary, $signature)
        } finally {
            if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
        }
        @{
            status = 'signed'
            algorithm = 'RSA-SHA256-PKCS1-v1_5'
            key_id = $keyId
            manifest_sha256 = [Convert]::ToHexString(
                [Security.Cryptography.SHA256]::HashData($manifestBytes)
            ).ToLowerInvariant()
            signature_sha256 = [Convert]::ToHexString(
                [Security.Cryptography.SHA256]::HashData($signatureBytes)
            ).ToLowerInvariant()
        } | ConvertTo-Json -Compress
    } finally {
        $rsa.Dispose()
    }
} catch {
    Write-Blocked 'RELEASE_SIGNING_FAILED'
}
