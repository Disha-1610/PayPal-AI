@echo off
taskkill /FI "WINDOWTITLE eq Aisling Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Aisling Frontend*" /T /F >nul 2>&1
echo Stopped.
