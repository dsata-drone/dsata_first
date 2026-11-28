# -*- coding: utf-8 -*-
"""通知レイヤー — Slack / メール送信とエマージェンシー・エスカレーション。

環境変数(すべて任意。未設定のチャネルはスキップされる):
  SLACK_WEBHOOK_URL            通常通知用 Slack Incoming Webhook
  EMERGENCY_SLACK_WEBHOOK_URL  エスカレーション用(未設定なら SLACK_WEBHOOK_URL を使う)
  SMTP_HOST / SMTP_PORT        メール送信サーバー(例: smtp.gmail.com / 587)
  SMTP_USER / SMTP_PASS        SMTP認証情報
  SMTP_FROM                    差出人アドレス(未設定なら SMTP_USER)
  GITHUB_REPOSITORY / GITHUB_TOKEN  Slack不達時のフォールバック(Issue起票)。
                               GitHub Actions 上では自動で入る
"""
import json
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr


class NotifyError(Exception):
    pass


def _post_json(url, payload, timeout=15):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def send_slack(text, webhook_env="SLACK_WEBHOOK_URL"):
    """Slackへ送信。webhook未設定なら False、送信成功で True、失敗は例外。"""
    url = os.environ.get(webhook_env, "").strip()
    if not url:
        return False
    status = _post_json(url, {"text": text})
    if status >= 300:
        raise NotifyError(f"Slack webhook returned {status}")
    return True


def send_email(to_addr, subject, body, attachments=None, sender_name="follow AIチーム"):
    """SMTPでメール送信。attachments は [(ファイル名, bytes), ...]。
    SMTP未設定なら False、送信成功で True、失敗は例外。"""
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    from_addr = os.environ.get("SMTP_FROM", user)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, from_addr))
    msg["To"] = to_addr
    msg.set_content(body)
    for name, data in attachments or []:
        msg.add_attachment(data, maintype="application",
                           subtype="octet-stream", filename=name)

    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls(context=ctx)
        if user:
            s.login(user, password)
        s.send_message(msg)
    return True


def _create_github_issue(title, body):
    """Slack全滅時の最終フォールバック: リポジトリにIssueを立てる。"""
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not repo or not token:
        return False
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": title, "body": body,
                         "labels": ["office-emergency"]}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status < 300


def escalate(summary, detail=""):
    """エマージェンシー通知。人間の判断が必要な時・エラー時だけ呼ぶこと。
    経路: 緊急Slack → 通常Slack → GitHub Issue の順にフォールバック。
    どこか1つに届けば True。全滅なら False(呼び出し側でログに残す)。"""
    text = f"🚨 *[AIオフィス] 人間の判断が必要です*\n{summary}"
    if detail:
        text += f"\n```{detail[:2500]}```"
    for env in ("EMERGENCY_SLACK_WEBHOOK_URL", "SLACK_WEBHOOK_URL"):
        try:
            if send_slack(text, webhook_env=env):
                return True
        except Exception:
            continue
    try:
        return _create_github_issue(f"[AIオフィス緊急] {summary}",
                                    f"{summary}\n\n```\n{detail}\n```")
    except Exception:
        return False
