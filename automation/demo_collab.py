# -*- coding: utf-8 -*-
"""連携デモ — 黄金の連携コンビのバトンパスをモック実行して検証する。

  python3 automation/demo_collab.py           # シナリオA/Bをテンポラリコピー上で検証(実ファイル無変更)
  python3 automation/demo_collab.py --apply   # 実際の office_state.json にデモ連携を書き込み、
                                              # オフィス画面でライン・パケット・吹き出しを目視確認

シナリオA(SEO・コンテンツ連携): セオが構成案を完了(100%) → フミへ執筆をバトンパス
シナリオB(SNS・クリエイティブ連携): ルナが投稿企画を完了 → ニジへ制作を自動依頼

画面だけで見たい場合はブラウザで ai_office_1.html?demo=collab を開けば、
バックエンドなしで12秒周期のモック連携が再生される。
"""
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orchestrator as orch

JST = ZoneInfo("Asia/Tokyo")


def force_complete(taskboard, who):
    """指定メンバーのP0〜P3タスクをすべて進捗100%に書き換える(モック)。"""
    out = []
    for line in taskboard.splitlines():
        m = orch.TASK_RE.match(line)
        if m and m.group(1).strip() == who:
            line = re.sub(r"進捗[:：]?\s*\d+%", "進捗:100%", line)
        out.append(line)
    return "\n".join(out) + "\n"


def run_scenario(label, upstream, downstream, st, taskboard, now):
    """上流(upstream)の全タスクを完了させ、下流(downstream)へのバトンパスを検証。"""
    taskboard = force_complete(taskboard, upstream)
    st["overrides"] = [o for o in st.get("overrides", [])
                       if o.get("name") not in (upstream, downstream)]
    names = orch.team_names()
    handed = orch.process_handoffs(st, taskboard, names, now)
    ok = any(h.startswith(f"{upstream}→{downstream}") for h in handed)
    collab = next((c for c in st.get("collaborations", [])
                   if c["from_agent"] == upstream and c["to_agent"] == downstream), None)
    assigned = next((o for o in st["overrides"]
                     if o["name"] == downstream and o["status"] == "working"), None)
    return {
        "scenario": label,
        "pass": bool(ok and collab and assigned),
        "handoff": handed,
        "collaboration": collab,
        "downstream_status": assigned and f"作業中({upstream}からの依頼): {assigned['task']}",
    }, taskboard


def main():
    now = datetime.now(JST)
    apply_mode = "--apply" in sys.argv

    if apply_mode:
        # 実stateにデモ連携を1件書き込む(10分で自動失効・オフィス画面での目視確認用)
        st = orch.load_state()
        names = orch.team_names()
        upstream, downstream = "セオ", "フミ"
        until = (now + orch.timedelta(minutes=10)).isoformat(timespec="seconds")
        st["overrides"] = [o for o in st.get("overrides", []) if o.get("name") != downstream]
        st["overrides"].append({
            "name": downstream, "status": "working",
            "task": f"比較記事の初稿執筆({upstream}からの依頼)【デモ】",
            "at": now.isoformat(timespec="seconds"), "until": until,
        })
        orch._add_collab(st, now, upstream, downstream, "handoff",
                         "【デモ】構成案ができたので、本文執筆をお願いします！",
                         "比較記事の初稿執筆", until)
        hm = now.strftime("%H:%M")
        st.setdefault("log", []).append(f"{hm} {upstream}: ✉ {downstream}へバトンパス『比較記事の初稿執筆』(デモ)")
        orch.save_state(st)
        print(json.dumps({"applied": f"{upstream}→{downstream}", "until": until},
                         ensure_ascii=False, indent=2))
        return 0

    # デフォルト: テンポラリコピー上で関数を直接検証(実ファイルに触れない)
    with tempfile.TemporaryDirectory() as tmp:
        state_copy = os.path.join(tmp, "office_state.json")
        shutil.copy(orch.STATE, state_copy)
        orch.STATE = state_copy  # 以後の save/load はコピーへ
        st = orch.load_state()
        st["collaborations"] = []
        with open(orch.TASKBOARD, encoding="utf-8") as f:
            taskboard = f.read()

        results = []
        r, taskboard = run_scenario("A: セオ(SEO)→フミ(ライター)", "セオ", "フミ",
                                    st, taskboard, now)
        results.append(r)
        r, taskboard = run_scenario("B: ルナ(SNS)→ニジ(デザイナー)", "ルナ", "ニジ",
                                    st, taskboard, now)
        results.append(r)

        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if all(x["pass"] for x in results) else 1


if __name__ == "__main__":
    sys.exit(main())
