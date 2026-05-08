#!/bin/bash
# ============================================================
# Obsidian LLM Wiki - macOS 一键安装脚本
# ============================================================
# 用法: 打开终端，运行: bash setup-mac.sh
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STEP=1

green()  { printf "\033[32m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }
red()    { printf "\033[31m%s\033[0m\n" "$1"; }
step()   { echo ""; printf "\033[36m[%d] %s\033[0m\n" "$STEP" "$1"; STEP=$((STEP+1)); }

echo ""
green "============================================"
green "  Obsidian LLM Wiki 一键安装 (macOS)"
green "============================================"

# ----------------------------------------------------------
# 0. 激活码验证
# ----------------------------------------------------------
step "验证激活码..."
printf "  请输入激活码: "
read -r ACTIVATION_CODE

if [[ "$ACTIVATION_CODE" =~ ^WIKI-([A-Z0-9]{4})-([A-Z0-9]{4})-([A-F0-9]{4})$ ]]; then
    PREFIX="${BASH_REMATCH[1]}${BASH_REMATCH[2]}"
    INPUT_HASH="${BASH_REMATCH[3]}"
    SECRET="wiki2026salt"
    COMPUTED_HASH=$(printf '%s' "${PREFIX}${SECRET}" | shasum -a 256 | cut -c1-4 | tr 'a-f' 'A-F')
    if [ "$INPUT_HASH" != "$COMPUTED_HASH" ]; then
        red "  激活码无效，请联系服务提供者获取正确的激活码"
        exit 1
    fi
    green "  激活码验证通过"
else
    red "  激活码格式错误，正确格式: WIKI-XXXX-XXXX-XXXX"
    exit 1
fi

# ----------------------------------------------------------
# 1. 安装 Obsidian
# ----------------------------------------------------------
step "检查 Obsidian..."
if [ -d "/Applications/Obsidian.app" ]; then
    green "  Obsidian 已安装"
else
    LOCAL_DMG="$SCRIPT_DIR/installers-mac/Obsidian-1.12.7.dmg"
    if [ ! -f "$LOCAL_DMG" ]; then
        # 尝试从分片合并
        PART1="$SCRIPT_DIR/installers-mac/Obsidian-mac-part1.bin"
        if [ -f "$PART1" ]; then
            yellow "  正在合并 Obsidian 安装包分片..."
            cat "$SCRIPT_DIR/installers-mac/Obsidian-mac-part"*.bin > "$LOCAL_DMG"
            green "  合并完成"
        fi
    fi
    if [ -f "$LOCAL_DMG" ]; then
        yellow "  正在安装 Obsidian..."
        MOUNT_DIR=$(hdiutil attach "$LOCAL_DMG" -nobrowse -quiet | grep "/Volumes" | awk '{print $NF}')
        if [ -d "$MOUNT_DIR/Obsidian.app" ]; then
            cp -R "$MOUNT_DIR/Obsidian.app" /Applications/
            hdiutil detach "$MOUNT_DIR" -quiet
            green "  Obsidian 安装完成"
        else
            red "  Obsidian 安装失败，请手动打开 dmg 文件安装"
            hdiutil detach "$MOUNT_DIR" -quiet 2>/dev/null
        fi
    else
        yellow "  未找到 Obsidian 安装包，请手动下载安装: https://obsidian.md"
    fi
fi

# ----------------------------------------------------------
# 2. 安装 Git（通过 Xcode Command Line Tools）
# ----------------------------------------------------------
step "检查 Git..."
if command -v git &>/dev/null; then
    green "  Git 已安装: $(git --version)"
else
    yellow "  Git 未安装，正在安装 Xcode Command Line Tools..."
    yellow "  （从 Apple 服务器下载，请耐心等待）"
    xcode-select --install 2>/dev/null || true
    echo ""
    yellow "  请在弹出的窗口中点击「安装」"
    yellow "  安装完成后按回车继续..."
    read -r
    if command -v git &>/dev/null; then
        green "  Git 安装成功: $(git --version)"
    else
        red "  Git 安装失败，请手动运行: xcode-select --install"
        exit 1
    fi
fi

# ----------------------------------------------------------
# 2. 安装 Claude Code（使用本地二进制文件）
# ----------------------------------------------------------
step "检查 Claude Code..."
if command -v claude &>/dev/null; then
    green "  Claude Code 已安装: $(claude --version 2>/dev/null || echo '已安装')"
else
    # 检测芯片架构
    ARCH=$(uname -m)
    if [ "$ARCH" = "arm64" ]; then
        LOCAL_CLAUDE="$SCRIPT_DIR/installers-mac/claude-arm64"
        ARCH_NAME="Apple Silicon"
    else
        LOCAL_CLAUDE="$SCRIPT_DIR/installers-mac/claude-x64"
        ARCH_NAME="Intel"
    fi

    if [ -f "$LOCAL_CLAUDE" ]; then
        yellow "  使用本地安装包部署 Claude Code ($ARCH_NAME)..."
        CLAUDE_INSTALL_DIR="$HOME/.local/bin"
        mkdir -p "$CLAUDE_INSTALL_DIR"
        cp "$LOCAL_CLAUDE" "$CLAUDE_INSTALL_DIR/claude"
        chmod +x "$CLAUDE_INSTALL_DIR/claude"

        # 添加到 PATH
        SHELL_RC="$HOME/.zshrc"
        [ -f "$HOME/.bash_profile" ] && [ ! -f "$SHELL_RC" ] && SHELL_RC="$HOME/.bash_profile"
        if ! grep -q '.local/bin' "$SHELL_RC" 2>/dev/null; then
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
        fi
        export PATH="$HOME/.local/bin:$PATH"
        green "  Claude Code 已部署到: $CLAUDE_INSTALL_DIR/claude"
    else
        yellow "  本地安装包未找到，尝试在线安装..."
        if command -v brew &>/dev/null; then
            brew install --cask claude-code
        elif command -v npm &>/dev/null; then
            npm install -g @anthropic-ai/claude-code
        else
            red "  安装失败：未找到本地安装包，也无法在线安装"
            red "  请确保 installers-mac/ 目录中包含 claude-arm64 或 claude-x64"
            exit 1
        fi
    fi

    if command -v claude &>/dev/null || [ -x "$HOME/.local/bin/claude" ]; then
        green "  Claude Code 安装成功"
    else
        red "  Claude Code 安装失败"
        exit 1
    fi
fi

# ----------------------------------------------------------
# 3. 配置 AI 模型
# ----------------------------------------------------------
step "配置 AI 模型..."
CLAUDE_DIR="$HOME/.claude"
SETTINGS_PATH="$CLAUDE_DIR/settings.json"
SKIP_API=false

if [ -f "$SETTINGS_PATH" ]; then
    EXISTING_KEY=$(python3 -c "import json; d=json.load(open('$SETTINGS_PATH')); print(d.get('env',{}).get('ANTHROPIC_AUTH_TOKEN',''))" 2>/dev/null || echo "")
    if [ -n "$EXISTING_KEY" ] && [ "$EXISTING_KEY" != "sk-YOUR-API-KEY" ]; then
        green "  API 已配置，跳过"
        SKIP_API=true
    fi
fi

if [ "$SKIP_API" = false ]; then
    echo ""
    yellow "  请选择 AI 模型:"
    echo "    1. DeepSeek (推荐，国内直连，无需 VPN)"
    echo "    2. Claude 原版 (需要 VPN 或海外网络)"
    echo "    3. OpenAI (需要 VPN 或海外网络)"
    echo "    4. 自定义 API"
    printf "  请输入编号 (默认 1): "
    read -r MODEL_CHOICE
    [ -z "$MODEL_CHOICE" ] && MODEL_CHOICE="1"

    case "$MODEL_CHOICE" in
        1)
            BASE_URL="https://api.deepseek.com/anthropic"
            MAIN_MODEL="deepseek-v4-pro"
            FAST_MODEL="deepseek-v4-flash"
            API_URL="https://platform.deepseek.com"
            PROVIDER="DeepSeek"
            ;;
        2)
            BASE_URL="https://api.anthropic.com"
            MAIN_MODEL="claude-sonnet-4-6"
            FAST_MODEL="claude-haiku-4-5-20251001"
            API_URL="https://console.anthropic.com"
            PROVIDER="Claude"
            ;;
        3)
            BASE_URL="https://api.openai.com/v1"
            MAIN_MODEL="gpt-4.1"
            FAST_MODEL="gpt-4.1-mini"
            API_URL="https://platform.openai.com"
            PROVIDER="OpenAI"
            ;;
        4)
            yellow "  请输入 API Base URL (如 https://api.example.com/v1):"
            printf "  Base URL: "; read -r BASE_URL
            yellow "  请输入主模型名称:"
            printf "  主模型: "; read -r MAIN_MODEL
            yellow "  请输入快速模型名称 (可与主模型相同):"
            printf "  快速模型: "; read -r FAST_MODEL
            API_URL="$BASE_URL"
            PROVIDER="自定义"
            ;;
        *)
            BASE_URL="https://api.deepseek.com/anthropic"
            MAIN_MODEL="deepseek-v4-pro"
            FAST_MODEL="deepseek-v4-flash"
            API_URL="https://platform.deepseek.com"
            PROVIDER="DeepSeek"
            ;;
    esac

    echo ""
    green "  已选择: $PROVIDER"
    yellow "  请输入你的 API Key"
    yellow "  (获取地址: $API_URL)"
    printf "  API Key: "
    read -r API_KEY

    if [ -z "$API_KEY" ]; then
        yellow "  未输入 API Key，跳过配置。稍后可手动编辑: $SETTINGS_PATH"
        API_KEY="sk-YOUR-API-KEY"
    fi

    mkdir -p "$CLAUDE_DIR"
    cat > "$SETTINGS_PATH" << JSONEOF
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "$API_KEY",
    "ANTHROPIC_BASE_URL": "$BASE_URL",
    "ANTHROPIC_MODEL": "$MAIN_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "$MAIN_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "$MAIN_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "$FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL": "$FAST_MODEL"
  }
}
JSONEOF
    green "  API 配置完成 ($PROVIDER: $MAIN_MODEL)"
fi

# ----------------------------------------------------------
# 4. 部署 Obsidian Vault
# ----------------------------------------------------------
step "部署知识库..."
DEFAULT_VAULT="$HOME/Documents/ObsidianVault"
echo "  知识库将部署到: $DEFAULT_VAULT"
yellow "  (按回车确认，或输入自定义路径)"
printf "  路径: "
read -r CUSTOM_PATH
if [ -n "$CUSTOM_PATH" ]; then
    DEFAULT_VAULT="$CUSTOM_PATH"
fi

VAULT_ZIP="$SCRIPT_DIR/vault.zip"
if [ ! -f "$VAULT_ZIP" ]; then
    red "  未找到 vault.zip，请确认它和此脚本在同一目录"
    exit 1
fi

SKIP_DEPLOY=false
if [ -d "$DEFAULT_VAULT" ]; then
    yellow "  目录已存在: $DEFAULT_VAULT"
    printf "  是否覆盖？(y/N): "
    read -r OVERWRITE
    if [ "$OVERWRITE" != "y" ] && [ "$OVERWRITE" != "Y" ]; then
        yellow "  跳过部署"
        SKIP_DEPLOY=true
    fi
fi

if [ "$SKIP_DEPLOY" = false ]; then
    yellow "  正在解压知识库..."
    PARENT_DIR=$(dirname "$DEFAULT_VAULT")
    mkdir -p "$PARENT_DIR"
    unzip -q -o "$VAULT_ZIP" -d "$PARENT_DIR"
    EXTRACTED="$PARENT_DIR/vault"
    if [ -d "$EXTRACTED" ] && [ "$EXTRACTED" != "$DEFAULT_VAULT" ]; then
        rm -rf "$DEFAULT_VAULT" 2>/dev/null || true
        mv "$EXTRACTED" "$DEFAULT_VAULT"
    fi
    green "  知识库已部署到: $DEFAULT_VAULT"
fi

# ----------------------------------------------------------
# 5. 配置 Claudian 插件
# ----------------------------------------------------------
step "配置 Claudian 插件..."
CLAUDE_EXE=$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")
if [ -x "$CLAUDE_EXE" ]; then
    CLAUDIAN_DIR="$DEFAULT_VAULT/.obsidian/plugins/claudian"
    if [ -d "$CLAUDIAN_DIR" ]; then
        echo "{\"claudePath\": \"$CLAUDE_EXE\"}" > "$CLAUDIAN_DIR/data.json"
        green "  Claudian 已配置 CLI 路径: $CLAUDE_EXE"
    fi
fi

# ----------------------------------------------------------
# 6. 验证安装
# ----------------------------------------------------------
step "验证安装..."
echo ""

check_cmd() {
    if command -v "$2" &>/dev/null; then
        green "  [OK] $1: $($3 2>&1 | head -1)"
    else
        red "  [X]  $1: 未安装"
    fi
}
check_path() {
    if [ -e "$2" ]; then
        green "  [OK] $1: $2"
    else
        red "  [X]  $1: 未找到"
    fi
}

check_cmd  "Git"         git   "git --version"
check_cmd  "Claude Code" claude "claude --version"
check_path "API 配置"    "$SETTINGS_PATH"
check_path "知识库"      "$DEFAULT_VAULT"

# ----------------------------------------------------------
# 完成
# ----------------------------------------------------------
echo ""
green "============================================"
green "  安装完成！"
green "============================================"
echo ""
yellow "下一步："
echo "  1. 打开 Obsidian -> 打开文件夹作为库 -> 选择 $DEFAULT_VAULT"
echo "  2. 信任插件并启用"
echo "  3. 阅读「使用指南」文件夹中的文档"
echo ""
read -rp "按回车退出"
