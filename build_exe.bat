@echo off
cd /d "%~dp0"
".venv\Scripts\pyinstaller.exe" --noconfirm sgDesk.spec
echo.
echo Build selesai! File exe ada di: dist\sgDesk.exe
pause
