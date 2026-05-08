@echo off
chcp 65001 >nul 2>&1
if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" (
    "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -ExecutionPolicy Bypass -File "%~dp0change-model.ps1"
) else (
    echo [!] 未找到 PowerShell，请确认系统为 Windows 7 SP1 或以上版本
    echo     下载地址: https://aka.ms/wmf5download
)
pause
