@echo off
chcp 65001 >nul
title Chime Deposit Monitor - Telegram Alerts
cd /d "%~dp0"

echo ============================================================
echo   Chime Auto Deposit Detection and Telegram Alert System
echo ============================================================
echo.

python run_monitor.py

echo.
echo Service stopped.
pause
