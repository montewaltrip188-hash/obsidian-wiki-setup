#!/bin/bash
# Obsidian LLM Wiki - Mac 一键下载脚本
# Apple Silicon: curl -sL "https://gitee.com/jiegeng333/obsidian-wiki-setup/raw/master/download-mac.sh" | bash
# Intel:         curl -sL "https://gitee.com/jiegeng333/obsidian-wiki-setup/raw/master/download-mac.sh" | bash -s x64

set -e

ARCH="${1:-arm64}"
if [ "$ARCH" != "arm64" ] && [ "$ARCH" != "x64" ]; then
    echo "用法: bash download-mac.sh [arm64|x64]"
    exit 1
fi

d=~/Downloads/OB
rm -rf "$d"
mkdir -p "$d"
cd "$d"

TOKEN="c98f03ebdd388c284fcb93a1b19712a2"
B1="https://gitee.com/jiegeng333/obsidian-wiki-setup/releases/download/v2.0"
B2="https://gitee.com/jiegeng333/obsidian-wiki-setup/releases/download/v1.8"

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

FILE_COUNT=2
[ "$DOWNLOAD_OB" = true ] && FILE_COUNT=5

echo "正在下载安装包（共${FILE_COUNT}个文件）..."
IDX=1

# 下载 main.zip（安装脚本 + Claude Code）
echo -n "  [$IDX/$FILE_COUNT] 正在下载 main.zip ... "
if curl -L --progress-bar -o "main.zip" "${B1}/obsidian-wiki-mac-${ARCH}.zip?access_token=${TOKEN}" && [ -s main.zip ]; then
    echo "完成 ($(du -h main.zip | cut -f1))"
else
    echo "失败，请检查网络后重试"
    exit 1
fi
IDX=$((IDX+1))

# 下载 vault.zip（知识库模板）
echo -n "  [$IDX/$FILE_COUNT] 正在下载 vault.zip ... "
if curl -L --progress-bar -o "vault.zip" "${B2}/vault.zip?access_token=${TOKEN}" && [ -s vault.zip ]; then
    echo "完成 ($(du -h vault.zip | cut -f1))"
else
    echo "失败，请检查网络后重试"
    exit 1
fi
IDX=$((IDX+1))

# 下载 Obsidian 分片
if [ "$DOWNLOAD_OB" = true ]; then
    for i in 1 2 3; do
        echo -n "  [$IDX/$FILE_COUNT] 正在下载 Obsidian-mac-part${i}.bin ... "
        if curl -L --progress-bar -o "Obsidian-mac-part${i}.bin" "${B2}/Obsidian-mac-part${i}.bin?access_token=${TOKEN}" && [ -s "Obsidian-mac-part${i}.bin" ]; then
            echo "完成 ($(du -h Obsidian-mac-part${i}.bin | cut -f1))"
        else
            echo "失败，请稍后手动安装 Obsidian: https://obsidian.md"
            DOWNLOAD_OB=false
            break
        fi
        IDX=$((IDX+1))
    done
fi

# 验证 main.zip 有效性
if ! unzip -t -P wiki2026 main.zip > /dev/null 2>&1; then
    echo "main.zip 文件损坏，请重试"
    exit 1
fi

# 解压
echo "正在解压 main.zip ..."
unzip -q -P wiki2026 -o main.zip -d install

if [ ! -f "install/setup-mac.sh" ]; then
    echo "解压失败，请重试"
    ls -la install/
    exit 1
fi
echo "解压完成: $(ls install/ | tr '\n' ' ')"

# vault.zip 放入 install（覆盖旧版）
mv vault.zip install/
echo "vault.zip 已放入安装目录"

# 合并 Obsidian 分片
if [ "$DOWNLOAD_OB" = true ]; then
    echo "正在合并 Obsidian 安装包 ..."
    mkdir -p install/installers-mac
    cat Obsidian-mac-part*.bin > install/installers-mac/Obsidian-1.12.7.dmg
    echo "合并完成 ($(du -h install/installers-mac/Obsidian-1.12.7.dmg | cut -f1))"
fi

# 清理
rm -f main.zip Obsidian-mac-part*.bin

echo ""
echo "============================================"
echo "  全部完成！"
echo "============================================"
echo ""
echo "运行以下命令开始安装:"
echo "  bash ~/Downloads/OB/install/setup-mac.sh"
