"""
Kelime defteri + aralıklı tekrar (Leitner kutuları) + günün kelimesi ayarları.

Veriler MONGODB_URI tanımlıysa MongoDB'de (kalıcı), değilse yerel bir JSON dosyasında
saklanır. Render'ın ücretsiz planında yerel dosya bot yeniden başlayınca silinir.
"""

import json
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
JSON_PATH = BASE_DIR / "veri" / "defter.json"

# Leitner kutuları: doğru bilinen kelime bir üst kutuya çıkar ve daha seyrek sorulur
BOX_DAYS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}
MAX_BOX = 5
QUIZ_LENGTH = 10
ISTANBUL = timezone(timedelta(hours=3))  # Türkiye yaz/kış saati uygulamıyor


def word_key(en):
    # MongoDB alan adlarında "." ve "$" kullanılamaz
    return re.sub(r"[.$]", "_", en.strip().lower())


# --------------------------------------------------------------------------- #
# Depolama
# --------------------------------------------------------------------------- #
class JsonStore:
    def __init__(self, path=JSON_PATH):
        self.path = path
        self.lock = threading.Lock()

    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self, data):
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def get_user(self, uid):
        with self.lock:
            return self._load().get(str(uid), {})

    def update(self, uid, set_fields=None, unset_fields=()):
        """set_fields: {"words.abandon": {...}, "daily": {...}} — noktalı yol desteklenir."""
        with self.lock:
            data = self._load()
            user = data.setdefault(str(uid), {})
            for path, value in (set_fields or {}).items():
                *parents, last = path.split(".", 1) if path.startswith("words.") else [path]
                target = user.setdefault(parents[0], {}) if parents else user
                target[last] = value
            for path in unset_fields:
                section, _, key = path.partition(".")
                (user.get(section) or {}).pop(key, None)
            self._save(data)

    def daily_users(self):
        with self.lock:
            return [(int(uid), u["daily"]) for uid, u in self._load().items()
                    if u.get("daily", {}).get("enabled")]


class MongoStore:
    def __init__(self, uri):
        from pymongo import MongoClient
        self.users = MongoClient(uri, serverSelectionTimeoutMS=10000)["kelime_botu"]["users"]

    def get_user(self, uid):
        return self.users.find_one({"_id": uid}) or {}

    def update(self, uid, set_fields=None, unset_fields=()):
        ops = {}
        if set_fields:
            ops["$set"] = set_fields
        if unset_fields:
            ops["$unset"] = {f: "" for f in unset_fields}
        if ops:
            self.users.update_one({"_id": uid}, ops, upsert=True)

    def daily_users(self):
        return [(u["_id"], u["daily"]) for u in self.users.find({"daily.enabled": True})]


_store = None


def store():
    global _store
    if _store is None:
        uri = os.environ.get("MONGODB_URI")
        _store = MongoStore(uri) if uri else JsonStore()
    return _store


def is_persistent():
    return bool(os.environ.get("MONGODB_URI"))


# --------------------------------------------------------------------------- #
# Defter işlemleri
# --------------------------------------------------------------------------- #
def entry_from_lookup(r):
    """sozluk.lookup sonucundan defter kaydı: her zaman (İngilizce, Türkçe) çifti."""
    if not r.get("meanings") or not r.get("english"):
        return None
    if r["source_lang"] == "en":
        tr = ", ".join(m["target"] for m in r["meanings"][:2])
    else:
        tr = r["word"]
    ex = r["examples"][0] if r.get("examples") else None
    return {"en": r["english"], "tr": tr, "example": ex}


def add_word(uid, entry):
    """Kelimeyi ekler; zaten varsa ilerlemesine dokunmaz. Yeni eklendiyse True."""
    key = word_key(entry["en"])
    existing = store().get_user(uid).get("words", {})
    if key in existing:
        return False
    now = time.time()
    store().update(uid, {f"words.{key}": {
        **entry, "box": 1, "due": now, "added": now, "correct": 0, "wrong": 0}})
    return True


def remove_word(uid, text):
    """İngilizce ya da Türkçe karşılığıyla eşleşen kelimeyi siler; silinenin adını döndürür."""
    text = text.strip().lower()
    for key, w in store().get_user(uid).get("words", {}).items():
        if text in (key, w["en"].lower(), w["tr"].lower()):
            store().update(uid, unset_fields=[f"words.{key}"])
            return w["en"]
    return None


def words(uid):
    return store().get_user(uid).get("words", {})


def record_answer(uid, key, correct):
    w = words(uid).get(key)
    if w is None:
        return
    now = time.time()
    if correct:
        box = min(w["box"] + 1, MAX_BOX)
        due = now + BOX_DAYS[box] * 86400
    else:
        box, due = 1, now  # bilinmeyen kelime bir sonraki tekrarda yine gelir
    store().update(uid, {f"words.{key}": {
        **w, "box": box, "due": due,
        "correct": w["correct"] + int(correct), "wrong": w["wrong"] + int(not correct)}})


# --------------------------------------------------------------------------- #
# Tekrar sınavı
# --------------------------------------------------------------------------- #
def build_quiz(uid, length=QUIZ_LENGTH):
    """Zamanı gelmiş kelimelerden soru listesi. Hiçbiri gelmemişse en yakın olanlar."""
    all_words = words(uid)
    if not all_words:
        return [], False
    now = time.time()
    due = [k for k, w in all_words.items() if w["due"] <= now]
    early = not due
    if early:
        due = sorted(all_words, key=lambda k: all_words[k]["due"])[:min(5, length)]
    random.shuffle(due)
    return [make_question(all_words, k) for k in due[:length]], early


def make_question(all_words, key):
    """Defterde en az 4 kelime varsa 4 şıklı soru, yoksa kart (cevabı göster) sorusu."""
    w = all_words[key]
    direction = random.choice(["en", "tr"])  # sorulan tarafın dili
    answer_side = "tr" if direction == "en" else "en"
    q = {"key": key, "prompt": w[direction], "answer": w[answer_side], "direction": direction,
         "example": w.get("example")}

    others = list({o[answer_side] for k, o in all_words.items()
                   if k != key and o[answer_side].lower() != w[answer_side].lower()})
    if len(others) >= 3:
        options = random.sample(others, 3) + [w[answer_side]]
        random.shuffle(options)
        q.update(kind="choice", options=options, correct=options.index(w[answer_side]))
    else:
        q["kind"] = "card"
    return q


def stats(uid):
    all_words = words(uid)
    now = time.time()
    return {
        "total": len(all_words),
        "due": sum(w["due"] <= now for w in all_words.values()),
        "learned": sum(w["box"] >= 4 for w in all_words.values()),
        "boxes": {b: sum(w["box"] == b for w in all_words.values()) for b in range(1, MAX_BOX + 1)},
    }


# --------------------------------------------------------------------------- #
# Günün kelimesi
# --------------------------------------------------------------------------- #
def daily_settings(uid):
    return store().get_user(uid).get("daily", {})


def set_daily(uid, **fields):
    current = daily_settings(uid)
    store().update(uid, {"daily": {"enabled": False, "time": "09:00", "last": "", "sent": [],
                                   **current, **fields}})


def now_istanbul():
    return datetime.now(ISTANBUL)


def users_due_daily():
    """Saati gelmiş ve bugün henüz kelime almamış kullanıcılar."""
    now = now_istanbul()
    today, clock = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    return [uid for uid, d in store().daily_users()
            if d.get("last") != today and clock >= d.get("time", "09:00")]


def pick_daily_word(uid, pool):
    sent = set(daily_settings(uid).get("sent", []))
    known = {w["en"].lower() for w in words(uid).values()}
    fresh = [w for w in pool if w not in sent and w not in known] or \
            [w for w in pool if w not in sent] or pool  # hepsi bittiyse baştan
    return random.choice(fresh)


def mark_daily_sent(uid, word):
    d = daily_settings(uid)
    sent = (d.get("sent", []) + [word])[-len(DAILY_WORDS):]
    set_daily(uid, last=now_istanbul().strftime("%Y-%m-%d"), sent=sent)


# Yazılım mühendisinin iş hayatında sık karşılaşacağı kelimeler
DAILY_WORDS = [
    "deprecate", "leverage", "bottleneck", "scalable", "robust", "legacy", "refactoring",
    "deploy", "rollback", "throughput", "latency", "overhead", "redundant", "consistent",
    "mitigate", "outage", "incident", "stakeholder", "requirement", "constraint", "trade-off",
    "maintainable", "workaround", "deadline", "milestone", "estimate", "prioritize",
    "feasible", "straightforward", "cumbersome", "seamless", "granular", "intuitive",
    "comprehensive", "deliverable", "iteration", "backlog", "assignee", "approve", "merge",
    "conflict", "resolve", "reproduce", "investigate", "root cause", "postmortem",
    "escalate", "allocate", "concurrency", "dependency", "vulnerability", "authenticate",
    "authorize", "encrypt", "validate", "sanitize", "retrieve", "fetch", "cache", "invalidate",
    "threshold", "benchmark", "optimize", "bloated", "verbose", "concise", "ambiguous",
    "explicit", "implicit", "arbitrary", "mandatory", "optional", "fallback", "edge case",
    "regression", "coverage", "flaky", "intermittent", "deterministic", "idempotent",
    "backward compatible", "migrate", "provision", "monitor", "alert", "downtime",
    "uptime", "capacity", "overwhelm", "streamline", "automate", "tedious", "error-prone",
    "accountable", "clarify", "elaborate", "align", "sync up", "follow up", "hand over",
    "onboard", "feedback", "acknowledge", "on hold", "ship", "roll out", "sunset",
    "abstraction", "encapsulate", "decouple", "inherit", "override", "instantiate",
    "boilerplate", "nitpick", "sanity check", "bandwidth",
]
