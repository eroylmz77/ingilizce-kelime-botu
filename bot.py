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
import re
import sys
from itertools import count

import requests

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import defter
import ogretmen
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
            if ex.get("meaning"):
                line += f"\n    🏷 <i>anlam: {e(ex['meaning'])}</i>"
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
async def send_audio(context, chat_id, text, fallback_url=None):
    await context.bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
    try:
        path = await asyncio.to_thread(sozluk.audio_for, text, fallback_url)
    except Exception as exc:  # ağ / API hatası
        log.warning("Ses alınamadı: %s", exc)
        await context.bot.send_message(chat_id, "⚠️ Ses alınamadı, biraz sonra tekrar dene.")
        return
    if path is None:
        await context.bot.send_message(
            chat_id, "🔇 Bu metin için ses yok (ELEVENLABS_API_KEY tanımlı değil).")
        return
    with open(path, "rb") as f:
        await context.bot.send_voice(chat_id, voice=f, caption=text[:1024])


async def send_ai_text(message, text):
    """Yapay zekâ cevabını Telegram biçimine çevirip (gerekirse bölerek) gönderir."""
    for part in ogretmen.split_message(text):
        try:
            await message.reply_text(ogretmen.to_telegram_html(part), parse_mode=ParseMode.HTML)
        except BadRequest:  # biçimlendirme Telegram'a uymadıysa düz metin gönder
            await message.reply_text(part)


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
        "📝 Bir cümle yazarsan onu da çeviririm (DeepL, yoksa Gemini).\n"
        "Örnek: I gave up on that bug yesterday.\n\n"
        "👩‍🏫 İngilizce öğretmenine soru sormak için /sor yaz:\n"
        "/sor present perfect ne zaman kullanılır?\n"
        "/sor \"I am agree with you\" doğru mu?\n"
        "Öğretmen konuşmayı hatırlar; yeni konuya geçmek için /sifirla.\n\n"
        "✍️ /duzelt + metin → commit mesajı, e-posta, Slack mesajını düzeltirim.\n"
        "/duzelt Fixed the bug which was causing crash\n\n"
        "📒 Aradığın kelimeler deftere kaydedilir:\n"
        "/tekrar → defterdeki kelimelerle sınav\n"
        "/defter → kelime listen · /sil kelime → defterden çıkar\n\n"
        "☀️ /gunluk ac → her sabah 09:00'da yazılım dünyasından bir kelime\n\n"
        f"Kullanıcı ID'n: {user.id}"
    )


async def ask_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Bu bot özel kullanım içindir.")
        return
    # "/sor" sonrasındaki metnin tamamı (satır sonları dahil)
    question = update.message.text.partition(" ")[2].strip()
    if not question:
        await update.message.reply_text(
            "Sorunu komutun yanına yaz 🙂\nÖrnek: /sor 'since' ile 'for' farkı nedir?")
        return

    await context.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    history = context.chat_data.setdefault("ogretmen", [])
    try:
        answer = await asyncio.to_thread(ogretmen.ask, list(history), question)
    except ogretmen.TeacherError as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        return

    history += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
    del history[:-ogretmen.HISTORY_LIMIT]
    await send_ai_text(update.message, answer)


async def reset_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.pop("ogretmen", None)
    await update.message.reply_text("🧹 Öğretmen konuşmayı unuttu, yeni bir konuya geçebilirsin.")


async def correct_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Bu bot özel kullanım içindir.")
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text(
            "Düzeltmemi istediğin metni komutun yanına yaz 🙂\n"
            "Örnek: /duzelt Fixed the bug which was causing crash\n"
            "(Çok satırlı metinler de olur: commit mesajı, e-posta, PR açıklaması...)")
        return
    await context.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    try:
        answer = await asyncio.to_thread(ogretmen.correct, text)
    except ogretmen.TeacherError as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        return
    await send_ai_text(update.message, answer)


# --------------------------------------------------------------------------- #
# Kelime defteri ve tekrar sınavı
# --------------------------------------------------------------------------- #
def save_to_notebook(uid, r):
    """Aranan kelimeyi deftere ekler; veritabanı sorunu aramayı bozmasın."""
    entry = defter.entry_from_lookup(r)
    if entry is None:
        return False
    try:
        return defter.add_word(uid, entry)
    except Exception as exc:
        log.warning("Deftere eklenemedi: %s", exc)
        return False


async def show_notebook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    uid = update.effective_user.id
    words = await asyncio.to_thread(defter.words, uid)
    if not words:
        await update.message.reply_text(
            "📒 Defterin henüz boş. Bir kelime aradığında otomatik eklenir 🙂")
        return
    st = await asyncio.to_thread(defter.stats, uid)
    e = html.escape
    latest = sorted(words.values(), key=lambda w: w["added"], reverse=True)[:30]
    lines = [f"• <b>{e(w['en'])}</b> — {e(w['tr'])}  {'⭐' * (w['box'] - 1)}" for w in latest]
    text = (f"📒 <b>Kelime defterin</b>: {st['total']} kelime · {st['learned']} öğrenildi · "
            f"{st['due']} tekrar bekliyor\n\n" + "\n".join(lines))
    if st["total"] > len(latest):
        text += f"\n… ve {st['total'] - len(latest)} kelime daha"
    text += "\n\n⭐ = kaç kez üst üste bildin · /tekrar ile çalış · /sil kelime ile çıkar"
    if not defter.is_persistent():
        text += ("\n\n⚠️ <i>Kalıcı veritabanı (MONGODB_URI) bağlı değil: bot yeniden "
                 "başlayınca defter silinir.</i>")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def delete_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Silmek istediğin kelimeyi yaz: /sil abandon")
        return
    removed = await asyncio.to_thread(defter.remove_word, update.effective_user.id, text)
    await update.message.reply_text(
        f"🗑 {removed} defterden çıkarıldı." if removed else f"'{text}' defterde bulunamadı.")


async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    uid = update.effective_user.id
    questions, early = await asyncio.to_thread(defter.build_quiz, uid)
    if not questions:
        await update.message.reply_text(
            "📒 Defterin boş. Önce birkaç kelime ara, sonra /tekrar ile çalışalım 🙂")
        return
    context.chat_data["quiz"] = {"id": next(_ids), "uid": uid, "questions": questions,
                                 "i": 0, "score": 0}
    intro = (f"🧠 Tekrar zamanı! {len(questions)} soru.")
    if early:
        intro = ("✨ Bugün tekrar zamanı gelen kelime yok, yine de en yakın "
                 f"{len(questions)} kelimeyle pratik yapalım.")
    await update.message.reply_text(intro)
    await send_question(context, update.message.chat_id)


def question_text(quiz):
    q = quiz["questions"][quiz["i"]]
    flag, ask = ("🇬🇧", "Türkçesi ne?") if q["direction"] == "en" else ("🇹🇷", "İngilizcesi ne?")
    return (f"❓ <b>{quiz['i'] + 1}/{len(quiz['questions'])}</b>   "
            f"{flag} <b>{html.escape(q['prompt'])}</b>\n{ask}")


async def send_question(context, chat_id):
    quiz = context.chat_data["quiz"]
    q = quiz["questions"][quiz["i"]]
    prefix = f"t:{quiz['id']}:{quiz['i']}"
    if q["kind"] == "choice":
        rows = [[InlineKeyboardButton(opt[:60], callback_data=f"{prefix}:{j}")]
                for j, opt in enumerate(q["options"])]
    else:
        rows = [[InlineKeyboardButton("👀 Cevabı göster", callback_data=f"{prefix}:show")]]
    await context.bot.send_message(chat_id, question_text(quiz), parse_mode=ParseMode.HTML,
                                   reply_markup=InlineKeyboardMarkup(rows))


async def handle_quiz_button(query, context):
    _, quiz_id, index, choice = query.data.split(":")
    quiz = context.chat_data.get("quiz")
    # Eski bir sınavın ya da zaten cevaplanmış sorunun butonuna basıldıysa yok say
    if not quiz or str(quiz["id"]) != quiz_id or str(quiz["i"]) != index:
        await query.edit_message_reply_markup(None)
        return
    q = quiz["questions"][quiz["i"]]
    e = html.escape

    if choice == "show":  # kart sorusu: önce cevabı göster, sonra kendini değerlendir
        prefix = f"t:{quiz_id}:{index}"
        await query.edit_message_text(
            f"{question_text(quiz)}\n\n👉 <b>{e(q['answer'])}</b>", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Bildim", callback_data=f"{prefix}:yes"),
                InlineKeyboardButton("❌ Bilemedim", callback_data=f"{prefix}:no")]]))
        return

    correct = choice == "yes" if choice in ("yes", "no") else int(choice) == q["correct"]
    await asyncio.to_thread(defter.record_answer, quiz["uid"], q["key"], correct)
    quiz["score"] += correct
    quiz["i"] += 1

    feedback = "✅ <b>Doğru!</b>" if correct else f"❌ Doğrusu: <b>{e(q['answer'])}</b>"
    if q.get("example"):
        feedback += f"\n\n✏️ <i>{e(q['example']['en'])}</i>"
        if q["example"].get("tr"):
            feedback += f"\n    {e(q['example']['tr'])}"
    await query.edit_message_text(f"{question_text({**quiz, 'i': quiz['i'] - 1})}\n\n{feedback}",
                                  parse_mode=ParseMode.HTML)

    if quiz["i"] < len(quiz["questions"]):
        await send_question(context, query.message.chat_id)
        return
    total = len(quiz["questions"])
    st = await asyncio.to_thread(defter.stats, quiz["uid"])
    del context.chat_data["quiz"]
    await context.bot.send_message(
        query.message.chat_id,
        f"🏁 Bitti! <b>{quiz['score']}/{total}</b> doğru.\n"
        f"📒 Defterinde {st['total']} kelime var, {st['learned']} tanesini öğrendin.\n"
        "Bilemediklerin bir sonraki /tekrar'da yine gelecek; bildiklerin daha seyrek sorulacak.",
        parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Günün kelimesi
# --------------------------------------------------------------------------- #
async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    uid = update.effective_user.id
    arg = update.message.text.partition(" ")[2].strip().lower()

    if arg in ("ac", "aç", "on"):
        await asyncio.to_thread(defter.set_daily, uid, enabled=True)
    elif re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", arg):
        await asyncio.to_thread(defter.set_daily, uid, enabled=True, time=arg.zfill(5))
    elif arg in ("kapat", "off"):
        await asyncio.to_thread(defter.set_daily, uid, enabled=False)
    elif arg in ("simdi", "şimdi"):
        word = await asyncio.to_thread(defter.pick_daily_word, uid, defter.DAILY_WORDS)
        context.job_queue.run_once(send_daily_word, 0, chat_id=update.message.chat_id,
                                   user_id=uid, data=word)
        return
    elif arg:
        await update.message.reply_text("Anlayamadım 🙂 Örnek: /gunluk ac, /gunluk 08:30, /gunluk kapat")
        return

    d = await asyncio.to_thread(defter.daily_settings, uid)
    if d.get("enabled"):
        status = f"✅ Açık — her gün saat <b>{d.get('time', '09:00')}</b>'da (Türkiye saati)."
    else:
        status = "⏸ Kapalı."
    await update.message.reply_text(
        f"☀️ <b>Günün kelimesi</b>: {status}\n\n"
        "/gunluk ac → aç (09:00)\n/gunluk 08:30 → saati değiştir\n"
        "/gunluk kapat → kapat\n/gunluk simdi → hemen bir tane gönder",
        parse_mode=ParseMode.HTML)


async def daily_tick(context: ContextTypes.DEFAULT_TYPE):
    """Her dakika çalışır: saati gelmiş kullanıcılara günün kelimesini gönderir.
    Bot uyurken saat kaçtıysa, uyandığında gönderir."""
    try:
        due = await asyncio.to_thread(defter.users_due_daily)
    except Exception as exc:
        log.warning("Günlük kelime kontrolü yapılamadı: %s", exc)
        return
    for uid in due:
        word = await asyncio.to_thread(defter.pick_daily_word, uid, defter.DAILY_WORDS)
        # Önce işaretle: gönderim uzun sürerse bir sonraki kontrol tekrar göndermesin
        await asyncio.to_thread(defter.mark_daily_sent, uid, word)
        context.job_queue.run_once(send_daily_word, 0, chat_id=uid, user_id=uid, data=word)


async def send_daily_word(context: ContextTypes.DEFAULT_TYPE):
    chat_id, word = context.job.chat_id, context.job.data
    r = await asyncio.to_thread(sozluk.lookup, word)
    if not r["meanings"]:
        log.warning("Günün kelimesi '%s' bulunamadı: %s", word, r["errors"])
        return
    added = await asyncio.to_thread(save_to_notebook, context.job.user_id, r)
    rid = remember(context, r)
    text = "☀️ <b>Günün kelimesi</b>\n\n" + format_result(r)
    if added:
        text += "\n\n📒 <i>Deftere eklendi · /tekrar</i>"
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                   reply_markup=result_keyboard(rid, r))
    await send_audio(context, chat_id, r["english"], r["tureng_audio"])


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
    await lookup_and_reply(update.message, context, text.rstrip(".?!"), update.effective_user.id)


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
    return (f"📝 <b>Cümle çevirisi</b> ({t['engine']})   {direction}\n\n"
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


async def lookup_and_reply(message, context, word, uid):
    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
    r = await asyncio.to_thread(sozluk.lookup, word)

    if not r["meanings"] and not r["deepl"]:
        if r["errors"]:
            # Kelime yok değil, kaynağa ulaşılamadı — sebebini göster ve loga yaz
            log.warning("'%s' aranamadı: %s", word, "; ".join(r["errors"]))
            text = "⚠️ " + "\n⚠️ ".join(html.escape(e) for e in r["errors"])
            await message.reply_text(text, parse_mode=ParseMode.HTML)
            return
        text = f"🤔 <b>{html.escape(word)}</b> bulunamadı."
        if r["suggestions"]:
            text += "\nBunu mu demek istedin?"
        await message.reply_text(text, parse_mode=ParseMode.HTML,
                                 reply_markup=suggestion_keyboard(r["suggestions"]))
        return

    rid = remember(context, r)
    text = format_result(r)
    if await asyncio.to_thread(save_to_notebook, uid, r):
        text += "\n\n📒 <i>Deftere eklendi · /tekrar</i>"
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=result_keyboard(rid, r))
    if r["english"]:
        await send_audio(context, message.chat_id, r["english"], r["tureng_audio"])


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    data = query.data

    if data.startswith("q:"):  # öneri butonu -> o kelimeyi ara
        await lookup_and_reply(query.message, context, data[2:], query.from_user.id)
        return

    if data.startswith("t:"):  # tekrar sınavı cevabı
        await handle_quiz_button(query, context)
        return

    if data.startswith("s:"):  # seslendirme butonu
        _, rid, which = data.split(":")
        r = context.chat_data.get("results", {}).get(int(rid))
        chat_id = query.message.chat_id
        if r is None:
            await query.message.reply_text("Bu sonuç eskidi, kelimeyi tekrar gönder 🙂")
            return
        if which == "w":
            await send_audio(context, chat_id, r["english"], r["tureng_audio"])
        else:
            await send_audio(context, chat_id, r["examples"][int(which)]["en"])


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Hata", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("⚠️ Bir şeyler ters gitti, tekrar dener misin?")


def build_app(token, webhook):
    # Birden fazla kişi aynı anda kullanırsa biri diğerini beklemesin
    builder = Application.builder().token(token).concurrent_updates(True)
    if webhook:
        builder = builder.updater(None)  # güncellemeleri web sunucusu getirir
    app = builder.post_init(set_commands).build()
    app.add_handler(CommandHandler(["start", "help", "yardim"], start))
    app.add_handler(CommandHandler("sor", ask_teacher))
    app.add_handler(CommandHandler("sifirla", reset_teacher))
    app.add_handler(CommandHandler("duzelt", correct_text))
    app.add_handler(CommandHandler("defter", show_notebook))
    app.add_handler(CommandHandler("sil", delete_word))
    app.add_handler(CommandHandler("tekrar", start_quiz))
    app.add_handler(CommandHandler("gunluk", daily_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_word))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(daily_tick, interval=60, first=15)
    return app


COMMANDS = [
    ("sor", "İngilizce öğretmenine soru sor"),
    ("duzelt", "İngilizce metnini düzelt (commit, e-posta...)"),
    ("tekrar", "Defterdeki kelimelerle sınav"),
    ("defter", "Kelime defterini göster"),
    ("gunluk", "Günün kelimesi ayarları"),
    ("sil", "Defterden kelime çıkar"),
    ("sifirla", "Öğretmen konuşmasını sıfırla"),
    ("start", "Botun kullanımı"),
]


async def set_commands(app):
    """Telegram'da "/" yazınca çıkan komut menüsü."""
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    except Exception as exc:
        log.warning("Komut menüsü ayarlanamadı: %s", exc)


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
        # Render hangi commit'in çalıştığını RENDER_GIT_COMMIT ile bildirir
        return PlainTextResponse(f"ok {os.environ.get('RENDER_GIT_COMMIT', '')[:7]}".strip())

    web = Starlette(routes=[
        Route("/telegram", telegram, methods=["POST"]),
        Route("/", health, methods=["GET", "HEAD"]),
    ])
    port = int(os.environ.get("PORT", "10000"))
    server = uvicorn.Server(uvicorn.Config(web, host="0.0.0.0", port=port, log_level="warning"))

    async with app:
        await set_commands(app)  # post_init sadece run_polling'de kendiliğinden çalışır
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
    provider = ogretmen.active_provider()
    if provider:
        log.info("Öğretmen: %s (%s)", provider[0], provider[3])
    else:
        log.warning("GEMINI_API_KEY / OPENAI_API_KEY tanımlı değil — /sor devre dışı.")
    if defter.is_persistent():
        log.info("Kelime defteri: MongoDB")
    else:
        log.warning("MONGODB_URI tanımlı değil — kelime defteri yerel dosyada (kalıcı değil).")

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
