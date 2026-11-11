@echo off
rem Starts the local yt-dlp web UI at http://127.0.0.1:5000 and opens it in the browser.
cd /d "%~dp0"
"venv\Scripts\python.exe" -c "import flask" 2>nul || "venv\Scripts\python.exe" -m pip install -r requirements.txt
"venv\Scripts\python.exe" webapp\app.py
pause
