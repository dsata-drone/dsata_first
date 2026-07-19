# -*- coding: utf-8 -*-
"""オフィスの「動くスナップショット」を1ファイルのHTMLとして書き出す。

使い方: python3 make_snapshot.py [出力パス]
  （省略時は office_snapshot.html）

office_state.json やダッシュボード用ファイルの現在値をHTMLに埋め込み、
fetch を内蔵データに差し替えるため、サーバー無し（file://直開き・チャット添付・
共有）でもキャラのアニメーションとダッシュボードがそのまま動く。
社長指示フォームも画面内でだけ反映される（ファイルには書き戻さない）。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "AIチーム_ナレッジ")


def read(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


SHIM = """<script>
// ---- スナップショット用シム: サーバー無しで動くよう fetch を内蔵データに差し替え ----
const __OFFICE_DATA__ = %s;
window.fetch = async (url, opts) => {
  const path = decodeURIComponent(String(url).split("?")[0]);
  const T = (s, ct) => new Response(s, {status: 200, headers: {"Content-Type": ct}});
  if (opts && opts.method === "POST") {
    try {
      const req = JSON.parse(opts.body);
      const now = new Date(), hm = now.toTimeString().slice(0, 5);
      const st = __OFFICE_DATA__.state;
      st.overrides = st.overrides.filter(o => o.name !== req.name);
      st.overrides.push({name: req.name, status: "working", task: req.text,
        at: now.toISOString(), until: new Date(now.getTime() + 2 * 3600e3).toISOString()});
      st.log.push(hm + " 社長: " + req.name + "へ指示『" + req.text + "』",
                  hm + " " + req.name + ": 拝承。直ちに着手します（※スナップショット内のみ反映）");
      return T(JSON.stringify({ok: true}), "application/json");
    } catch (e) { return T(JSON.stringify({ok: false}), "application/json"); }
  }
  if (path.endsWith("office_state.json")) return T(JSON.stringify(__OFFICE_DATA__.state), "application/json");
  if (path.endsWith("company_dashboard.json")) return T(JSON.stringify(__OFFICE_DATA__.dash), "application/json");
  if (path.endsWith("JOHN_TASKBOARD.md")) return T(__OFFICE_DATA__.taskboard, "text/plain");
  if (path.endsWith("_更新ログ.md")) return T(__OFFICE_DATA__.evolog, "text/plain");
  if (path.endsWith("VENTURE_RANKING.md")) return T(__OFFICE_DATA__.rank, "text/plain");
  if (path === "activity" || path.endsWith("/activity")) return T("[]", "application/json");
  if (path.endsWith("DAILY_LAB_REPORT/")) return T("", "text/html");
  return new Response("", {status: 404});
};
</script>
"""


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "office_snapshot.html")
    base = read(os.path.join(BASE, "ai_office_1.html"))
    if "<body>" not in base:
        sys.exit("ai_office_1.html が読めないか形式が想定と違います")
    data = {
        "state": read_json(os.path.join(BASE, "office_state.json"), {"overrides": [], "log": []}),
        "dash": read_json(os.path.join(BASE, "company_dashboard.json"), {}),
        "taskboard": read(os.path.join(KB, "JOHN_TASKBOARD.md")),
        "evolog": read(os.path.join(KB, "_更新ログ.md")),
        "rank": read(os.path.join(KB, "ai_lab", "VENTURE_RANKING.md")),
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = base.replace("<body>", "<body>\n" + SHIM % payload, 1)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"書き出しました: {out_path}")


if __name__ == "__main__":
    main()
