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

ANKARA_PLACES = [
    "ankara", "çankaya", "keçiören", "yenimahalle", "mamak",
    "etimesgut", "sincan", "altındağ", "pursaklar", "gölbaşı",
    "polatlı", "beypazarı", "kızılcahamam", "elmadag", "elmadağ",
    "akyurt", "çubuk", "kahramankazan", "şereflikoçhisar",
    "haymana", "nallıhan", "ayaş", "bala", "kalecik", "güdül", "evren"
]
OTHER_CITIES = [
    "eskişehir", "antalya", "istanbul", "izmir", "bursa", "konya",
    "adana", "mersin", "kayseri", "samsun", "trabzon", "gaziantep",
    "diyarbakır", "şanlıurfa", "muğla", "balıkesir", "denizli",
    "manisa", "sakarya", "kocaeli"
]
QUERIES = [
    '"Ankara" asayiş OR operasyon OR gözaltı OR güvenlik',
    '"Ankara" trafik kazası OR yol kapandı OR ulaşım',
    '"Ankara" yangın OR patlama OR deprem OR afet',
    'site:ankara.gov.tr duyuru OR açıklama',
    'site:ankara.pol.tr trafik OR asayiş OR duyuru',
    'site:ankara.bel.tr yol OR ulaşım OR duyuru',
    'site:afad.gov.tr Ankara açıklama OR uyarı'
]
STATE_FILE = "state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AnkaraNewsMonitor/1.1)"}
STOPWORDS = {
    "ankara", "icin", "icin", "hangi", "neler", "son", "dakika", "gundem",
    "aciklama", "haber", "haberleri", "gelisme", "gelismesi", "bugun",
    "yeni", "ile", "ve", "bir", "de", "da", "mi", "mu", "ne"
}


def normalize(text):
    text = unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def has_phrase(text, phrase):
    return f" {normalize(phrase)} " in f" {normalize(text)} "


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and "sent" in raw:
            return raw
        return {"sent": {}, "legacy": raw if isinstance(raw, dict) else {}}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"sent": {}, "legacy": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(message):
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
        timeout=20
    )
    response.raise_for_status()


def get_source(entry):
    source = entry.get("source", {})
    href = source.get("href", "") if hasattr(source, "get") else ""
    name = source.get("title", "") if hasattr(source, "get") else ""
    domain = urlparse(href).netloc.lower().replace("www.", "") if href else ""
    return name or domain or "Kaynak", domain


def clean_headline(title, source_name):
    # Google News başlıklarının sonundaki yayın adı, olay konumu değildir.
    cleaned = title.strip()
    if source_name:
        cleaned = re.sub(
            r"\s+[–—-]\s+" + re.escape(source_name.strip()) + r"\s*$",
            "", cleaned, flags=re.IGNORECASE
        )
    return cleaned.strip(" -–—")


def is_really_about_ankara(title, summary, source_name):
    headline = clean_headline(title, source_name)
    h = normalize(headline)

    # Ankara yönü/istikameti, olayın Ankara'da olduğu anlamına gelmez.
    directional = [
        "ankara istikametinde", "ankara yonunde", "ankara ya dogru",
        "ankara istikametine", "ankara yonune"
    ]
    if any(has_phrase(headline, phrase) for phrase in directional):
        return False

    # Başlıkta gerçek olay yeri olarak Ankara veya Ankara ilçesi aranır.
    if not any(has_phrase(headline, place) for place in ANKARA_PLACES):
        return False

    # Başlıkta başka şehir de olay yeri olarak geçiyorsa, Ankara açıkça
    # olayın yeri değilse ele. Yayın adı clean_headline ile çıkarılmıştır.
    other_city = any(has_phrase(headline, city) for city in OTHER_CITIES)
    explicit_ankara = has_phrase(headline, "ankara")
    if other_city and not explicit_ankara:
        return False

    # Sadece Ankara etiketi taşıyan ulusal/siyasi haberleri elemek için
    # başlıkta Ankara konumuna ek olarak yerel olay/duyuru bağlamı aranır.
    event_terms = [
        "kaza", "trafik", "yol", "kapali", "kapandi", "yangin", "patlama",
        "gozalti", "operasyon", "polis", "emniyet", "valilik", "belediye",
        "deprem", "afet", "duyuru", "uyari", "denetim", "olay", "yarali",
        "hayatini kaybetti", "toren", "ulasim", "su kesintisi", "elektrik"
    ]
    if not any(term in h for term in map(normalize, event_terms)):
        return False
    return True


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


def extract_direct_url(entry):
    # Google News RSS özetinde yayıncının gerçek makale bağlantısı varsa al.
    summary = entry.get("summary", "") or ""
    urls = re.findall(r'''href=["'](https?://[^"' ]+)["']''', summary, flags=re.I)
    for candidate in urls:
        host = urlparse(candidate).netloc.lower()
        if host and "google.com" not in host and "googleusercontent.com" not in host:
            return candidate
    return entry.get("link", "")


def resolve_direct_link(url):
    try:
        response = requests.get(url, headers=HEADERS, allow_redirects=True, timeout=12, stream=True)
        final_url = response.url
        response.close()
        if final_url and "news.google.com" not in urlparse(final_url).netloc:
            return final_url
    except requests.RequestException:
        pass
    return url


def event_key(title, published):
    h = normalize(title)
    day = published.strftime("%Y-%m-%d")
    # 10 Kasım töreniyle ilgili farklı başlıklı yol kapatma haberlerini
    # aynı olay olarak ele al.
    if "10 kasim" in h and any(x in h for x in ("yol", "trafik", "kapali", "ulasim")):
        return f"{day}:10-kasim-ankara-trafik"
    tokens = {x for x in h.split() if len(x) > 3 and x not in STOPWORDS}
    return day + ":" + " ".join(sorted(tokens))


def similar(a, b):
    aa, bb = normalize(a), normalize(b)
    if difflib.SequenceMatcher(None, aa, bb).ratio() >= 0.78:
        return True
    ta = {x for x in aa.split() if len(x) > 3 and x not in STOPWORDS}
    tb = {x for x in bb.split() if len(x) > 3 and x not in STOPWORDS}
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / len(ta | tb)
    return overlap >= 0.48


def detect_location(title, source_name):
    headline = normalize(clean_headline(title, source_name))
    for place in ANKARA_PLACES:
        if has_phrase(headline, place):
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
                raw_title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = entry.get("summary", "")
                if not raw_title or not link:
                    continue
                source_name, source_domain = get_source(entry)
                title = clean_headline(raw_title, source_name)
                if not is_really_about_ankara(raw_title, summary, source_name):
                    print("Ankara'da gerçekleştiği doğrulanamadı, elendi:", raw_title)
                    continue
                published = get_published(entry)
                if not published or published < cutoff:
                    continue

                direct = extract_direct_url(entry) or link
                identity = event_key(title, published)
                link_id = hashlib.sha256((identity + "|" + normalize(title)).encode("utf-8")).hexdigest()
                if link_id in legacy:
                    continue
                candidates.append({
                    "id": link_id, "event_key": identity, "title": title,
                    "link": direct, "date": published, "source": source_name,
                    "domain": source_domain, "location": detect_location(title, source_name)
                })
        except Exception as error:
            print("Kaynak hatası:", error)

    groups = []
    for item in sorted(candidates, key=lambda x: x["date"], reverse=True):
        match = next((g for g in groups if
                      item["event_key"] == g[0]["event_key"]
                      or similar(item["title"], g[0]["title"])), None)
        if match is None:
            groups.append([item])
        elif all(x["domain"] != item["domain"] for x in match):
            match.append(item)

    for group in groups:
        primary = group[0]
        title_key = normalize(primary["title"])
        already_sent = any(
            record.get("event_key") == primary["event_key"]
            or record.get("title_key") == title_key
            or similar(record.get("title", ""), primary["title"])
            for record in sent.values() if isinstance(record, dict)
        )
        if already_sent:
            continue

        domains = {x["domain"] for x in group if x["domain"]}
        corroborated = len(domains) >= 2
        links = []
        for item in group[:3]:
            direct = resolve_direct_link(item["link"])
            links.append((item["source"], direct))

        confirmation = (
            "✅ İki farklı yayın kuruluşunda yer aldı; resmî teyit ayrıca kontrol edilmeli"
            if corroborated else
            "⚠️ Tek kaynak; bağımsız teyit henüz bulunamadı"
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
                "title": primary["title"], "title_key": title_key,
                "event_key": primary["event_key"],
                "date": primary["date"].isoformat(),
                "sent_at": int(time.time()), "links": [u for _, u in links]
            }
            print("Gönderildi:", primary["title"])
            time.sleep(1)
        except Exception as error:
            print("Gönderim hatası:", error)

    limit = int(time.time()) - 7 * 86400
    state["sent"] = {
        k: v for k, v in sent.items()
        if isinstance(v, dict) and v.get("sent_at", 0) > limit
    }
    state["legacy"] = {
        k: v for k, v in legacy.items()
        if isinstance(v, (int, float)) and v > limit
    }
    save_state(state)


if __name__ == "__main__":
    main()
