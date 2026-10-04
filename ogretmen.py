"""
İngilizce öğretmeni: /sor komutuyla gelen soruları bir yapay zekâ modeline sorar.

Gemini ve OpenAI aynı "chat completions" formatını kabul ettiği için tek kod ikisiyle de
çalışır. Hangi anahtar tanımlıysa o kullanılır (ikisi de varsa Gemini, çünkü ücretsiz).
"""

import html
import json
import os
import re

import requests

PROVIDERS = {
    # ad: (adres, anahtar değişkeni, varsayılan model)
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
               "GEMINI_API_KEY", "gemini-3.8-flash"),
    "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY", "gpt-5-mini"),
}
HISTORY_LIMIT = 12  # öğretmenin hatırladığı son mesaj sayısı (soru + cevap)

SYSTEM_PROMPT = """Sen Türk bir yazılım mühendisine İngilizce öğreten sabırlı ve samimi bir öğretmensin.

Kurallar:
- Açıklamaları Türkçe yap; İngilizce örnek cümlelerin hemen altına Türkçe çevirisini yaz.
- Gramer konularını önce kısa ve net bir kuralla, sonra 2-4 örnekle anlat.
- Uygun olduğunda en az bir örneği yazılım/iş hayatından ver (kod incelemesi, toplantı, hata ayıklama, e-posta gibi).
- Öğrenci İngilizce bir cümle yazdıysa hatalarını nazikçe düzelt ve nedenini açıkla.
- Türklerin sık yaptığı hataları (ör. "I am agree", "-ing" ile "to" karışıklığı) gerekiyorsa belirt.
- Cevapları Telegram'da okunacak şekilde kısa tut (genelde 150-250 kelime). Tablo kullanma.
- Biçimlendirme için sadece **kalın**, `kod` ve "- " ile başlayan maddeler kullan.
- İngilizce öğrenimiyle ilgisiz sorularda kibarca İngilizce konusuna geri dön."""


class TeacherError(Exception):
    pass


def active_provider():
    for name, (url, key_var, default_model) in PROVIDERS.items():
        key = os.environ.get(key_var)
        if key:
            return name, url, key, os.environ.get("AI_MODEL", default_model)
    return None


def provider_label():
    provider = active_provider()
    return {"gemini": "Gemini", "openai": "OpenAI"}[provider[0]] if provider else None


def ask(history, question):
    """history: önceki [{"role", "content"}] mesajları. Cevap metnini döndürür."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history[-HISTORY_LIMIT:],
                {"role": "user", "content": question}]
    return _chat(messages)


CORRECT_PROMPT = """Sen Türk bir yazılım mühendisinin İngilizce metinlerini düzelten bir editörsün.
Metin bir commit mesajı, PR açıklaması, e-posta, Slack/Teams mesajı, kod yorumu ya da günlük bir cümle olabilir.

Cevabını tam olarak şu bölümlerle ver:
**✅ Düzeltilmiş hali**
```
<düzeltilmiş metin — sadece hataları düzelt, anlamı ve tonu koru>
```
**🔍 Değişiklikler**
- <her değişiklik: "eski" → "yeni" ve Türkçe kısa nedeni>
**💡 Daha doğal bir alternatif**
```
<anadili İngilizce olan bir yazılımcının yazacağı hali>
```

Kurallar:
- Metin zaten doğruysa bunu söyle ve "Değişiklikler" bölümüne "Hata yok 👏" yaz.
- Commit mesajıysa commit kurallarına uy (emir kipi: "Fix", "Add"; ilk satır ~50 karakter; sonda nokta yok) ve bunu açıkla.
- E-posta/mesajsa tonu (fazla resmi/kaba) değerlendir.
- Metin Türkçeyse İngilizceye çevir ve bunu belirt.
- Açıklamalar Türkçe, kısa ve net olsun. Tablo kullanma."""


def correct(text):
    return _chat([{"role": "system", "content": CORRECT_PROMPT},
                  {"role": "user", "content": text}])


TRANSLATE_PROMPT ="""You are a professional Turkish-English translator.
Detect the language of the user's text. If it is Turkish, translate it into natural English.
Otherwise translate it into natural Turkish. Keep the meaning, tone and technical terms.
Reply with ONLY a JSON object, no other text:
{"source_lang": "<ISO 639-1 code of the original, e.g. tr or en>", "translation": "<translation>"}"""


def translate(text):
    """Cümleyi çevirir: {"source_lang": "tr"/"en"/..., "translation": "..."}"""
    content = _chat([{"role": "system", "content": TRANSLATE_PROMPT},
                     {"role": "user", "content": text}])
    match = re.search(r"\{.*\}", content, re.DOTALL)  # model JSON'u ``` içine alabilir
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        data = {}
    if not data.get("translation"):
        raise TeacherError("Çeviri cevabı anlaşılamadı, tekrar dener misin?")
    return {"source_lang": str(data.get("source_lang", "")).lower()[:2] or "?",
            "translation": data["translation"].strip()}


def _chat(messages):
    provider = active_provider()
    if provider is None:
        raise TeacherError("GEMINI_API_KEY ya da OPENAI_API_KEY tanımlı değil.")
    name, url, key, model = provider
    try:
        resp = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                             json={"model": model, "messages": messages}, timeout=60)
    except requests.RequestException as e:
        raise TeacherError(f"{name} sunucusuna ulaşılamadı ({type(e).__name__}).") from e

    if resp.status_code == 429:
        raise TeacherError("Soru limiti doldu (dakikalık ya da günlük). Biraz sonra tekrar dene.")
    if resp.status_code in (401, 403):
        raise TeacherError(f"{name} API anahtarı geçersiz ya da yetkisiz (HTTP {resp.status_code}).")
    if resp.status_code != 200:
        detail = resp.text[:200].replace("\n", " ")
        raise TeacherError(f"{name} hatası: HTTP {resp.status_code} — {detail}")
    try:
        return resp.json()["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError) as e:
        raise TeacherError(f"{name} beklenmeyen bir cevap döndürdü.") from e


def to_telegram_html(text):
    """Modelin Markdown cevabını Telegram'ın desteklediği HTML'e çevirir."""
    text = html.escape(text, quote=False)
    text = re.sub(r"```(?:\w+)?\n?(.*?)```", r"<pre>\1</pre>", text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.MULTILINE)
    return text


def split_message(text, limit=4000):
    """Telegram'ın 4096 karakter sınırı için uzun cevabı paragraflardan böl."""
    parts, current = [], ""
    for para in text.split("\n\n"):
        if current and len(current) + len(para) + 2 > limit:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        parts.append(current)
    return [p[i:i + limit] for p in parts for i in range(0, len(p), limit)]
