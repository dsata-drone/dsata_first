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
BACKLOG_FILE = os.path.join(OFFICE, "AIチーム_ナレッジ", "ロール別バックログ.md")
HTML = os.path.join(OFFICE, "ai_office_1.html")
LOG_MAX = 40
JST = ZoneInfo("Asia/Tokyo")
# 常時稼働: 待機の許容は0分。手空きのメンバーは毎サイクル、その場で
# 先行準備・定常業務・自主研究タスクが采配され「作業中」になる。
# 「指示待ち」「条件待ち」による長時間停止は存在しない(休憩室は画面上の
# 数分リフレッシュ演出のみ)。値を増やせば旧・稼働保証(待機許容)に戻せる。
IDLE_LIMIT_MIN = 0


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


TASK_RE = re.compile(r"- \(([^)]+)\)[:：]\s*(.+)")


def parse_taskboard(taskboard):
    """采配盤を構造化する。行書式: `- (名前)：本文 [依存:名前] [進捗:N%]`
    → {section, who, task, prog, dep} のリスト。
    `依存:名前` を付けたタスクは、その名前の担当のP0〜P3タスクが完了(100%)する
    まで采配されず、待機理由「〇〇のタスク完了待ち」として画面に表示される。"""
    items, section = [], ""
    for line in taskboard.splitlines():
        if line.startswith("## "):
            section = line[3:].split("（")[0].split("(")[0].strip()
            continue
        m = TASK_RE.match(line)
        if not m:
            continue
        who, body = m.group(1).strip(), m.group(2).strip()
        prog = re.search(r"進捗[:：]?\s*(\d+)%", body)
        dep = re.search(r"依存[:：]\s*([^\s]+)", body)
        help_m = re.search(r"応援[:：]\s*([^\s]+)", body)
        task = re.sub(r"\s*依存[:：]\s*[^\s]+", "", body)
        task = re.sub(r"\s*応援[:：]\s*[^\s]+", "", task)
        task = re.sub(r"\s*進捗[:：]?\s*\d+%", "", task).strip()
        items.append({
            "section": section, "who": who, "task": task,
            "prog": int(prog.group(1)) if prog else None,
            "dep": dep.group(1) if dep else None,
            "help": help_m.group(1) if help_m else None,
        })
    return items


def _is_active(item):
    """実行中セクション(P0-P1/P2-P3)のタスクか。"""
    return item["section"].startswith(("P0", "P2"))


def _is_open(item):
    """未完了(進捗100%未満または進捗表記なし)か。"""
    return item["prog"] is None or item["prog"] < 100


def dep_unmet(items, dep_name):
    """依存先メンバーに未完了のP0〜P3タスクが残っているか。"""
    return any(_is_active(i) and _is_open(i) and i["who"] == dep_name
               for i in items)


def rule_based_decide(names, st, taskboard, now):
    """APIキーなしのフォールバック采配。采配盤の実行中セクション(P0〜P3)から
    未完了タスクを拾い、担当が手空きかつ依存タスクが完了していれば割り当てる。"""
    busy = busy_names(st, now)
    items = parse_taskboard(taskboard)
    assignments, seen = [], set()
    for it in items:
        if not _is_active(it) or not _is_open(it):
            continue
        who = it["who"]
        if who not in names or who in busy or who in seen:
            continue
        if it["dep"] and it["dep"] != who and dep_unmet(items, it["dep"]):
            continue  # 依存タスクが未完了 → 待機(理由は compute_waiting が書く)
        seen.add(who)
        assignments.append({"name": who, "task": f"{it['task']}(自動采配)", "hours": 2})
    return {"assignments": assignments, "log_lines": [], "escalations": []}


PROGRESS_PER_BLOCK = 25  # 1作業ブロック(采配1回=最大4h)の完了ごとに進む進捗%


def _strip_auto_suffix(task):
    """overrides のタスク文から自動付与サフィックスを外し、采配盤の本文と比較可能にする。"""
    t = re.sub(r"\(自動判定: 作業時間満了\)$", "", task or "")
    t = re.sub(r"\(自動采配\)$", "", t)
    t = re.sub(r"\([^()]*からの依頼\)$", "", t)
    return t.strip()


def bump_progress(taskboard, st, expired_names):
    """満了した作業ブロック分、采配盤の該当タスクの進捗を自動で進める。
    100%に達したタスクは「完了」となり、依存する次のメンバーへのバトンパス
    (process_handoffs)の引き金になる。
    返り値: (新しい采配盤テキスト, 変更有無, 100%に到達した[(名前,タスク)])"""
    if not expired_names:
        return taskboard, False, []
    done_tasks = {}
    for o in st.get("overrides", []):
        if o.get("name") in expired_names and o.get("status") == "done":
            done_tasks.setdefault(o["name"], set()).add(_strip_auto_suffix(o.get("task")))
    changed, completed, out = False, [], []
    for line in taskboard.splitlines():
        new_line = line
        m = TASK_RE.match(line)
        pm = re.search(r"進捗[:：]?\s*(\d+)%", line)
        if m and pm:
            who = m.group(1).strip()
            body = re.sub(r"\s*依存[:：]\s*[^\s]+", "", m.group(2))
            body = re.sub(r"\s*応援[:：]\s*[^\s]+", "", body)
            body = re.sub(r"\s*進捗[:：]?\s*\d+%", "", body).strip()
            if who in done_tasks and body in done_tasks[who]:
                new = min(100, int(pm.group(1)) + PROGRESS_PER_BLOCK)
                if new != int(pm.group(1)):
                    new_line = line[:pm.start(1)] + str(new) + line[pm.end(1):]
                    changed = True
                    if new >= 100:
                        completed.append((who, body))
        out.append(new_line)
    return "\n".join(out) + "\n", changed, completed


def expire_collabs(st, now):
    """連携レコードの寿命管理。until を過ぎた active は completed にし、
    完了から6時間経ったものは削除。最大12件までに保つ。"""
    collabs = st.get("collaborations", [])
    for c in collabs:
        if c.get("status") == "active":
            u = parse_iso(c.get("until", ""))
            if u and u < now:
                c["status"] = "completed"

    def keep(c):
        if c.get("status") != "completed":
            return True
        u = parse_iso(c.get("until") or c.get("timestamp") or "")
        return bool(u and (now - u) < timedelta(hours=6))
    st["collaborations"] = [c for c in collabs if keep(c)][-12:]


def _add_collab(st, now, from_agent, to_agent, request_type, message, task, until):
    st.setdefault("collaborations", []).append({
        "from_agent": from_agent, "to_agent": to_agent,
        "request_type": request_type, "status": "active",
        "message": message, "task": task,
        "timestamp": now.isoformat(timespec="seconds"), "until": until,
    })


def process_handoffs(st, taskboard, names, now):
    """バトンパス(成果物の引き渡し)。依存関係(依存:名前)の上流タスクがすべて
    完了(100%)した瞬間、上流メンバーから下流メンバーへ自動で仕事を引き渡す:
    下流タスクを「作業中(〇〇からの依頼)」で起動し、collaborations に連携を記録。
    人間の指示なしで仕事がリレーされる。"""
    items = parse_taskboard(taskboard)
    busy = busy_names(st, now)
    collabs = st.get("collaborations", [])
    hm = now.strftime("%H:%M")
    handed = []
    for it in items:
        if not _is_active(it) or not _is_open(it) or not it["dep"]:
            continue
        who, dep = it["who"], it["dep"]
        if who not in names or dep == who or who in busy:
            continue
        if dep_unmet(items, dep):
            continue
        if any(c.get("to_agent") == who and c.get("task") == it["task"]
               and c.get("status") in ("pending", "active") for c in collabs):
            continue
        until = (now + timedelta(hours=2)).isoformat(timespec="seconds")
        st["overrides"] = [o for o in st.get("overrides", []) if o.get("name") != who]
        st["overrides"].append({
            "name": who, "status": "working",
            "task": f"{it['task']}({dep}からの依頼)",
            "at": now.isoformat(timespec="seconds"), "until": until,
        })
        _add_collab(st, now, dep, who, "handoff",
                    f"担当分が完了しました。「{it['task']}」をお願いします！",
                    it["task"], until)
        st.setdefault("log", []).append(f"{hm} {dep}: ✉ {who}へバトンパス『{it['task']}』")
        st.setdefault("log", []).append(f"{hm} {who}: {dep}さんから依頼を受信、作業を開始します！")
        busy.add(who)
        handed.append(f"{dep}→{who}: {it['task']}")
    return handed


def process_help_requests(st, taskboard, names, now):
    """ヘルプ・共同作業の要請。采配盤のタスクに `応援:名前` が付いていて、
    担当者が現にそのタスクを作業中かつ応援者が手空きなら、応援者へ
    手伝いサブタスクを発行して共同作業(help連携)にする。"""
    items = parse_taskboard(taskboard)
    busy = busy_names(st, now)
    collabs = st.get("collaborations", [])
    hm = now.strftime("%H:%M")
    issued = []
    working_tasks = {o["name"]: _strip_auto_suffix(o.get("task"))
                     for o in st.get("overrides", []) if o.get("status") == "working"}
    for it in items:
        helper, owner = it.get("help"), it["who"]
        if not helper or helper not in names or helper in busy or helper == owner:
            continue
        if working_tasks.get(owner) != it["task"]:
            continue  # 担当者がそのタスクを作業中のときだけ応援を呼ぶ
        if any(c.get("to_agent") == helper and c.get("status") in ("pending", "active")
               for c in collabs):
            continue
        until = (now + timedelta(hours=2)).isoformat(timespec="seconds")
        sub = f"【応援】{owner}の「{it['task']}」を支援({owner}からの依頼)"
        st["overrides"] = [o for o in st.get("overrides", []) if o.get("name") != helper]
        st["overrides"].append({
            "name": helper, "status": "working", "task": sub,
            "at": now.isoformat(timespec="seconds"), "until": until,
        })
        _add_collab(st, now, owner, helper, "help",
                    f"「{it['task']}」の応援をお願いします！", it["task"], until)
        st.setdefault("log", []).append(f"{hm} {owner}: 🤝 {helper}へ応援要請『{it['task']}』")
        st.setdefault("log", []).append(f"{hm} {helper}: {owner}さんの応援に入ります！")
        busy.add(helper)
        issued.append(f"{owner}→{helper}: {it['task']}")
    return issued


def update_idle_clock(st, names, now):
    """待機時間の日次累計(idle_clock)を更新する。
    前サイクルからの経過分を working でないメンバーに加算。日付が変わればリセット。
    初回のみ、各メンバーの直近の活動時刻(overrides の at)から待機分を推定シードする
    (再起動直後でも長期待機者がすぐ稼働保証の対象になるように)。"""
    today = now.strftime("%Y-%m-%d")
    clock = st.get("idle_clock")
    if not clock or clock.get("date") != today:
        seeded = {}
        if not clock:
            busy = busy_names(st, now)
            for o in st.get("overrides", []):
                at = parse_iso(o.get("at"))
                if o.get("name") in names and o["name"] not in busy and at:
                    m = (now - at).total_seconds() / 60
                    seeded[o["name"]] = round(min(max(m, 0), IDLE_LIMIT_MIN))
        clock = {"date": today, "minutes": seeded,
                 "last_at": now.isoformat(timespec="seconds")}
        st["idle_clock"] = clock
        return clock["minutes"]
    last = parse_iso(clock.get("last_at")) or now
    elapsed = max(0.0, min((now - last).total_seconds() / 60, 180.0))
    busy = busy_names(st, now)
    mins = clock.setdefault("minutes", {})
    for n in names:
        if n in busy:
            continue
        e = elapsed
        o = next((x for x in st.get("overrides", []) if x.get("name") == n), None)
        if o:
            at = parse_iso(o.get("at"))
            if at and at > last:   # サイクル間まで働いていた分は完了時刻から数える
                e = min(e, max(0.0, (now - at).total_seconds() / 60))
        mins[n] = round(mins.get(n, 0) + e)
    clock["last_at"] = now.isoformat(timespec="seconds")
    return mins


def load_backlog():
    """ロール別バックログ.md を読み込む。{名前: [タスク, ...]}。
    `## 名前` セクションに `- タスク` を列挙する書式。`_default` は共通プール。"""
    pools, cur = {}, None
    try:
        with open(BACKLOG_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip()
                if line.startswith("## "):
                    cur = line[3:].strip()
                    pools[cur] = []
                elif cur and line.startswith("- "):
                    pools[cur].append(line[2:].strip())
    except OSError:
        pass
    return {k: v for k, v in pools.items() if v}


def pick_activity_task(st, items, backlog, name):
    """稼働保証タスクの選定。優先順:
    ① 本人のバックログ(ローテーション) ② 将来タスク(P4-P5)の先行準備を自動創出
    ③ 共通バックログ(_default) ④ 汎用の自主研究。"""
    cursor = st.setdefault("backlog_cursor", {})
    pool = backlog.get(name)
    if pool:
        i = cursor.get(name, 0)
        cursor[name] = i + 1
        return f"【定常業務】{pool[i % len(pool)]}"
    future = [x for x in items if x["who"] == name and x["section"].startswith("P4")]
    if future:
        base = re.split(r"[—(（]", future[0]["task"])[0].strip() or future[0]["task"]
        return f"【先行準備】{base}に向けた事前調査・準備ドキュメントの作成"
    pool = backlog.get("_default")
    if pool:
        i = cursor.get(name, 0)
        cursor[name] = i + 1
        return f"【定常業務】{pool[i % len(pool)]}"
    return "【自主研究】担当領域の最新動向リサーチとナレッジMDの更新"


def ensure_min_activity(st, taskboard, names, now):
    """常時稼働の保証。待機の日次累計が IDLE_LIMIT_MIN(現在0=待機を許容しない)に
    達したメンバーへ、先行準備・定常業務タスクを自動生成して4時間の「作業中」にする。
    つまり毎サイクル、手空きの全員に必ず仕事が入る — 停止しているエージェントは存在しない。
    通常采配(P0〜P3)が先に走るので、実タスクがある人はそちらが優先される。"""
    mins = st.get("idle_clock", {}).get("minutes", {})
    busy = busy_names(st, now)
    items = parse_taskboard(taskboard)
    backlog = load_backlog()
    hm = now.strftime("%H:%M")
    assigned = []
    for name in names:
        if name in busy or mins.get(name, 0) < IDLE_LIMIT_MIN:
            continue
        task = pick_activity_task(st, items, backlog, name)
        st["overrides"] = [o for o in st.get("overrides", []) if o.get("name") != name]
        st["overrides"].append({
            "name": name, "status": "working", "task": task,
            "at": now.isoformat(timespec="seconds"),
            "until": (now + timedelta(hours=4)).isoformat(timespec="seconds"),
        })
        st.setdefault("log", []).append(
            f"{hm} ジン: {name}へ常時稼働タスク『{task}』(手空きゼロ運用)")
        assigned.append(f"{name}: {task}")
    return assigned


def _fmt_min(m):
    h, mm = int(m) // 60, int(m) % 60
    return (f"{h}時間{mm}分" if mm else f"{h}時間") if h else f"{mm}分"


def compute_waiting(st, taskboard, names, now):
    """手空き(working でない)メンバーの「次の一歩を踏み出す条件」を算出し、
    office_state.json の waiting マップ({名前: 理由})として書き出す。
    オフィス画面はこれを「待機中(〇〇待ち)」として状態欄に表示する。
    判定順: 依存タスク未完了 → 実行中タスクあり(再采配待ち) → P4-P5の時期・条件
    → 何も持っていなければ社長指示待ち。
    どの理由でも、稼働保証(1日の待機上限4h)までの残り時間を「あと〇〇で自動稼働」
    として併記する — 長期待機(「9月まで待機」等)のまま放置される表示は存在しない。"""
    busy = busy_names(st, now)
    items = parse_taskboard(taskboard)
    mins = st.get("idle_clock", {}).get("minutes", {})
    waiting = {}
    for name in names:
        if name in busy:
            continue
        mine = [i for i in items if i["who"] == name]
        active = [i for i in mine if _is_active(i) and _is_open(i)]
        reason = None
        for it in active:
            if it["dep"] and it["dep"] != name and dep_unmet(items, it["dep"]):
                reason = f"{it['dep']}のタスク完了待ち"
                break
        if reason is None and active:
            reason = "ジンの再采配待ち"  # 次サイクルで自動采配される
        if reason is None:
            for it in (i for i in mine if i["section"].startswith("P4")):
                t = it["task"]
                if "補助金" in t:
                    reason = "補助金採択待ち"
                elif "9月" in t:
                    reason = "9月本番待ち"
                elif "毎朝" in t:
                    reason = "毎朝7時の定期作業待ち"
                elif "毎週土曜" in t or "週次" in t:
                    reason = "週次実行日(土曜)待ち"
                else:
                    reason = "時期・条件待ち"
                break
        base = reason or "社長指示待ち"
        rem = max(0, IDLE_LIMIT_MIN - mins.get(name, 0))
        # 常時稼働(IDLE_LIMIT_MIN=0)では ensure_min_activity が同サイクルで全員を
        # 稼働させるため、通常この waiting は空になる(残るのは瞬間的な表示のみ)
        suffix = f"あと{_fmt_min(rem)}で自動稼働" if rem else "まもなく自動稼働"
        waiting[name] = f"{base}・{suffix}"
    st["waiting"] = waiting
    return waiting


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

    # 1. 満了整理 + 進捗の自動加算 + 連携レコード整理 + 待機累計 + 日次ナレッジ更新
    summary["expired"] = expire_finished(st, now)
    taskboard, tb_changed, tb_completed = bump_progress(st=st, taskboard=taskboard,
                                                        expired_names=summary["expired"])
    summary["progress_completed"] = [f"{w}: {t}" for w, t in tb_completed]
    if tb_changed and not dry_run:
        with open(TASKBOARD, "w", encoding="utf-8") as f:
            f.write(taskboard)
    expire_collabs(st, now)
    summary["idle_minutes"] = update_idle_clock(st, names, now)
    summary["daily_digest"] = daily_knowledge_update(st, taskboard, now)

    # 1.5 エージェント間連携: バトンパス(依存タスクの引き渡し)と応援要請
    summary["handoffs"] = process_handoffs(st, taskboard, names, now)
    summary["help_requests"] = process_help_requests(st, taskboard, names, now)

    # 2. 采配(Claude → ルールベースの順)
    decision = brain.decide(names, st, taskboard)
    source = "Claude采配"
    if decision is None:
        decision = rule_based_decide(names, st, taskboard, now)
        source = "自動采配"
    summary["assigned"] = apply_decision(st, decision, names, now, source)
    summary["decision_source"] = source

    # 2.5 稼働保証: 待機が1日の上限(4h)に達したメンバーへ先行準備・定常業務を采配
    summary["min_activity"] = ensure_min_activity(st, taskboard, names, now)

    # 2.6 待機理由の算出(手空きメンバーの「〇〇待ち・あと〇〇で自動稼働」を画面へ)
    summary["waiting"] = compute_waiting(st, taskboard, names, now)

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
            s_name = line.split(":")[0]
            st.setdefault("log", []).append(f"{hm} ルナ: 【自動投稿】SNSへ投稿完了({s_name})")

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
