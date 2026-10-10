# 【社内指示書】AIO（AI検索最適化）実装 — エンジニア向け完全版

対象: 社内エンジニア／作成: 株式会社follow AIチーム（ジン・セオ・レイ）／2026-07-24

外注向けの簡易版（`外注依頼書_W1実装.md`）と違い、本書は**背景・全フェーズ・検証・継続運用まで**を1冊にまとめた社内向けの完全版です。上から順に実施してください。

---

## 0. 目的と現状（なぜやるのか）

**AIO = ChatGPT・Perplexity・Google AIオーバービュー・Gemini等の「AI検索」が、弊社製品を回答に引用するようにするための最適化。**

2026-07-19 実施のベースライン計測（定点20クエリ、詳細は `ベースライン計測_2026-07-19.md`）:

| クエリ種別 | 結果 |
|---|---|
| 指名系（「ALUX」等） | 公式が1位に出る（良好） |
| 指名系（「コーディングライダー」） | 公式7位（改善余地） |
| **非指名系（「教育用ドローン おすすめ」等）** | **16クエリ中15クエリで圏外**（ここが主戦場） |

非指名クエリでAIに引用されるには、①AIクローラーが読める技術基盤（W1）、②引用に値する構造化された事実情報（W2）、③効果検証の計測基盤（W3）の3点セットが必要です。

## 1. 対象サイト

| サイト | 役割 |
|---|---|
| https://alux-follow.com/ | 製品サイト（ALUX PROGRAMMING） |
| https://follow.ne.jp/ | コーポレートサイト |

## 2. 全体ロードマップ

| フェーズ | 内容 | 目安工数 |
|---|---|---|
| **W1: 技術基盤**（本書 §3） | llms.txt設置・構造化データ実装・AIクローラー許可 | 2〜4h |
| **W2: コンテンツ受け皿**（§4） | FAQ・比較・製品情報ページのAI引用対応 | 4〜8h |
| **W3: 計測基盤**（§5） | Search Console / GA4 / 定点計測の運用開始 | 2〜3h |
| **継続運用**（§6） | 月次の定点計測と改善ループ | 月1h |

---

## 3. W1: 技術基盤の実装

作業ファイルはすべて作成済みです → `aio-project/w1_deliverables/`

### 3-1. llms.txt の設置（2サイト）

1. `w1_deliverables/llms.txt_alux-follow.com` を **`llms.txt` にリネーム**し、alux-follow.com のドキュメントルート直下に設置
2. `llms.txt_follow.ne.jp` も同様に follow.ne.jp のルート直下へ
3. 確認: `https://alux-follow.com/llms.txt` / `https://follow.ne.jp/llms.txt` がブラウザでプレーンテキスト表示されること（Content-Type: `text/plain; charset=utf-8` 推奨）

### 3-2. 構造化データ（JSON-LD）の実装（alux-follow.com）

`w1_deliverables/structured_data_alux-follow.com.html` 内の6ブロックを、各ページの `</head>` 直前に**サーバー出力HTMLとして**埋め込む:

| ブロック | 貼り付け先 |
|---|---|
| ① Organization + ② WebSite | トップページ |
| ③ Product（コーディングドローン ¥37,920税込） | /product/codingdrone |
| ④ Product（コーディングライダー ¥23,760税込） | /product/codingrider |
| ⑤ FAQPage | FAQページ（存在する場合のみ） |
| ⑥ NewsArticle | お知らせ記事テンプレート（CMS対応可なら） |

follow.ne.jp は ①Organization のみトップへ。

**`[要確認]` プレースホルダの確定（重要）:**
- ロゴ画像・製品画像の実URL
- VINU（¥38,400税込）の製品ページURL — ページが無ければVINUブロックは保留
- 価格はカタログ値を設定済み。**サイト表示価格と異なる場合はサイト側に合わせる**
- ⑤FAQの質問・回答は実際のFAQページの記載と一致させる（ページに無い内容を構造化データだけに入れるのはスパム判定リスクがあるため厳禁）

### 3-3. AIクローラーの受け入れ（robots.txt）— 社内版で追加

AI検索に引用されるには、AI各社のクローラーをブロックしないことが前提です。両サイトの `robots.txt` を確認し、以下を**明示的に許可**（既にブロック行があれば削除）:

```
# AI検索クローラー(引用元として読んでもらう)
User-agent: GPTBot
Allow: /

User-agent: OAI-SearchBot
Allow: /

User-agent: ClaudeBot
Allow: /

User-agent: PerplexityBot
Allow: /

User-agent: Google-Extended
Allow: /
```

- `Sitemap:` 行があることも確認（無ければ sitemap.xml を生成して追記）
- CDN/WAF（Cloudflare等）を使っている場合、**Bot Fight Mode等がAIクローラーを弾いていないか**必ず確認。サーバーログで `GPTBot` 等のUAが200を返しているかが最終確認

### 3-4. レンダリング方式の確認（重要）

本文・構造化データが**JavaScript描画後にしか存在しない**構成だと、AIクローラーの多く（JSを実行しない）に読まれません。
- `curl -A "GPTBot" https://alux-follow.com/` の生HTMLに、製品名・価格・JSON-LDが含まれることを確認
- 含まれない場合はSSR/プリレンダリングを検討（タグマネージャ経由のJSON-LD後入れは不可）

### 3-5. W1検証チェックリスト

- [ ] 2サイトの `/llms.txt` がブラウザ表示できる
- [ ] [リッチリザルトテスト](https://search.google.com/test/rich-results)で対象4ページ**エラー0件**
- [ ] 各ページでJSON-LDが1回だけ出力（重複なし）
- [ ] `[要確認]` の最終値一覧を記録（何をどの値にしたか）
- [ ] robots.txt でAIクローラー許可・Sitemap行あり
- [ ] curl（UA偽装）で生HTMLに製品情報とJSON-LDを確認
- [ ] Search Console で対象ページのインデックス再登録

---

## 4. W2: コンテンツの受け皿（AIが引用したくなるページ）

AIは「**構造化された事実**」を引用します。以下をサイト側で整備:

1. **FAQページの拡充**: 「対象年齢は?」「プログラミング言語は?」「学校導入の実績は?」「保証・サポートは?」など、ベースラインで拾えなかった非指名クエリに対応する質問を追加。1問1答・簡潔な事実ベースで（FAQPage構造化データと本文を必ず一致させる）
2. **製品スペックの表組み化**: 価格（税込）・対象年齢・重量・飛行時間・接続方式・受賞歴（キッズデザイン賞等）を`<table>`で明記。AIは表を抽出しやすい
3. **比較コンテンツの受け皿**: AIチームが『教育用ドローン 比較』記事を制作中（セオ構成→フミ執筆）。公開先ページ（/blog or /media 配下）のテンプレートに `Article` 構造化データ・目次・更新日を用意
4. **導入事例ページ**: 学校名（許諾済みのもの）・導入台数・成果を事実ベースで。`NewsArticle`/`Article` でマークアップ
5. **著者・運営者情報**: 記事に運営者情報（株式会社follow・所在地・連絡先）へのリンクを付け、E-E-A-Tを担保

## 5. W3: 計測基盤

1. **Google Search Console**: 2サイトの所有権確認 → 週次でインデックス状況・検索クエリを確認
2. **GA4**: 導入 + 基本イベント。**UTM規約はレイ起案のルールに従う**（`utm_source=chatgpt.com` 等のAI経由流入を分離計測できる形。詳細は采配盤のレイのタスク成果物を参照）
3. **AI経由流入のセグメント**: GA4で参照元 `chatgpt.com` / `perplexity.ai` / `gemini.google.com` / `copilot.microsoft.com` の探索レポートを作成
4. **定点計測**: `定点プロンプト台帳.md` の20クエリを月1回（毎月19日目安）、ChatGPT・Perplexity・AIオーバービューで実行し、引用有無と順位を台帳に追記（AIチームのセオが週次で自動実行しているものと同じ台帳を使用）

## 6. 継続運用（月次ループ）

```
毎月19日: 定点20クエリ計測(セオの台帳) → 圏外クエリの特定
        → 該当クエリのFAQ/記事をW2の型で追加・改修 → 翌月に効果確認
```

- 構造化データはサイト改修のたびにリッチリザルトテストで再検証
- llms.txt は新製品・価格改定・受賞のたびに更新（3ヶ月に1回は棚卸し）

## 7. 完了報告（社内検収）

以下をAIチーム宛て（リポジトリ or 社長経由）に共有してください:
1. §3-5チェックリストの全項目✓とスクリーンショット
2. `[要確認]` 確定値の一覧
3. Search Console / GA4 の閲覧権限
4. 発生した課題・保留事項（VINUページ未作成、CMS制約など）

## 8. 参考ファイル（リポジトリ `dsata-drone/dsata_first`）

| ファイル | 内容 |
|---|---|
| `aio-project/w1_deliverables/llms.txt_alux-follow.com` | 設置用llms.txt（製品サイト） |
| `aio-project/w1_deliverables/llms.txt_follow.ne.jp` | 設置用llms.txt（コーポレート） |
| `aio-project/w1_deliverables/structured_data_alux-follow.com.html` | JSON-LD 6ブロック |
| `aio-project/ベースライン計測_2026-07-19.md` | 現状の計測結果（20クエリ） |
| `aio-project/定点プロンプト台帳.md` | 定点計測の台帳（月次で追記） |
| `aio-project/w1_deliverables/実装手順書.md` | W1の詳細実装手順（本書§3の補足） |
| `aio-project/README.md` | AIOプロジェクト全体の概要 |
| `ai-office-kit/AIチーム_ナレッジ/資料/ALUX製品カタログ_要点.md` | 製品事実の一次情報 |

質問・ブロッカーはリポジトリのIssue、または社長経由でAIチーム（ジン）まで。
