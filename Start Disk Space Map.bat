@echo off
rem Starts the live Disk Space Map server in a minimized window and opens it in your browser.
rem Close the "Disk Space Map" window to stop it.
cd /d "%~dp0"
start "Disk Space Map" /min python server.py
