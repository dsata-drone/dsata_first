# -*- coding: utf-8 -*-
"""自律オーケストレータ — オフィスの監視→采配→送信→記録を1サイクル実行する。

GitHub Actions の定期実行(cron)から呼ばれる想定。ローカル実行も可:
  python3 automation/orchestrator.py           # 1サイクル実行
  python3 automation/orchestrator.py --dry-run # 送信・通知なしで動作確認

1サイクルの処理:
  1. office_state.json / JOHN_TASKBOARD.md を読み込み
  2. 作業時間が満了した working エントリを完了扱いに整理
  3. 采配: ANTHROPIC_API_KEY があれば Claude(brain.py)、なければルールベースで
     手空きメンバーに采配盤のタスクを割り当て
  4. 送信キュー(outbox)の処理 — 外注依頼書などの自動送付
  5. ブロッカー・送信失敗・判断保留があれば人間へエスカレーション(notify.escalate)
  6. 状態を保存し、サマリーをstdoutへ(Actionsのログになる)
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brain
import dispatch
import notify
import sns_publisher

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
OFFICE = os.path.join(REPO, "ai-office-kit")
STATE = os.path.join(OFFICE, "office_state.json")
TASKBOARD = os.path.join(OFFICE, "AIチーム_ナレッジ", "JOHN_TASKBOARD.md")
HTML = os.path.join(OFFICE, "ai_office_1.html")
LOG_MAX = 40
JST = ZoneInfo("Asia/Tokyo")


def team_names():
    """ai_office_1.html の TEAM 定義からメンバー名を取り出す。"""
    with open(HTML, encoding="utf-8") as f:
        html = f.read()
    return re.findall(r'\{\s*room:"[^"]*",\s*name:"([^"]+)"', html)


def load_state():
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE)


def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def expire_finished(st, now):
    """until を過ぎた working エントリを「時間満了」の done に変換。"""
    changed = []
    for o in st.get("overrides", []):
        if o.get("status") != "working":
            continue
        until = parse_iso(o.get("until", ""))
        if until and until < now:
            o["status"] = "done"
            o["at"] = o.pop("until")
            o["task"] = o.get("task", "") + "(自動判定: 作業時間満了)"
            changed.append(o["name"])
    return changed


def busy_names(st, now):
    names = set()
    for o in st.get("overrides", []):
        if o.get("status") != "working":
            continue
        until = parse_iso(o.get("until", ""))
        if until is None or until >= now:
            names.add(o["name"])
    return names


def rule_based_decide(names, st, taskboard, now):
    """APIキーなしのフォールバック采配。采配盤の実行中セクション(P0〜P3)から
    未完了タスクを拾い、担当が手空きなら割り当てる。"""
    busy = busy_names(st, now)
    assignments, seen = [], set()
    section_ok = False
    for line in taskboard.splitlines():
        if line.startswith("## "):
            section_ok = line.startswith(("## P0", "## P2"))
            continue
        if not section_ok or not line.startswith("- "):
            continue
        m = re.match(r"- \(([^)]+)\)[:：]\s*(.+)", line)
        if not m:
            continue
        who, task = m.group(1).strip(), m.group(2).strip()
        prog = re.search(r"進捗[:：]?\s*(\d+)%", task)
        if prog and int(prog.group(1)) >= 100:
            continue
        task = re.sub(r"\s*進捗[:：]?\s*\d+%", "", task).strip()
        if who not in names or who in busy or who in seen:
            continue
        seen.add(who)
        assignments.append({"name": who, "task": f"{task}(自動采配)", "hours": 2})
    return {"assignments": assignments, "log_lines": [], "escalations": []}


def daily_knowledge_update(st, taskboard, now):
    """ライブラの自動ナレッジ更新。毎日7時以降の最初のサイクルで、
    前日の成果(コミット・日誌)を日次ダイジェストとして書き出し、更新ログに1行追記する。"""
    digest_dir = os.path.join(OFFICE, "AIチーム_ナレッジ", "日次ダイジェスト")
    os.makedirs(digest_dir, exist_ok=True)
    today = now.strftime("%Y-%m-%d")
    path = os.path.join(digest_dir, f"{today}.md")
    if now.hour < 7 or os.path.exists(path):
        return False

    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        commits = subprocess.run(
            ["git", "log", "--since", f"{yesterday} 00:00 +0900",
             "--until", f"{today} 07:00 +0900", "--format=- %s", "--no-merges"],
            capture_output=True, text=True, cwd=REPO, timeout=30).stdout.strip()
    except Exception:
        commits = "(取得失敗)"
    done_now = [f"- {o['name']}: {o.get('task','')}" for o in st.get("overrides", [])
                if o.get("status") == "done"]
    body = (
        f"# 日次ダイジェスト {today}(ライブラ自動更新)\n\n"
        f"## 前日のコミット(成果の記録)\n{commits or '- (なし)'}\n\n"
        f"## 直近の完了報告\n" + ("\n".join(done_now) or "- (なし)") + "\n\n"
        f"## 業務日誌(直近)\n" + "\n".join(f"- {l}" for l in st.get("log", [])[-10:]) + "\n\n"
        f"## 采配盤スナップショット\n```\n{taskboard.strip()}\n```\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    evolog = os.path.join(OFFICE, "AIチーム_ナレッジ", "_更新ログ.md")
    try:
        with open(evolog, encoding="utf-8") as f:
            cur = f.read().rstrip("\n")
        with open(evolog, "w", encoding="utf-8") as f:
            f.write(cur + f"\n{today} | ライブラ | 日次ダイジェストを自動更新(前日の成果と采配を記録)\n")
    except OSError:
        pass
    hm = now.strftime("%H:%M")
    st["overrides"] = [o for o in st.get("overrides", []) if o.get("name") != "ライブラ"]
    st["overrides"].append({
        "name": "ライブラ", "status": "done",
        "task": f"日次ダイジェスト({today})を自動更新",
        "at": now.isoformat(timespec="seconds"),
    })
    st.setdefault("log", []).append(f"{hm} ライブラ: おはようございます。日次ダイジェストを更新しました")
    return True


def taskboard_blockers(taskboard):
    lines, in_section = [], False
    for line in taskboard.splitlines():
        if line.startswith("## "):
            in_section = line.startswith("## ブロッカー")
            continue
        if in_section and line.startswith("- "):
            item = line[2:].strip()
            if item and not re.match(r"^\(な(し|ん)?\)$", item):
                lines.append(item)
    return lines


def apply_decision(st, decision, names, now, source):
    hm = now.strftime("%H:%M")
    applied = []
    busy = busy_names(st, now)
    for a in decision.get("assignments", []):
        name = a.get("name", "")
        if name not in names or name in busy:
            continue
        hours = min(max(int(a.get("hours", 2)), 1), 4)
        st["overrides"] = [o for o in st.get("overrides", []) if o.get("name") != name]
        st["overrides"].append({
            "name": name,
            "status": "working",
            "task": a["task"],
            "at": now.isoformat(timespec="seconds"),
            "until": (now + timedelta(hours=hours)).isoformat(timespec="seconds"),
        })
        applied.append(f"{name}: {a['task']}")
        st.setdefault("log", []).append(f"{hm} ジン: {name}へ采配『{a['task']}』({source})")
    for line in decision.get("log_lines", []):
        st.setdefault("log", []).append(f"{hm} {line}")
    return applied


def main():
    dry_run = "--dry-run" in sys.argv
    now = datetime.now(JST)
    summary = {"time": now.isoformat(timespec="seconds"), "dry_run": dry_run}

    try:
        st = load_state()
        names = team_names()
        with open(TASKBOARD, encoding="utf-8") as f:
            taskboard = f.read()
    except Exception as e:
        if not dry_run:
            notify.escalate("オフィスの状態ファイルが読めません", repr(e))
        print(json.dumps({"fatal": repr(e)}, ensure_ascii=False))
        return 1

    # 1. 満了整理 + 日次ナレッジ更新(ライブラ・毎朝7時以降の初回)
    summary["expired"] = expire_finished(st, now)
    summary["daily_digest"] = daily_knowledge_update(st, taskboard, now)

    # 2. 采配(Claude → ルールベースの順)
    decision = brain.decide(names, st, taskboard)
    source = "Claude采配"
    if decision is None:
        decision = rule_based_decide(names, st, taskboard, now)
        source = "自動采配"
    summary["assigned"] = apply_decision(st, decision, names, now, source)
    summary["decision_source"] = source

    # 3. 送信キュー
    if dry_run:
        summary["outbox"] = {"skipped": "dry-run"}
        summary["sns"] = {"skipped": "dry-run"}
    else:
        outbox = dispatch.process_outbox()
        summary["outbox"] = outbox
        hm = now.strftime("%H:%M")
        for line in outbox["sent"]:
            st.setdefault("log", []).append(f"{hm} ジン: 【自動送信】{line}")
        sns = sns_publisher.process_queue(now)
        summary["sns"] = sns
        for line in sns["posted"]:
            st.setdefault("log", []).append(f"{hm} ルナ: 【自動投稿】Instagramへ投稿しました({line.split(':')[0]})")

    # 4. エスカレーション判定
    issues = list(decision.get("escalations", []))
    issues += [f"ブロッカー: {b}" for b in taskboard_blockers(taskboard)]
    if not dry_run:
        issues += [f"送信失敗: {x}" for x in summary.get("outbox", {}).get("failed", [])]
        issues += [f"Instagram投稿失敗: {x}" for x in summary.get("sns", {}).get("failed", [])]
    summary["escalations"] = issues
    if issues:
        text = "\n".join(f"・{i}" for i in issues)
        if dry_run:
            summary["escalated"] = "dry-run(通知せず)"
        else:
            summary["escalated"] = notify.escalate(
                f"オフィス自律運転からの相談 {len(issues)}件", text)

    # 5. 保存(dry-runでは書き込まない)
    st["log"] = st.get("log", [])[-LOG_MAX:]
    if dry_run:
        summary["state_saved"] = False
    else:
        save_state(st)
        summary["state_saved"] = True

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
