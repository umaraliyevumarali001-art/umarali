"""Oilaviy harajatlar boti — bulut backend (bot + Mini App API).

Bitta xizmatda ikkita narsa ishlaydi:
1. Telegram webhook — /webhook/{WEBHOOK_SECRET} manziliga Telegram xabar yuboradi.
2. Mini App REST API — /api/... — Netlify'dagi sahifa shu yerdan ma'lumot oladi.

Muhit o'zgaruvchilari (Render'da "Environment" bo'limida beriladi):
    BOT_TOKEN         — @BotFather bergan token (majburiy)
    WEBHOOK_SECRET     — o'zingiz o'ylab topgan maxfiy so'z, masalan "kx9f2m1q" (majburiy)
    ANTHROPIC_API_KEY  — AI orqali matn tushunish uchun (ixtiyoriy)
    ANTHROPIC_MODEL    — standart: claude-haiku-4-5
    MINIAPP_URL        — Netlify'dagi Mini App manzili, masalan https://sizning-ilova.netlify.app
    ALLOWED_CHATS      — ixtiyoriy, vergul bilan ajratilgan chat ID'lar

Ishga tushirish (lokal test uchun):
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000
"""

import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

# ----------------------------------------------------------------- sozlamalar
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
if not TOKEN or not WEBHOOK_SECRET:
    raise RuntimeError("BOT_TOKEN va WEBHOOK_SECRET muhit o'zgaruvchilari shart.")

API = f"https://api.telegram.org/bot{TOKEN}/"
DB_FILE = os.getenv("DB_FILE", "harajat.db")
MINIAPP_URL = os.getenv("MINIAPP_URL", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ALLOWED = {
    int(x) for x in os.getenv("ALLOWED_CHATS", "").replace(" ", "").split(",")
    if x.lstrip("-").isdigit()
}
TZ = timezone(timedelta(hours=5))
MAX_SUMMA = 10**12
MAX_NOTE = 60
INITDATA_MAX_AGE = 24 * 3600  # Mini App ochilgan "chipta" 24 soatgacha amal qiladi

CATEGORIES = {
    "oziq": "🛒 Oziq-ovqat", "kiyim": "👕 Kiyim", "poyabzal": "👟 Poyabzal",
    "kommunal": "💡 Kommunal", "transport": "🚗 Transport", "sogliq": "💊 Sog'liq",
    "talim": "🎓 Ta'lim", "boshqa": "📦 Boshqa",
}
ALIASES = {
    "oziq": "oziq", "oziqovqat": "oziq", "oziq-ovqat": "oziq", "ovqat": "oziq",
    "kiyim": "kiyim", "poyabzal": "poyabzal", "kommunal": "kommunal",
    "transport": "transport", "sogliq": "sogliq", "talim": "talim", "boshqa": "boshqa",
}
CURRENCIES = {"UZS": "so'm", "USD": "$", "RUB": "₽", "EUR": "€"}
CURRENCY_WORDS = {
    "som": "UZS", "so'm": "UZS", "sum": "UZS", "uzs": "UZS", "so'mda": "UZS",
    "dollar": "USD", "dollarda": "USD", "doll": "USD", "usd": "USD", "$": "USD",
    "euro": "EUR", "evro": "EUR", "eur": "EUR", "€": "EUR", "yevro": "EUR",
    "rubl": "RUB", "rublda": "RUB", "rub": "RUB", "₽": "RUB",
}
# Jonli kurs olib bo'lmasa ishlatiladigan zaxira qiymatlar (1 chet el puli necha so'm)
FALLBACK_RATES = {"UZS": 1.0, "USD": 12000.0, "EUR": 13000.0, "RUB": 140.0}
_rate_cache = {"ts": 0.0, "rates": {}}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("karmon-backend")

http_client = httpx.Client(timeout=20)


def get_rates():
    """1 dollar/yevro/rubl necha so'mligini qaytaradi, 6 soatga keshlaydi."""
    if time.time() - _rate_cache["ts"] < 6 * 3600 and _rate_cache["rates"]:
        return _rate_cache["rates"]
    try:
        r = http_client.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        data = r.json()
        if data.get("result") == "success":
            usd_to_uzs = data["rates"]["UZS"]
            rates = {
                "UZS": 1.0,
                "USD": usd_to_uzs,
                "EUR": usd_to_uzs / data["rates"]["EUR"],
                "RUB": usd_to_uzs / data["rates"]["RUB"],
            }
            _rate_cache.update(ts=time.time(), rates=rates)
            return rates
    except (httpx.HTTPError, ValueError, KeyError, ZeroDivisionError):
        log.exception("Valyuta kursini olishda xato, zaxira qiymatlar ishlatiladi")
    return _rate_cache["rates"] or FALLBACK_RATES


def convert_to_uzs(amount, currency):
    if currency == "UZS":
        return round(amount)
    rate = get_rates().get(currency)
    if not rate:
        return round(amount)
    return round(amount * rate)


def convert_from_uzs(amount_uzs, currency):
    if currency == "UZS":
        return amount_uzs
    rate = get_rates().get(currency)
    if not rate:
        return amount_uzs
    return amount_uzs / rate

# ------------------------------------------------------------ ma'lumotlar bazasi
db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        user_name TEXT NOT NULL,
        category TEXT NOT NULL,
        amount INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        orig_amount REAL,
        orig_currency TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_expenses_chat ON expenses(chat_id, created_at);
    CREATE TABLE IF NOT EXISTS settings (
        chat_id INTEGER PRIMARY KEY,
        budget INTEGER NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'UZS'
    );
    CREATE TABLE IF NOT EXISTS states (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        state TEXT NOT NULL,
        data TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (chat_id, user_id)
    );
    """
)
# Eski bazadan o'tish: yangi ustunlar yo'q bo'lsa qo'shiladi
_existing_cols = {r["name"] for r in db.execute("PRAGMA table_info(expenses)")}
if "orig_amount" not in _existing_cols:
    db.execute("ALTER TABLE expenses ADD COLUMN orig_amount REAL")
if "orig_currency" not in _existing_cols:
    db.execute("ALTER TABLE expenses ADD COLUMN orig_currency TEXT")
db.commit()

# --------------------------------------------------------------------- yordamchi
def fmt(n, currency="UZS"):
    return f"{int(n):,}".replace(",", " ") + " " + CURRENCIES.get(currency, currency)


def now():
    return datetime.now(TZ)


def month_now():
    return now().strftime("%Y-%m")


def month_shift(month, delta):
    year, mon = map(int, month.split("-"))
    mon += delta
    year += (mon - 1) // 12
    mon = (mon - 1) % 12 + 1
    return f"{year:04d}-{mon:02d}"


def get_settings(chat_id):
    row = db.execute("SELECT budget, currency FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    return {"budget": row["budget"], "currency": row["currency"]} if row else {"budget": 0, "currency": "UZS"}


def set_settings(chat_id, budget=None, currency=None):
    current = get_settings(chat_id)
    budget = current["budget"] if budget is None else budget
    currency = current["currency"] if currency is None else currency
    with db:
        db.execute(
            "INSERT INTO settings (chat_id, budget, currency) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET budget=excluded.budget, currency=excluded.currency",
            (chat_id, budget, currency),
        )


def add_expense(chat_id, user_id, user_name, category, amount, note="",
                 orig_amount=None, orig_currency=None):
    with db:
        db.execute(
            "INSERT INTO expenses (chat_id, user_id, user_name, category, amount, note, "
            "created_at, orig_amount, orig_currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, user_name, category, amount, note,
             now().strftime("%Y-%m-%d %H:%M:%S"), orig_amount, orig_currency),
        )


def month_total(chat_id, month=None):
    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS t FROM expenses WHERE chat_id=? AND created_at LIKE ?",
        (chat_id, (month or month_now()) + "%"),
    ).fetchone()
    return row["t"]


def get_state(chat_id, user_id):
    row = db.execute("SELECT state, data FROM states WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    if not row:
        return "menyu", {}
    try:
        return row["state"], json.loads(row["data"] or "{}")
    except ValueError:
        return row["state"], {}


def set_state(chat_id, user_id, state, data=None):
    with db:
        if state == "menyu":
            db.execute("DELETE FROM states WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        else:
            db.execute(
                "INSERT OR REPLACE INTO states VALUES (?, ?, ?, ?)",
                (chat_id, user_id, state, json.dumps(data or {})),
            )


_QUICK = re.compile(r"(\d{1,3}(?: \d{3})+|\d+(?:[.,]\d+)?)(ming|mln|k|m)?(?=\s|$)")


def parse_quick(text):
    m = _QUICK.match(text.strip())
    if not m:
        return None
    raw, suffix = m.group(1), m.group(2)
    if suffix:
        value = float(raw.replace(" ", "").replace(",", ".")) * (1000 if suffix in ("k", "ming") else 1_000_000)
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        value = int(re.sub(r"[.,]", "", raw))
    elif re.fullmatch(r"[\d ]+", raw):
        value = int(raw.replace(" ", ""))
    else:
        return None
    amount = round(value)
    if amount > MAX_SUMMA:
        return None
    return amount, text.strip()[m.end():].strip()


def norm(word):
    return re.sub(r"['’ʻʼ`]", "", word.lower())


def split_category(rest):
    if not rest:
        return None, ""
    first, _, tail = rest.partition(" ")
    category = ALIASES.get(norm(first))
    if category:
        return category, tail.strip()[:MAX_NOTE]
    return None, rest[:MAX_NOTE]


def extract_currency(rest):
    """'dollar oziq non' -> ('USD', 'oziq non'); topilmasa ('UZS', rest o'zgarishsiz)."""
    if not rest:
        return "UZS", rest
    first, _, tail = rest.partition(" ")
    code = CURRENCY_WORDS.get(norm(first))
    if code:
        return code, tail.strip()
    return "UZS", rest


AI_SYSTEM_PROMPT = (
    "Sen oilaviy harajatlar botining yordamchisisan. Foydalanuvchi o'zbek tilida "
    "(lotin yoki kirill) erkin yozgan xabardan xarajat ma'lumotini ajratib olasan.\n\n"
    "Javobni FAQAT quyidagi JSON formatida ber, boshqa hech qanday matn, izoh yoki "
    "``` belgilarisiz:\n"
    '{"amount": <yozilgan xom son>, "currency": "<UZS|USD|EUR|RUB>", '
    '"category": "<kategoriya kaliti>", "note": "<qisqa izoh>"}\n\n'
    "amount — foydalanuvchi yozgan asl sondagi qiymat, hech qanday kursga o'girmasdan "
    "(masalan \"100 dollar\" -> amount: 100, currency: \"USD\"; hech qanday valyuta "
    "aytilmasa currency: \"UZS\").\n"
    "Kategoriya kaliti FAQAT shulardan biri bo'lishi kerak: " + ", ".join(CATEGORIES) + ".\n"
    "Kategoriya moslashtirish namunalari: taksi/avtobus/benzin -> transport; "
    "non/bozor/supermarket -> oziq; dorixona/shifokor -> sogliq; "
    "kurs/kitob/maktab -> talim; svet/gaz/suv/internet -> kommunal; "
    "ko'ylak/tufli -> kiyim/poyabzal; boshqa hech qayerga to'g'ri kelmasa -> boshqa.\n"
    "note — 3-4 so'zdan oshmasin. Agar summani topa olmasang yoki xabar xarajat haqida "
    "bo'lmasa, amount ni 0 qil."
)

def ai_parse_expense(text):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        r = http_client.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL, "max_tokens": 200, "temperature": 0,
                "system": AI_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": text[:500]}],
            },
        )
        data = r.json()
    except (httpx.HTTPError, ValueError):
        log.exception("AI so'rovi muvaffaqiyatsiz")
        return None
    if "content" not in data:
        log.warning("AI xatosi: %s", data.get("error", data))
        return None
    raw = "".join(b.get("text", "") for b in data["content"]).strip().strip("`")
    if raw.lower().startswith("json"):
        raw = raw[4:].strip()
    try:
        parsed = json.loads(raw)
        amount = float(parsed.get("amount") or 0)
        category = parsed.get("category")
        currency = parsed.get("currency") or "UZS"
        note = str(parsed.get("note") or "").strip()[:MAX_NOTE]
    except (ValueError, TypeError, AttributeError):
        return None
    if category not in CATEGORIES or amount <= 0:
        return None
    if currency not in CURRENCIES:
        currency = "UZS"
    amount_uzs = convert_to_uzs(amount, currency)
    if amount_uzs <= 0 or amount_uzs > MAX_SUMMA:
        return None
    result = {"amount": amount_uzs, "category": category, "note": note}
    if currency != "UZS":
        result["orig_amount"] = amount
        result["orig_currency"] = currency
    return result


def tg(method, payload=None):
    try:
        r = http_client.post(API + method, json=payload or {})
        return r.json()
    except (httpx.HTTPError, ValueError):
        log.exception("Telegram so'rovi xato: %s", method)
        return {"ok": False}


def send(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard is not None:
        payload["reply_markup"] = keyboard
    result = tg("sendMessage", payload)
    if not result.get("ok"):
        log.warning("sendMessage xatosi: %s", result.get("description"))


def main_menu(chat_id):
    url = f"{MINIAPP_URL}?chat={chat_id}" if MINIAPP_URL else None
    row = [{"text": "📊 Ilovani ochish", "web_app": {"url": url}}] if url else [{"text": "📊 Hisobot"}]
    return {
        "keyboard": [row, [{"text": "➕ Harajat qo'shish"}], [{"text": "💰 Byudjet"}]],
        "resize_keyboard": True,
    }


CATEGORY_KEYBOARD = {
    "inline_keyboard": [
        [{"text": name, "callback_data": f"cat:{key}"} for key, name in list(CATEGORIES.items())[i:i + 2]]
        for i in range(0, len(CATEGORIES), 2)
    ]
}


def save_and_reply(chat_id, user_id, user_name, category, amount, note="",
                    orig_amount=None, orig_currency=None):
    add_expense(chat_id, user_id, user_name, category, amount, note, orig_amount, orig_currency)
    set_state(chat_id, user_id, "menyu")
    settings = get_settings(chat_id)
    disp = settings["currency"]
    total = month_total(chat_id)
    extra = f" · {note}" if note else ""
    orig_line = (f" ({fmt(orig_amount, orig_currency)})" if orig_currency and orig_currency != disp else "")
    text = (f"✅ Saqlandi ({user_name}): {CATEGORIES[category]}{extra} — "
            f"{fmt(convert_from_uzs(amount, disp), disp)}{orig_line}\n"
            f"Shu oy jami: {fmt(convert_from_uzs(total, disp), disp)}")
    if settings["budget"]:
        left = settings["budget"] - convert_from_uzs(total, disp)
        text += (f"\n⚠️ Byudjetdan {fmt(-left, disp)} oshib ketdi!" if left < 0
                 else f"\nByudjetdan qolgan: {fmt(left, disp)}")
    send(chat_id, text, main_menu(chat_id))


# ------------------------------------------------------------- Telegram xabarlari
def handle_message(msg):
    text = (msg.get("text") or "").strip()
    if not text or "from" not in msg:
        return
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    user_name = msg["from"].get("first_name", "Noma'lum")[:30]

    if ALLOWED and chat_id not in ALLOWED:
        send(chat_id, f"⛔ Bu bot yopiq. Chat ID: {chat_id}")
        return

    if text.startswith("/"):
        text = text.split()[0].split("@")[0].lower()
    text = {"/qosh": "➕ Harajat qo'shish", "/byudjet": "💰 Byudjet"}.get(text, text)

    state, data = get_state(chat_id, user_id)
    if text in ("➕ Harajat qo'shish", "💰 Byudjet", "📊 Hisobot"):
        state = "menyu"
        set_state(chat_id, user_id, "menyu")

    if text == "/start":
        set_state(chat_id, user_id, "menyu")
        send(chat_id, "Assalomu alaykum! 👋\nHarajatlaringizni yozing yoki 📊 Ilovani oching.",
             main_menu(chat_id))
    elif text == "/bekor":
        set_state(chat_id, user_id, "menyu")
        send(chat_id, "Bekor qilindi.", main_menu(chat_id))
    elif state == "summa":
        parsed = parse_quick(text)
        if parsed and parsed[0] > 0:
            currency, note_rest = extract_currency(parsed[1])
            amount_uzs = convert_to_uzs(parsed[0], currency)
            orig_amount = parsed[0] if currency != "UZS" else None
            orig_currency = currency if currency != "UZS" else None
            save_and_reply(chat_id, user_id, user_name, data["cat"], amount_uzs,
                            note_rest[:MAX_NOTE], orig_amount, orig_currency)
        else:
            send(chat_id, "Summani yuboring, masalan: 50000 yoki 100 dollar (bekor qilish: /bekor).")
    elif state == "byudjet":
        parsed = parse_quick(text)
        if parsed and not parsed[1]:
            set_settings(chat_id, budget=parsed[0])
            set_state(chat_id, user_id, "menyu")
            send(chat_id, f"✅ Oylik byudjet: {fmt(parsed[0], get_settings(chat_id)['currency'])}",
                 main_menu(chat_id))
        else:
            send(chat_id, "Iltimos, faqat summa yuboring (limit kerak bo'lmasa 0).")
    elif state == "tanlash":
        send(chat_id, "Yuqoridagi tugmalardan kategoriyani tanlang (bekor qilish: /bekor).")
    elif text == "➕ Harajat qo'shish":
        set_state(chat_id, user_id, "tanlash")
        send(chat_id, "Kategoriyani tanlang 👇", CATEGORY_KEYBOARD)
    elif text == "💰 Byudjet":
        set_state(chat_id, user_id, "byudjet")
        send(chat_id, "Oylik byudjetni yuboring (limit kerak bo'lmasa 0):")
    else:
        parsed = parse_quick(text)
        if parsed and parsed[0] > 0:
            currency, rest = extract_currency(parsed[1])
            category, note = split_category(rest)
            amount_uzs = convert_to_uzs(parsed[0], currency)
            orig_amount = parsed[0] if currency != "UZS" else None
            orig_currency = currency if currency != "UZS" else None
            if category:
                save_and_reply(chat_id, user_id, user_name, category, amount_uzs, note,
                                orig_amount, orig_currency)
            else:
                set_state(chat_id, user_id, "kategoriya", {
                    "amount": amount_uzs, "note": note,
                    "orig_amount": orig_amount, "orig_currency": orig_currency,
                })
                label = fmt(parsed[0], currency) if orig_currency else fmt(amount_uzs)
                send(chat_id, f"{label} — qaysi kategoriya?", CATEGORY_KEYBOARD)
        elif ANTHROPIC_API_KEY and len(text) >= 4:
            tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            result = ai_parse_expense(text)
            if result:
                save_and_reply(chat_id, user_id, user_name, result["category"], result["amount"],
                                result["note"], result.get("orig_amount"), result.get("orig_currency"))
            else:
                send(chat_id, "Tushunolmadim 🤔 Masalan: \"taksiga 15000 sarfladim\" yoki "
                              "\"100 dollar transport\" deb yozing.")
        else:
            send(chat_id, "📊 Ilovani oching yoki summani yozing 👇", main_menu(chat_id))


def handle_callback(cb):
    message = cb.get("message")
    data = cb.get("data", "")
    if not message:
        return
    chat_id = message["chat"]["id"]
    user_id = cb["from"]["id"]
    user_name = cb["from"].get("first_name", "Noma'lum")[:30]
    tg("answerCallbackQuery", {"callback_query_id": cb["id"]})

    if data.startswith("cat:") and data[4:] in CATEGORIES:
        category = data[4:]
        state, saved = get_state(chat_id, user_id)
        if state == "kategoriya" and "amount" in saved:
            save_and_reply(chat_id, user_id, user_name, category, saved["amount"], saved.get("note", ""),
                            saved.get("orig_amount"), saved.get("orig_currency"))
        else:
            set_state(chat_id, user_id, "summa", {"cat": category})
            send(chat_id, f"{CATEGORIES[category]}\nSummani yuboring (masalan: 50000):")


# ------------------------------------------------------------------------- FastAPI
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[MINIAPP_URL] if MINIAPP_URL else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def webhook(request: Request):
    update = await request.json()
    try:
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception:
        log.exception("Yangilanishni qayta ishlashda xato")
    return {"ok": True}


@app.get("/")
async def health():
    return {"status": "ok"}


# --------------------------------------------------------- Mini App uchun autentifikatsiya
def verify_init_data(init_data: str):
    """Telegram WebApp initData imzosini tekshiradi va chat_id'ni qaytaradi.

    Bu funksiya so'rov haqiqatan ham Telegram orqali kelganini tasdiqlaydi —
    soxta so'rovlar (boshqa odam ma'lumotlaringizni ko'rishi) shu yerda to'xtatiladi.
    """
    if not init_data:
        raise HTTPException(401, "initData yo'q")
    parsed = dict(urllib.parse.parse_qsl(init_data))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "hash yo'q")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(401, "Imzo mos emas")

    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > INITDATA_MAX_AGE:
        raise HTTPException(401, "Sessiya eskirgan, ilovani qayta oching")

    user = json.loads(parsed.get("user", "{}"))
    chat_id = user.get("id")
    if not chat_id:
        raise HTTPException(401, "Foydalanuvchi topilmadi")
    if ALLOWED and chat_id not in ALLOWED:
        raise HTTPException(403, "Ruxsat yo'q")
    return chat_id, user.get("first_name", "Foydalanuvchi")


def auth(x_telegram_init_data: str = Header(default="")):
    return verify_init_data(x_telegram_init_data)


# --------------------------------------------------------------------------- API
class ExpenseIn(BaseModel):
    amount: float
    category: str
    note: str = ""
    currency: str = "UZS"  # foydalanuvchi qaysi valyutada kiritdi ("Qo'shish" ekranidagi tugma)


class SettingsIn(BaseModel):
    budget: int | None = None
    currency: str | None = None


@app.get("/api/rates")
def api_rates(x_telegram_init_data: str = Header(default="")):
    """1 dollar/yevro/rubl necha so'mligini qaytaradi (Sozlamalar ekranida ko'rsatish uchun)."""
    verify_init_data(x_telegram_init_data)
    return get_rates()


@app.get("/api/summary")
def api_summary(month: str = None, x_telegram_init_data: str = Header(default="")):
    chat_id, _ = verify_init_data(x_telegram_init_data)
    month = month or month_now()
    like = month + "%"
    rows = db.execute(
        "SELECT category, SUM(amount) AS s, COUNT(*) AS c FROM expenses "
        "WHERE chat_id=? AND created_at LIKE ? GROUP BY category ORDER BY s DESC",
        (chat_id, like),
    ).fetchall()
    people = db.execute(
        "SELECT user_name, SUM(amount) AS s, COUNT(*) AS c FROM expenses "
        "WHERE chat_id=? AND created_at LIKE ? GROUP BY user_id ORDER BY s DESC",
        (chat_id, like),
    ).fetchall()
    total_uzs = sum(r["s"] for r in rows)
    settings = get_settings(chat_id)
    disp = settings["currency"]
    prev_month, next_month = month_shift(month, -1), month_shift(month, 1)
    return {
        "month": month,
        "prev_month": prev_month,
        "next_month": next_month if month < month_now() else None,
        "currency": disp,
        "budget": settings["budget"],
        "total": convert_from_uzs(total_uzs, disp),
        "by_category": [
            {"key": r["category"], "label": CATEGORIES.get(r["category"], r["category"]),
             "amount": convert_from_uzs(r["s"], disp), "count": r["c"]}
            for r in rows
        ],
        "by_user": (
            [{"name": p["user_name"], "amount": convert_from_uzs(p["s"], disp), "count": p["c"]} for p in people]
            if len(people) > 1 else []
        ),
        "top_category": (CATEGORIES.get(rows[0]["category"]) if rows else None),
        "daily_avg": convert_from_uzs(total_uzs / now().day, disp) if month == month_now() and total_uzs else None,
    }


@app.get("/api/expenses")
def api_expenses(month: str = None, limit: int = 30, x_telegram_init_data: str = Header(default="")):
    chat_id, _ = verify_init_data(x_telegram_init_data)
    like = (month or month_now()) + "%"
    settings = get_settings(chat_id)
    disp = settings["currency"]
    rows = db.execute(
        "SELECT id, category, amount, note, created_at, user_name, orig_amount, orig_currency FROM expenses "
        "WHERE chat_id=? AND created_at LIKE ? ORDER BY id DESC LIMIT ?",
        (chat_id, like, limit),
    ).fetchall()
    return [
        {"id": r["id"], "category": r["category"], "label": CATEGORIES.get(r["category"], r["category"]),
         "amount": convert_from_uzs(r["amount"], disp), "currency": disp,
         "orig_amount": r["orig_amount"], "orig_currency": r["orig_currency"],
         "note": r["note"], "date": r["created_at"], "user": r["user_name"]}
        for r in rows
    ]


@app.post("/api/expenses")
def api_add_expense(body: ExpenseIn, x_telegram_init_data: str = Header(default="")):
    chat_id, user_name = verify_init_data(x_telegram_init_data)
    if body.category not in CATEGORIES:
        raise HTTPException(400, "Noto'g'ri kategoriya")
    if body.currency not in CURRENCIES:
        raise HTTPException(400, "Noto'g'ri valyuta")
    if body.amount <= 0:
        raise HTTPException(400, "Noto'g'ri summa")
    amount_uzs = convert_to_uzs(body.amount, body.currency)
    if amount_uzs <= 0 or amount_uzs > MAX_SUMMA:
        raise HTTPException(400, "Noto'g'ri summa")
    orig_amount = body.amount if body.currency != "UZS" else None
    orig_currency = body.currency if body.currency != "UZS" else None
    add_expense(chat_id, chat_id, user_name, body.category, amount_uzs, body.note[:MAX_NOTE],
                orig_amount, orig_currency)
    return {"ok": True, "amount_uzs": amount_uzs}


@app.put("/api/expenses/{expense_id}")
def api_edit_expense(expense_id: int, body: ExpenseIn, x_telegram_init_data: str = Header(default="")):
    chat_id, _ = verify_init_data(x_telegram_init_data)
    row = db.execute("SELECT id FROM expenses WHERE id=? AND chat_id=?", (expense_id, chat_id)).fetchone()
    if not row:
        raise HTTPException(404, "Topilmadi")
    if body.category not in CATEGORIES:
        raise HTTPException(400, "Noto'g'ri kategoriya")
    if body.currency not in CURRENCIES:
        raise HTTPException(400, "Noto'g'ri valyuta")
    if body.amount <= 0:
        raise HTTPException(400, "Noto'g'ri summa")
    amount_uzs = convert_to_uzs(body.amount, body.currency)
    if amount_uzs <= 0 or amount_uzs > MAX_SUMMA:
        raise HTTPException(400, "Noto'g'ri summa")
    orig_amount = body.amount if body.currency != "UZS" else None
    orig_currency = body.currency if body.currency != "UZS" else None
    with db:
        db.execute(
            "UPDATE expenses SET category=?, amount=?, note=?, orig_amount=?, orig_currency=? WHERE id=?",
            (body.category, amount_uzs, body.note[:MAX_NOTE], orig_amount, orig_currency, expense_id),
        )
    return {"ok": True, "amount_uzs": amount_uzs}


@app.delete("/api/expenses/{expense_id}")
def api_delete_expense(expense_id: int, x_telegram_init_data: str = Header(default="")):
    chat_id, _ = verify_init_data(x_telegram_init_data)
    with db:
        cur = db.execute("DELETE FROM expenses WHERE id=? AND chat_id=?", (expense_id, chat_id))
    if cur.rowcount == 0:
        raise HTTPException(404, "Topilmadi")
    return {"ok": True}


@app.get("/api/settings")
def api_get_settings(x_telegram_init_data: str = Header(default="")):
    chat_id, _ = verify_init_data(x_telegram_init_data)
    return {**get_settings(chat_id), "categories": CATEGORIES, "currencies": CURRENCIES}


@app.post("/api/settings")
def api_set_settings(body: SettingsIn, x_telegram_init_data: str = Header(default="")):
    chat_id, _ = verify_init_data(x_telegram_init_data)
    if body.currency is not None and body.currency not in CURRENCIES:
        raise HTTPException(400, "Noto'g'ri valyuta")
    set_settings(chat_id, budget=body.budget, currency=body.currency)
    return get_settings(chat_id)


@app.get("/api/top")
def api_top(months: int = 3, x_telegram_init_data: str = Header(default="")):
    """Oxirgi N oy bo'yicha eng ko'p sarflangan kategoriyalar."""
    chat_id, _ = verify_init_data(x_telegram_init_data)
    disp = get_settings(chat_id)["currency"]
    since = (now().replace(day=1) - timedelta(days=30 * (months - 1))).strftime("%Y-%m")
    rows = db.execute(
        "SELECT category, SUM(amount) AS s FROM expenses "
        "WHERE chat_id=? AND substr(created_at,1,7) >= ? GROUP BY category ORDER BY s DESC LIMIT 5",
        (chat_id, since),
    ).fetchall()
    return [{"key": r["category"], "label": CATEGORIES.get(r["category"], r["category"]),
             "amount": convert_from_uzs(r["s"], disp)}
            for r in rows]


# --------------------------------------------------------------------- CSV eksport
# Mini App ichida oddiy havola CSV yuklab bo'lmaydi (initData sarlavhasi shart),
# shuning uchun avval qisqa umrli token olinadi, keyin o'sha token bilan brauzerda ochiladi.
_export_tokens = {}  # token -> (chat_id, muddati)
EXPORT_TOKEN_TTL = 300  # 5 daqiqa


@app.get("/api/export/token")
def api_export_token(x_telegram_init_data: str = Header(default="")):
    chat_id, _ = verify_init_data(x_telegram_init_data)
    token = secrets.token_urlsafe(24)
    _export_tokens[token] = (chat_id, time.time() + EXPORT_TOKEN_TTL)
    # eskirgan tokenlarni tozalab turamiz
    for t, (_, exp) in list(_export_tokens.items()):
        if exp < time.time():
            _export_tokens.pop(t, None)
    return {"token": token}


def csv_safe(value):
    """Excel'da formula sifatida ishlab ketmasligi uchun."""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


@app.get("/api/export")
def api_export(token: str):
    entry = _export_tokens.get(token)
    if not entry or entry[1] < time.time():
        raise HTTPException(401, "Havola eskirgan, ilovada qaytadan urinib ko'ring")
    chat_id = entry[0]
    rows = db.execute(
        "SELECT created_at, user_name, category, note, amount, orig_amount, orig_currency "
        "FROM expenses WHERE chat_id=? ORDER BY id",
        (chat_id,),
    ).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Sana", "Kim", "Kategoriya", "Izoh", "Summa (so'm)", "Asl summa", "Asl valyuta"])
    for r in rows:
        writer.writerow([
            r["created_at"], csv_safe(r["user_name"]), CATEGORIES.get(r["category"], r["category"]),
            csv_safe(r["note"]), r["amount"], r["orig_amount"] or "", r["orig_currency"] or "",
        ])
    data = buf.getvalue().encode("utf-8-sig")  # Excel'da o'zbekcha harflar to'g'ri chiqishi uchun
    return Response(
        content=data, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=harajatlar.csv"},
    )
