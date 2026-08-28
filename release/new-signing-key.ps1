param(
    [Parameter(Mandatory = $true)][string]$PrivateKeyPath,
    [Parameter(Mandatory = $true)][string]$ProtectedPassphrasePath,
    [Parameter(Mandatory = $true)][string]$PublicKeyPath,
    [Parameter(Mandatory = $true)][string]$PolicyPath
)

$ErrorActionPreference = 'Stop'
$entropyLabel = 'junyong-ai/obsidian-wiki-setup/release-signing-v1'

function Write-Blocked([string]$Code) {
    [Console]::Error.WriteLine((@{ status = 'blocked'; error = $Code } | ConvertTo-Json -Compress))
    exit 2
}

function Test-InsideRepository([string]$Repository, [string]$Candidate) {
    $prefix = $Repository.TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    ) + [IO.Path]::DirectorySeparatorChar
    return $Candidate.Equals($Repository, [StringComparison]::OrdinalIgnoreCase) -or
        $Candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Write-AtomicBytes([string]$Path, [byte[]]$Content) {
    $directory = [IO.Path]::GetDirectoryName($Path)
    if (-not [IO.Directory]::Exists($directory)) {
        [IO.Directory]::CreateDirectory($directory) | Out-Null
    }
    $temporary = $Path + '.tmp-' + [guid]::NewGuid().ToString('N')
    try {
        [IO.File]::WriteAllBytes($temporary, $Content)
        [IO.File]::Move($temporary, $Path)
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Write-AtomicText([string]$Path, [string]$Content) {
    Write-AtomicBytes $Path ([Text.UTF8Encoding]::new($false).GetBytes($Content))
}

if (-not $IsWindows) { Write-Blocked 'RELEASE_KEYGEN_WINDOWS_REQUIRED' }

$created = [Collections.Generic.List[string]]::new()
$passphraseBytes = $null
$entropyBytes = $null
try {
    $repository = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
    $privateKey = [IO.Path]::GetFullPath($PrivateKeyPath)
    $protectedPassphrase = [IO.Path]::GetFullPath($ProtectedPassphrasePath)
    $publicKey = [IO.Path]::GetFullPath($PublicKeyPath)
    $policy = [IO.Path]::GetFullPath($PolicyPath)
    $allPaths = @($privateKey, $protectedPassphrase, $publicKey, $policy)
    if (($allPaths | Select-Object -Unique).Count -ne 4) {
        Write-Blocked 'RELEASE_KEY_OUTPUT_PATH_COLLISION'
    }
    if (
        (Test-InsideRepository $repository $privateKey) -or
        (Test-InsideRepository $repository $protectedPassphrase)
    ) {
        Write-Blocked 'RELEASE_PRIVATE_MATERIAL_INSIDE_REPOSITORY'
    }
    foreach ($path in $allPaths) {
        if ([IO.File]::Exists($path) -or [IO.Directory]::Exists($path)) {
            Write-Blocked 'RELEASE_KEY_OUTPUT_EXISTS'
        }
    }

    $passphraseBytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(48)
    $passphrase = [Convert]::ToBase64String($passphraseBytes)
    $entropyBytes = [Text.UTF8Encoding]::new($false).GetBytes($entropyLabel)
    $protectedBytes = [Security.Cryptography.ProtectedData]::Protect(
        [Text.UTF8Encoding]::new($false).GetBytes($passphrase),
        $entropyBytes,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $rsa = [Security.Cryptography.RSA]::Create(3072)
    try {
        $pbe = [Security.Cryptography.PbeParameters]::new(
            [Security.Cryptography.PbeEncryptionAlgorithm]::Aes256Cbc,
            [Security.Cryptography.HashAlgorithmName]::SHA256,
            600000
        )
        $privatePem = $rsa.ExportEncryptedPkcs8PrivateKeyPem($passphrase, $pbe)
        $publicPem = $rsa.ExportSubjectPublicKeyInfoPem()
        $publicDer = $rsa.ExportSubjectPublicKeyInfo()
        $keyId = [Convert]::ToHexString(
            [Security.Cryptography.SHA256]::HashData($publicDer)
        ).ToLowerInvariant()

        Write-AtomicText $privateKey $privatePem
        $created.Add($privateKey)
        Write-AtomicBytes $protectedPassphrase $protectedBytes
        $created.Add($protectedPassphrase)
        Write-AtomicText $publicKey $publicPem
        $created.Add($publicKey)
        $policyValue = [ordered]@{
            schema_version = 1
            algorithm = 'RSA-SHA256-PKCS1-v1_5'
            minimum_rsa_bits = 3072
            key_id = $keyId
            public_key = 'release/release-signing-public-key.pem'
            private_key_storage = 'encrypted-pkcs8-dpapi-current-user'
        }
        Write-AtomicText $policy (($policyValue | ConvertTo-Json) + "`n")
        $created.Add($policy)

        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        foreach ($secretPath in @($privateKey, $protectedPassphrase)) {
            & icacls.exe $secretPath '/inheritance:r' '/grant:r' "${identity}:F" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'ACL_FAILED' }
        }
        @{
            status = 'created'
            algorithm = 'RSA-SHA256-PKCS1-v1_5'
            key_id = $keyId
            minimum_rsa_bits = 3072
            private_key_storage = 'encrypted-pkcs8-dpapi-current-user'
        } | ConvertTo-Json -Compress
    } finally {
        $rsa.Dispose()
    }
} catch {
    foreach ($path in $created) {
        if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) }
    }
    Write-Blocked 'RELEASE_KEY_GENERATION_FAILED'
} finally {
    if ($passphraseBytes) { [Array]::Clear($passphraseBytes, 0, $passphraseBytes.Length) }
    if ($entropyBytes) { [Array]::Clear($entropyBytes, 0, $entropyBytes.Length) }
}
