@echo off
cd /d "%~dp0"
title Shopee Dashboard Server
start "" http://localhost:8765
python server.py
pause
