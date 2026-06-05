@echo off
echo Killing uvicorn process on port 8000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000.*LISTENING"') do (
    echo   PID %%a
    taskkill /F /PID %%a 2>nul
)
echo Done.
echo.
echo.
echo Dashboard: http://localhost:8000/dashboard
echo Restarting backend...
uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000 --log-level debug
pause
