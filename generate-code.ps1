# ============================================================
# 激活码生成工具（你自己用，不要给客户）
# ============================================================
# 用法: powershell -File generate-code.ps1
#       powershell -File generate-code.ps1 -Count 10
# ============================================================

param([int]$Count = 1)

$secret = "wiki2026salt"

for ($i = 0; $i -lt $Count; $i++) {
    # 生成随机 8 位前缀
    $prefix = -join ((65..90) + (48..57) | Get-Random -Count 8 | ForEach-Object { [char]$_ })
    # 用前缀 + 密钥算校验码（取哈希前4位）
    $raw = "$prefix$secret"
    $hash = [System.BitConverter]::ToString(
        [System.Security.Cryptography.SHA256]::Create().ComputeHash(
            [System.Text.Encoding]::UTF8.GetBytes($raw)
        )
    ).Replace("-","").Substring(0,4)
    $code = "WIKI-$($prefix.Substring(0,4))-$($prefix.Substring(4,4))-$hash"
    Write-Host $code
}
