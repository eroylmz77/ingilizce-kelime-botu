"""
Sözlük mantığı: Tureng + DeepL + ElevenLabs (+ fonetik yazım için dictionaryapi.dev).
Telegram'dan bağımsızdır; bot.py bu modülü kullanır.
"""

import hashlib
import os
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    # Tureng Cloudflare arkasında; bulut sunucularından gelen sıradan istekleri engelleyebiliyor.
    # curl_cffi istekleri gerçek bir Chrome tarayıcısının bağlantı imzasıyla gönderir.
    from curl_cffi import requests as browser_requests
except ImportError:
    browser_requests = None

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "ses_onbellek"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
}
TURKISH_CHARS = set("çğıöşüÇĞİÖŞÜ")
POS_SUFFIX = re.compile(r"\s+(n|v|adj|adv|prep|conj|interj|pron|abbr|expr|phr|i|f|s|zf)\.$")
EXAMPLE_COUNT = 5


def load_env():
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


class TurengError(requests.RequestException):
    pass


def tureng_get(url):
    """Tureng'den sayfa/dosya indirir; engellenirse anlaşılır bir hata verir."""
    try:
        if browser_requests:
            resp = browser_requests.get(url, impersonate="chrome", timeout=15)
        else:
            resp = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        raise TurengError(f"bağlantı hatası ({type(e).__name__})") from e
    if resp.status_code != 200:
        blocked = "Just a moment" in resp.text[:3000]
        raise TurengError(f"HTTP {resp.status_code}" + (" — Cloudflare engeli" if blocked else ""))
    return resp


# --------------------------------------------------------------------------- #
# 1) Tureng — anlamlar, ses
# --------------------------------------------------------------------------- #
def tureng_lookup(word):
    url = f"https://tureng.com/en/turkish-english/{requests.utils.quote(word)}"
    soup = BeautifulSoup(tureng_get(url).content.decode("utf-8", "replace"), "html.parser")

    # Başlıkları ve tabloları sırayla gez; "with other terms" tabloları birebir eşleşme değildir
    tables = []
    other_terms = False
    for el in soup.find_all(["h2", "table"]):
        if el.name == "h2":
            other_terms = "other terms" in el.get_text()
        elif "searchResultsTable" in (el.get("class") or []) and not other_terms:
            tables.append(parse_tureng_table(el))

    suggestions = [li.get_text(strip=True) for li in soup.select(".suggestion-list li")]
    src = soup.select_one('audio source[src*="/en-US/"]') or soup.select_one("audio source")
    audio = src["src"] if src else None

    tables = [t for t in tables if t["rows"]]
    if not tables:
        # Gerçek bir "sonuç yok" sayfası mı, yoksa engel/doğrulama sayfası mı?
        headings = " ".join(h.get_text() for h in soup.find_all("h2"))
        if "Or try these" not in headings and "Meanings of" not in headings and not suggestions:
            title = clean(soup.title.get_text()) if soup.title else "başlıksız"
            raise TurengError(f"beklenmeyen sayfa geldi: '{title[:80]}'")
        return {"found": False, "suggestions": suggestions}

    best = max(tables, key=score_table)
    best.update(found=True, audio=audio, suggestions=suggestions)
    return best


def parse_tureng_table(table):
    head = [c.get_text(strip=True) for c in table.select("tr")[0].find_all(["th", "td"])]
    source_lang = "tr" if head.index("Turkish") < head.index("English") else "en"

    rows = []
    for tr in table.select("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 4 or "example-sentences-row" in (tr.get("class") or []):
            continue
        src_term = tds[2].get_text(" ", strip=True)
        dst_term = tds[3].get_text(" ", strip=True)
        pos = POS_SUFFIX.search(src_term) or POS_SUFFIX.search(dst_term)
        rows.append({
            "category": tds[1].get_text(" ", strip=True),
            "source": POS_SUFFIX.sub("", src_term),
            "target": POS_SUFFIX.sub("", dst_term),
            "pos": pos.group(1) if pos else "",
        })
    return {"source_lang": source_lang, "rows": rows}


def score_table(t):
    """Gündelik anlamı olan tabloyu öne al (ör. 'elma' -> 'apple', 'Elma, Iowa' değil)."""
    score = 0
    for r in t["rows"]:
        if r["category"] in ("Common Usage", "General"):
            score += 10
        elif r["category"] == "Geography":
            score -= 1
        else:
            score += 2
    return score


# --------------------------------------------------------------------------- #
# 2) Tureng — örnek cümleler
# --------------------------------------------------------------------------- #
def tureng_sentences(word):
    """Tureng cümle sayfasından (EN, TR) çiftleri. Hem Türkçe hem İngilizce kelimeyle çalışır."""
    url = f"https://tureng.com/en/turkish-english-sentences/{requests.utils.quote(word)}"
    soup = BeautifulSoup(tureng_get(url).content.decode("utf-8", "replace"), "html.parser")

    # Cümleler anlam gruplarına ayrılmış: önce "save | kurtarmak" satırı, altında cümleleri
    pairs, meaning = [], None
    for tr in soup.select("table.sentencesSearchResultsTable tbody tr"):
        cell = tr.find("td", class_="sentencesPerTermList")
        if cell is None:
            tds = tr.find_all("td")
            if len(tds) >= 3:
                en_term, tr_term = tds[1].get_text(strip=True), tds[2].get_text(strip=True)
                # Anlam, aranan kelimenin karşı dildeki karşılığıdır
                meaning = en_term if tr_term.lower() == word.lower() else tr_term
            continue
        for pair in cell.select("div.sentencePair"):
            items = pair.select("li")
            if len(items) >= 2:
                pairs.append({"en": clean(items[0].get_text()), "tr": clean(items[1].get_text()),
                              "meaning": meaning})
    return pairs


# Örnek cümle uzunluğu (kelime sayısı): çok kısa cümleler fazla basit kalıyor
MIN_WORDS, IDEAL_WORDS, MAX_WORDS = 9, 14, 25


def pick_examples(candidates, count=EXAMPLE_COUNT):
    """Her cümleyi kelimenin farklı bir anlamından seç (anlamlar sırayla dolaşılır);
    her anlamda ideal uzunluğa en yakın cümleyi al. Uygun uzunlukta cümle kalmazsa
    uzunluk şartını gevşet."""
    seen, groups = set(), {}
    for c in candidates:
        key = c["en"].lower().rstrip(".!? ")
        if key not in seen:
            seen.add(key)
            groups.setdefault(c.get("meaning"), []).append(c)

    def words(c):
        return len(c["en"].split())

    pools = [sorted(g, key=lambda c: abs(words(c) - IDEAL_WORDS)) for g in groups.values()]
    chosen = []
    for strict in (True, False):
        progress = True
        while len(chosen) < count and progress:
            progress = False
            for pool in pools:
                if len(chosen) >= count:
                    break
                pick = next((c for c in pool if c not in chosen
                             and (not strict or MIN_WORDS <= words(c) <= MAX_WORDS)), None)
                if pick:
                    chosen.append(pick)
                    progress = True
    return chosen


# Yazılım cümlesi tespiti: güçlü kelime tek başına yeter; zayıf kelimeler
# ("code", "data", "network"...) genel metinlerde de geçtiği için en az iki tane gerekir.
SOFTWARE_STRONG = re.compile(
    r"\b(software|source code|coding|coder|programmer|developer|computer|database|server|"
    r"algorithm|website|web ?page|web design|apps?|API|bugs?|debug\w*|compil\w+|browser|"
    r"operating system|user interface|cloud|Ethernet|Python|Java(Script)?|iOS|Android|Linux|"
    r"Windows|download\w*|upload\w*|login|log in|password|backup|e-?mail|hacker|cyber\w*|"
    r"big data|laptop|smartphone|wireless|Wi-?Fi|SQL|HTML|GitHub|repository|framework)s?\b",
    re.IGNORECASE,
)
SOFTWARE_WEAK = re.compile(
    r"\b(code|data|network|file|folder|program|programming|online|internet|digital|interface|"
    r"script|keyboard|device|users?|system|technology|install\w*|update\w*|version)s?\b",
    re.IGNORECASE,
)


def is_software_sentence(text):
    return bool(SOFTWARE_STRONG.search(text)) or len(SOFTWARE_WEAK.findall(text)) >= 2


def choose_examples(sentences, count=EXAMPLE_COUNT):
    """Varsa 1 yazılım cümlesi (en kısası) + kalan genel cümleler."""
    software = sorted((s for s in sentences if is_software_sentence(s["en"])),
                      key=lambda s: len(s["en"]))
    if not software:
        return pick_examples(sentences, count)
    chosen = dict(software[0], software=True)
    rest = [s for s in sentences if s["en"] != chosen["en"]]
    return [chosen] + pick_examples(rest, count - 1)


# --------------------------------------------------------------------------- #
# 3) DeepL — çeviri
# --------------------------------------------------------------------------- #
def _deepl_request(texts, target_lang, source_lang=None):
    key = os.environ.get("DEEPL_API_KEY")
    if not key or not texts:
        return None
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    body = {"text": texts, "target_lang": "EN-US" if target_lang == "en" else target_lang.upper()}
    if source_lang:
        body["source_lang"] = source_lang.upper()
    resp = requests.post(
        f"https://{host}/v2/translate",
        headers={"Authorization": f"DeepL-Auth-Key {key}"},
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["translations"]


def deepl_translate(texts, source_lang, target_lang):
    out = _deepl_request(texts, target_lang, source_lang)
    return [t["text"] for t in out] if out else None


# --------------------------------------------------------------------------- #
# 3b) Cümle çevirisi — DeepL, anahtarı yoksa Gemini/OpenAI (dili kendileri algılar)
# --------------------------------------------------------------------------- #
def is_sentence(text):
    """3 kelimeden uzunsa ya da noktalama ile bitiyorsa cümle say
    ('give up', 'take care of' gibi kalıplar sözlükte aranmaya devam eder)."""
    words = text.strip().split()
    return len(words) > 3 or (len(words) > 1 and text.strip()[-1:] in ".?!")


def translate_sentence(text):
    text = clean(text)
    result = {"text": text, "translation": None, "source_lang": None, "english": None,
              "error": None, "engine": "DeepL"}
    if not os.environ.get("DEEPL_API_KEY"):
        return translate_with_ai(result)
    try:
        # Türkçe harf varsa doğrudan İngilizceye; yoksa Türkçeye çevir, DeepL metni
        # Türkçe algılarsa İngilizceye tekrar çevir
        target = "en" if set(text) & TURKISH_CHARS else "tr"
        out = _deepl_request([text], target)[0]
        detected = out["detected_source_language"].lower()
        if detected == target:
            target = "en" if target == "tr" else "tr"
            out = _deepl_request([text], target)[0]
            detected = out["detected_source_language"].lower()
    except requests.RequestException as e:
        result["error"] = f"DeepL hatası: {e}"
        return result

    result.update(
        translation=out["text"],
        source_lang=detected,
        # Seslendirilecek taraf her zaman İngilizce olan
        english=text if detected == "en" else out["text"],
    )
    return result


def translate_with_ai(result):
    """DeepL anahtarı yoksa çeviriyi öğretmenin kullandığı model (Gemini/OpenAI) yapar."""
    import ogretmen  # sözlük modülü öğretmen olmadan da çalışabilsin diye burada

    engine = ogretmen.provider_label()
    if engine is None:
        result["error"] = ("Cümle çevirisi için GEMINI_API_KEY (ücretsiz) ya da DEEPL_API_KEY "
                           "gerekli (.env dosyasına / Render'a ekle).")
        return result
    try:
        out = ogretmen.translate(result["text"])
    except ogretmen.TeacherError as e:
        result["error"] = f"{engine} çeviri hatası: {e}"
        return result

    detected = out["source_lang"]
    result.update(
        engine=engine,
        translation=out["translation"],
        source_lang=detected,
        english=result["text"] if detected == "en" else out["translation"],
    )
    return result


# --------------------------------------------------------------------------- #
# 4) Fonetik yazım (IPA) — dictionaryapi.dev (ücretsiz, anahtarsız)
# --------------------------------------------------------------------------- #
def phonetic(english_word):
    try:
        resp = requests.get(
            f"https://api.dictionaryapi.dev/api/v2/entries/en/{requests.utils.quote(english_word)}",
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        for entry in resp.json():
            ipa = entry.get("phonetic") or next(
                (p["text"] for p in entry.get("phonetics", []) if p.get("text")), None)
            if ipa:
                return ipa
    except (requests.RequestException, ValueError):
        pass
    return None


# --------------------------------------------------------------------------- #
# 5) Ses — ElevenLabs, yoksa Tureng'in ses dosyası
# --------------------------------------------------------------------------- #
def elevenlabs_tts(text):
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return None
    voice = os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    model = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")

    # Aynı metin için tekrar kredi harcamamak adına önbellekle
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{hashlib.md5(f'{voice}|{model}|{text}'.encode()).hexdigest()}.mp3"
    if path.exists():
        return path

    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
        headers={"xi-api-key": key},
        json={"text": text, "model_id": model},
        timeout=30,
    )
    resp.raise_for_status()
    path.write_bytes(resp.content)
    return path


def download_audio(url):
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{hashlib.md5(url.encode()).hexdigest()}.mp3"
    if not path.exists():
        path.write_bytes(tureng_get(url).content)
    return path


def audio_for(text, fallback_url=None):
    """Metnin ses dosyasının yolunu döndürür (ElevenLabs > Tureng). Yoksa None."""
    path = elevenlabs_tts(text)
    if path is None and fallback_url:
        path = download_audio(fallback_url)
    return path


# --------------------------------------------------------------------------- #
# Hepsini birleştir
# --------------------------------------------------------------------------- #
def lookup(word):
    word = clean(word)
    result = {"word": word, "errors": []}
    try:
        tureng = tureng_lookup(word)
    except requests.RequestException as e:
        tureng = {"found": False, "suggestions": []}
        result["errors"].append(f"Tureng'e ulaşılamadı: {e}")

    if tureng["found"]:
        source_lang = tureng["source_lang"]
    else:
        source_lang = "tr" if set(word) & TURKISH_CHARS else "en"
    target_lang = "en" if source_lang == "tr" else "tr"

    # Anlamları sırayla, tekrarsız topla; yazılım ("Computer") anlamlarını ayrıca ayır
    meanings, seen = [], set()
    software_meanings, seen_sw = [], set()
    for r in tureng.get("rows", []):
        key = r["target"].lower()
        if key not in seen:
            seen.add(key)
            meanings.append(r)
        if r["category"] == "Computer" and key not in seen_sw:
            seen_sw.add(key)
            software_meanings.append(r)

    deepl = None
    try:
        out = deepl_translate([word], source_lang, target_lang)
        deepl = out[0] if out else None
    except requests.RequestException as e:
        result["errors"].append(f"DeepL hatası: {e}")

    # Seslendirilecek İngilizce kelime
    if source_lang == "en":
        english = word
    elif meanings:
        english = meanings[0]["target"]
    else:
        english = deepl
    english = clean(english) if english else None

    # Tureng'in sesi aranan kelimeye aittir; Türkçe aramada (ElevenLabs yoksa)
    # İngilizce karşılığın sesini Tureng'den ayrıca al
    tureng_audio = tureng.get("audio") if source_lang == "en" else None
    if source_lang == "tr" and english and not os.environ.get("ELEVENLABS_API_KEY"):
        try:
            tureng_audio = tureng_lookup(english).get("audio")
        except requests.RequestException:
            pass

    # Örnek cümleler (Tureng) — varsa biri yazılımla ilgili
    try:
        examples = choose_examples(tureng_sentences(word))
    except requests.RequestException:
        examples = []

    result.update(
        source_lang=source_lang,
        meanings=meanings,
        software_meanings=software_meanings,
        deepl=deepl,
        english=english,
        ipa=phonetic(english) if english else None,
        examples=examples,
        tureng_audio=tureng_audio,
        suggestions=tureng.get("suggestions", []),
    )
    return result
