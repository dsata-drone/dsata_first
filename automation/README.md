# AIオフィス 24時間自律運転システム

人間の手動操作を待たずにオフィスが回り続けるための自動化レイヤー。
「社長の送付」のような外向きアクションの自動化(①)、定期実行ループ(②)、
エラー・判断保留時だけ人間に知らせるエマージェンシー通知(③)で構成される。

## 全体像

```
GitHub Actions cron(毎時) ──▶ orchestrator.py の1サイクル
                                  │
     ┌────────────────────────────┼──────────────────────────┐
     ▼                            ▼                          ▼
 満了整理+采配                outbox 送信                エスカレーション
 (brain.py: Claude API      (dispatch.py: メール/       (notify.py: 緊急Slack
  → ルールベースにFB)          Slack で依頼書等を送付)     → Slack → GitHub Issue)
     │
     ▼
 office_state.json を更新してコミット(オフィス画面に反映)
```

## ① ボトルネックの自動化(送信キュー)
外注依頼書の送付などは `automation/outbox/pending/` にJSONジョブを置くだけで、
次のサイクルで自動送信される。書式は `dispatch.py` の冒頭コメントと
`pending/外注依頼書_W1実装.json.example` を参照。

- メール(添付つき)と Slack に対応
- 送信成功 → `outbox/sent/` に移動(送信記録つき)。業務日誌にも自動記録
- 送信失敗 → `outbox/failed/` に移動し、人間へエスカレーション
- 送信チャネル未設定なら送らずに保留(誤送信しない)

## ①-2 Instagram自動投稿(sns_publisher.py)
`sns/queue/pending/` に投稿ジョブ(JSON)を置くと、`scheduled_at` を過ぎた最初の
サイクルで Meta Graph API 経由で自動投稿される(image/carousel/reel対応)。
- 必要Secrets: `IG_USER_ID` / `IG_ACCESS_TOKEN`(未設定の間は投稿されず保留)
- 画像・動画は**公開URL**が必要(Metaのサーバーが取得するため)
- 投稿成功は `sent/`+業務日誌に記録、失敗はエスカレーション
- 長期トークンは60日で失効するため、失効前の更新が必要(失効すると投稿失敗
  →エスカレーション通知が飛ぶので気づける)

## ①-3 X(Twitter)自動投稿(x_publisher.py)
同じ `sns/queue/pending/` に `"platform": "x"` のジョブを置くと、X API v2で自動投稿。
- 必要Secrets: `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_SECRET`
  (developers.x.com でアプリ作成→キー4点を発行。無料枠で月数百件投稿可能)
- 画像は最大4枚、**リポジトリ内のパス指定でOK**(公開URL不要 — IGとの違い)
- OAuth 1.0a署名は標準ライブラリで実装済み(追加依存なし)

## ② 常時稼働ループ(GitHub Actions)
`.github/workflows/office-autorun.yml` が毎時23分に1サイクル実行する。
加えて `outbox/pending/` や采配盤(JOHN_TASKBOARD.md)への push でも即時実行される。
手動実行は Actions タブ → office-autorun → Run workflow。

1サイクルの内容(orchestrator.py):
1. 作業時間が満了した working エントリを完了扱いに整理し、采配盤の進捗を+25%自動加算
   (100%到達=完了)。**エージェント間連携**: 依存(依存:名前)の上流が完了した瞬間の
   バトンパスと、応援(応援:名前)の自動発行を処理し、collaborations に連携を記録
   (オフィス画面に連携ライン・パケット・対話吹き出しが出る)
2. **采配**: `ANTHROPIC_API_KEY` があれば Claude(claude-opus-4-8)が采配盤と稼働状態から
   次の割り当てを判断(brain.py・構造化出力)。なければルールベース
   (采配盤のP0〜P3から未完了タスクを手空きの担当に割り当て)。
   `依存:名前` 付きタスクは依存先の完了まで采配しない
3. **稼働保証**(ensure_min_activity): 待機の日次累計が上限(4時間)に達したメンバーへ、
   「ロール別バックログ」(定常業務・自主研究)または将来タスクの先行準備を自動生成して
   4時間の作業を采配。長期待機(「9月まで待機」等)は存在しない
4. **待機理由の算出**(compute_waiting): 手空きメンバー全員の「次の一歩の条件」を
   `office_state.json` の `waiting` に書き出し、画面の状態欄に
   「待機中(〇〇待ち・あと〇〇で自動稼働)」と表示させる
5. outbox の送信処理
6. ブロッカー・送信失敗・Claudeの判断保留があればエスカレーション
7. `office_state.json` を更新してコミット → オフィス画面が動く

状態遷移の正式仕様は `ai-office-kit/状態ライフサイクル.md` を参照。

## ③ エマージェンシー通知
人間に通知が飛ぶのは次の場合**だけ**(平常時は無音):
- 采配ブレーンが「社長判断が必要」と判定した事項(escalations)
- 采配盤にブロッカーが書かれている
- outbox の送信失敗
- サイクル自体の実行失敗(ワークフローの failure ステップ)

通知経路は 緊急Slack → 通常Slack → GitHub Issue(`office-emergency` ラベル)の順に
フォールバックする。どれか1つでも設定されていれば届く。

## セットアップ
1. GitHub リポジトリ → Settings → Secrets and variables → Actions で以下を登録
   (すべて任意。設定した機能だけ有効になる):

   | Secret | 用途 |
   |---|---|
   | `ANTHROPIC_API_KEY` | Claude采配(未設定ならルールベース) |
   | `SLACK_WEBHOOK_URL` | 通常通知 |
   | `EMERGENCY_SLACK_WEBHOOK_URL` | 緊急通知(別チャンネル推奨) |
   | `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM` | メール送信(依頼書の自動送付) |

   `GITHUB_TOKEN` は Actions が自動注入するので設定不要。
2. ローカルで試す場合は `config.example.env` を `config.env` にコピーして値を入れ、
   読み込んでから実行:
   ```bash
   set -a; source automation/config.env; set +a
   python3 automation/orchestrator.py --dry-run   # まず動作確認
   python3 automation/orchestrator.py             # 本実行
   ```

## 安全設計
- **勝手に新しい送信先を作らない**: 送信されるのは人間(または明示的に承認された
  プロセス)が outbox に置いたジョブだけ。雛形(.example)は送信されない
- **社長決裁事項は実行しない**: 采配ブレーンには「予算・外部への新規送信・社長タスクは
  escalations に回す」ルールを課している(brain.py の SYSTEM 参照)
- **チャネル未設定なら何もしない**: Secrets が空のうちは送信・通知は発生せず、
  状態更新だけが動く(段階的に有効化できる)
- Secrets はワークフローの env 経由でのみ渡る。コードやリポジトリに書かないこと
