"""
İngilizce Kelime Botu — Telegram
--------------------------------
Bota Türkçe ya da İngilizce bir kelime yaz; çevirisini, okunuşunu (sesli)
ve 5 örnek cümlesini gönderir.

Çalıştırma:
  Yerelde:  py -3.11 bot.py   (.env içinde TELEGRAM_BOT_TOKEN gerekli)
  Bulutta:  RENDER_EXTERNAL_URL / WEBHOOK_URL tanımlıysa webhook modunda çalışır
"""

import asyncio
import hashlib
import html
import logging
import os
import sys
from itertools import count

import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import sozluk

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("kelime-botu")

MAX_MEANINGS = 6
MAX_QUERY_LEN = 60
MAX_SENTENCE_LEN = 1500
KEEP_RESULTS = 30          # sohbet başına butonları çalışır tutulan son sonuç sayısı
_ids = count(1)


def allowed_users():
    raw = os.environ.get("ALLOWED_USER_IDS", "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


def is_allowed(update: Update):
    allowed = allowed_users()
    return not allowed or (update.effective_user and update.effective_user.id in allowed)


# --------------------------------------------------------------------------- #
# Mesaj biçimlendirme
# --------------------------------------------------------------------------- #
def format_result(r):
    e = html.escape
    direction = "🇹🇷 → 🇬🇧" if r["source_lang"] == "tr" else "🇬🇧 → 🇹🇷"
    parts = [f"<b>{e(r['word'])}</b>   {direction}"]

    if r["meanings"]:
        lines = []
        for i, m in enumerate(r["meanings"][:MAX_MEANINGS], 1):
            pos = f" <i>({e(m['pos'])}.)</i>" if m["pos"] else ""
            lines.append(f"{i}. {e(m['target'])}{pos}  <i>· {e(m['category'])}</i>")
        parts.append("📖 <b>Anlamlar</b> (Tureng)\n" + "\n".join(lines))
    if r["software_meanings"]:
        sw = ", ".join(e(m["target"]) for m in r["software_meanings"][:MAX_MEANINGS])
        parts.append(f"💻 <b>Yazılımda:</b> {sw}")
    if r["deepl"]:
        parts.append(f"🔁 <b>DeepL:</b> {e(r['deepl'])}")

    if r["english"]:
        ipa = f"  <code>{e(r['ipa'])}</code>" if r["ipa"] else ""
        parts.append(f"🔊 <b>Okunuş:</b> {e(r['english'])}{ipa}")

    if r["examples"]:
        lines = []
        for i, ex in enumerate(r["examples"], 1):
            tag = " 💻" if ex.get("software") else ""
            line = f"<b>{i}.</b>{tag} {e(ex['en'])}"
            if ex.get("tr"):
                line += f"\n    <i>{e(ex['tr'])}</i>"
            lines.append(line)
        parts.append("✏️ <b>Örnek cümleler</b>\n" + "\n\n".join(lines))

    for err in r["errors"]:
        parts.append(f"⚠️ {e(err)}")
    return "\n\n".join(parts)


def result_keyboard(rid, r):
    """Kelimeyi ve (ElevenLabs varsa) cümleleri seslendirme butonları."""
    rows = []
    if r["english"] and (r["tureng_audio"] or os.environ.get("ELEVENLABS_API_KEY")):
        rows.append([InlineKeyboardButton(f"🔊 {r['english']}", callback_data=f"s:{rid}:w")])
    if r["examples"] and os.environ.get("ELEVENLABS_API_KEY"):
        rows.append([
            InlineKeyboardButton(f"🔊 {i}", callback_data=f"s:{rid}:{i - 1}")
            for i in range(1, len(r["examples"]) + 1)
        ])
    return InlineKeyboardMarkup(rows) if rows else None


def suggestion_keyboard(suggestions):
    buttons = [
        InlineKeyboardButton(s, callback_data=f"q:{s}")
        for s in suggestions[:6]
        if len(f"q:{s}".encode()) <= 64  # Telegram callback_data sınırı
    ]
    return InlineKeyboardMarkup([buttons[i:i + 3] for i in range(0, len(buttons), 3)]) if buttons else None


# --------------------------------------------------------------------------- #
# Ses gönderme
# --------------------------------------------------------------------------- #
async def send_audio(message, context, text, fallback_url=None):
    await context.bot.send_chat_action(message.chat_id, ChatAction.RECORD_VOICE)
    try:
        path = await asyncio.to_thread(sozluk.audio_for, text, fallback_url)
    except Exception as exc:  # ağ / API hatası
        log.warning("Ses alınamadı: %s", exc)
        await message.reply_text("⚠️ Ses alınamadı, biraz sonra tekrar dene.")
        return
    if path is None:
        await message.reply_text("🔇 Bu metin için ses yok (ELEVENLABS_API_KEY tanımlı değil).")
        return
    with open(path, "rb") as f:
        await message.reply_voice(voice=f, caption=text[:1024])


# --------------------------------------------------------------------------- #
# Handler'lar
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        "Merhaba! 👋 Bana Türkçe ya da İngilizce bir kelime yaz.\n\n"
        "Sana şunları göndereceğim:\n"
        "📖 çevirisi (Tureng + DeepL)\n"
        "🔊 okunuşu, sesli olarak (ElevenLabs)\n"
        "✏️ 5 örnek cümle\n\n"
        "Örnek: apple, vazgeçmek, give up\n\n"
        "📝 Bir cümle yazarsan onu da DeepL ile çeviririm.\n"
        "Örnek: I gave up on that bug yesterday.\n\n"
        f"Kullanıcı ID'n: {user.id}"
    )


async def handle_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Bu bot özel kullanım içindir.")
        return
    text = update.message.text.strip()
    if sozluk.is_sentence(text):
        if len(text) > MAX_SENTENCE_LEN:
            await update.message.reply_text(
                f"Metin çok uzun, en fazla {MAX_SENTENCE_LEN} karakter gönder 🙂")
            return
        await translate_and_reply(update.message, context, text)
        return
    if len(text) > MAX_QUERY_LEN:
        await update.message.reply_text("Lütfen tek bir kelime ya da kısa bir kalıp gönder 🙂")
        return
    await lookup_and_reply(update.message, context, text.rstrip(".?!"))


def remember(context, r):
    """Butonların eski mesajlarda da çalışması için sonucu kimlikle sakla."""
    rid = next(_ids)
    results = context.chat_data.setdefault("results", {})
    results[rid] = r
    for old in sorted(results)[:-KEEP_RESULTS]:
        del results[old]
    return rid


def format_sentence(t):
    e = html.escape
    flags = {"tr": "🇹🇷", "en": "🇬🇧"}
    target = "en" if t["source_lang"] == "tr" else "tr"
    direction = f"{flags.get(t['source_lang'], t['source_lang'].upper())} → {flags[target]}"
    return (f"📝 <b>Cümle çevirisi</b> (DeepL)   {direction}\n\n"
            f"<i>{e(t['text'])}</i>\n\n"
            f"<b>{e(t['translation'])}</b>")


async def translate_and_reply(message, context, text):
    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
    t = await asyncio.to_thread(sozluk.translate_sentence, text)
    if t["error"]:
        await message.reply_text(f"⚠️ {t['error']}")
        return

    # Cümle sesi ElevenLabs kredisi harcadığı için otomatik değil, butonla gönderilir
    keyboard = None
    if t["english"] and os.environ.get("ELEVENLABS_API_KEY"):
        rid = remember(context, {"english": t["english"], "tureng_audio": None, "examples": []})
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔊 İngilizcesini dinle", callback_data=f"s:{rid}:w")]])
    await message.reply_text(format_sentence(t), parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def lookup_and_reply(message, context, word):
    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
    r = await asyncio.to_thread(sozluk.lookup, word)

    if not r["meanings"] and not r["deepl"]:
        text = f"🤔 <b>{html.escape(word)}</b> bulunamadı."
        if r["suggestions"]:
            text += "\nBunu mu demek istedin?"
        await message.reply_text(text, parse_mode=ParseMode.HTML,
                                 reply_markup=suggestion_keyboard(r["suggestions"]))
        return

    rid = remember(context, r)
    await message.reply_text(format_result(r), parse_mode=ParseMode.HTML,
                             reply_markup=result_keyboard(rid, r))
    if r["english"]:
        await send_audio(message, context, r["english"], r["tureng_audio"])


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    data = query.data

    if data.startswith("q:"):  # öneri butonu -> o kelimeyi ara
        await lookup_and_reply(query.message, context, data[2:])
        return

    if data.startswith("s:"):  # seslendirme butonu
        _, rid, which = data.split(":")
        r = context.chat_data.get("results", {}).get(int(rid))
        if r is None:
            await query.message.reply_text("Bu sonuç eskidi, kelimeyi tekrar gönder 🙂")
            return
        if which == "w":
            await send_audio(query.message, context, r["english"], r["tureng_audio"])
        else:
            await send_audio(query.message, context, r["examples"][int(which)]["en"])


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Hata", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("⚠️ Bir şeyler ters gitti, tekrar dener misin?")


def build_app(token, webhook):
    # Birden fazla kişi aynı anda kullanırsa biri diğerini beklemesin
    builder = Application.builder().token(token).concurrent_updates(True)
    if webhook:
        builder = builder.updater(None)  # güncellemeleri web sunucusu getirir
    app = builder.build()
    app.add_handler(CommandHandler(["start", "help", "yardim"], start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_word))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_error_handler(on_error)
    return app


async def run_webhook(app, token, base_url):
    """Bulut modu: Telegram mesajları POST /telegram adresine gönderir."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, Response
    from starlette.routing import Route

    # İsteklerin gerçekten Telegram'dan geldiğini doğrulamak için gizli anahtar
    secret = hashlib.sha256(token.encode()).hexdigest()[:32]

    async def telegram(request: Request):
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return Response(status_code=403)
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return Response()

    async def health(_: Request):
        return PlainTextResponse("ok")

    web = Starlette(routes=[
        Route("/telegram", telegram, methods=["POST"]),
        Route("/", health, methods=["GET", "HEAD"]),
    ])
    port = int(os.environ.get("PORT", "10000"))
    server = uvicorn.Server(uvicorn.Config(web, host="0.0.0.0", port=port, log_level="warning"))

    async with app:
        await app.bot.set_webhook(f"{base_url}/telegram", secret_token=secret,
                                  allowed_updates=Update.ALL_TYPES)
        await app.start()
        log.info("Bot bulutta çalışıyor (webhook: %s/telegram, port %s).", base_url, port)
        await server.serve()
        await app.stop()


def active_webhook(token):
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=10)
        return resp.json().get("result", {}).get("url") or None
    except (requests.RequestException, ValueError):
        return None


def main():
    sozluk.load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN tanımlı değil. .env dosyasına ekle (bkz. .env.example).")

    for key in ("DEEPL_API_KEY", "ELEVENLABS_API_KEY"):
        if not os.environ.get(key):
            log.warning("%s tanımlı değil — bu özellik devre dışı.", key)

    # Render bu değişkeni kendisi tanımlar; başka bir sunucuda WEBHOOK_URL kullanılabilir
    base_url = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL") or "").rstrip("/")
    if base_url:
        asyncio.run(run_webhook(build_app(token, webhook=True), token, base_url))
        return

    # Yerel mod. Bot bulutta çalışırken burada başlatmak bulut botunu devre dışı bırakır
    cloud = active_webhook(token)
    if cloud and "--yerel" not in sys.argv:
        raise SystemExit(
            f"Bu bot şu an bulutta çalışıyor ({cloud}).\n"
            "Yerelde başlatırsan bulut botu mesaj almayı bırakır. Yine de istiyorsan:\n"
            "  py -3.11 bot.py --yerel\n"
            "(Sonra bulut botunu geri açmak için Render'da servisi yeniden başlat.)"
        )
    log.info("Bot çalışıyor. Durdurmak için Ctrl+C.")
    build_app(token, webhook=False).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
