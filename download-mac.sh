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

# 检测 Obsidian 是否已安装
DOWNLOAD_OB=true
if [ -d "/Applications/Obsidian.app" ]; then
    echo "检测到 Obsidian 已安装，跳过下载 Obsidian 安装包"
    DOWNLOAD_OB=false
else
    printf "是否需要下载 Obsidian 安装包？(Y/n): "
    read -r OB_CHOICE
    if [ "$OB_CHOICE" = "n" ] || [ "$OB_CHOICE" = "N" ]; then
        DOWNLOAD_OB=false
        echo "跳过 Obsidian 下载"
    fi
fi

if [ "$DOWNLOAD_OB" = true ]; then
    FILE_COUNT=4
else
    FILE_COUNT=1
fi

echo "正在下载安装包（共${FILE_COUNT}个文件）..."
IDX=1
curl -L --progress-bar -o "main.zip" "$BASE1/obsidian-wiki-mac-${ARCH}.zip"
echo "  [$IDX/$FILE_COUNT] main.zip 完成"; IDX=$((IDX+1))
if [ "$DOWNLOAD_OB" = true ]; then
    for i in 1 2 3; do
        curl -L --progress-bar -o "Obsidian-mac-part${i}.bin" "$BASE2/Obsidian-mac-part${i}.bin"
        echo "  [$IDX/$FILE_COUNT] Obsidian-mac-part${i}.bin 完成"; IDX=$((IDX+1))
    done
fi

echo "正在解压..."
unzip -P wiki2026 -o main.zip -d install

if [ "$DOWNLOAD_OB" = true ]; then
    echo "正在合并 Obsidian 安装包..."
    mkdir -p install/installers-mac
    cat Obsidian-mac-part*.bin > install/installers-mac/Obsidian-1.12.7.dmg
fi

echo "清理临时文件..."
rm -f main.zip Obsidian-mac-part*.bin

echo ""
echo "============================================"
echo "  全部完成！"
echo "============================================"
echo ""
echo "运行以下命令开始安装:"
echo "  bash ~/Downloads/OB/install/setup-mac.sh"
