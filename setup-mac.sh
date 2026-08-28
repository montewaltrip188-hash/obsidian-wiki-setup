#!/bin/bash
# ============================================================
# Obsidian LLM Wiki - macOS 一键安装脚本
# ============================================================
# 用法: 打开终端，运行: bash setup-mac.sh
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STEP=1

VALIDATE_ACTIVATION_ONLY=false
ACTIVATION_CODE=""
ACTIVATION_PUBLIC_KEY_PATH="$SCRIPT_DIR/activation-public-key.xml"
REVOKED_ACTIVATION_IDS_PATH="$SCRIPT_DIR/revoked-activation-ids.txt"
EXPECTED_PRODUCT="obsidian-llm-wiki"
EXPECTED_VERSION="2.1"
NOW_UTC=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --validate-activation-only) VALIDATE_ACTIVATION_ONLY=true; shift ;;
        --activation-code) ACTIVATION_CODE="${2:-}"; shift 2 ;;
        --public-key) ACTIVATION_PUBLIC_KEY_PATH="${2:-}"; shift 2 ;;
        --revoked-ids) REVOKED_ACTIVATION_IDS_PATH="${2:-}"; shift 2 ;;
        --expected-product) EXPECTED_PRODUCT="${2:-}"; shift 2 ;;
        --expected-version) EXPECTED_VERSION="${2:-}"; shift 2 ;;
        --now-utc) NOW_UTC="${2:-}"; shift 2 ;;
        *) printf "未知参数: %s\n" "$1" >&2; exit 1 ;;
    esac
done

MACHINE_ARCH=$(uname -m)
case "$MACHINE_ARCH" in
    arm64|aarch64) RUNTIME_SOURCE="$SCRIPT_DIR/runtime/targets/macos-arm64" ;;
    x86_64|amd64) RUNTIME_SOURCE="$SCRIPT_DIR/runtime/targets/macos-x64" ;;
    *) RUNTIME_SOURCE="" ;;
esac
PACKAGED_PYTHON_BIN="${RUNTIME_SOURCE:+$RUNTIME_SOURCE/python/bin/python3}"

green()  { printf "\033[32m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }
red()    { printf "\033[31m%s\033[0m\n" "$1"; }
step()   { echo ""; printf "\033[36m[%d] %s\033[0m\n" "$STEP" "$1"; STEP=$((STEP+1)); }

verify_wiki2_activation() {
    local code="$1"
    local public_key_path="$2"
    local revoked_ids_path="$3"
    local expected_product="$4"
    local expected_version="$5"
    local now_utc="$6"
    local python_bin=""

    if [ -n "$PACKAGED_PYTHON_BIN" ] && [ -x "$PACKAGED_PYTHON_BIN" ]; then
        python_bin="$PACKAGED_PYTHON_BIN"
    elif command -v python3 >/dev/null 2>&1; then
        python_bin="python3"
    elif command -v python >/dev/null 2>&1; then
        python_bin="python"
    else
        return 1
    fi

    "$python_bin" - "$code" "$public_key_path" "$revoked_ids_path" \
        "$expected_product" "$expected_version" "$now_utc" <<'PY'
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
import xml.etree.ElementTree as ET


def decode_base64url(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def parse_utc(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(dt.timezone.utc)


try:
    code, key_path, revoked_path, expected_product, expected_version, now_value = sys.argv[1:]
    if not revoked_path or not os.path.isfile(revoked_path):
        raise ValueError("revocation list required")
    segments = code.split(".")
    if len(segments) != 3 or segments[0] != "WIKI2":
        raise ValueError("invalid format")

    payload_segment = segments[1]
    signature = decode_base64url(segments[2])
    root = ET.parse(key_path).getroot()
    modulus = int.from_bytes(base64.b64decode(root.findtext("Modulus")), "big")
    exponent = int.from_bytes(base64.b64decode(root.findtext("Exponent")), "big")
    key_size = (modulus.bit_length() + 7) // 8
    if len(signature) != key_size:
        raise ValueError("invalid signature size")

    encoded_message = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(key_size, "big")
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(
        payload_segment.encode("ascii")
    ).digest()
    padding_length = key_size - len(digest_info) - 3
    if padding_length < 8:
        raise ValueError("invalid key size")
    expected_message = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
    if not hmac.compare_digest(encoded_message, expected_message):
        raise ValueError("invalid signature")

    payload = json.loads(decode_base64url(payload_segment).decode("utf-8"))
    required = ("activation_id", "product", "version", "expires_at")
    if any(not isinstance(payload.get(field), str) or not payload[field].strip() for field in required):
        raise ValueError("missing field")
    if payload["product"] != expected_product or payload["version"] != expected_version:
        raise ValueError("product or version mismatch")

    now = parse_utc(now_value) if now_value else dt.datetime.now(dt.timezone.utc)
    if parse_utc(payload["expires_at"]) <= now:
        raise ValueError("expired")

    with open(revoked_path, encoding="utf-8-sig") as revoked_file:
        revoked_ids = {
            line.strip() for line in revoked_file
            if line.strip() and not line.lstrip().startswith("#")
        }
    if payload["activation_id"] in revoked_ids:
        raise ValueError("revoked")
except Exception:
    sys.exit(1)
PY
}

echo ""
green "============================================"
green "  Obsidian LLM Wiki 一键安装 (macOS)"
green "============================================"

# ----------------------------------------------------------
# 0. 激活码验证
# ----------------------------------------------------------
step "验证激活码..."
if [ -z "$ACTIVATION_CODE" ]; then
    printf "  请输入激活码: "
    IFS= read -r -s ACTIVATION_CODE
    echo ""
fi

if verify_wiki2_activation "$ACTIVATION_CODE" "$ACTIVATION_PUBLIC_KEY_PATH" \
    "$REVOKED_ACTIVATION_IDS_PATH" "$EXPECTED_PRODUCT" "$EXPECTED_VERSION" "$NOW_UTC"; then
    green "  激活码验证通过"
else
    red "  激活码无效，请联系服务提供者获取新的 WIKI2 激活码"
    exit 1
fi

[ "$VALIDATE_ACTIVATION_ONLY" = true ] && exit 0

if [ -z "$RUNTIME_SOURCE" ] || [ ! -f "$RUNTIME_SOURCE/.runtime-target.json" ] || \
   [ ! -x "$PACKAGED_PYTHON_BIN" ]; then
    red "  安装包不含当前架构 ($MACHINE_ARCH) 的已签名离线 Query 运行时"
    exit 1
fi
PYTHON_BIN="$PACKAGED_PYTHON_BIN"

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
    EXISTING_KEY=$("$PYTHON_BIN" -c "import json; d=json.load(open('$SETTINGS_PATH')); print(d.get('env',{}).get('ANTHROPIC_AUTH_TOKEN',''))" 2>/dev/null || echo "")
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
    read -r -s API_KEY
    echo

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
VAULT_MANIFEST="$SCRIPT_DIR/deploy-manifest.json"
VAULT_DEPLOYER="$SCRIPT_DIR/extract-vault.py"
SKILL_SOURCE="$SCRIPT_DIR/skills/claudecode-wiki-skills"
SKILL_MANAGER="$SCRIPT_DIR/tools/manage_wiki_skills.py"
RUNTIME_PROBE="$SCRIPT_DIR/tools/verify_keyword_runtime.py"
for required_file in "$VAULT_ZIP" "$VAULT_MANIFEST" "$VAULT_DEPLOYER"; do
    if [ ! -f "$required_file" ]; then
        red "  缺少安全部署文件: $required_file"
        exit 1
    fi
done
if [ ! -f "$SKILL_MANAGER" ] || [ ! -f "$RUNTIME_PROBE" ] || [ ! -d "$SKILL_SOURCE" ]; then
    red "  缺少 Wiki Skill 安装组件"
    exit 1
fi
# 在修改 Vault 前完成 Skill 冲突与所有权只读预检，避免半安装。
if ! SKILL_PLAN=$("$PYTHON_BIN" "$SKILL_MANAGER" plan \
    --source "$SKILL_SOURCE" --home "$HOME" --runtime-source "$RUNTIME_SOURCE" 2>&1); then
    red "  Wiki Skill 预检未通过: $SKILL_PLAN"
    exit 1
fi
SKILL_ACTION=$(printf '%s' "$SKILL_PLAN" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["action"])')
green "  Wiki Skill 预检完成: $SKILL_ACTION"

ALLOW_EXISTING_VAULT=false
DEPLOY_RECEIPT=""
if [ -e "$DEFAULT_VAULT" ] || [ -L "$DEFAULT_VAULT" ]; then
    yellow "  目录已存在: $DEFAULT_VAULT"
    printf "  是否先保留同级备份再升级？(y/N): "
    read -r OVERWRITE
    if [ "$OVERWRITE" != "y" ] && [ "$OVERWRITE" != "Y" ]; then
        yellow "  安装未完成：已保留现有 Vault，未执行部署"
        exit 2
    else
        ALLOW_EXISTING_VAULT=true
    fi
fi

step "安装 Wiki Skills..."
    if ! SKILL_INSTALL=$("$PYTHON_BIN" "$SKILL_MANAGER" install \
        --source "$SKILL_SOURCE" --home "$HOME" --runtime-source "$RUNTIME_SOURCE" 2>&1); then
        red "  Wiki Skill 安装失败: $SKILL_INSTALL"
        exit 1
    fi
    if ! SKILL_UNDO_RECEIPT=$(printf '%s' "$SKILL_INSTALL" | "$PYTHON_BIN" -c \
        'import json,sys; print(json.load(sys.stdin)["undo_receipt"])'); then
        red "  Wiki Skill 安装输出缺少事务恢复回执，需要人工处理"
        exit 1
    fi
    if [ -z "$SKILL_UNDO_RECEIPT" ]; then
        red "  Wiki Skill 安装输出缺少事务恢复回执，需要人工处理"
        exit 1
    fi
    if ! SKILL_VERIFY=$("$PYTHON_BIN" "$SKILL_MANAGER" verify --home "$HOME" 2>&1); then
        red "  Wiki Skill 验收失败: $SKILL_VERIFY"
        if ! "$PYTHON_BIN" "$SKILL_MANAGER" undo --home "$HOME" --receipt "$SKILL_UNDO_RECEIPT" >/dev/null; then
            red "  Wiki Skill 验收失败，回执绑定恢复也失败，需要人工处理"
        fi
        exit 1
    fi
    SKILL_NAMES=$(printf '%s' "$SKILL_VERIFY" | "$PYTHON_BIN" -c \
        'import json,sys; print(", ".join(json.load(sys.stdin)["skills"]))')
    KEYWORD_READY=$(printf '%s' "$SKILL_INSTALL" | "$PYTHON_BIN" -c \
        'import json,sys; print(str(json.load(sys.stdin)["keyword_runtime_ready"]).lower())')
    green "  Wiki Skills 已安装并验收: $SKILL_NAMES"
    if [ "$KEYWORD_READY" != "true" ]; then
        red "  离线关键词 Query 运行时验收失败"
        "$PYTHON_BIN" "$SKILL_MANAGER" undo --home "$HOME" --receipt "$SKILL_UNDO_RECEIPT" >/dev/null || true
        exit 1
    fi
    INSTALLED_VERSION=$(printf '%s' "$SKILL_INSTALL" | "$PYTHON_BIN" -c \
        'import json,sys; print(json.load(sys.stdin)["version"])')
    INSTALLED_ROOT="$HOME/.agents/packages/claudecode-wiki-skills/versions/$INSTALLED_VERSION"
    INSTALLED_RUNTIME_PYTHON="$INSTALLED_ROOT/.runtime/python/bin/python3"
    INSTALLED_QUERY_SCRIPT="$INSTALLED_ROOT/core/wiki-hybrid-search/scripts/wiki_search.py"
    if ! RUNTIME_PROBE_OUTPUT=$("$PYTHON_BIN" "$RUNTIME_PROBE" \
        --runtime-python "$INSTALLED_RUNTIME_PYTHON" \
        --query-script "$INSTALLED_QUERY_SCRIPT" 2>&1); then
        red "  离线关键词 Query 运行时实测失败: $RUNTIME_PROBE_OUTPUT"
        "$PYTHON_BIN" "$SKILL_MANAGER" undo --home "$HOME" --receipt "$SKILL_UNDO_RECEIPT" >/dev/null || true
        exit 1
    fi
    green "  离线关键词 Query 运行时实测通过"

    yellow "  正在验证候选包并安全部署知识库..."
    DEPLOY_ARGS=(
        "$VAULT_DEPLOYER" deploy
        --archive "$VAULT_ZIP"
        --manifest "$VAULT_MANIFEST"
        --target "$DEFAULT_VAULT"
    )
    if [ "$ALLOW_EXISTING_VAULT" = true ]; then
        DEPLOY_ARGS+=(--allow-existing)
    fi
    if DEPLOY_OUTPUT=$("$PYTHON_BIN" "${DEPLOY_ARGS[@]}" 2>&1); then
        if ! DEPLOY_RECEIPT=$(printf '%s' "$DEPLOY_OUTPUT" | "$PYTHON_BIN" -c \
            'import json,sys; print(json.load(sys.stdin)["receipt"])'); then
            red "  无法绑定部署回执；已保留升级备份"
            exit 1
        fi
        if [ -z "$DEPLOY_RECEIPT" ]; then
            red "  部署输出缺少 receipt；已保留升级备份"
            exit 1
        fi
        green "  知识库部署完成"
    else
        DEPLOY_EXIT=$?
        red "  Vault 部署失败: $DEPLOY_OUTPUT"
        if ! "$PYTHON_BIN" "$SKILL_MANAGER" undo --home "$HOME" --receipt "$SKILL_UNDO_RECEIPT" >/dev/null; then
            red "  Vault 部署失败，Wiki Skill 回执绑定恢复也失败，需要人工处理"
        fi
        exit "$DEPLOY_EXIT"
    fi

# ----------------------------------------------------------
# 5. 配置 Claudian 插件（部署后受控运行时变更）
# ----------------------------------------------------------
step "配置 Claudian 插件..."
CLAUDE_EXE=$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")
if [ -x "$CLAUDE_EXE" ]; then
    CLAUDIAN_DIR="$DEFAULT_VAULT/.obsidian/plugins/claudian"
    if [ -d "$CLAUDIAN_DIR" ]; then
        echo "{\"claudePath\": \"$CLAUDE_EXE\"}" > "$CLAUDIAN_DIR/data.json"
        if [ -n "$DEPLOY_RECEIPT" ]; then
            if ! FINALIZE_OUTPUT=$("$PYTHON_BIN" "$VAULT_DEPLOYER" finalize-runtime-config \
                --target "$DEFAULT_VAULT" --deploy-receipt "$DEPLOY_RECEIPT" 2>&1); then
                red "  Claudian 运行时配置验收失败；已保留升级备份: $FINALIZE_OUTPUT"
                exit 1
            fi
        fi
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
