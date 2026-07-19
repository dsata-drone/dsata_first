#!/bin/sh
# follow VIRTUAL OFFICE 起動スクリプト (Mac/Linux用。Windowsは start_office.bat)
cd "$(dirname "$0")"
echo "follow VIRTUAL OFFICE: http://localhost:8899/ai_office_1.html"
# ブラウザを自動で開く(開けない環境では無視される)
( sleep 1; open "http://localhost:8899/ai_office_1.html" 2>/dev/null || xdg-open "http://localhost:8899/ai_office_1.html" 2>/dev/null ) &
exec python3 office_server.py
