@echo off
rem Allow other computers on the lab network to reach CPRI ATRG (run once, as administrator).
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    echo This script must be run as administrator.
    echo Right-click allow_firewall.bat and choose "Run as administrator".
    pause
    exit /b 1
)
set "PORT=5000"
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if /i "%%A"=="ATRG_PORT" if not "%%B"=="" set "PORT=%%B"
    )
)
echo Opening TCP port %PORT% for CPRI ATRG (private and domain networks)...
netsh advfirewall firewall delete rule name="CPRI ATRG" >nul 2>&1
netsh advfirewall firewall add rule name="CPRI ATRG" dir=in action=allow protocol=TCP localport=%PORT% profile=private,domain
if errorlevel 1 (
    echo Could not add the firewall rule.
) else (
    echo Done. Other computers can now connect on port %PORT%.
    echo Make sure this computer's network profile is set to Private.
)
pause
