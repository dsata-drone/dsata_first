# -*- coding: utf-8 -*-
"""X(Twitter)自動投稿 — API v2 + OAuth 1.0a(標準ライブラリのみで署名)。

必要な環境変数(developers.x.com のアプリで発行、GitHub Secretsに登録):
  X_API_KEY / X_API_SECRET          アプリのConsumer Key/Secret
  X_ACCESS_TOKEN / X_ACCESS_SECRET  アカウント(@alux_japan等)のAccess Token/Secret
未設定の間ジョブは保留される。

ジョブは sns/queue/pending/*.json に "platform": "x" で置く:
{
  "platform": "x",
  "text": "ツイート本文(URLは23文字換算・全体280字/日本語140字)",
  "image_paths": ["assets/logo/alux_logo_2000.png"],   // 任意・最大4枚・リポジトリ相対
  "scheduled_at": "2026-07-21T18:00:00+09:00"
}
※ 無料枠は投稿数に月間上限がある(2025年時点で月500件程度)。週2〜3本なら余裕。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
TWEET_URL = "https://api.x.com/2/tweets"


def _creds():
    keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")
    vals = [os.environ.get(k, "").strip() for k in keys]
    return vals if all(vals) else None


def _pct(s):
    return urllib.parse.quote(str(s), safe="~")


def _oauth1_header(method, url, extra_params=None):
    ck, cs, at, ats = _creds()
    oauth = {
        "oauth_consumer_key": ck,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": at,
        "oauth_version": "1.0",
    }
    sig_params = dict(oauth)
    sig_params.update(extra_params or {})
    param_str = "&".join(f"{_pct(k)}={_pct(v)}"
                         for k, v in sorted(sig_params.items()))
    base_str = "&".join([method.upper(), _pct(url), _pct(param_str)])
    key = f"{_pct(cs)}&{_pct(ats)}".encode()
    sig = base64.b64encode(
        hmac.new(key, base_str.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"'
                                for k, v in sorted(oauth.items()))


def _upload_media(path):
    """v1.1 media/upload(multipart)。multipart本文はOAuth署名に含めない仕様。"""
    full = os.path.normpath(os.path.join(REPO, path))
    if not full.startswith(REPO):
        raise ValueError(f"リポジトリ外のパス: {path}")
    with open(full, "rb") as f:
        data = f.read()
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="media"; filename="{os.path.basename(full)}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(UPLOAD_URL, data=body, headers={
        "Authorization": _oauth1_header("POST", UPLOAD_URL),
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())["media_id_string"]


def post(job):
    """1ジョブ投稿。成功でツイートIDを返す。認証未設定なら None(保留)。"""
    if _creds() is None:
        return None
    payload = {"text": job["text"]}
    paths = job.get("image_paths", [])[:4]
    if paths:
        payload["media"] = {"media_ids": [_upload_media(p) for p in paths]}
    req = urllib.request.Request(
        TWEET_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": _oauth1_header("POST", TWEET_URL),
            "Content-Type": "application/json",
        })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())["data"]["id"]
