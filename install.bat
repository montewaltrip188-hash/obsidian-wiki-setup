@echo off
chcp 65001 >nul 2>&1
set "PS_SCRIPT=%~dp0setup-win.ps1"
if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" (
    "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -ExecutionPolicy Bypass -File "%PS_SCRIPT%"
) else (
    where pwsh >nul 2>&1
    if not errorlevel 1 (
        pwsh -ExecutionPolicy Bypass -File "%PS_SCRIPT%"
    ) else (
        echo [!] 未找到 PowerShell
        echo     请安装 PowerShell: https://aka.ms/powershell
    )
)
pause
