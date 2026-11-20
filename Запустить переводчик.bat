@echo off
cd /d "%~dp0"
set "BT_PYTHON=.venv\Scripts\python.exe"

if not exist "%BT_PYTHON%" goto missing_python
"%BT_PYTHON%" desktop_app.py
if errorlevel 1 goto launch_failed
exit /b 0

:missing_python
echo Python environment is missing.
echo Open "Book Translator.bat" to install dependencies first.
pause
exit /b 1

:launch_failed
echo The application could not start. See the error above.
pause
exit /b 1
