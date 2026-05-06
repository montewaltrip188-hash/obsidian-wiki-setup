#!/bin/bash
# ============================================================
# Obsidian LLM Wiki - Mac 一键安装脚本
# ============================================================
# 用法: 打开终端，执行:
#   chmod +x setup-mac.sh && ./setup-mac.sh
# ============================================================

set -e

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
STEP=1

green()  { printf "\033[32m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }
red()    { printf "\033[31m%s\033[0m\n" "$1"; }
cyan()   { printf "\033[36m%s\033[0m\n" "$1"; }

step() { cyan ""; cyan "[$STEP] $1"; STEP=$((STEP+1)); }

echo ""
green "============================================"
green "  Obsidian LLM Wiki 一键安装"
green "============================================"

# ----------------------------------------------------------
# 1. 安装 Homebrew
# ----------------------------------------------------------
step "检查 Homebrew..."
if command -v brew &>/dev/null; then
    green "  Homebrew 已安装: $(brew --version | head -1)"
else
    yellow "  正在安装 Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Apple Silicon
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
    fi
    green "  Homebrew 安装完成"
fi

# ----------------------------------------------------------
# 2. 安装 Node.js
# ----------------------------------------------------------
step "检查 Node.js..."
if command -v node &>/dev/null; then
    NODE_VER=$(node --version)
    NODE_MAJOR=${NODE_VER#v}
    NODE_MAJOR=${NODE_MAJOR%%.*}
    if [ "$NODE_MAJOR" -ge 18 ]; then
        green "  Node.js 已安装: $NODE_VER"
    else
        yellow "  Node.js 版本过低 ($NODE_VER)，需要 >= 18，正在升级..."
        brew install node
        green "  Node.js 升级完成: $(node --version)"
    fi
else
    yellow "  正在安装 Node.js..."
    brew install node
    green "  Node.js 安装完成: $(node --version)"
fi

# ----------------------------------------------------------
# 3. 安装 Git
# ----------------------------------------------------------
step "检查 Git..."
if command -v git &>/dev/null; then
    green "  Git 已安装: $(git --version)"
else
    yellow "  正在安装 Git..."
    brew install git
    green "  Git 安装完成"
fi

# ----------------------------------------------------------
# 4. 安装 Claude Code
# ----------------------------------------------------------
step "检查 Claude Code..."
if command -v claude &>/dev/null; then
    green "  Claude Code 已安装: $(claude --version 2>/dev/null || echo 'OK')"
else
    yellow "  正在安装 Claude Code..."
    # 避免权限问题
    if npm install -g @anthropic-ai/claude-code 2>/dev/null; then
        green "  Claude Code 安装完成"
    else
        yellow "  全局安装遇到权限问题，改用用户目录..."
        mkdir -p ~/.npm-global
        npm config set prefix '~/.npm-global'
        export PATH=~/.npm-global/bin:$PATH
        # 写入 shell 配置
        SHELL_RC="$HOME/.zshrc"
        if ! grep -q "npm-global" "$SHELL_RC" 2>/dev/null; then
            echo 'export PATH=~/.npm-global/bin:$PATH' >> "$SHELL_RC"
        fi
        npm install -g @anthropic-ai/claude-code
        green "  Claude Code 安装完成"
    fi
fi

# ----------------------------------------------------------
# 5. 配置 DeepSeek API
# ----------------------------------------------------------
step "配置 DeepSeek API..."
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
    yellow "  请输入你的 DeepSeek API Key"
    yellow "  (获取地址: https://platform.deepseek.com)"
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
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_MODEL": "deepseek-v4-pro",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
    "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash"
  }
}
JSONEOF
    green "  API 配置完成"
fi

# ----------------------------------------------------------
# 6. 部署 Obsidian Vault
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

VAULT_ZIP="$REPO_ROOT/vault.zip"
if [ ! -f "$VAULT_ZIP" ]; then
    red "  [!] 未找到 vault.zip，请确认它和此脚本在同一目录"
    exit 1
fi

SKIP_DEPLOY=false
if [ -d "$DEFAULT_VAULT" ]; then
    yellow "  [!] 目录已存在: $DEFAULT_VAULT"
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
    # 解压后是 vault/ 目录，重命名
    EXTRACTED="$PARENT_DIR/vault"
    if [ -d "$EXTRACTED" ] && [ "$EXTRACTED" != "$DEFAULT_VAULT" ]; then
        rm -rf "$DEFAULT_VAULT" 2>/dev/null || true
        mv "$EXTRACTED" "$DEFAULT_VAULT"
    fi
    green "  知识库已部署到: $DEFAULT_VAULT"
fi

# ----------------------------------------------------------
# 7. 配置 Claudian 插件
# ----------------------------------------------------------
step "配置 Claudian 插件..."
CLAUDE_EXE=$(which claude 2>/dev/null || echo "")
if [ -n "$CLAUDE_EXE" ]; then
    CLAUDIAN_DATA="$DEFAULT_VAULT/.obsidian/plugins/claudian/data.json"
    if [ -d "$(dirname "$CLAUDIAN_DATA")" ]; then
        echo "{\"claudePath\": \"$CLAUDE_EXE\"}" > "$CLAUDIAN_DATA"
        green "  Claudian 已配置 CLI 路径: $CLAUDE_EXE"
    fi
fi

# ----------------------------------------------------------
# 8. 验证安装
# ----------------------------------------------------------
step "验证安装..."
echo ""
check_cmd() {
    if command -v "$2" &>/dev/null; then
        green "  [OK] $1: $($2 --version 2>/dev/null | head -1)"
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

check_cmd  "Git"         git
check_cmd  "Node.js"     node
check_cmd  "Claude Code" claude
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
