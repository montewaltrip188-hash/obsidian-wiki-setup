# Obsidian LLM Wiki - Windows 一键下载脚本
# 用法: powershell -ExecutionPolicy Bypass -File download-win.ps1

$d = "D:\OB"
mkdir $d -Force | Out-Null
cd $d

# 1. 查找或安装 7-Zip
$7z = "C:\Program Files\7-Zip\7z.exe"
if (-not (Test-Path $7z)) {
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
}

# 2. 下载安装包
Write-Host "正在下载安装包（共5个文件）..." -ForegroundColor Yellow
$base1 = "https://gitee.com/jiegeng333/obsidian-wiki-setup/releases/download/v1.7"
$base2 = "https://gitee.com/jiegeng333/obsidian-wiki-setup/releases/download/v1.8"

Invoke-WebRequest "$base1/obsidian-wiki-v1.4-part1.zip" -OutFile "part1.zip"
Write-Host "  [1/5] part1.zip 完成" -ForegroundColor Green
Invoke-WebRequest "$base1/obsidian-wiki-v1.4-part2.zip" -OutFile "part2.zip"
Write-Host "  [2/5] part2.zip 完成" -ForegroundColor Green
1..3 | ForEach-Object {
    Invoke-WebRequest "$base2/Obsidian-win-part$_.bin" -OutFile "Obsidian-win-part$_.bin"
    Write-Host "  [$($_ + 2)/5] Obsidian-win-part$_.bin 完成" -ForegroundColor Green
}

# 3. 解压
Write-Host "正在解压..." -ForegroundColor Yellow
if (Test-Path $7z) {
    & $7z x -pwiki2026 -o"$d\install" -y part1.zip | Out-Null
    & $7z x -pwiki2026 -o"$d\install" -y part2.zip | Out-Null
    Write-Host "解压完成" -ForegroundColor Green
} else {
    Write-Host "请手动解压 D:\OB 下的 part1.zip 和 part2.zip" -ForegroundColor Yellow
    Write-Host "  密码: wiki2026" -ForegroundColor Yellow
    Write-Host "  解压到 D:\OB\install 文件夹" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "解压完成后按回车继续"
}

# 4. 合并 Obsidian 分片
Write-Host "正在合并 Obsidian 安装包..." -ForegroundColor Yellow
mkdir "$d\install\installers" -Force | Out-Null
$out = [System.IO.File]::Create("$d\install\installers\Obsidian-1.12.7.exe")
1..3 | ForEach-Object {
    $bytes = [System.IO.File]::ReadAllBytes("$d\Obsidian-win-part$_.bin")
    $out.Write($bytes, 0, $bytes.Length)
}
$out.Close()
Write-Host "合并完成" -ForegroundColor Green

# 5. 清理临时文件
Remove-Item part1.zip, part2.zip, Obsidian-win-part1.bin, Obsidian-win-part2.bin, Obsidian-win-part3.bin -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  全部完成！" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "请进入 $d\install 双击 install.bat 开始安装" -ForegroundColor Yellow
Write-Host ""
Read-Host "按回车退出"
