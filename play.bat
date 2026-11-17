@echo off
rem Starts Games over IP without a console window.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw main.py) || (start "" pyw -3 main.py)
