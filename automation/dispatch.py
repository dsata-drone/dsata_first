# -*- coding: utf-8 -*-
"""送信キュー(outbox) — 「社長の送付」を自動化するレイヤー。

外注依頼書の送付などの外向きアクションを、ジョブファイルとしてキューに置くと
オーケストレータが自動送信する。ジョブは JSON 1ファイル = 1件:

  automation/outbox/pending/xxx.json   ← 送信待ち(ここに置く)
  automation/outbox/sent/              ← 送信成功(結果を追記して移動)
  automation/outbox/failed/            ← 送信失敗(エスカレーション対象)

ジョブのスキーマ:
{
  "channel": "email" | "slack",
  "to": "宛先メールアドレス",            // email のみ
  "subject": "件名",                    // email のみ
  "body_file": "aio-project/外注依頼書_W1実装.md",  // リポジトリルート相対。または "body": "本文"
  "attachments": ["aio-project/w1_deliverables/llms.txt_alux-follow.com", ...],  // email のみ
  "note": "人間向けメモ(送信されない)"
}

※ .example 拡張子のファイルは送信されない(雛形用)。
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import notify

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
PENDING = os.path.join(BASE, "outbox", "pending")
SENT = os.path.join(BASE, "outbox", "sent")
FAILED = os.path.join(BASE, "outbox", "failed")

JST = ZoneInfo("Asia/Tokyo")


def _read_repo_file(rel_path):
    path = os.path.normpath(os.path.join(REPO, rel_path))
    if not path.startswith(REPO):
        raise ValueError(f"リポジトリ外のパスは添付できません: {rel_path}")
    with open(path, "rb") as f:
        return f.read()


def _send_job(job):
    """1ジョブ送信。成功で説明文字列を返す。チャネル未設定なら None(保留)。"""
    channel = job.get("channel", "email")
    body = job.get("body")
    if not body and job.get("body_file"):
        body = _read_repo_file(job["body_file"]).decode("utf-8")
    if not body:
        raise ValueError("body か body_file が必要です")

    if channel == "slack":
        ok = notify.send_slack(body)
        return "Slackへ送信" if ok else None
    if channel == "email":
        to = job.get("to", "").strip()
        if not to:
            raise ValueError("email ジョブには to が必要です")
        attachments = [(os.path.basename(p), _read_repo_file(p))
                       for p in job.get("attachments", [])]
        ok = notify.send_email(to, job.get("subject", "(件名なし)"), body, attachments)
        return f"{to} へメール送信({len(attachments)}添付)" if ok else None
    raise ValueError(f"未知のchannel: {channel}")


def process_outbox():
    """送信待ちジョブを全処理。結果を dict で返す。
    チャネル未設定(SMTP/Slack両方なし)のジョブは pending に残す。"""
    os.makedirs(PENDING, exist_ok=True)
    os.makedirs(SENT, exist_ok=True)
    os.makedirs(FAILED, exist_ok=True)
    results = {"sent": [], "held": [], "failed": []}

    for name in sorted(os.listdir(PENDING)):
        if not name.endswith(".json"):
            continue  # .example などはスキップ
        src = os.path.join(PENDING, name)
        try:
            with open(src, encoding="utf-8") as f:
                job = json.load(f)
            desc = _send_job(job)
            if desc is None:
                results["held"].append(f"{name}: 送信チャネル未設定のため保留")
                continue
            job["_sent_at"] = datetime.now(JST).isoformat(timespec="seconds")
            job["_result"] = desc
            with open(os.path.join(SENT, name), "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False, indent=2)
            os.remove(src)
            results["sent"].append(f"{name}: {desc}")
        except Exception as e:
            results["failed"].append(f"{name}: {e}")
            try:
                os.replace(src, os.path.join(FAILED, name))
            except OSError:
                pass
    return results
