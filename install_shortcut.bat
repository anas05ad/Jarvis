@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$d=[Environment]::GetFolderPath('Desktop');" ^
 "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'JARVIS.lnk'));" ^
 "$s.TargetPath='%~dp0start_jarvis.bat';" ^
 "$s.WorkingDirectory='%~dp0';" ^
 "$s.IconLocation='%~dp0jarvis.ico';" ^
 "$s.WindowStyle=7;" ^
 "$s.Description='Start JARVIS';" ^
 "$s.Save()"
echo.
echo Done! The JARVIS shortcut is on your Desktop.
pause
