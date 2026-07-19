# 05. セットアップ & 表示方法

## なぜローカルサーバーが要るか
オフィスは `office_state.json` などを `fetch` で読む。ブラウザは `file://` での fetch を
セキュリティ上ブロックするため、**簡易HTTPサーバー経由で開く必要がある**（見た目はサーバー不要だが読み込みに必要）。

## 最短の起動（Python標準のみ）
HTMLと `office_state.json` を同じフォルダに置き、そのフォルダで：
```bash
cd /path/to/office
python3 -m http.server 8899
# ブラウザで http://localhost:8899/ai_office_template.html を開く
```
- ポートは任意（例では8899）。停止は Ctrl+C。
- 更新は「JSONを保存 → 最大30秒で自動反映」または手動リロード。

## ワンクリック起動（mac: ランチャー + .app）
最新の `ai_office_NN.html` を自動で開くランチャー例（`~/.aioffice/launch.sh`）：
```bash
#!/bin/bash
DIR="$HOME/office"       # HTMLとjsonを置くフォルダ
PORT=8899
PY=/usr/bin/python3

# フォルダ内で最も番号の大きい ai_office_N.html を最新版として選ぶ
LATEST=""; maxn=-1
for f in "$DIR"/ai_office_*.html; do
  [ -e "$f" ] || continue
  n=$(basename "$f" | sed -E 's/ai_office_([0-9]+)\.html/\1/')
  case "$n" in ''|*[!0-9]*) continue;; esac
  if [ "$n" -gt "$maxn" ]; then maxn="$n"; LATEST="$f"; fi
done
[ -z "$LATEST" ] && { echo "ai_office_*.html が無い"; exit 3; }

# サーバーが未起動なら起動
if ! curl -s -o /dev/null "http://localhost:$PORT/"; then
  ( cd "$DIR" && nohup "$PY" -m http.server "$PORT" >/tmp/aioffice.log 2>&1 & )
fi
for i in $(seq 1 40); do curl -s -o /dev/null "http://localhost:$PORT/" && break; sleep .2; done
open "http://localhost:$PORT/$(basename "$LATEST")"
```
- `.app` 化（デスクトップからダブルクリック）：**スクリプトエディタ**で
  `do shell script "bash $HOME/.aioffice/launch.sh"` を「アプリケーション」として書き出す。
- ファイル名を `ai_office_1.html, _2.html…` と採番していく運用なら、常に最新版が開く。

## 任意: 経営ダッシュボード（下半分）のデータ源
画面下部の司令室パネルは、あれば読む・無ければ「未接続/集計待ち」を出す**任意機能**。
使わないなら放置でよい（架空の数字は出さない設計）。使うなら同フォルダに：

| ファイル / パス | 用途 |
|---|---|
| `company_dashboard.json` | 目標・KENTA直接指示・売上などの表示値（オーケストレータが書く想定） |
| `AIチーム_ナレッジ/JOHN_TASKBOARD.md` | 優先度別タスク（P0/P2/P4/ブロッカー）を採配盤に表示 |
| `AIチーム_ナレッジ/ai_lab/VENTURE_RANKING.md` | 事業ランキング表（Markdownの表をパース） |
| `AIチーム_ナレッジ/ai_lab/DAILY_LAB_REPORT/YYYY-MM-DD.md` | 当日の実験ラボ日報の数値 |
| `AIチーム_ナレッジ/_更新ログ.md` | 「進化の一文」（最新行） |

- これらは**無くても本体（オフィス＋キャラ＋state連携）は動く**。ダッシュボードだけ「未接続」表示になる。
- 受け取り側が独自の指標を出したい場合は `company_dashboard.json` を自分のスキーマに合わせて用意するのが最短。

## office_server.py での起動（推奨・2026-07-19更新）
`python3 -m http.server` の代わりに同梱の `office_server.py` を使うと、静的配信に加えてAPIが生える。
Windowsは `start_office.bat`、Mac/Linuxは `start_office.sh` をダブルクリック/実行するだけでよい。

| エンドポイント | 用途 |
|---|---|
| `POST /api/command` `{"name":"ジン","text":"指示内容"}` | 社長指示。担当を2時間「作業中」にし日誌に記録（画面右上のフォームと同じ） |
| `POST /api/command` `{"name":"ジン","text":"作業内容","status":"done"}` | **完了報告**。「完了✓」表示にし日誌に完了として記録 |
| `GET /api/state` | `office_state.json` の現在値を返す（スクリプト連携用） |
| `GET /activity` | `activity.json` があればその中身、無ければ `[]`（activity bridgeの受け口。404が出なくなる） |

このリポジトリでは上記ダッシュボード用ファイル一式（采配盤・更新ログ・ランキング）も同梱済みで、
起動すれば司令室パネルが「未接続」なしで点灯する。

## 任意: 実稼働の自動検知（activity bridge）
「ローカルのAIツールが実際に動いたら該当キャラを作業中にする」高度機能のフック（`?activity_url=` で接続）。
必須ではなく、`office_state.json` を手で/スクリプトで書くだけでも全機能は成立する。
使う場合は、プロジェクト名→担当メンバー名の対応表を用意して、直近更新を検知する小さなエンドポイントを立てる。

## 配布時のチェックリスト
- [ ] `ai_office_template.html`（`TEAM=[]` またはサンプル1名）
- [ ] `office_state.json`（`{"overrides":[],"log":[]}` の空でOK）
- [ ] この `docs/office-template/` 一式
- [ ] （任意）`company_dashboard.json` と `AIチーム_ナレッジ/`
- [ ] 起動確認：`python3 -m http.server` → ブラウザで表示 → JSONに1件書いて30秒以内に反映されるか
