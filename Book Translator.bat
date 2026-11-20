@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
py -3.11 -m venv .venv
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
if errorlevel 1 goto failed
echo Installation completed. Open the application launcher.
pause
exit /b 0
:failed
echo Installation failed. Python 3.11 and an internet connection are required.
echo See README.md for troubleshooting.
pause
exit /b 1
