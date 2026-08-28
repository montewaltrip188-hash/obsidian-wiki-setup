param(
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][string]$SignaturePath,
    [Parameter(Mandatory = $true)][string]$PublicKeyPath
)

$ErrorActionPreference = 'Stop'

function Write-Blocked([string]$Code) {
    [Console]::Error.WriteLine((@{ status = 'blocked'; error = $Code } | ConvertTo-Json -Compress))
    exit 2
}

try {
    $manifest = (Resolve-Path -LiteralPath $ManifestPath).Path
    $signature = (Resolve-Path -LiteralPath $SignaturePath).Path
    $publicKey = (Resolve-Path -LiteralPath $PublicKeyPath).Path
    $rsa = [Security.Cryptography.RSA]::Create()
    try {
        $rsa.ImportFromPem([IO.File]::ReadAllText($publicKey))
        if ($rsa.KeySize -lt 3072) {
            Write-Blocked 'RELEASE_RSA_KEY_TOO_SMALL'
        }
        $publicDer = $rsa.ExportSubjectPublicKeyInfo()
        $keyId = [Convert]::ToHexString(
            [Security.Cryptography.SHA256]::HashData($publicDer)
        ).ToLowerInvariant()
        $manifestBytes = [IO.File]::ReadAllBytes($manifest)
        $signatureBytes = [IO.File]::ReadAllBytes($signature)
        $valid = $rsa.VerifyData(
            $manifestBytes,
            $signatureBytes,
            [Security.Cryptography.HashAlgorithmName]::SHA256,
            [Security.Cryptography.RSASignaturePadding]::Pkcs1
        )
        if (-not $valid) { Write-Blocked 'RELEASE_SIGNATURE_INVALID' }
        @{
            status = 'verified'
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
    if ($_.Exception.Message -eq 'RELEASE_SIGNATURE_INVALID') {
        Write-Blocked 'RELEASE_SIGNATURE_INVALID'
    }
    Write-Blocked 'RELEASE_SIGNATURE_VERIFICATION_FAILED'
}
