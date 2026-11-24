@echo off
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo Python 3 nao foi encontrado. Instale Python 3.11+ e tente novamente.
  pause
  exit /b 1
)
python app.py
pause
