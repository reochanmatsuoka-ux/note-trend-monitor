"""
note トレンド監視 → Claude採点 → Chatwork通知

処理の流れ:
1. config/sources.yaml に登録されたRSSフィードから新着記事を取得
2. data/seen_articles.json と突き合わせて未処理の記事だけ抽出
3. Claude APIに一括で渡し、「note記事化する価値」を0-100点でスコアリング
4. 閾値を超えた記事だけ Chatwork に通知（想定タイトル案つき）
5. data/seen_articles.json を更新してコミット（GitHub Actions側で実施）
"""

import os
import re
import json
import hashlib
from datetime import datetime, timedelta, timezone

import feedparser
import requests
import yaml

CONFIG_PATH = "config/sources.yaml"
SEEN_PATH = "data/seen_articles.json"

SCORE_THRESHOLD = int(os.environ.get("SCORE_THRESHOLD", "75"))
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "6"))
MAX_ARTICLES_PER_RUN = int(os.environ.get("MAX_ARTICLES_PER_RUN", "25"))
MAX_SEEN_ENTRIES = int(os.environ.get("MAX_SEEN_ENTRIES", "3000"))

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CHATWORK_API_TOKEN = os.environ["CHATWORK_API_TOKEN"]
CHATWORK_ROOM_ID = os.environ["CHATWORK_ROOM_ID"]

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


# ---------- 設定・状態の読み書き ----------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen():
    if not os.path.exists(SEEN_PATH):
        return {}
    with open(SEEN_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_seen(seen):
    # 際限なく増え続けないよう、古いものから間引く
    if len(seen) > MAX_SEEN_ENTRIES:
        items = sorted(seen.items(), key=lambda kv: kv[1].get("seen_at", ""))
        seen = dict(items[-MAX_SEEN_ENTRIES:])
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def article_hash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def strip_html(text):
    return re.sub(r"<[^<]+?>", "", text or "").strip()


# ---------- 記事収集 ----------

def fetch_entries(source):
    feed = feedparser.parse(source["url"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    entries = []

    for e in feed.entries:
        published = None
        for key in ("published_parsed", "updated_parsed"):
            val = getattr(e, key, None)
            if val:
                published = datetime(*val[:6], tzinfo=timezone.utc)
                break

        # 日付が取れない記事は除外せず「新着扱い」にする（フィードによっては日付なしもあるため）
        if published and published < cutoff:
            continue

        link = e.get("link", "").strip()
        if not link:
            continue

        entries.append({
            "title": strip_html(e.get("title", "")),
            "link": link,
            "summary": strip_html(e.get("summary", ""))[:400],
            "source": source["name"],
            "published": published.isoformat() if published else None,
        })

    return entries


def collect_new_articles(config, seen):
    new_articles = []
    for source in config.get("sources", []):
        if not source.get("enabled", True):
            continue
        try:
            entries = fetch_entries(source)
        except Exception as ex:
            print(f"[WARN] failed to fetch '{source['name']}': {ex}")
            continue

        for entry in entries:
            h = article_hash(entry["link"])
            if h in seen:
                continue
            seen[h] = {
                "title": entry["title"],
                "seen_at": datetime.now(timezone.utc).isoformat(),
            }
            new_articles.append(entry)

    print(f"[INFO] collected {len(new_articles)} new articles (pre-limit)")
    return new_articles[:MAX_ARTICLES_PER_RUN]


# ---------- Claudeによるスコアリング ----------

SCORE_SYSTEM_PROMPT = """あなたは日本語テック系note記事の編集者です。
筆者(@tolove)はAI/LLM関連の記事をnoteで発信しており、特に以下の記事形式で高いパフォーマンスを出しています。

- AI対決系: 各社モデル・サービスの比較や競争構造を扱う記事
- エンジニア回顧録シリーズ: 自身の長年のキャリアを絡めた記事
- 経験格差系: 世代・立場によるスキルや知識のギャップを扱う記事

与えられたニュース記事それぞれについて、「今すぐnote記事化する価値があるか」を0〜100点でスコアリングしてください。

評価基準:
- 速報性(発表から間もない、まだ日本語であまり書かれていない)
- 上記の得意な記事形式に絡められるか
- 議論を呼びそうか、SNSで話題になりそうか
- 単発ニュースの受け売りでなく独自の切り口を作れそうか

出力は必ず以下のJSON配列のみとしてください。前置き・説明・Markdownのコードフェンスは一切不要です。
[
  {"index": 0, "score": 82, "reason": "一文での理由(日本語、60字以内)", "title_ideas": ["想定タイトル案1", "想定タイトル案2"]}
]
"""


def score_articles(articles):
    if not articles:
        return []

    numbered = [
        f"{i}. [{a['source']}] {a['title']}\n概要: {a['summary']}\nURL: {a['link']}"
        for i, a in enumerate(articles)
    ]
    user_content = "以下のニュース記事をスコアリングしてください:\n\n" + "\n\n".join(numbered)

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 2000,
            "system": SCORE_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()
    text = re.sub(r"^```json|```$", "", text).strip()

    try:
        scored = json.loads(text)
    except json.JSONDecodeError:
        print("[WARN] failed to parse Claude response as JSON:")
        print(text)
        return []

    results = []
    for item in scored:
        idx = item.get("index")
        if idx is None or not (0 <= idx < len(articles)):
            continue
        results.append({**articles[idx], **item})
    return results


# ---------- Chatwork通知 ----------

def build_message(hits):
    lines = ["[info][title]🔥 noteトレンド速報[/title]"]
    lines.append(f"検知件数: {len(hits)}件 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    lines.append("")

    for h in sorted(hits, key=lambda x: -x.get("score", 0)):
        lines.append(f"■ score {h.get('score')} | {h['source']}")
        lines.append(h["title"])
        if h.get("reason"):
            lines.append(f"理由: {h['reason']}")
        if h.get("title_ideas"):
            lines.append("タイトル案: " + " / ".join(h["title_ideas"]))
        lines.append(h["link"])
        lines.append("")

    lines.append("[/info]")
    return "\n".join(lines)


def post_to_chatwork(hits):
    if not hits:
        print("[INFO] no hits above threshold, skip Chatwork post")
        return

    body = build_message(hits)
    resp = requests.post(
        f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages",
        headers={"X-ChatWorkToken": CHATWORK_API_TOKEN},
        data={"body": body},
        timeout=30,
    )
    resp.raise_for_status()
    print(f"[INFO] posted {len(hits)} hits to Chatwork")


# ---------- メイン ----------

def main():
    config = load_config()
    seen = load_seen()

    new_articles = collect_new_articles(config, seen)
    print(f"[INFO] scoring {len(new_articles)} articles")

    scored = score_articles(new_articles)
    hits = [a for a in scored if a.get("score", 0) >= SCORE_THRESHOLD]
    print(f"[INFO] {len(hits)} articles scored >= {SCORE_THRESHOLD}")

    post_to_chatwork(hits)

    # 新着として収集した記事は既にseenに記録済み（スコア閾値未満でも再通知しないため）
    save_seen(seen)


if __name__ == "__main__":
    main()
