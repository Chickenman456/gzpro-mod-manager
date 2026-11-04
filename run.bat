@echo off
setlocal
cd /d "%~dp0"
python manager.py %*
if errorlevel 1 (
  echo.
  echo The GZPro Mod Manager could not start.
  pause
)
