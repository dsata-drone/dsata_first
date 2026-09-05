# -*- coding: utf-8 -*-
"""Instagram自動投稿 — Meta Graph API(Content Publishing)による予約投稿キュー。

ルナが sns/queue/pending/ に投稿ジョブ(JSON)を置くと、オーケストレータの
サイクルが scheduled_at を過ぎたジョブを自動投稿する。

必要な環境変数(GitHub Secrets):
  IG_USER_ID       InstagramビジネスアカウントのユーザーID(数字)
  IG_ACCESS_TOKEN  長期アクセストークン(60日有効・要定期更新)
未設定の間、ジョブは送信されず pending に保留される(安全側)。

ジョブのスキーマ(sns/queue/pending/xxx.json):
{
  "type": "image" | "carousel" | "reel",
  "caption": "本文+ハッシュタグ",
  "image_urls": ["https://...公開URL"],   // image:1枚 / carousel:2〜10枚
  "video_url": "https://...",             // reel のみ
  "scheduled_at": "2026-07-21T18:00:00+09:00",  // この時刻以降のサイクルで投稿
  "note": "人間向けメモ"
}
※ 画像・動画は「Metaのサーバーから取得できる公開URL」である必要がある。
  (リポジトリが非公開の場合、GitHubのrawリンクは使えないので注意)
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import x_publisher

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
PENDING = os.path.join(REPO, "sns", "queue", "pending")
SENT = os.path.join(REPO, "sns", "queue", "sent")
FAILED = os.path.join(REPO, "sns", "queue", "failed")
GRAPH = "https://graph.facebook.com/v21.0"
JST = ZoneInfo("Asia/Tokyo")


def _creds():
    return (os.environ.get("IG_USER_ID", "").strip(),
            os.environ.get("IG_ACCESS_TOKEN", "").strip())


def _post(path, params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"{GRAPH}/{path}", data=data, timeout=60) as r:
        return json.loads(r.read().decode())


def _create_container(user_id, token, job):
    t = job.get("type", "image")
    if t == "image":
        return _post(f"{user_id}/media", {
            "image_url": job["image_urls"][0],
            "caption": job.get("caption", ""),
            "access_token": token})["id"]
    if t == "carousel":
        children = [
            _post(f"{user_id}/media", {
                "image_url": u, "is_carousel_item": "true",
                "access_token": token})["id"]
            for u in job["image_urls"]]
        return _post(f"{user_id}/media", {
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": job.get("caption", ""),
            "access_token": token})["id"]
    if t == "reel":
        return _post(f"{user_id}/media", {
            "media_type": "REELS",
            "video_url": job["video_url"],
            "caption": job.get("caption", ""),
            "access_token": token})["id"]
    raise ValueError(f"未知のtype: {t}")


def process_queue(now=None):
    """投稿時刻を過ぎたジョブを投稿する。結果dictを返す。"""
    now = now or datetime.now(JST)
    for d in (PENDING, SENT, FAILED):
        os.makedirs(d, exist_ok=True)
    results = {"posted": [], "held": [], "failed": []}
    user_id, token = _creds()

    for name in sorted(os.listdir(PENDING)):
        if not name.endswith(".json"):
            continue
        src = os.path.join(PENDING, name)
        try:
            with open(src, encoding="utf-8") as f:
                job = json.load(f)
            sched = job.get("scheduled_at")
            if sched and datetime.fromisoformat(sched) > now:
                continue  # まだ時刻前(heldにも数えない)
            platform = job.get("platform", "instagram")
            if platform == "x":
                tweet_id = x_publisher.post(job)
                if tweet_id is None:
                    results["held"].append(f"{name}: X認証情報が未設定のため保留")
                    continue
                media_id = f"tweet:{tweet_id}"
            else:
                if not user_id or not token:
                    results["held"].append(f"{name}: IG認証情報が未設定のため保留")
                    continue
                container = _create_container(user_id, token, job)
                pub = _post(f"{user_id}/media_publish", {
                    "creation_id": container, "access_token": token})
                media_id = pub.get("id", "")
            job["_published_at"] = now.isoformat(timespec="seconds")
            job["_media_id"] = media_id
            with open(os.path.join(SENT, name), "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False, indent=2)
            os.remove(src)
            results["posted"].append(f"{name}: media_id={pub.get('id','')}")
        except Exception as e:
            results["failed"].append(f"{name}: {e}")
            try:
                os.replace(src, os.path.join(FAILED, name))
            except OSError:
                pass
    return results
