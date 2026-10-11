@echo off
rem Start CPRI ATRG for this computer and the lab network.
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
)
python run.py %*
echo.
echo The server has stopped.
pause
