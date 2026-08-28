#!/bin/bash
# Obsidian LLM Wiki - 修改 AI 模型 (macOS)
# 用法: bash change-model.sh

SETTINGS_PATH="$HOME/.claude/settings.json"

if [ ! -f "$SETTINGS_PATH" ]; then
    echo "[!] 未找到配置文件，请先运行安装脚本"
    exit 1
fi

echo ""
echo "============================================"
echo "  修改 AI 模型"
echo "============================================"

# 显示当前配置
CURRENT_MODEL=$(python3 -c "import json; d=json.load(open('$SETTINGS_PATH')); print(d['env']['ANTHROPIC_MODEL'])" 2>/dev/null)
CURRENT_URL=$(python3 -c "import json; d=json.load(open('$SETTINGS_PATH')); print(d['env']['ANTHROPIC_BASE_URL'])" 2>/dev/null)
echo ""
echo "当前配置:"
echo "  模型: $CURRENT_MODEL"
echo "  API:  $CURRENT_URL"
echo ""

echo "请选择新的 AI 模型:"
echo "  1. DeepSeek (推荐，国内直连，无需 VPN)"
echo "  2. Claude 原版 (需要 VPN 或海外网络)"
echo "  3. OpenAI (需要 VPN 或海外网络)"
echo "  4. 自定义 API"
echo "  0. 取消"
printf "请输入编号: "
read -r CHOICE

case "$CHOICE" in
    1)
        BASE_URL="https://api.deepseek.com/anthropic"
        MAIN_MODEL="deepseek-v4-pro"
        FAST_MODEL="deepseek-v4-flash"
        API_URL="https://platform.deepseek.com"
        NAME="DeepSeek"
        ;;
    2)
        BASE_URL="https://api.anthropic.com"
        MAIN_MODEL="claude-sonnet-4-6"
        FAST_MODEL="claude-haiku-4-5-20251001"
        API_URL="https://console.anthropic.com"
        NAME="Claude"
        ;;
    3)
        BASE_URL="https://api.openai.com/v1"
        MAIN_MODEL="gpt-4.1"
        FAST_MODEL="gpt-4.1-mini"
        API_URL="https://platform.openai.com"
        NAME="OpenAI"
        ;;
    4)
        printf "请输入 API Base URL: "; read -r BASE_URL
        printf "请输入主模型名称: "; read -r MAIN_MODEL
        printf "请输入快速模型名称: "; read -r FAST_MODEL
        API_URL="$BASE_URL"
        NAME="自定义"
        ;;
    0)
        echo "已取消"
        exit 0
        ;;
    *)
        echo "无效选择"
        exit 1
        ;;
esac

echo ""
echo "已选择: $NAME"
printf "是否同时更换 API Key？(y/N): "
read -r CHANGE_KEY

if [ "$CHANGE_KEY" = "y" ] || [ "$CHANGE_KEY" = "Y" ]; then
    printf "请输入新的 API Key (获取地址: $API_URL): "
    read -r -s API_KEY
    echo
else
    API_KEY=$(python3 -c "import json; d=json.load(open('$SETTINGS_PATH')); print(d['env']['ANTHROPIC_AUTH_TOKEN'])" 2>/dev/null)
fi

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

echo ""
echo "============================================"
echo "  修改完成！"
echo "============================================"
echo ""
echo "  模型: $MAIN_MODEL"
echo "  API:  $BASE_URL"
