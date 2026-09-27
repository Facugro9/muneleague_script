"""
Pitch watcher bot for ATC Sports (atcsports.io) — Comu CABA.
 
Telegram commands
  /check 2026-10-03 15-17        watch a day + time window (default size, see DEFAULT_SPORT)
  /check 03/10 15-17 f5          same, day as DD/MM, Fútbol 5
  /check sab 15:00-17:00 all     next Saturday, any pitch size
  /now 03/10                     show everything free that day right now
  /list                          active watches
  /stop                          stop all watches   (/stop 2 -> stop watch #2)
  /help
 
How it works
  The venue page is a Next.js app: the free slots come embedded as JSON in the
  <script id="__NEXT_DATA__"> tag of the HTML. We read that JSON (no browser
  needed), keep the slots that START inside your window and END by its close,
  and message you once per newly-freed slot with a direct booking link.
  The club only opens bookings 6 days ahead; earlier than that the day shows
  no courts, so the bot simply keeps watching until the window opens.
 
Environment variables (Render → Environment)
  TELEGRAM_BOT_TOKEN   required
  OWNER_CHAT_ID        recommended: only this chat can use the bot, and active
                       watches survive restarts (saved in a pinned message)
  VENUE                default "comu-caba"
  DEFAULT_SPORT        default "f9" (f5, f6, f7, f8, f9 or all)
  CHECK_INTERVAL_MIN   default 5
  RENDER_EXTERNAL_URL  set automatically by Render; used to self-ping so the
                       free instance does not spin down
"""
 
import asyncio
import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote
from zoneinfo import ZoneInfo
 
import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes
 
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("pitchbot")
 
TZ = ZoneInfo("America/Argentina/Buenos_Aires")
BASE_URL = "https://atcsports.io"
VENUE = os.environ.get("VENUE", "comu-caba")
DEFAULT_SPORT = os.environ.get("DEFAULT_SPORT", "f9").lower()
CHECK_INTERVAL_MIN = float(os.environ.get("CHECK_INTERVAL_MIN", "5"))
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "").strip()
SLOT_MINUTES = 60
BOOKING_WINDOW_DAYS = 6
 
# ATC sport ids (from the site's own sport list)
SPORTS = {"f5": "2", "f6": "15", "f7": "3", "f8": "13", "f9": "4"}
SPORT_NAMES = {v: k.upper() for k, v in SPORTS.items()}
 
STATE_MARKER = "📌 PITCHBOT_STATE (don't delete)"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "es-AR,es;q=0.9",
}
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
WEEKDAYS = {"lun": 0, "mon": 0, "mar": 1, "tue": 1, "mie": 2, "mié": 2, "wed": 2,
            "jue": 3, "thu": 3, "vie": 4, "fri": 4, "sab": 5, "sáb": 5, "sat": 5,
            "dom": 6, "sun": 6}
 
 
# ---------------------------------------------------------------- scraping ---
 
def booking_url(day: str, hhmm: str | None = None, sport_id: str | None = None) -> str:
    url = f"{BASE_URL}/venues/{VENUE}?dia={day}"
    if sport_id:
        url += f"&sportIds={sport_id}"
    if hhmm:
        url += f"&horario={quote(hhmm)}"
    return url
 
 
def parse_slots(html: str) -> list[dict]:
    """Return every free slot in the page as dicts: court, sport_id, start, duration, price."""
    m = NEXT_DATA_RE.search(html)
    if not m:
        raise ValueError("__NEXT_DATA__ not found — the site layout may have changed")
    props = json.loads(m.group(1))["props"]["pageProps"]
    club = props.get("sportclub") or {}
    slots = []
    for court in club.get("available_courts") or []:
        for s in court.get("available_slots") or []:
            start = datetime.fromisoformat(s["start"])  # e.g. 2026-10-03T15:00-03:00
            price = (s.get("price") or {}).get("cents")
            slots.append({
                "court": court.get("name", "?"),
                "court_id": court.get("id"),
                "sport_id": (court.get("sport_ids") or ["?"])[0],
                "start": start,
                "duration": int(s.get("duration", 60)),
                "price": price / 100 if price is not None else None,
            })
    return slots
 
 
def fetch_slots(day: str) -> list[dict]:
    r = requests.get(booking_url(day), headers=HEADERS, timeout=25)
    r.raise_for_status()
    return parse_slots(r.text)
 
 
def matching_slots(slots: list[dict], watch: dict) -> list[dict]:
    lo, hi = watch["start"], watch["end"]  # minutes after midnight
    out = []
    for s in slots:
        if watch["sport"] != "all" and s["sport_id"] != SPORTS[watch["sport"]]:
            continue
        if s["duration"] != SLOT_MINUTES:
            continue
        st = s["start"].hour * 60 + s["start"].minute
        if lo <= st and st + s["duration"] <= hi:
            out.append(s)
    return sorted(out, key=lambda s: (s["start"], s["court"]))
 
 
# ----------------------------------------------------------------- parsing ---
 
def parse_day(text: str, today: date) -> date:
    t = text.strip().lower()
    if t in ("hoy", "today"):
        return today
    if t in ("mañana", "manana", "tomorrow"):
        return today + timedelta(days=1)
    if t[:3] in WEEKDAYS or t in WEEKDAYS:
        wd = WEEKDAYS.get(t, WEEKDAYS.get(t[:3]))
        return today + timedelta(days=(wd - today.weekday()) % 7)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        return date.fromisoformat(t)
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", t)
    if m:
        d, mo, y = int(m[1]), int(m[2]), m[3]
        if y:
            year = int(y) + (2000 if len(y) == 2 else 0)
            return date(year, mo, d)
        candidate = date(today.year, mo, d)
        return candidate if candidate >= today else date(today.year + 1, mo, d)
    raise ValueError(f"I can't read the date '{text}'")
 
 
def parse_hhmm(t: str) -> int:
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(?:h|hs)?", t.strip().lower())
    if not m:
        raise ValueError(f"I can't read the time '{t}'")
    h, mi = int(m[1]), int(m[2] or 0)
    if not (0 <= h <= 24 and 0 <= mi < 60) or h * 60 + mi > 24 * 60:
        raise ValueError(f"'{t}' is not a valid time")
    return h * 60 + mi
 
 
def parse_range(text: str) -> tuple[int, int]:
    parts = re.split(r"\s*(?:-|–|a|to)\s*", text.strip().lower(), maxsplit=1)
    if len(parts) != 2:
        raise ValueError("Time window must look like 15-17 or 15:00-17:00")
    lo, hi = parse_hhmm(parts[0]), parse_hhmm(parts[1])
    if hi - lo < SLOT_MINUTES:
        raise ValueError("The window must be at least 1 hour long (e.g. 15-17)")
    return lo, hi
 
 
def fmt_min(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"
 
 
def fmt_price(p) -> str:
    return "" if p is None else f" · ${p:,.0f}".replace(",", ".")
 
 
def describe(w: dict) -> str:
    d = date.fromisoformat(w["date"])
    sport = "any size" if w["sport"] == "all" else w["sport"].upper()
    return f"{d:%a %d/%m} {fmt_min(w['start'])}–{fmt_min(w['end'])} · {sport}"
 
 
# ------------------------------------------------------------------- state ---
 
def watches(app: Application) -> list[dict]:
    return app.bot_data.setdefault("watches", [])
 
 
async def save_state(app: Application):
    """Persist watches in a pinned message in the owner's chat (Render's disk is wiped on restart)."""
    if not OWNER_CHAT_ID:
        return
    data = [{k: v for k, v in w.items() if k != "seen"} | {"seen": sorted(w["seen"])}
            for w in watches(app)]
    text = f"{STATE_MARKER}\n{json.dumps(data, separators=(',', ':'))}"
    msg_id = app.bot_data.get("state_msg_id")
    try:
        if msg_id:
            await app.bot.edit_message_text(text, chat_id=OWNER_CHAT_ID, message_id=msg_id)
            return
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        log.warning("Could not edit state message (%s); sending a new one", e)
    try:
        msg = await app.bot.send_message(OWNER_CHAT_ID, text, disable_notification=True)
        app.bot_data["state_msg_id"] = msg.message_id
        await app.bot.pin_chat_message(OWNER_CHAT_ID, msg.message_id, disable_notification=True)
    except Exception as e:  # persistence is best-effort
        log.warning("Could not save state: %s", e)
 
 
async def restore_state(app: Application):
    if not OWNER_CHAT_ID:
        return
    try:
        chat = await app.bot.get_chat(OWNER_CHAT_ID)
        pinned = chat.pinned_message
        if pinned and pinned.text and pinned.text.startswith(STATE_MARKER):
            data = json.loads(pinned.text.split("\n", 1)[1])
            for w in data:
                w["seen"] = set(w.get("seen", []))
            app.bot_data["watches"] = data
            app.bot_data["state_msg_id"] = pinned.message_id
            log.info("Restored %d watch(es) from pinned message", len(data))
            if data:
                await app.bot.send_message(
                    OWNER_CHAT_ID,
                    "🔄 Bot restarted — still watching:\n" + "\n".join(f"• {describe(w)}" for w in data),
                    disable_notification=True,
                )
    except Exception as e:
        log.warning("Could not restore state: %s", e)
 
 
def allowed(update: Update) -> bool:
    return not OWNER_CHAT_ID or str(update.effective_chat.id) == OWNER_CHAT_ID
 
 
# ----------------------------------------------------------------- checker ---
 
def slot_key(s: dict) -> str:
    return f"{s['court_id']}@{s['start'].isoformat()}"
 
 
def slot_lines(slots: list[dict]) -> str:
    lines = []
    for s in slots:
        hhmm = s["start"].strftime("%H:%M")
        url = booking_url(s["start"].date().isoformat(), hhmm, s["sport_id"])
        lines.append(f'• <a href="{url}">{hhmm} — {s["court"]}</a>{fmt_price(s["price"])}')
    # the club page always opens on Fútbol 5, whatever the link says
    if any(s["sport_id"] != SPORTS["f5"] for s in slots):
        lines.append("<i>The page opens on F5 — tap ⚽ at the top-left to switch size.</i>")
    return "\n".join(lines)
 
 
async def run_checks(app: Application):
    now = datetime.now(TZ)
    ws = watches(app)
    changed = False
 
    # drop watches whose window has started
    for w in list(ws):
        window_start = datetime.fromisoformat(w["date"]).replace(tzinfo=TZ) + timedelta(minutes=w["start"])
        if now >= window_start:
            ws.remove(w)
            changed = True
            await app.bot.send_message(w["chat_id"], f"⏹ Watch ended (time reached): {describe(w)}")
 
    cache: dict[str, list[dict] | Exception] = {}
    for w in ws:
        if w["date"] not in cache:
            try:
                cache[w["date"]] = await asyncio.to_thread(fetch_slots, w["date"])
            except Exception as e:
                cache[w["date"]] = e
                log.error("Fetch failed for %s: %s", w["date"], e)
        result = cache[w["date"]]
        if isinstance(result, Exception):
            w["errors"] = w.get("errors", 0) + 1
            if w["errors"] == 3:  # tell the user once if the site keeps failing
                await app.bot.send_message(w["chat_id"], f"⚠️ I can't read the club page right now ({result}). I'll keep trying.")
            continue
        w["errors"] = 0
 
        found = matching_slots(result, w)
        current = {slot_key(s) for s in found}
        new = [s for s in found if slot_key(s) not in w["seen"]]
        if new:
            await app.bot.send_message(
                w["chat_id"],
                f"⚽ <b>Free pitch!</b> {describe(w)}\n{slot_lines(new)}\n\nTap a slot to book it. "
                f"I'll keep watching — /stop when you're done.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        if current != w["seen"]:
            w["seen"] = current  # forget slots that got taken, so a re-freed one alerts again
            changed = True
        log.info("%s → %d matching, %d new", describe(w), len(found), len(new))
 
    if changed:
        await save_state(app)
 
 
async def check_job(context: ContextTypes.DEFAULT_TYPE):
    if context.application.bot_data.get("checking"):
        return
    context.application.bot_data["checking"] = True
    try:
        await run_checks(context.application)
    finally:
        context.application.bot_data["checking"] = False
 
 
async def self_ping(context: ContextTypes.DEFAULT_TYPE):
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        try:
            await asyncio.to_thread(requests.get, url, timeout=15)
        except Exception as e:
            log.warning("Self-ping failed: %s", e)
 
 
# ---------------------------------------------------------------- commands ---
 
HELP = (
    "<b>Pitch watcher</b> — Comu\n\n"
    "/check <i>day window [size]</i> — alert me when a 1-hour slot frees up\n"
    "   e.g. <code>/check 03/10 15-17</code>\n"
    "   <code>/check 2026-10-03 15:00-17:00 f5</code>\n"
    "   <code>/check sab 19-23 all</code>\n"
    "   day: YYYY-MM-DD, DD/MM, hoy, mañana, lun…dom\n"
    f"   size: f5 f6 f7 f8 f9 or all (default {DEFAULT_SPORT})\n"
    "/now <i>day [size]</i> — what's free that day right now\n"
    "/list — active watches\n"
    "/stop — stop all (or <code>/stop 2</code>)\n\n"
    f"Checks every {CHECK_INTERVAL_MIN:g} min. The club opens bookings {BOOKING_WINDOW_DAYS} days ahead."
)
 
 
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    extra = "" if OWNER_CHAT_ID else f"\n\nYour chat id is <code>{update.effective_chat.id}</code> — set it as OWNER_CHAT_ID on Render."
    await update.message.reply_text(HELP + extra, parse_mode=ParseMode.HTML)
 
 
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        if len(context.args) < 2:
            raise ValueError("Usage: /check 03/10 15-17 [f5|f6|f7|f8|f9|all]")
        today = datetime.now(TZ).date()
        day = parse_day(context.args[0], today)
        # allow "15 - 17" typed with spaces
        rest = context.args[1:]
        sport = DEFAULT_SPORT
        if rest and rest[-1].lower() in (*SPORTS, "all"):
            sport = rest.pop().lower()
        lo, hi = parse_range("".join(rest))
        if day < today:
            raise ValueError(f"{day:%d/%m/%Y} is in the past")
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}\n\nExample: /check 03/10 15-17")
        return
 
    w = {"chat_id": update.effective_chat.id, "date": day.isoformat(), "start": lo,
         "end": hi, "sport": sport, "seen": set()}
    ws = watches(context.application)
    ws[:] = [x for x in ws if not (x["chat_id"] == w["chat_id"] and x["date"] == w["date"]
                                    and x["start"] == lo and x["end"] == hi and x["sport"] == sport)]
    ws.append(w)
 
    opens = day - timedelta(days=BOOKING_WINDOW_DAYS)
    note = (f"\nℹ️ Bookings for that day open around {opens:%a %d/%m}; until then I'll just keep checking."
            if opens > today else "")
    await update.message.reply_text(f"👀 Watching {describe(w)} every {CHECK_INTERVAL_MIN:g} min.{note}")
 
    # immediate check so you get feedback right away
    try:
        slots = await asyncio.to_thread(fetch_slots, w["date"])
        found = matching_slots(slots, w)
        w["seen"] = {slot_key(s) for s in found}
        if found:
            await update.message.reply_text(
                f"⚽ <b>Free right now:</b>\n{slot_lines(found)}\n\nI'll also tell you if another one opens up.",
                parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        elif opens <= today:
            await update.message.reply_text("Nothing free in that window right now. I'll message you as soon as something opens.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ First check failed ({e}). I'll keep trying.")
    await save_state(context.application)
 
 
async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        today = datetime.now(TZ).date()
        day = parse_day(context.args[0], today) if context.args else today
        sport = context.args[1].lower() if len(context.args) > 1 else DEFAULT_SPORT
        if sport not in (*SPORTS, "all"):
            raise ValueError(f"Unknown size '{sport}'")
        slots = await asyncio.to_thread(fetch_slots, day.isoformat())
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return
    w = {"date": day.isoformat(), "start": 0, "end": 24 * 60, "sport": sport}
    found = matching_slots(slots, w)
    title = f"{day:%a %d/%m} · {'any size' if sport == 'all' else sport.upper()}"
    if found:
        await update.message.reply_text(f"<b>Free on {title}</b>\n{slot_lines(found)}",
                                        parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    elif not slots and day - timedelta(days=BOOKING_WINDOW_DAYS) > today:
        await update.message.reply_text(f"Bookings for {title} aren't open yet (the club opens them {BOOKING_WINDOW_DAYS} days ahead).")
    else:
        await update.message.reply_text(f"Nothing free on {title}.")
 
 
async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    mine = [w for w in watches(context.application) if w["chat_id"] == update.effective_chat.id]
    if not mine:
        await update.message.reply_text("No active watches. Start one with /check 03/10 15-17")
        return
    await update.message.reply_text("Active watches:\n" + "\n".join(f"{i}. {describe(w)}" for i, w in enumerate(mine, 1)))
 
 
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    ws = watches(context.application)
    mine = [w for w in ws if w["chat_id"] == update.effective_chat.id]
    if not mine:
        await update.message.reply_text("I'm not watching anything for you.")
        return
    if context.args and context.args[0].isdigit():
        i = int(context.args[0])
        if not 1 <= i <= len(mine):
            await update.message.reply_text(f"There's no watch #{i}. See /list")
            return
        ws.remove(mine[i - 1])
        await update.message.reply_text(f"Stopped: {describe(mine[i - 1])}")
    else:
        for w in mine:
            ws.remove(w)
        await update.message.reply_text(f"Stopped {len(mine)} watch(es).")
    await save_state(context.application)
 
 
# --------------------------------------------------------------- keepalive ---
 
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
 
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
 
    def log_message(self, *args):  # keep Render logs clean
        pass
 
 
def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    log.info("Health server on port %s", port)
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()
 
 
async def post_init(app: Application):
    await restore_state(app)
    jq = app.job_queue
    jq.run_repeating(check_job, interval=CHECK_INTERVAL_MIN * 60, first=10, name="checker")
    if os.environ.get("RENDER_EXTERNAL_URL"):
        jq.run_repeating(self_ping, interval=10 * 60, first=60, name="self-ping")
 
 
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    threading.Thread(target=run_health_server, daemon=True).start()
 
    app = ApplicationBuilder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "help"], help_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("now", now_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("stop", stop_command))
    log.info("Telegram bot is online (venue=%s, default=%s, every %g min)", VENUE, DEFAULT_SPORT, CHECK_INTERVAL_MIN)
    app.run_polling(drop_pending_updates=True)
 
 
if __name__ == "__main__":
    main()
 
