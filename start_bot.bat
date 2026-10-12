@echo off
chcp 65001 >nul
title iPhone Stock Bot
cd /d "%~dp0"
python iphone_stock_bot.py %*
echo.
echo Bot stopped.
pause
