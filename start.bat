@echo off
setlocal
set "ROOT=%~dp0"
set "PY=python"
if exist "%ROOT%backend\.venv\Scripts\python.exe" (
  "%ROOT%backend\.venv\Scripts\python.exe" -c "import uvicorn" >nul 2>&1 && set "PY=%ROOT%backend\.venv\Scripts\python.exe"
)

if not exist "%ROOT%frontend\node_modules" (
  echo Installing frontend dependencies...
  pushd "%ROOT%frontend" && call npm install && popd
)

start "Aisling Backend" /D "%ROOT%backend" cmd /k ""%PY%" -m uvicorn app.api.main:app --reload --port 8000"
start "Aisling Frontend" /D "%ROOT%frontend" cmd /k "npm run dev"

echo Waiting for the frontend to start...
timeout /t 8 /nobreak >nul
start "" http://localhost:3000
endlocal
