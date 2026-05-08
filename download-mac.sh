#!/bin/bash
# Obsidian LLM Wiki - Mac 一键下载脚本
# Apple Silicon: bash download-mac.sh arm64
# Intel:         bash download-mac.sh x64
# 不加参数默认 arm64

ARCH="${1:-arm64}"
if [ "$ARCH" != "arm64" ] && [ "$ARCH" != "x64" ]; then
    echo "用法: bash download-mac.sh [arm64|x64]"
    exit 1
fi

d=~/Downloads/OB
mkdir -p "$d"
cd "$d"

BASE1="https://gitee.com/jiegeng333/obsidian-wiki-setup/releases/download/v1.7"
BASE2="https://gitee.com/jiegeng333/obsidian-wiki-setup/releases/download/v1.8"

echo "正在下载安装包（共4个文件）..."
curl -L --progress-bar -o "main.zip" "$BASE1/obsidian-wiki-mac-${ARCH}.zip"
echo "  [1/4] main.zip 完成"
for i in 1 2 3; do
    curl -L --progress-bar -o "Obsidian-mac-part${i}.bin" "$BASE2/Obsidian-mac-part${i}.bin"
    echo "  [$((i+1))/4] Obsidian-mac-part${i}.bin 完成"
done

echo "正在解压..."
unzip -P wiki2026 -o main.zip -d install

echo "正在合并 Obsidian 安装包..."
mkdir -p install/installers-mac
cat Obsidian-mac-part*.bin > install/installers-mac/Obsidian-1.12.7.dmg

echo "清理临时文件..."
rm -f main.zip Obsidian-mac-part*.bin

echo ""
echo "============================================"
echo "  全部完成！"
echo "============================================"
echo ""
echo "运行以下命令开始安装:"
echo "  bash ~/Downloads/OB/install/setup-mac.sh"
