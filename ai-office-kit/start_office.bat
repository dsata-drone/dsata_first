@echo off
rem ============================================
rem follow VIRTUAL OFFICE 起動ランチャー
rem  ダブルクリックでローカルサーバーを立てて
rem  ブラウザでオフィスを開きます (port 8899)
rem ============================================
cd /d "%~dp0"
start "follow-office-server" cmd /c "py office_server.py || python office_server.py"
timeout /t 2 >nul
start "" http://localhost:8899/ai_office_1.html
