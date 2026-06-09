#!/bin/bash
# Obsidian LLM Wiki - Mac 一键下载脚本
# Apple Silicon: curl -sL "https://gitee.com/jiegeng333/obsidian-wiki-setup/raw/master/download-mac.sh" | bash
# Intel:         curl -sL "https://gitee.com/jiegeng333/obsidian-wiki-setup/raw/master/download-mac.sh" | bash -s x64

set -e

green()  { printf "\033[32m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }
red()    { printf "\033[31m%s\033[0m\n" "$1"; }

ARCH="${1:-arm64}"
if [ "$ARCH" != "arm64" ] && [ "$ARCH" != "x64" ]; then
    echo "用法: bash download-mac.sh [arm64|x64]"
    exit 1
fi

d=~/Downloads/OB
rm -rf "$d"
mkdir -p "$d"
cd "$d"

TOKEN="5e28dbb7eff603a08db961ca67dc32bd"
BASE="https://gitee.com/jiegeng333/obsidian-wiki-setup/releases/download/v2.1"

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

# 选择 AI 编码工具
DOWNLOAD_CC=false
DOWNLOAD_CODEX=false
echo ""
yellow "请选择要安装的 AI 编码工具:"
echo "  1. Claude Code (推荐)"
echo "  2. Codex (OpenAI)"
echo "  3. 两者都安装"
echo "  4. 都不安装（稍后手动安装）"
printf "请输入编号 (默认 1): "
read -r TOOL_CHOICE
[ -z "$TOOL_CHOICE" ] && TOOL_CHOICE="1"
case "$TOOL_CHOICE" in
    1) DOWNLOAD_CC=true ;;
    2) DOWNLOAD_CODEX=true ;;
    3) DOWNLOAD_CC=true; DOWNLOAD_CODEX=true ;;
    4) ;;
    *) DOWNLOAD_CC=true ;;
esac

FILE_COUNT=1
[ "$DOWNLOAD_CC" = true ] && FILE_COUNT=$((FILE_COUNT+1))
[ "$DOWNLOAD_CODEX" = true ] && FILE_COUNT=$((FILE_COUNT+1))
[ "$DOWNLOAD_OB" = true ] && FILE_COUNT=$((FILE_COUNT+3))

echo "正在并行下载安装包（共${FILE_COUNT}个文件）..."

# 并行下载所有文件
PIDS=""
NAMES=""

curl -L -s -o "main.zip" "${BASE}/obsidian-wiki-mac-part1.zip?access_token=${TOKEN}" &
PIDS="$PIDS $!"
NAMES="main.zip"

if [ "$DOWNLOAD_CC" = true ]; then
    curl -L -s -o "claude-part.zip" "${BASE}/obsidian-wiki-mac-part2-${ARCH}.zip?access_token=${TOKEN}" &
    PIDS="$PIDS $!"
    NAMES="$NAMES claude-part.zip"
fi

if [ "$DOWNLOAD_CODEX" = true ]; then
    if [ "$ARCH" = "arm64" ]; then
        curl -L -s -o "codex-part.zip" "${BASE}/codex-mac-arm64.zip?access_token=${TOKEN}" &
        PIDS="$PIDS $!"
        NAMES="$NAMES codex-part.zip"
    else
        echo "  [提示] Codex Intel Mac 版暂未打包，安装时将在线安装"
        FILE_COUNT=$((FILE_COUNT-1))
    fi
fi

if [ "$DOWNLOAD_OB" = true ]; then
    for i in 1 2 3; do
        curl -L -s -o "Obsidian-mac-part${i}.bin" "${BASE}/Obsidian-mac-part${i}.bin?access_token=${TOKEN}" &
        PIDS="$PIDS $!"
        NAMES="$NAMES Obsidian-mac-part${i}.bin"
    done
fi

# 等待所有下载完成并检查结果（临时关闭 set -e，避免 wait 失败直接退出）
set +e
FAILED=""
IDX=1
for PID in $PIDS; do
    NAME=$(echo "$NAMES" | cut -d' ' -f$IDX)
    if wait $PID && [ -s "$NAME" ]; then
        echo "  [$IDX/$FILE_COUNT] $NAME 完成 ($(du -h "$NAME" | cut -f1))"
    else
        echo "  [$IDX/$FILE_COUNT] $NAME 失败"
        FAILED="$FAILED $NAME"
    fi
    IDX=$((IDX+1))
done

if [ -n "$FAILED" ]; then
    echo "以下文件下载失败:$FAILED"
    echo "请检查网络后重试"
    # main.zip 是必须的，失败则退出
    if echo "$FAILED" | grep -q "main.zip"; then
        exit 1
    fi
    # CC/Codex 失败仅警告，Obsidian 失败则跳过
    if echo "$FAILED" | grep -q "Obsidian"; then
        echo "将跳过 Obsidian 安装包，请稍后手动安装: https://obsidian.md"
        DOWNLOAD_OB=false
    fi
fi
set -e

# 验证 main.zip 有效性
if ! unzip -t -P wiki2026 main.zip > /dev/null 2>&1; then
    echo "main.zip 文件损坏，请重试"
    exit 1
fi

# 解压
echo "正在解压 main.zip ..."
unzip -q -P wiki2026 -o main.zip -d install

if [ -f "claude-part.zip" ]; then
    echo "正在解压 claude-${ARCH} ..."
    unzip -q -P wiki2026 -o claude-part.zip -d install
fi

if [ -f "codex-part.zip" ]; then
    echo "正在解压 codex-${ARCH} ..."
    unzip -q -P wiki2026 -o codex-part.zip -d install
fi

if [ ! -f "install/setup-mac.sh" ]; then
    echo "解压失败，请重试"
    ls -la install/
    exit 1
fi
echo "解压完成: $(ls install/ | tr '\n' ' ')"

# 合并 Obsidian 分片
if [ "$DOWNLOAD_OB" = true ]; then
    echo "正在合并 Obsidian 安装包 ..."
    mkdir -p install/installers-mac
    cat Obsidian-mac-part*.bin > install/installers-mac/Obsidian-1.12.7.dmg
    echo "合并完成 ($(du -h install/installers-mac/Obsidian-1.12.7.dmg | cut -f1))"
fi

# 清理
rm -f main.zip claude-part.zip codex-part.zip Obsidian-mac-part*.bin

echo ""
echo "============================================"
echo "  全部完成！"
echo "============================================"
echo ""
echo "运行以下命令开始安装:"
echo "  bash ~/Downloads/OB/install/setup-mac.sh"
