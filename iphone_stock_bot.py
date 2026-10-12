"""
Перевірка наявності iPhone в Apple Store (Канада) з повідомленнями в Telegram.

Запуск:
    python iphone_stock_bot.py            # безкінечний цикл
    python iphone_stock_bot.py --once     # одна перевірка + повідомлення
    python iphone_stock_bot.py --dry-run  # одна перевірка, друк у консоль без Telegram

Залежностей немає — тільки стандартна бібліотека Python 3.8+.
"""

import argparse
import html
import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from logging.handlers import RotatingFileHandler

# ============================== НАЛАШТУВАННЯ ==============================

def load_dotenv(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Читає KEY=VALUE з .env поруч зі скриптом (для локального запуску). Змінні середовища мають пріоритет."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

# Секрети НЕ зберігаються в коді: локально — у файлі .env, на GitHub — у Secrets.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# SKU звірені з apple.com/ca/shop/buy-iphone/iphone-18-pro (суфікс VC/A = Канада).
# Формат назви: "<модель> <пам'ять> <колір>" — останнє слово використовується як колір.
MODELS_TO_CHECK = {
    "iPhone 18 Pro 256GB Black": "MJR54VC/A",
    "iPhone 18 Pro 256GB Silver": "MJR64VC/A",
    "iPhone 18 Pro 256GB Burgundy": "MJR74VC/A",
    "iPhone 18 Pro 256GB Glacier": "MJR84VC/A",
    "iPhone 18 Pro Max 512GB Burgundy": "MJXC4VC/A",
}

# Поштові індекси, біля яких шукати. Apple повертає ~10 найближчих магазинів на кожен.
# Кожен індекс = 1 запит до Apple за цикл (всі моделі йдуть в одному запиті).
# Для M5V 3L9 у відповідь потрапляють усі магазини з TARGET_STORES.
LOCATIONS = [
    "M5V 3L9",  # Торонто
]

# Магазини, які показувати (назва як на сайті Apple або номер R###), у цьому ж порядку.
# Порожньо = всі магазини, які повернула Apple.
TARGET_STORES = [
    "Eaton Centre",
    "Yorkdale",
    "Sherway Gardens",
    "Fairview",
    "Square One",                # Mississauga
    "Markville Shopping Centre", # Markham
]

BUY_URL = "https://www.apple.com/ca/shop/buy-iphone/iphone-18-pro"

COLOR_EMOJI = {
    "black": "⚫",
    "silver": "⚪",
    "burgundy": "🍷",
    "glacier": "🧊",
}

CHECK_INTERVAL_SEC = 5 * 60   # пауза між циклами перевірки
REQUEST_DELAY_SEC = (8, 15)   # випадкова пауза між запитами до Apple в межах циклу
REPORT_INTERVAL_SEC = 60 * 60 # як часто слати повний звіт, навіть якщо нічого не змінилось
ALERT_ON_CHANGE = True        # одразу писати, коли щось з'явилось/зникло
MAX_BACKOFF_SEC = 30 * 60     # максимальна пауза після блокування Apple
REQUEST_TIMEOUT_SEC = 30      # скільки чекати відповіді Apple
RETRIES = 2                   # повторні спроби при таймауті/мережевій помилці
RETRY_DELAY_SEC = (20, 40)    # пауза перед повторною спробою
ERROR_ALERT_AFTER = 3         # після скількох невдалих циклів підряд написати в Telegram (0 = ніколи)

# Логи: logs/bot.log поруч зі скриптом, ротація 5 файлів по 5 МБ.
# У файл пишеться все (DEBUG), у консоль — тільки основне (INFO).
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# ==========================================================================

APPLE_URL = "https://www.apple.com/ca/shop/retail/pickup-message"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
# 541 — так Apple відповідає, коли обмежує запити; 429/503 — стандартні аналоги.
RATE_LIMIT_CODES = {429, 503, 541}


class RateLimited(Exception):
    pass


logger = logging.getLogger("iphone_bot")


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "bot.log"), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)

    logger.setLevel(logging.DEBUG)
    logger.handlers[:] = [file_handler, console]


def log(msg):
    logger.info(msg)


def describe_error(e):
    """Коротко і зрозуміло: тип помилки + причина."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code} {e.reason}"
    if isinstance(e, urllib.error.URLError):
        return f"мережа: {type(e.reason).__name__}: {e.reason}"
    return f"{type(e).__name__}: {e}"


def fetch_once(location):
    params = [("pl", "true"), ("location", location)]
    params += [(f"parts.{i}", part) for i, part in enumerate(MODELS_TO_CHECK.values())]
    url = f"{APPLE_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-CA,en;q=0.9",
        "Referer": "https://www.apple.com/ca/shop/buy-iphone",
    })
    logger.debug(f"GET {url}")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            raw = resp.read()
            logger.debug(f"  -> HTTP {resp.status}, {len(raw)} байт, {time.monotonic() - started:.1f} с")
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", errors="replace")
        logger.debug(f"  -> HTTP {e.code} за {time.monotonic() - started:.1f} с, тіло: {body!r}")
        if e.code in RATE_LIMIT_CODES:
            raise RateLimited(f"HTTP {e.code}") from e
        raise
    except Exception as e:
        logger.debug(f"  -> {describe_error(e)} після {time.monotonic() - started:.1f} с")
        raise

    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        logger.debug(f"  -> не JSON, початок відповіді: {raw[:300]!r}")
        raise


def fetch_availability(location):
    """Повертає {store_key: {"name", "city", "parts": {part: (available, quote)}}}."""
    for attempt in range(1, RETRIES + 2):
        try:
            data = fetch_once(location)
            break
        except RateLimited:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            if attempt > RETRIES:
                raise
            delay = random.uniform(*RETRY_DELAY_SEC)
            logger.warning(f"Спроба {attempt}/{RETRIES + 1} невдала ({describe_error(e)}). Повтор через {delay:.0f} с")
            time.sleep(delay)

    result = {}
    for store in data.get("body", {}).get("stores", []):
        parts = {}
        for part, info in (store.get("partsAvailability") or {}).items():
            available = info.get("pickupDisplay") == "available"
            quote = info.get("pickupSearchQuote", "")
            parts[part] = (available, quote)
        result[store.get("storeNumber")] = {
            "name": store.get("storeName", "?"),
            "city": store.get("city", ""),
            "parts": parts,
        }
    return result


def target_index(store_no, store):
    """Позиція магазину в TARGET_STORES або None, якщо він не потрібен."""
    if not TARGET_STORES:
        return 0
    for i, wanted in enumerate(TARGET_STORES):
        if wanted.lower() in (store_no.lower(), store["name"].lower()):
            return i
    return None


def check_all():
    """Опитує всі LOCATIONS з паузами. Повертає магазини в порядку TARGET_STORES."""
    found = {}
    for i, location in enumerate(LOCATIONS):
        if i > 0:
            time.sleep(random.uniform(*REQUEST_DELAY_SEC))
        for store_no, store in fetch_availability(location).items():
            idx = target_index(store_no, store)
            if idx is not None:
                found[store_no] = (idx, store)
    if TARGET_STORES and len(found) < len(TARGET_STORES):
        names = {s["name"].lower() for _, s in found.values()} | {n.lower() for n in found}
        missing = [t for t in TARGET_STORES if t.lower() not in names]
        logger.warning(f"Apple не повернула магазини {missing} — додайте ближчий індекс у LOCATIONS")
    return {no: s for no, (_, s) in sorted(found.items(), key=lambda kv: kv[1][0])}


def in_stock_set(stores):
    return {
        (store_no, part)
        for store_no, s in stores.items()
        for part, (available, _) in s["parts"].items()
        if available
    }


def split_name(name):
    """'iPhone 18 Pro 256GB Black' -> ('iPhone 18 Pro 256GB', 'Black')."""
    model, _, color = name.rpartition(" ")
    return model, color


def color_emoji(color):
    return COLOR_EMOJI.get(color.lower(), "📱")


def stores_with(stores, part):
    return [s["name"] for s in stores.values() if s["parts"].get(part, (False,))[0]]


def buy_link():
    return f'🛒 <a href="{BUY_URL}">Купити на apple.com</a>'


def build_report(stores, title):
    total = len(TARGET_STORES) or len(stores)
    lines = [f"<b>{html.escape(title)}</b>", f"🕐 {datetime.now():%d.%m.%Y, %H:%M}"]

    # Групуємо кольори під спільною назвою моделі.
    groups = {}
    for name, part in MODELS_TO_CHECK.items():
        model, color = split_name(name)
        groups.setdefault(model, []).append((color, part))

    models_in_stock = 0
    for model, colors in groups.items():
        lines += ["", f"📱 <b>{html.escape(model)}</b>"]
        for color, part in colors:
            where = stores_with(stores, part)
            if where:
                models_in_stock += 1
                lines.append(f"{color_emoji(color)} {html.escape(color)} — ✅ <b>{len(where)}/{total}</b>")
                lines.append(f"      └ {html.escape(', '.join(where))}")
            else:
                lines.append(f"{color_emoji(color)} {html.escape(color)} — ❌ немає")

    lines += [
        "",
        "━━━━━━━━━━━━━━━",
        f"🟢 В наявності: <b>{models_in_stock}</b> з {len(MODELS_TO_CHECK)} моделей",
        f"🏬 Магазинів відповіло: {len(stores)}/{total}",
        buy_link(),
    ]
    return "\n".join(lines)


def build_change_alert(stores, appeared, gone):
    names = {part: name for name, part in MODELS_TO_CHECK.items()}

    def by_part(pairs, store_source):
        grouped = {}
        for store_no, part in pairs:
            grouped.setdefault(part, []).append(store_source.get(store_no, {"name": store_no})["name"])
        # порядок як у MODELS_TO_CHECK
        return [(p, grouped[p]) for p in names if p in grouped]

    lines = []
    if appeared:
        lines += ["🔔 <b>З'явився в наявності!</b>", ""]
        for part, where in by_part(appeared, stores):
            _, color = split_name(names[part])
            lines.append(f"{color_emoji(color)} <b>{html.escape(names[part])}</b>")
            lines.append(f"📍 {html.escape(', '.join(where))}")
            lines.append("")
    if gone:
        lines += ["🔕 <b>Закінчився</b>", ""]
        for part, where in by_part(gone, stores):
            _, color = split_name(names[part])
            lines.append(f"{color_emoji(color)} {html.escape(names[part])}")
            lines.append(f"      └ {html.escape(', '.join(where))}")
        lines.append("")
    lines.append(buy_link())
    return "\n".join(lines)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Ліміт Telegram — 4096 символів; ріжемо по рядках.
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 4000:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for chunk in chunks:
        body = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=30).read()
            logger.debug(f"Telegram: відправлено {len(chunk)} символів")
        except urllib.error.HTTPError as e:
            logger.error(f"Telegram помилка {e.code}: {e.read().decode(errors='replace')}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            logger.error(f"Telegram недоступний: {describe_error(e)}")


def log_details(stores):
    """Детальний стан по кожному магазину — тільки у файл."""
    names = {part: name for name, part in MODELS_TO_CHECK.items()}
    for store_no, s in stores.items():
        status = ", ".join(
            f"{names.get(part, part).removeprefix('iPhone ')}={'ТАК' if ok else 'ні'}"
            for part, (ok, _) in s["parts"].items()
        )
        logger.debug(f"  {store_no} {s['name']}: {status}")


def run(once=False, dry_run=False, max_runtime=0):
    notify = print if dry_run else send_telegram
    deadline = time.monotonic() + max_runtime if max_runtime else None
    prev = None
    last_report = 0.0
    backoff = CHECK_INTERVAL_SEC
    failures = 0

    log(f"Старт. Моделей: {len(MODELS_TO_CHECK)}, магазинів: {len(TARGET_STORES) or 'всі'}, "
        f"інтервал {CHECK_INTERVAL_SEC // 60} хв. Лог: {os.path.join(LOG_DIR, 'bot.log')}")

    while True:
        error = None
        try:
            started = time.monotonic()
            stores = check_all()
            backoff = CHECK_INTERVAL_SEC
            current = in_stock_set(stores)
            log(f"OK: магазинів {len(stores)}, позицій в наявності {len(current)} "
                f"({time.monotonic() - started:.1f} с)")
            log_details(stores)
            if failures:
                log(f"Відновлено після {failures} невдалих циклів")
            failures = 0

            now = time.time()
            if once or now - last_report >= REPORT_INTERVAL_SEC:
                logger.info("Відправляю звіт")
                notify(build_report(stores, "📦 Наявність iPhone в Apple Store"))
                last_report = now
            elif ALERT_ON_CHANGE and prev is not None and current != prev:
                logger.info(f"Зміна: з'явилось {len(current - prev)}, зникло {len(prev - current)}")
                notify(build_change_alert(stores, current - prev, prev - current))
            prev = current
            sleep_for = CHECK_INTERVAL_SEC

        except RateLimited as e:
            backoff = min(backoff * 2, MAX_BACKOFF_SEC)
            error = f"Apple обмежує запити ({e})"
            logger.warning(f"{error}. Пауза {backoff // 60} хв.")
            sleep_for = backoff
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, ConnectionError) as e:
            error = describe_error(e)
            logger.error(f"Цикл не вдався після {RETRIES + 1} спроб: {error}")
            sleep_for = CHECK_INTERVAL_SEC
        except Exception as e:
            # Непередбачена помилка — пишемо повний traceback у лог, але бот не падає.
            error = describe_error(e)
            logger.exception(f"Непередбачена помилка: {error}")
            sleep_for = CHECK_INTERVAL_SEC

        if error:
            failures += 1
            if ERROR_ALERT_AFTER and failures == ERROR_ALERT_AFTER and not dry_run:
                send_telegram(f"⚠️ <b>Бот не може перевірити наявність</b>\n"
                              f"{failures} спроби підряд невдалі.\nОстання помилка: "
                              f"<code>{html.escape(error)}</code>")

        if once:
            return
        # невеликий джитер, щоб запити не йшли рівно за таймером
        sleep_for += random.uniform(0, 30)
        if deadline and time.monotonic() + sleep_for > deadline:
            log("Досягнуто --max-runtime, завершую роботу (наступний запуск підхопить розклад).")
            return
        logger.debug(f"Наступна перевірка через {sleep_for:.0f} с")
        time.sleep(sleep_for)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="одна перевірка і вихід")
    ap.add_argument("--dry-run", action="store_true", help="не слати в Telegram, друкувати в консоль")
    ap.add_argument("--max-runtime", type=int, default=0, metavar="SEC",
                    help="завершити роботу через SEC секунд (для GitHub Actions)")
    args = ap.parse_args()
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    setup_logging()
    if not args.dry_run and not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        logger.error("Не задано TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (файл .env або змінні середовища).")
        sys.exit(1)
    try:
        run(once=args.once or args.dry_run, dry_run=args.dry_run, max_runtime=args.max_runtime)
    except KeyboardInterrupt:
        log("Зупинено.")
