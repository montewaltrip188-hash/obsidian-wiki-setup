# Obsidian LLM Wiki - Windows 一键下载脚本
# 用法: powershell -ExecutionPolicy Bypass -File download-win.ps1

$d = if (Test-Path "D:\") { "D:\OB" } else { "C:\OB" }
mkdir $d -Force | Out-Null
cd $d

# 1. 查找或安装 7-Zip
$7z = @("C:\Program Files\7-Zip\7z.exe","C:\Program Files (x86)\7-Zip\7z.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $7z) {
    Write-Host "正在下载 7-Zip..." -ForegroundColor Yellow
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest "https://www.7-zip.org/a/7z2600-x64.exe" -OutFile "$d\7z-setup.exe"
        Start-Process "$d\7z-setup.exe" -ArgumentList "/S" -Wait
        Remove-Item "$d\7z-setup.exe" -ErrorAction SilentlyContinue
        Write-Host "7-Zip 安装完成" -ForegroundColor Green
    } catch {
        Write-Host "7-Zip 下载失败，将使用 Windows 资源管理器解压" -ForegroundColor Yellow
    }
    $7z = @("C:\Program Files\7-Zip\7z.exe","C:\Program Files (x86)\7-Zip\7z.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}

# 2. 检测 Obsidian 是否已安装
$obsidianInstalled = (Test-Path "$env:LOCALAPPDATA\Obsidian\Obsidian.exe") -or (Test-Path "$env:LOCALAPPDATA\Programs\Obsidian\Obsidian.exe") -or (Test-Path "$env:ProgramFiles\Obsidian\Obsidian.exe")
$downloadObsidian = $true
if ($obsidianInstalled) {
    Write-Host "检测到 Obsidian 已安装" -ForegroundColor Green
    $downloadObsidian = $false
} else {
    $choice = Read-Host "是否需要下载 Obsidian 安装包？(Y/n)"
    if ($choice -eq 'n' -or $choice -eq 'N') {
        $downloadObsidian = $false
    }
}

# 3. 计算文件数
$fileCount = 3
if ($downloadObsidian) { $fileCount = 6 }

# 4. 下载
Write-Host "正在下载安装包（共${fileCount}个文件）..." -ForegroundColor Yellow
$token = "5e28dbb7eff603a08db961ca67dc32bd"
$base = "https://gitee.com/jiegeng333/obsidian-wiki-setup/releases/download/v2.1"
$baseObs = "https://gitee.com/jiegeng333/obsidian-wiki-setup/releases/download/v1.8"
$idx = 1

Invoke-WebRequest "$base/obsidian-wiki-v2.1-part1.zip?access_token=$token" -OutFile "part1.zip"
Write-Host "  [$idx/$fileCount] part1.zip 完成" -ForegroundColor Green; $idx++
Invoke-WebRequest "$base/obsidian-wiki-v2.1-part2.zip?access_token=$token" -OutFile "part2.zip"
Write-Host "  [$idx/$fileCount] part2.zip 完成" -ForegroundColor Green; $idx++
Invoke-WebRequest "$base/obsidian-wiki-v2.1-part3.zip?access_token=$token" -OutFile "part3.zip"
Write-Host "  [$idx/$fileCount] part3.zip 完成" -ForegroundColor Green; $idx++
if ($downloadObsidian) {
    1..3 | ForEach-Object {
        Invoke-WebRequest "$baseObs/Obsidian-win-part$_.bin?access_token=$token" -OutFile "Obsidian-win-part$_.bin"
        Write-Host "  [$idx/$fileCount] Obsidian-win-part$_.bin 完成" -ForegroundColor Green; $script:idx++
    }
}

# 5. 解压
Write-Host "正在解压..." -ForegroundColor Yellow
if (Test-Path $7z) {
    & $7z x -pwiki2026 -o"$d\install" -y part1.zip | Out-Null
    & $7z x -pwiki2026 -o"$d\install" -y part2.zip | Out-Null
    & $7z x -pwiki2026 -o"$d\install" -y part3.zip | Out-Null
    Write-Host "解压完成" -ForegroundColor Green
} else {
    Write-Host "请手动解压 C:\OB 下的 part1.zip / part2.zip / part3.zip" -ForegroundColor Yellow
    Write-Host "  密码: wiki2026" -ForegroundColor Yellow
    Write-Host "  解压到 C:\OB\install 文件夹" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "解压完成后按回车继续"
}

# 6. 合并 Obsidian 分片
if ($downloadObsidian) {
    Write-Host "正在合并 Obsidian 安装包..." -ForegroundColor Yellow
    mkdir "$d\install\installers" -Force | Out-Null
    $out = [System.IO.File]::Create("$d\install\installers\Obsidian-1.12.7.exe")
    1..3 | ForEach-Object {
        $bytes = [System.IO.File]::ReadAllBytes("$d\Obsidian-win-part$_.bin")
        $out.Write($bytes, 0, $bytes.Length)
    }
    $out.Close()
    Write-Host "合并完成" -ForegroundColor Green
}

# 7. 清理
Remove-Item part1.zip, part2.zip, part3.zip -Force -ErrorAction SilentlyContinue
Remove-Item Obsidian-win-part1.bin, Obsidian-win-part2.bin, Obsidian-win-part3.bin -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  全部完成！" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "请进入 $d\install 双击 install.bat 开始安装" -ForegroundColor Yellow
Write-Host ""
Read-Host "按回车退出"
