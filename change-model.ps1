# Obsidian LLM Wiki - 修改 AI 模型
# 用法: powershell -ExecutionPolicy Bypass -File change-model.ps1

$settingsPath = "$env:USERPROFILE\.claude\settings.json"

if (-not (Test-Path $settingsPath)) {
    Write-Host "[!] 未找到配置文件，请先运行安装脚本" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  修改 AI 模型" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green

# 显示当前配置
$current = Get-Content $settingsPath -Raw | ConvertFrom-Json
Write-Host ""
Write-Host "当前配置:" -ForegroundColor Cyan
Write-Host "  模型: $($current.env.ANTHROPIC_MODEL)" -ForegroundColor White
Write-Host "  API:  $($current.env.ANTHROPIC_BASE_URL)" -ForegroundColor White
Write-Host ""

Write-Host "请选择新的 AI 模型:" -ForegroundColor Yellow
Write-Host "  1. DeepSeek (推荐，国内直连，无需 VPN)" -ForegroundColor White
Write-Host "  2. Claude 原版 (需要 VPN 或海外网络)" -ForegroundColor White
Write-Host "  3. OpenAI (需要 VPN 或海外网络)" -ForegroundColor White
Write-Host "  4. 自定义 API" -ForegroundColor White
Write-Host "  0. 取消" -ForegroundColor White
$choice = Read-Host "请输入编号"

switch ($choice) {
    "1" {
        $baseUrl = "https://api.deepseek.com/anthropic"
        $mainModel = "deepseek-v4-pro"
        $fastModel = "deepseek-v4-flash"
        $apiUrl = "https://platform.deepseek.com"
        $name = "DeepSeek"
    }
    "2" {
        $baseUrl = "https://api.anthropic.com"
        $mainModel = "claude-sonnet-4-6"
        $fastModel = "claude-haiku-4-5-20251001"
        $apiUrl = "https://console.anthropic.com"
        $name = "Claude"
    }
    "3" {
        $baseUrl = "https://api.openai.com/v1"
        $mainModel = "gpt-4.1"
        $fastModel = "gpt-4.1-mini"
        $apiUrl = "https://platform.openai.com"
        $name = "OpenAI"
    }
    "4" {
        Write-Host "请输入 API Base URL:" -ForegroundColor Yellow
        $baseUrl = Read-Host "  Base URL"
        Write-Host "请输入主模型名称:" -ForegroundColor Yellow
        $mainModel = Read-Host "  主模型"
        Write-Host "请输入快速模型名称 (可与主模型相同):" -ForegroundColor Yellow
        $fastModel = Read-Host "  快速模型"
        $apiUrl = $baseUrl
        $name = "自定义"
    }
    "0" {
        Write-Host "已取消" -ForegroundColor Yellow
        Read-Host "按回车退出"
        exit 0
    }
    default {
        Write-Host "无效选择" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
}

Write-Host ""
Write-Host "已选择: $name" -ForegroundColor Green
Write-Host "是否同时更换 API Key？(y/N)" -ForegroundColor Yellow
$changeKey = Read-Host "  "

if ($changeKey -eq 'y' -or $changeKey -eq 'Y') {
    Write-Host "请输入新的 API Key (获取地址: $apiUrl)" -ForegroundColor Yellow
    $apiKey = Read-Host "  API Key"
} else {
    $apiKey = $current.env.ANTHROPIC_AUTH_TOKEN
}

$settings = @"
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "$apiKey",
    "ANTHROPIC_BASE_URL": "$baseUrl",
    "ANTHROPIC_MODEL": "$mainModel",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "$mainModel",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "$mainModel",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "$fastModel",
    "CLAUDE_CODE_SUBAGENT_MODEL": "$fastModel"
  }
}
"@
$settings | Out-File -FilePath $settingsPath -Encoding utf8 -Force

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  修改完成！" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  模型: $mainModel" -ForegroundColor White
Write-Host "  API:  $baseUrl" -ForegroundColor White
Write-Host ""
Read-Host "按回车退出"
