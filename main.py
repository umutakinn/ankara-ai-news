import os
import json
import time
import hashlib
import re
import unicodedata
import difflib
import requests
import feedparser
try:
    import trafilatura
except ImportError:
    trafilatura = None
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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
ANKARA_QUERIES = [
    '"Ankara" asayiş OR operasyon OR gözaltı OR güvenlik',
    '"Ankara" trafik kazası OR yol kapandı OR ulaşım',
    '"Ankara" yangın OR patlama OR deprem OR afet',
    'site:ankara.gov.tr duyuru OR açıklama',
    'site:ankara.pol.tr trafik OR asayiş OR duyuru',
    'site:ankara.bel.tr yol OR ulaşım OR duyuru',
    'site:afad.gov.tr Ankara açıklama OR uyarı'
]
# Türkiye genelinde yalnızca yüksek etkili olayları arar.
NATIONAL_QUERIES = [
    '"fabrikada patlama" OR "iş yerinde patlama" OR "demir çelik fabrikası"',
    '"maden kazası" OR madende göçük OR "işçi hayatını kaybetti"',
    '"çok sayıda kişi hayatını kaybetti" OR "can kaybı" OR "ölü sayısı"',
    '"orman yangını" OR "büyük yangın" OR "sel felaketi" OR heyelan',
    '"tren kazası" OR "uçak kazası" OR "otobüs kazası"'
]
NATIONAL_MAJOR_TERMS = [
    "patlama", "fabrika", "demir celik", "maden", "gocuk", "deprem",
    "orman yangini", "sel felaketi", "heyelan", "tren kazasi",
    "ucak kazasi", "otobus kazasi", "cok sayida olu", "can kaybi",
    "hayatini kaybetti", "hayatlarini kaybetti", "olu sayisi",
    "isciler hayatini kaybetti", "is kazasi"
]
NATIONAL_SEVERITY_TERMS = [
    "olu", "hayatini kaybetti", "hayatlarini kaybetti", "can kaybi",
    "yarali", "cok sayida", "fabrika", "maden", "gocuk", "patlama",
    "orman yangini", "sel felaketi", "tren kazasi", "ucak kazasi"
]
STATE_FILE = "state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AnkaraNewsMonitor/1.2)"}
STOPWORDS = {
    "ankara", "icin", "hangi", "neler", "son", "dakika", "gundem",
    "aciklama", "haber", "haberleri", "gelisme", "gelismesi", "bugun",
    "yeni", "ile", "ve", "bir", "de", "da", "mi", "mu", "ne"
}


def normalize(text):
    text = unescape(re.sub(r"<[^>]+>", " ", text or ""))
    # Türkçedeki noktasız ı, ASCII temizliğinde kaybolmasın.
    text = text.translate(str.maketrans({"ı": "i", "İ": "i"}))
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
    # Her haber için kısa ve benzersiz bir onay kimliği oluştur.
    approval_id = hashlib.sha256(message.encode("utf-8")).hexdigest()[:20]
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Instagram'da Paylaş", "callback_data": f"approve:{approval_id}"},
            {"text": "❌ Reddet", "callback_data": f"reject:{approval_id}"}
        ]]
    }
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
            "reply_markup": keyboard
        },
        timeout=20
    )
    response.raise_for_status()


def send_telegram_notice(message):
    """Ayrıntısı alınamayan haber için yalnızca bilgilendirme gönderir; onay butonu içermez."""
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        },
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
    cleaned = title.strip()
    if source_name:
        cleaned = re.sub(
            r"\s+[–—-]\s+" + re.escape(source_name.strip()) + r"\s*$",
            "", cleaned, flags=re.IGNORECASE
        )
    # ANKA gibi ajansların "(ANKARA)" veya "- ANKARA" mahreci olay yeri
    # değildir. Yalnızca başlığın en sonundaki dateline kaldırılır.
    cleaned = re.sub(r"\s*[–—-]\s*ANKARA\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*\(\s*ANKARA\s*\)\s*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" -–—")


def is_national_major_event(title, summary, source_name):
    headline = clean_headline(title, source_name)
    text = normalize(headline + " " + summary)
    has_major_event = any(normalize(term) in text for term in NATIONAL_MAJOR_TERMS)
    has_severity = any(normalize(term) in text for term in NATIONAL_SEVERITY_TERMS)
    # Sıradan şehir haberlerini değil, ülke çapında önem taşıyan ağır olayları al.
    return has_major_event and has_severity


def detect_national_location(title, summary, source_name):
    text = normalize(clean_headline(title, source_name) + " " + summary)
    # İlçe adı başlık/özet içinde geçiyorsa bilinen ilini göster.
    district_to_city = {
        "aliaga": "İzmir / Aliağa", "bergama": "İzmir / Bergama",
        "iskenderun": "Hatay / İskenderun", "dilovasi": "Kocaeli / Dilovası",
        "gebze": "Kocaeli / Gebze", "somali": "Manisa / Soma",
        "ermenek": "Karaman / Ermenek", "kozlu": "Zonguldak / Kozlu"
    }
    for district, label in district_to_city.items():
        if has_phrase(text, district):
            return label
    cities = {
        "izmir": "İzmir", "istanbul": "İstanbul", "mersin": "Mersin",
        "hatay": "Hatay", "kocaeli": "Kocaeli", "manisa": "Manisa",
        "zonguldak": "Zonguldak", "kahramanmaras": "Kahramanmaraş",
        "adana": "Adana", "antalya": "Antalya", "bursa": "Bursa",
        "gaziantep": "Gaziantep", "sanliurfa": "Şanlıurfa",
        "diyarbakir": "Diyarbakır", "erzincan": "Erzincan",
        "canakkale": "Çanakkale", "bolu": "Bolu", "ankara": "Ankara"
    }
    for city, label in cities.items():
        if has_phrase(text, city):
            return label
    return "Türkiye (olay yeri teyit edilmeli)"


def is_really_about_ankara(title, summary, source_name):
    headline = clean_headline(title, source_name)
    h = normalize(headline)
    body = normalize(clean_summary(summary, max_chars=1800))
    combined = (h + " " + body).strip()

    directional = [
        "ankara istikametinde", "ankara yonunde", "ankara ya dogru",
        "ankara istikametine", "ankara yonune"
    ]
    if any(has_phrase(headline, phrase) for phrase in directional):
        return False

    # Başlıkta başka bir il açıkça olay yeri olarak verilmişse Ankara akışına alma.
    title_has_ankara = any(has_phrase(headline, place) for place in ANKARA_PLACES)
    title_has_other_city = any(has_phrase(headline, city) for city in OTHER_CITIES)
    if title_has_other_city and not title_has_ankara:
        return False

    # Ankara/ilçe başlıkta geçiyorsa doğrudan yerel haber kabul et.
    if title_has_ankara:
        location_confirmed = True
    else:
        # Başlıkta Ankara yazmayan haberlerde özet içindeki açık Ankara bağlantısını ara.
        strong_ankara_signals = [
            "ankara merkezli", "ankarada", "ankaradan",
            "ankara il emniyet", "ankara cumhuriyet bassavciligi",
            "ankara cumhuriyet bassavciliginin", "ankara valiligi",
            "ankara buyuksehir", "ankara polisi", "ankara jandarma",
            "ankara savciligi", "ankara ilce emniyet"
        ]
        location_confirmed = any(normalize(signal) in combined for signal in strong_ankara_signals)

    if not location_confirmed:
        return False

    # Başlıkta Ankara veya Ankara ilçesi açıkça geçiyorsa, olay türü
    # listede olmasa bile yerel haber olarak kabul et. Böylece erişim engeli,
    # okul, belediye ve benzeri haberler yanlışlıkla elenmez.
    if title_has_ankara:
        return True

    event_terms = [
        "kaza", "trafik", "yol", "kapali", "kapandi", "yangin", "patlama",
        "gozalti", "operasyon", "polis", "emniyet", "valilik", "belediye",
        "deprem", "afet", "duyuru", "uyari", "denetim", "olay", "yarali",
        "hayatini kaybetti", "toren", "ulasim", "su kesintisi", "elektrik",
        "sorusturma", "supheli", "mahrem yapilanma", "erisim engeli",
        "siteye erisim", "internet sitesi", "egitim", "okul", "hastane"
    ]
    return any(normalize(term) in combined for term in event_terms)

def clean_summary(summary, max_chars=480):
    # RSS özetlerindeki HTML ve gereksiz boşlukları temizle.
    text = unescape(re.sub(r"<[^>]+>", " ", summary or ""))
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*[-–—|]\s*(Google Haberler|Google News)\s*$", "", text, flags=re.I)
    if len(text) > max_chars:
        shortened = text[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:-")
        sentence_end = max(shortened.rfind("."), shortened.rfind("!"), shortened.rfind("?"))
        text = shortened[:sentence_end + 1] if sentence_end > max_chars * 0.55 else shortened + "…"
    return text



class ArticleTextParser(HTMLParser):
    """Haber sayfalarından paragraf, meta ve yapılandırılmış haber metni toplar."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paragraphs = []
        self.meta_descriptions = []
        self.jsonld = []
        self._in_p = False
        self._parts = []
        self._in_jsonld = False
        self._script_parts = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key in ("og:description", "description", "twitter:description", "article:description"):
                value = (attrs.get("content") or "").strip()
                if value:
                    self.meta_descriptions.append(value)
        elif tag.lower() == "script" and "ld+json" in (attrs.get("type") or "").lower():
            self._in_jsonld = True
            self._script_parts = []
        elif tag.lower() == "p":
            self._in_p = True
            self._parts = []

    def handle_data(self, data):
        if self._in_p and data.strip():
            self._parts.append(data.strip())
        if self._in_jsonld:
            self._script_parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "p" and self._in_p:
            value = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
            if len(value) >= 45:
                self.paragraphs.append(value)
            self._in_p = False
            self._parts = []
        elif tag.lower() == "script" and self._in_jsonld:
            value = "".join(self._script_parts).strip()
            if value:
                self.jsonld.append(value)
            self._in_jsonld = False
            self._script_parts = []

def is_useful_detail(text, headline=""):
    text = clean_summary(text, max_chars=1200)
    if len(text) < 45:
        return False

    ntext = normalize(text)
    nhead = normalize(headline)

    # Google News RSS bazen özeti "başlık + yayın adı" olarak döndürüyor.
    # Başlık tekrarını çıkar; geriye yalnızca yayın adı/kısa ibare kalıyorsa reddet.
    if nhead and ntext.startswith(nhead):
        remainder = ntext[len(nhead):].strip(" :.-")
        words = remainder.split()
        if len(remainder) < 45 or len(words) < 8:
            return False
        ntext = remainder

    # Başlığın küçük değişikliklerle yeniden yazıldığı RSS metinlerini de ele.
    if nhead and difflib.SequenceMatcher(None, ntext, nhead).ratio() >= 0.82:
        return False

    junk = ("google haberler", "google news", "haberin devamini oku", "tum haberleri gor")
    return not any(x in ntext for x in junk)


def fetch_article_detail(url, headline):
    """Kaynak sayfasından gerçek haber paragrafı veya meta açıklaması alır."""
    if not url:
        return ""
    try:
        # Google News RSS bağlantısını gerçek yayıncı adresine çöz.
        resolved_url = resolve_direct_link(url)
        response = requests.get(
            resolved_url, headers=HEADERS, allow_redirects=True, timeout=20
        )
        if response.status_code != 200:
            return ""
        content_type = response.headers.get("Content-Type", "").lower()
        if "html" not in content_type and "<html" not in response.text[:1000].lower():
            return ""
        # Önce haber odaklı çıkarıcıyı dene; sayfa HTML'i karmaşık olsa da
        # articleBody metnini bulabilir.
        if trafilatura is not None:
            extracted = trafilatura.extract(
                response.text, include_comments=False, include_tables=False,
                favor_precision=True
            )
            cleaned_extracted = clean_summary(extracted or "", max_chars=1600)
            if is_useful_detail(cleaned_extracted, headline):
                return cleaned_extracted

        parser = ArticleTextParser()
        parser.feed(response.text[:1500000])
        paragraphs = []
        for paragraph in parser.paragraphs:
            cleaned = clean_summary(paragraph, max_chars=1200)
            if is_useful_detail(cleaned, headline) and cleaned not in paragraphs:
                paragraphs.append(cleaned)
        # İlk birkaç haber paragrafı, sayfa menüsü/alt bilgi yerine kullanılır.
        if paragraphs:
            # Birkaç anlamlı paragrafı birlikte aktararak haberin gelişme
            # ayrıntılarını da koru.
            return clean_summary(" ".join(paragraphs[:5]), max_chars=1600)
        # Birçok haber sitesi gövde metnini JSON-LD articleBody alanında sunar.
        structured = []
        def collect_structured(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() in ("articlebody", "description") and isinstance(item, str):
                        structured.append(item)
                    else:
                        collect_structured(item)
            elif isinstance(value, list):
                for item in value:
                    collect_structured(item)
        for raw in parser.jsonld:
            try:
                collect_structured(json.loads(raw))
            except (ValueError, TypeError):
                continue
        for candidate in structured + parser.meta_descriptions:
            cleaned = clean_summary(candidate, max_chars=1600)
            if is_useful_detail(cleaned, headline):
                return cleaned
    except (requests.RequestException, ValueError, TypeError) as error:
        print("Haber ayrıntısı alınamadı:", error)
    return ""


def choose_real_detail(group, headline):
    """Birden fazla kaynaktan başlığı tekrarlamayan, daha kapsamlı haber özeti kurar."""
    details = []
    # Birden fazla kaynağı kontrol et; RSS özeti kısa ise sayfanın içeriğini çek.
    for item in group[:4]:
        rss_detail = clean_summary(item.get("summary", ""), max_chars=1200)
        if is_useful_detail(rss_detail, headline):
            details.append(rss_detail)
        article_detail = fetch_article_detail(item.get("link", ""), headline)
        if is_useful_detail(article_detail, headline):
            details.append(article_detail)

    # Aynı ajans metninin kopyalarını çıkar, farklı ayrıntıları koru.
    unique = []
    for detail in sorted(details, key=len, reverse=True):
        if not any(similar(detail, old) for old in unique):
            unique.append(detail)

    if not unique:
        return ""

    # En fazla üç farklı metni birleştir; Telegram mesajı okunabilir kalsın.
    return clean_summary(" ".join(unique[:3]), max_chars=1800)

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
    return len(ta & tb) / len(ta | tb) >= 0.48


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
    detail_notices = state.setdefault("detail_notices", {})
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=3)
    candidates = []

    all_queries = [(q, "ANKARA") for q in ANKARA_QUERIES]
    all_queries += [(q, "TURKIYE") for q in NATIONAL_QUERIES]

    for query, feed_type in all_queries:
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
                if feed_type == "ANKARA":
                    # Ankara sorgularından gelen haberlerde artık ayrıca konum
                    # filtresi uygulama. Başlıkta il/ilçe yoksa konumu doğrulanmadı yaz.
                    category = "GÜNDEM"
                    location = detect_location(raw_title, source_name)
                    if location == "Ankara (ilçe belirtilmemiş)":
                        location = "Konum başlıkta belirtilmemiş"
                else:
                    if not is_national_major_event(raw_title, summary, source_name):
                        print("Türkiye geneli için yeterince büyük olay değil, elendi:", raw_title)
                        continue
                    category = "TÜRKİYE GÜNDEMİ"
                    location = detect_national_location(raw_title, summary, source_name)

                published = get_published(entry)
                if not published or published < cutoff:
                    continue

                direct = extract_direct_url(entry) or link
                identity = event_key(title, published)
                link_id = hashlib.sha256((identity + "|" + normalize(title)).encode("utf-8")).hexdigest()                if link_id in legacy:
                    continue
                candidates.append({
                    "id": link_id, "event_key": identity, "title": title,
                    "link": direct, "date": published, "source": source_name,
                    "domain": source_domain, "location": location, "category": category,
                    "summary": clean_summary(summary)
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
        detail = choose_real_detail(group, primary["title"])
        if not is_useful_detail(detail, primary["title"]):
            print("Gerçek haber ayrıntısı bulunamadı; yalnızca uyarı gönderilecek:", primary["title"])
            if primary["id"] not in detail_notices:
                notice = (
                    f"🟠 HABER TESPİT EDİLDİ — AYRINTI BEKLENİYOR\\n\\n"
                    f"📰 {primary['title']}\\n"
                    f"🕒 {primary['date'].astimezone(ZoneInfo('Europe/Istanbul')).strftime('%d.%m.%Y %H:%M')}\\n"
                    f"📍 {primary['location']}\\n"
                    f"📰 Kaynak: {primary['source']}\\n\\n"
                    "Kaynak sayfasından haber metni alınamadı. Yanlış bilgi üretmemek için bu bildirim Instagram paylaşım onay butonu içermez.\\n"
                    f"🔗 {primary['link']}"
                )
                try:
                    send_telegram_notice(notice)
                    detail_notices[primary["id"]] = int(time.time())
                    print("Ayrıntı bekleniyor bildirimi gönderildi:", primary["title"])
                except Exception as error:
                    print("Uyarı gönderim hatası:", error)
            continue
        detail_notices.pop(primary["id"], None)
        message = (
            f"🚨 {primary['category']}\n\n"
            f"📰 {primary['title']}\n\n"
            f"📝 HABERİN DETAYI\n{detail}\n\n"
            f"🕒 {primary['date'].astimezone(ZoneInfo('Europe/Istanbul')).strftime('%d.%m.%Y %H:%M')}\n"
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
    state["detail_notices"] = {
        k: v for k, v in detail_notices.items()
        if isinstance(v, (int, float)) and v > limit
    }
    save_state(state)


if __name__ == "__main__":
    main()