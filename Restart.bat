@echo off
set PYTHONDONTWRITEBYTECODE=1
set TF_CPP_MIN_LOG_LEVEL=2
set PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

echo Killing old processes on port 8000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000.*LISTENING"') do taskkill /PID %%a /F 2>nul

set retry=0
:wait
netstat -ano | findstr ":8000.*LISTENING" >nul 2>&1
if %errorlevel% neq 0 goto start
set /a retry+=1
if %retry% gtr 12 (
    echo Port still blocked after 60s - try rebooting or wait longer
    pause
    exit /b 1
)
echo Port 8000 in use (attempt %retry%/12), waiting 5s...
timeout /t 5 /nobreak >nul
goto wait

:start
echo Starting AI Assistant Backend on http://localhost:8000
:: 动态查看日志：打开 PowerShell 执行 →  Get-Content data/logs/app.log -Wait
cd /d "%~dp0"
uvicorn src.api.main:app --port 8000 --reload --log-level debug
pause
