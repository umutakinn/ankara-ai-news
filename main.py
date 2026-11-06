import os
import json
import time
import hashlib
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

KEYWORDS = [
    "ankara", "trafik", "kaza", "asayiş",
    "yangın", "patlama", "deprem", "afet",
    "polis", "jandarma", "valilik",
    "yol kapandı", "yol kapanması",
    "su kesintisi", "elektrik kesintisi",
    "patlatma", "tahliye"
]

QUERIES = [
    "Ankara son dakika asayiş",
    "Ankara trafik kazası yol kapanması",
    "Ankara yangın patlama afet",
    "Ankara Valiliği Emniyet duyuru",
    "Ankara belediye ulaşım duyuru"
]

STATE_FILE = "state.json"


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        },
        timeout=20
    )
    response.raise_for_status()


def main():
    state = load_state()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=3)

    news = []

    for query in QUERIES:
        url = (
            "https://news.google.com/rss/search?q="
            + quote(query + " when:3h")
            + "&hl=tr&gl=TR&ceid=TR:tr"
        )

        try:
            feed = feedparser.parse(url)

            for entry in feed.entries:
                title = entry.get("title", "")
                link = entry.get("link", "")
                summary = entry.get("summary", "")

                if not title or not link:
                    continue

                text = (title + " " + summary).lower()

                if not any(k in text for k in KEYWORDS):
                    continue

                published = entry.get("published")

                if not published:
                    continue

                try:
                    date = parsedate_to_datetime(published)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    date = date.astimezone(timezone.utc)
                except Exception:
                    continue

                if date < cutoff:
                    continue

                news_id = hashlib.sha256(
                    link.encode("utf-8")
                ).hexdigest()

                if news_id in state:
                    continue

                news.append({
                    "id": news_id,
                    "title": title,
                    "link": link,
                    "date": date
                })

        except Exception as error:
            print("Kaynak hatası:", error)

    news.sort(key=lambda x: x["date"])

    for item in news[:20]:
        message = (
            "🚨 ANKARA GÜNDEM\n\n"
            f"📰 {item['title']}\n\n"
            f"🕒 {item['date'].astimezone().strftime('%H:%M')}\n"
            "📍 Ankara\n\n"
            "🔗 Kaynak:\n"
            f"{item['link']}\n\n"
            "⚠️ Haber henüz bağımsız olarak doğrulanmamış olabilir."
        )

        try:
            send_telegram(message)
            state[item["id"]] = int(time.time())
            time.sleep(1)
            print("Gönderildi:", item["title"])
        except Exception as error:
            print("Gönderim hatası:", error)

    # Son 7 günlük kayıtları sakla
    limit = int(time.time()) - 7 * 86400
    state = {
        key: value for key, value in state.items()
        if value > limit
    }

    save_state(state)


if __name__ == "__main__":
    main()
