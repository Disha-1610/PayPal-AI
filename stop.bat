@echo off
taskkill /FI "WINDOWTITLE eq PayPal-AI Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq PayPal-AI Frontend*" /T /F >nul 2>&1
echo Stopped.
