@echo off
rem Starts Porthole in the tray (and the web UI on port 8790). Ctrl+Alt+K pulls it out.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0porthole.py"
