import os
import json
import time
import hashlib
import re
import unicodedata
import difflib
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse
from html import unescape

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Başlıkta açık Ankara/ilçe konumu arıyoruz; yalnızca arama sonucunda
# Ankara kelimesinin geçmesi haberin Ankara'da olduğu anlamına gelmez.
ANKARA_PLACES = [
    "ankara", "çankaya", "cankaya", "keçiören", "kecioren",
    "yenimahalle", "mamak", "etimesgut", "sincan", "altındağ",
    "altindag", "pursaklar", "gölbaşı", "golbasi", "polatlı",
    "polatli", "beypazarı", "beypazari", "kızılcahamam",
    "kizilcahamam", "elmadag", "elmadağ", "akyurt", "çubuk",
    "cubuk", "kahramankazan", "şereflikoçhisar",
    "sereflikochisar", "haymana", "nallıhan", "nallihan",
    "ayaş", "ayas", "bala", "kalecik", "güdül", "gudul", "evren"
]

OTHER_CITIES = [
    "eskişehir", "eskisehir", "antalya", "istanbul", "izmir",
    "bursa", "konya", "adana", "mersin", "kayseri", "samsun",
    "trabzon", "gaziantep", "diyarbakır", "diyarbakir",
    "şanlıurfa", "sanliurfa", "muğla", "mugla", "balıkesir",
    "balikesir", "denizli", "manisa", "sakarya", "kocaeli"
]

QUERIES = [
    '"Ankara" asayiş OR operasyon OR gözaltı OR güvenlik',
    '"Ankara" trafik kazası OR yol kapandı OR ulaşım',
    '"Ankara" yangın OR patlama OR deprem OR afet',
    'site:ankara.gov.tr Ankara duyuru OR açıklama',
    'site:ankara.pol.tr Ankara trafik OR asayiş OR duyuru',
    'site:ankara.bel.tr Ankara yol OR ulaşım OR duyuru',
    'site:afad.gov.tr Ankara açıklama OR uyarı'
]

STATE_FILE = "state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AnkaraNewsMonitor/1.0)"}


def normalize(text):
    text = unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Önceki sürümün link-hash sözlüğüyle geriye dönük uyum.
        if isinstance(raw, dict) and "sent" in raw:
            return raw
        return {"sent": {}, "legacy": raw if isinstance(raw, dict) else {}}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"sent": {}, "legacy": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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


def is_really_about_ankara(title, summary):
    title_n = normalize(title)
    full_n = normalize(title + " " + summary)
    has_ankara_place = any(normalize(place) in title_n for place in ANKARA_PLACES)
    # "Başkent" tek başına yeterli değil; metinde Ankara da bulunmalı.
    if not has_ankara_place:
        return False

    # Başlık başka bir şehri açıkça olay yeri olarak gösteriyorsa ele.
    other_city_in_title = any(normalize(city) in title_n for city in OTHER_CITIES)
    ankara_in_title = "ankara" in title_n or any(
        normalize(place) in title_n for place in ANKARA_PLACES[1:]
    )
    if other_city_in_title and not ankara_in_title:
        return False

    return bool(full_n)


def get_published(entry):
    published = entry.get("published") or entry.get("updated")
    if not published:
        return None
    try:
        dt = parsedate_to_datetime(published)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def get_source(entry):
    source = entry.get("source", {})
    href = source.get("href", "") if hasattr(source, "get") else ""
    name = source.get("title", "") if hasattr(source, "get") else ""
    domain = urlparse(href).netloc.lower().replace("www.", "") if href else ""
    return name or domain or "Kaynak belirtilmemiş", domain


def resolve_direct_link(url):
    # Google News yönlendirmesini mümkünse yayıncının doğrudan adresine çevir.
    try:
        response = requests.get(
            url, headers=HEADERS, allow_redirects=True,
            timeout=10, stream=True
        )
        final_url = response.url
        response.close()
        if final_url and "news.google.com" not in urlparse(final_url).netloc:
            return final_url
    except requests.RequestException:
        pass
    return url


def similar(a, b):
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio() >= 0.84


def detect_location(title):
    title_n = normalize(title)
    for place in ANKARA_PLACES:
        if normalize(place) in title_n:
            return "Ankara" if normalize(place) == "ankara" else place.title()
    return "Ankara (ilçe belirtilmemiş)"


def main():
    state = load_state()
    sent = state.setdefault("sent", {})
    legacy = state.setdefault("legacy", {})
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=3)
    candidates = []

    for query in QUERIES:
        url = (
            "https://news.google.com/rss/search?q="
            + quote("(" + query + ") when:3h")
            + "&hl=tr&gl=TR&ceid=TR:tr"
        )
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = entry.get("summary", "")
                if not title or not link:
                    continue
                if not is_really_about_ankara(title, summary):
                    print("Ankara dışı/konumu belirsiz, elendi:", title)
                    continue

                published = get_published(entry)
                if not published or published < cutoff:
                    continue

                link_id = hashlib.sha256(link.encode("utf-8")).hexdigest()
                if link_id in legacy:
                    continue

                source_name, source_domain = get_source(entry)
                candidates.append({
                    "id": link_id,
                    "title": title,
                    "link": link,
                    "date": published,
                    "source": source_name,
                    "domain": source_domain,
                    "location": detect_location(title)
                })
        except Exception as error:
            print("Kaynak hatası:", error)

    # Aynı olayın farklı yayınlardaki başlıklarını tek grupta topla.
    groups = []
    for item in sorted(candidates, key=lambda x: x["date"], reverse=True):
        match = None
        for group in groups:
            if similar(item["title"], group[0]["title"]):
                match = group
                break
        if match is None:
            groups.append([item])
        else:
            if all(x["domain"] != item["domain"] for x in match):
                match.append(item)

    for group in groups:
        primary = group[0]
        title_key = normalize(primary["title"])
        if any(
            record.get("title_key") == title_key
            or similar(record.get("title", ""), primary["title"])
            for record in sent.values()
            if isinstance(record, dict)
        ):
            continue

        domains = {x["domain"] for x in group if x["domain"]}
        corroborated = len(domains) >= 2
        links = []
        for item in group[:3]:
            direct = resolve_direct_link(item["link"])
            links.append((item["source"], direct))

        confirmation = (
            "✅ En az iki farklı yayın kuruluşunda yer aldı"
            if corroborated
            else "⚠️ Tek kaynak; bağımsız teyit henüz bulunamadı"
        )

        message = (
            "🚨 ANKARA GÜNDEM\n\n"
            f"📰 {primary['title']}\n"
            f"🕒 {primary['date'].astimezone().strftime('%d.%m.%Y %H:%M')}\n"
            f"📍 {primary['location']}\n"
            f"🔎 Doğrulama: {confirmation}\n\n"
            "🔗 Kaynak(lar):\n"
            + "\n".join(f"• {name}: {url}" for name, url in links)
        )

        try:
            send_telegram(message)
            sent[primary["id"]] = {
                "title": primary["title"],
                "title_key": title_key,
                "date": primary["date"].isoformat(),
                "sent_at": int(time.time()),
                "links": [url for _, url in links]
            }
            print("Gönderildi:", primary["title"])
            time.sleep(1)
        except Exception as error:
            print("Gönderim hatası:", error)

    # Son 7 günlük kayıtları sakla; tekrar haberleri engelle.
    limit = int(time.time()) - 7 * 86400
    state["sent"] = {
        key: value for key, value in sent.items()
        if isinstance(value, dict) and value.get("sent_at", 0) > limit
    }
    state["legacy"] = {
        key: value for key, value in legacy.items()
        if isinstance(value, (int, float)) and value > limit
    }
    save_state(state)


if __name__ == "__main__":
    main()
