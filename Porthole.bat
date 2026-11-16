@echo off
rem Opens the Porthole window (and the web UI on port 8790).
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0porthole.py"
