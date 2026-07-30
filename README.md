# note トレンド監視 → Chatwork通知

RSSフィードを定期的に巡回し、Claude APIで「note記事化する価値」をスコアリングして、
一定スコアを超えた記事だけChatworkに通知します。サーバー不要、GitHub Actionsで完結します。

## 仕組み

```
GitHub Actions（3時間おきに自動実行）
  ├─ config/sources.yaml のRSSフィードを巡回
  ├─ data/seen_articles.json と照合して未処理記事のみ抽出
  ├─ Claude API に一括で渡し、0〜100点でスコアリング
  ├─ 閾値(デフォルト75点)以上の記事をChatworkに通知
  └─ data/seen_articles.json を更新してリポジトリにコミット
```

## セットアップ手順

### 1. このフォルダをGitHubリポジトリにする

```bash
cd note-trend-monitor
git init
git add .
git commit -m "init: note trend monitor"
git branch -M main
git remote add origin https://github.com/<あなたのユーザー名>/note-trend-monitor.git
git push -u origin main
```

privateリポジトリ推奨です（APIキーはSecretsに入れるので漏れませんが、念のため）。

### 2. Chatwork APIトークンを取得

Chatwork の「サービス連携」→「API」からAPIトークンを発行してください。
通知したいルームのIDは、そのルームを開いたときのURL末尾の数字です
（例: `https://www.chatwork.com/#!rid123456789` なら `123456789`）。

### 3. Anthropic APIキーを取得

Anthropic Console（https://console.anthropic.com/）でAPIキーを発行してください。

### 4. GitHubリポジトリにSecretsを登録

リポジトリの `Settings → Secrets and variables → Actions → New repository secret` から、以下3つを登録:

| Secret名 | 値 |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropicで発行したAPIキー |
| `CHATWORK_API_TOKEN` | Chatworkで発行したAPIトークン |
| `CHATWORK_ROOM_ID` | 通知先ルームのID |

### 5. 動作確認

`Actions` タブ → `note trend monitor` → `Run workflow` で手動実行できます。
初回はログを見て、RSS取得やスコアリングが正しく動くか確認してください。

以降は `.github/workflows/trend-monitor.yml` の `cron` 設定に従い、3時間おきに自動実行されます。

## カスタマイズ

### 監視するRSSフィードを増やす・減らす

`config/sources.yaml` を編集してください。`enabled: false` にすると一時的に無効化できます。

Google News RSSはキーワードを変えるだけで簡単に追加できます:

```
https://news.google.com/rss/search?q=<URLエンコードしたキーワード>&hl=ja&gl=JP&ceid=JP:ja
```

日本語テックメディア（ITmedia、Publickeyなど）の個別フィードはURLが変更されることがあるため、
追加後に一度手動実行して記事が取得できているかログで確認することをおすすめします。

### 通知の閾値・頻度を変える

`.github/workflows/trend-monitor.yml` の以下を編集:

- `cron`: 実行頻度（現在は3時間おき）
- `SCORE_THRESHOLD`: 通知する最低スコア（デフォルト75点）。通知が多すぎる/少なすぎる場合に調整
- `LOOKBACK_HOURS`: 何時間前までの記事を「新着」として扱うか（デフォルト6時間。cronの間隔より少し長めに取ってあります）

### スコアリングの基準を変える

`monitor.py` 内の `SCORE_SYSTEM_PROMPT` を編集してください。
現在は「AI対決系」「エンジニア回顧録」「経験格差系」という得意な記事形式を軸に評価する設計になっています。
新しい得意フォーマットが増えたら、ここに追記すると精度が上がります。

## 通知イメージ

Chatworkには以下のような形式で通知されます:

```
🔥 noteトレンド速報
検知件数: 2件 (2026-07-30 15:00)

■ score 88 | Google News: Claude
[記事タイトル]
理由: [一文の理由]
タイトル案: [案1] / [案2]
[URL]

■ score 79 | Hacker News: AI
...
```

## 運用上の注意

- Google News RSSは検索結果ベースのため、まれに関連度の低い記事が混ざることがあります。
  スコアリングである程度フィルタされますが、`SCORE_THRESHOLD` で調整してください。
- `data/seen_articles.json` は既読管理用のファイルです。手動で消すと、既存記事が全て「新着」扱いになり、
  大量の記事がスコアリング対象になる（＝Anthropic APIのコストが増える）ので基本的に触らないでください。
- Claude APIの呼び出しコストは1回の実行あたり記事数×数百トークン程度です。3時間おき運用であれば、
  月あたりのコストは小さく収まるはずですが、心配な場合は `MAX_ARTICLES_PER_RUN`（monitor.py内）で
  1回の実行で処理する記事数の上限を絞れます。
