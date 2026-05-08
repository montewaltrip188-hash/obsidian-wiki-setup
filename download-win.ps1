# Obsidian LLM Wiki - Windows 一键下载脚本
# 用法: powershell -ExecutionPolicy Bypass -File download-win.ps1

$d = "D:\OB"
mkdir $d -Force | Out-Null
cd $d

# 1. 检查并安装 7-Zip
$7z = "C:\Program Files\7-Zip\7z.exe"
if (-not (Test-Path $7z)) {
    Write-Host "正在下载 7-Zip..." -ForegroundColor Yellow
    Invoke-WebRequest "https://www.7-zip.org/a/7z2600-x64.exe" -OutFile "$d\7z-setup.exe"
    Start-Process "$d\7z-setup.exe" -ArgumentList "/S" -Wait
    Remove-Item "$d\7z-setup.exe"
    Write-Host "7-Zip 安装完成" -ForegroundColor Green
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
& $7z x -pwiki2026 -o"$d\install" -y part1.zip | Out-Null
& $7z x -pwiki2026 -o"$d\install" -y part2.zip | Out-Null
Write-Host "解压完成" -ForegroundColor Green

# 4. 合并 Obsidian 分片
Write-Host "正在合并 Obsidian 安装包..." -ForegroundColor Yellow
cmd /c "copy /b Obsidian-win-part1.bin+Obsidian-win-part2.bin+Obsidian-win-part3.bin install\installers\Obsidian-1.12.7.exe"
Write-Host "合并完成" -ForegroundColor Green

# 5. 清理临时文件
Remove-Item part1.zip, part2.zip, Obsidian-win-part1.bin, Obsidian-win-part2.bin, Obsidian-win-part3.bin -Force

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  全部完成！" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "请进入 $d\install 双击 install.bat 开始安装" -ForegroundColor Yellow
Write-Host ""
Read-Host "按回车退出"
