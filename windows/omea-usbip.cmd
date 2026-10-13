@echo off
rem Startup-folder launcher for omea-usbip.ps1 (no console left open)
start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%USERPROFILE%\omea\omea-usbip.ps1"
