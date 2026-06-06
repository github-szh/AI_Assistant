@echo off
set BACKEND_DIR=%~dp0
set FRONTEND_DIR=%~dp0..\ai-assistant-web

echo ============================================
echo   AI Assistant - Starting...
echo ============================================

:: Clear stale Python bytecode cache (prevents old code from running)
echo   Clearing __pycache__...
for /d /r "%BACKEND_DIR%" %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d" 2>nul

:: Prevent Python from creating new bytecode cache files
set PYTHONDONTWRITEBYTECODE=1

:: Suppress torch/tensorflow WMI warnings on Windows
set TF_CPP_MIN_LOG_LEVEL=2

:: Read PHOENIX_ENABLED from .env (not system env, since .env is for Python)
set PHOENIX_ENABLED=false
for /f "tokens=1,2 delims==" %%a in ('findstr /b "PHOENIX_ENABLED" "%BACKEND_DIR%.env" 2^>nul') do set "PHOENIX_ENABLED=%%b"
:: Start Phoenix (optional LLM trace viewer)
if /i "%PHOENIX_ENABLED%"=="true" (
    echo   Phoenix : http://localhost:6006
    start "AI_Phoenix" /d "%BACKEND_DIR%" cmd /k "set PYTHONDONTWRITEBYTECODE=1 && python scripts/start_phoenix.py"
) else (
    echo   Phoenix : disabled (set PHOENIX_ENABLED=true in .env to enable)
)

:: Start backend
echo   Backend : http://localhost:8000/docs
echo   Monitor : http://localhost:8000/monitoring
start "AI_Backend" /d "%BACKEND_DIR%" cmd /k "set PYTHONDONTWRITEBYTECODE=1 && set TF_CPP_MIN_LOG_LEVEL=2 && uvicorn src.api.main:app --reload --port 8000"

:: Start frontend
if exist "%FRONTEND_DIR%" (
    echo   Frontend: http://localhost:5173
    start "AI_Frontend" /d "%FRONTEND_DIR%" cmd /k npm run dev
) else (
    echo   [WARNING] Frontend not found: %FRONTEND_DIR%
)

echo ============================================
pause
