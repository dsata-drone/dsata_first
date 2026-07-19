# -*- coding: utf-8 -*-
"""follow VIRTUAL OFFICE サーバー
静的ファイル配信 + 社長指示API (POST /api/command)
start_office.bat から起動される。

POST /api/command  {"name": "ジン", "text": "指示内容"}
  → office_state.json の overrides と log を更新し、画面に反映される
"""
import json
import os
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8899
BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "office_state.json")
LOG_MAX = 40        # 業務日誌の保持件数
WORK_HOURS = 2      # 指示による「作業中」表示を維持する時間


def apply_command(name, text):
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    now = datetime.now().astimezone()
    overrides = [o for o in st.get("overrides", []) if o.get("name") != name]
    overrides.append({
        "name": name,
        "status": "working",
        "task": text,
        "at": now.isoformat(timespec="seconds"),
        "until": (now + timedelta(hours=WORK_HOURS)).isoformat(timespec="seconds"),
    })
    hm = now.strftime("%H:%M")
    log = st.get("log", [])
    log.append(f"{hm} 社長: {name}へ指示『{text}』")
    log.append(f"{hm} {name}: 拝承。直ちに着手します")
    st["overrides"] = overrides
    st["log"] = log[-LOG_MAX:]
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=BASE, **kw)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/command":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            name = str(req.get("name", "")).strip()
            text = str(req.get("text", "")).strip()
            if not name or not text:
                raise ValueError("name と text は必須です")
            apply_command(name, text)
            self._json(200, {"ok": True})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def log_message(self, fmt, *args):
        pass  # アクセスログは出さない


if __name__ == "__main__":
    os.chdir(BASE)
    print(f"follow VIRTUAL OFFICE: http://localhost:{PORT}/ai_office_1.html")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
