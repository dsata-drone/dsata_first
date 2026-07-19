# -*- coding: utf-8 -*-
"""follow VIRTUAL OFFICE サーバー
静的ファイル配信 + 社長指示API (POST /api/command)
start_office.bat / start_office.sh から起動される。

POST /api/command  {"name": "ジン", "text": "指示内容"}
  → office_state.json の overrides と log を更新し、画面に反映される
POST /api/command  {"name": "ジン", "text": "作業内容", "status": "done"}
  → 完了報告。「完了✓」表示になり、日誌に完了として記録される
GET  /api/state    → office_state.json の現在値を返す
GET  /activity     → activity.json があればその中身、なければ [] を返す
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


def apply_command(name, text, status="working"):
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    now = datetime.now().astimezone()
    overrides = [o for o in st.get("overrides", []) if o.get("name") != name]
    entry = {
        "name": name,
        "status": status,
        "task": text,
        "at": now.isoformat(timespec="seconds"),
    }
    if status == "working":
        entry["until"] = (now + timedelta(hours=WORK_HOURS)).isoformat(timespec="seconds")
    overrides.append(entry)
    hm = now.strftime("%H:%M")
    log = st.get("log", [])
    if status == "done":
        log.append(f"{hm} {name}: 『{text}』完了しました")
    else:
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

    def do_GET(self):
        if self.path.split("?")[0] == "/api/state":
            try:
                with open(STATE, encoding="utf-8") as f:
                    self._json(200, json.load(f))
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return
        if self.path.split("?")[0] == "/activity":
            act = os.path.join(BASE, "activity.json")
            try:
                with open(act, encoding="utf-8") as f:
                    self._json(200, json.load(f))
            except Exception:
                self._json(200, [])
            return
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/command":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            name = str(req.get("name", "")).strip()
            text = str(req.get("text", "")).strip()
            status = str(req.get("status", "working")).strip() or "working"
            if not name or not text:
                raise ValueError("name と text は必須です")
            if status not in ("working", "done"):
                raise ValueError("status は working か done")
            apply_command(name, text, status)
            self._json(200, {"ok": True})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def log_message(self, fmt, *args):
        pass  # アクセスログは出さない


if __name__ == "__main__":
    os.chdir(BASE)
    print(f"follow VIRTUAL OFFICE: http://localhost:{PORT}/ai_office_1.html")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
