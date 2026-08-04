"""Discord bot that collects team availability and turns overlaps into real
Discord Scheduled Events.

Storage stays as a plain JSON file (see DATA_FILE) on purpose -- no database.
"""

from __future__ import annotations

import asyncio
import calendar as pycal  # aliased: this module defines a /calendar command
import copy
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from dashboard import CLOUDFLARED_RELEASE, PublicTunnel, start_dashboard

load_dotenv()

log = logging.getLogger("meetingbot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
# Optional: set GUILD_ID in .env to your server's id so command changes appear
# immediately instead of waiting on Discord's global command propagation.
GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID", "").isdigit() else None
# Anchored to this file so the bot reads the same data regardless of the
# directory it was launched from.
DATA_FILE = Path(__file__).resolve().parent / "availability.json"

# Retries for the atomic-write swap; see save_data.
SAVE_RETRIES = 5
SAVE_RETRY_DELAY = 0.1

# The dashboard exposes members' names, so it listens on localhost only unless
# you deliberately change the host. Set DASHBOARD_HOST=0.0.0.0 to reach it from
# other machines on your network.
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
DASHBOARD_ENABLED = os.environ.get("DASHBOARD", "1") != "0"

# All meeting times are interpreted in this timezone. ZoneInfo handles the
# EST/EDT switch for us, so we never hardcode a UTC offset.
TZ = ZoneInfo("America/New_York")

# Saved availability older than this is ignored and pruned, so a slot someone
# offered two months ago stops skewing /best.
AVAILABILITY_TTL = timedelta(weeks=3)

# Only members with one of these roles (or the Manage Events permission) may
# create scheduled events via /schedule or the /best button. Empty tuple would
# open it to everyone.
SCHEDULE_ROLE_NAMES: tuple[str, ...] = ("Captain",)

DEFAULT_EVENT_TITLE = "FTC Team Meeting"
DEFAULT_EVENT_LOCATION = "Team Room"

START_HOUR = 10
END_HOUR = 20
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

COLOR_OK = discord.Color.green()
COLOR_INFO = discord.Color.blurple()
COLOR_WARN = discord.Color.orange()
COLOR_ERROR = discord.Color.red()

FOOTER = "FTC Meeting Organizer • times shown in US Eastern"
MEDALS = ["🥇", "🥈", "🥉"]


def fmt_hour(h: int) -> str:
    def to12(x: int) -> str:
        suffix = "AM" if x < 12 else "PM"
        x12 = x % 12 or 12
        return f"{x12}{suffix}"

    return f"{to12(h)}-{to12(h + 1)}"


# Hourly blocks 10am-8pm; availability is stored as indices into this list.
HOUR_LABELS = [fmt_hour(h) for h in range(START_HOUR, END_HOUR)]

# Short "10AM" form -- buttons are narrow, and every slot is exactly one hour,
# so the start time alone is unambiguous.
HOUR_START_LABELS = [label.split("-")[0] for label in HOUR_LABELS]


# ---------- storage ----------
# On disk:
#   { "<user_id>": {
#       "name": str,
#       "avail": { "Monday": {"hours": [0, 1, 2], "updated": "<iso8601>"} }
#   } }
# The ints in "hours" are indices into HOUR_LABELS.
#
# Older files stored "Monday": [0, 1, 2] directly. _migrate_record upgrades
# those in place; entries with no timestamp are kept (we can't know their age)
# and get stamped the next time that user saves the day.


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)


def _migrate_record(record: dict) -> dict:
    """Coerce one user record into the current shape, dropping junk."""
    avail: dict[str, dict] = {}
    for day, value in (record.get("avail") or {}).items():
        if day not in DAYS:
            continue
        raw_hours = value if isinstance(value, list) else (value or {}).get("hours", [])
        updated = None if isinstance(value, list) else (value or {}).get("updated")
        hours = sorted({int(h) for h in raw_hours if isinstance(h, int) and 0 <= h < len(HOUR_LABELS)})
        if hours:
            avail[day] = {"hours": hours, "updated": updated}

    # Keep only well-formed, still-relevant off dates so the list can't grow
    # without bound as the season goes by.
    today = datetime.now(TZ).date().isoformat()
    off = sorted(
        {
            value
            for value in (record.get("off") or [])
            if isinstance(value, str) and _valid_date(value) and value >= today
        }
    )
    return {"name": str(record.get("name") or "Unknown"), "avail": avail, "off": off}


def _valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _prune_expired(data: dict) -> bool:
    """Drop day entries past the TTL. Returns True if anything changed."""
    cutoff = datetime.now(TZ) - AVAILABILITY_TTL
    changed = False
    for uid in list(data):
        avail = data[uid]["avail"]
        for day in list(avail):
            ts = _parse_ts(avail[day].get("updated"))
            if ts and ts < cutoff:
                del avail[day]
                changed = True
        if not avail:
            del data[uid]
            changed = True
    return changed


# Commands run on the asyncio event loop and Discord kills an interaction that
# isn't answered within 3 seconds, so disk access has to stay off the hot path.
# We keep the parsed data in memory and only re-read when the file changes.
_cache: dict | None = None
_cache_stamp: tuple[int, int] | None = None


def _file_stamp() -> tuple[int, int] | None:
    try:
        stat = DATA_FILE.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def load_data() -> dict:
    """Read, migrate and prune the availability file.

    Returns a private copy: callers mutate what they get back, and that must
    not corrupt the cache. Never raises on a missing or corrupt file -- a bad
    file is set aside so a stray character can't take every command down.
    """
    global _cache, _cache_stamp

    stamp = _file_stamp()
    if stamp is None:  # no file yet
        _cache, _cache_stamp = {}, None
        return {}
    if _cache is not None and stamp == _cache_stamp:
        return copy.deepcopy(_cache)

    try:
        text = DATA_FILE.read_text(encoding="utf-8")
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("top-level JSON is not an object")
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        backup = DATA_FILE.with_suffix(".corrupt.json")
        log.error("Could not read %s (%s); moving it to %s", DATA_FILE, exc, backup)
        try:
            DATA_FILE.replace(backup)
        except OSError:
            log.exception("Failed to move aside the unreadable data file")
        _cache, _cache_stamp = {}, None
        return {}

    data = {str(uid): _migrate_record(rec) for uid, rec in raw.items() if isinstance(rec, dict)}
    data = {uid: rec for uid, rec in data.items() if rec["avail"]}
    if _prune_expired(data):
        log.info("Pruned availability older than %s", AVAILABILITY_TTL)

    # Only write when migrating or pruning actually changed something. Writing
    # on every read is what previously made commands slow enough to time out.
    if json.dumps(data, indent=2) != text:
        save_data(data)
    else:
        _cache, _cache_stamp = copy.deepcopy(data), stamp
    return data


def save_data(data: dict) -> None:
    """Persist the availability file, then refresh the in-memory cache.

    Prefers an atomic temp-file swap, but Windows raises PermissionError
    (WinError 5) from os.replace whenever the destination is momentarily
    locked -- OneDrive, antivirus and open editors all do this on a synced
    folder like Desktop. So we retry briefly, then fall back to writing in
    place, which is less safe but far better than losing the save.
    """
    global _cache, _cache_stamp

    payload = json.dumps(data, indent=2)
    tmp = DATA_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        for attempt in range(SAVE_RETRIES):
            try:
                tmp.replace(DATA_FILE)
                break
            except PermissionError:
                if attempt == SAVE_RETRIES - 1:
                    raise
                time.sleep(SAVE_RETRY_DELAY)
    except PermissionError:
        log.warning("Atomic replace was blocked; writing %s in place instead", DATA_FILE)
        DATA_FILE.write_text(payload, encoding="utf-8")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    _cache, _cache_stamp = copy.deepcopy(data), _file_stamp()


# ---------- personal edit links ----------
# Tokens are "<user_id>.<hmac>" -- signed rather than stored, so a link keeps
# working even after pruning removes that member's record, and no extra file
# has to stay in sync with availability.json.
SECRET_FILE = Path(__file__).resolve().parent / ".dashboard_secret"


def _load_secret() -> bytes:
    from_env = os.environ.get("DASHBOARD_SECRET")
    if from_env:
        return from_env.encode()
    try:
        return SECRET_FILE.read_bytes()
    except OSError:
        value = secrets.token_bytes(32)
        try:
            SECRET_FILE.write_bytes(value)
        except OSError:
            log.warning("Could not save %s; edit links will stop working on restart", SECRET_FILE)
        return value


EDIT_SECRET = _load_secret()


def _sign(user_id: str) -> str:
    return hmac.new(EDIT_SECRET, user_id.encode(), hashlib.sha256).hexdigest()[:24]


def make_edit_token(user_id: str) -> str:
    return f"{user_id}.{_sign(user_id)}"


def verify_edit_token(token: str) -> str | None:
    """Return the user id a token belongs to, or None if it doesn't check out."""
    user_id, _, signature = str(token or "").partition(".")
    if not user_id.isdigit() or not signature:
        return None
    # compare_digest so a wrong signature can't be narrowed down by timing.
    return user_id if hmac.compare_digest(signature, _sign(user_id)) else None


async def resolve_name(user_id: str, data: dict) -> str:
    """Best available display name for a member.

    get_user() only sees people already in the bot's cache, which is why
    teammates the bot hadn't interacted with showed up as "Unknown". Fall back
    to fetch_user(), a real API lookup that works without the members intent.
    """
    record = data.get(user_id)
    if record and record["name"] != "Unknown":
        return record["name"]
    cached = bot.get_user(int(user_id))
    if cached:
        return cached.display_name
    try:
        fetched = await bot.fetch_user(int(user_id))
        return fetched.display_name
    except Exception:
        # A display name is cosmetic -- never let looking one up fail a save.
        log.warning("Could not resolve a display name for %s", user_id)
        return record["name"] if record else "Unknown"


async def dashboard_get_user(token: str) -> dict | None:
    user_id = verify_edit_token(token)
    if user_id is None:
        return None
    data = load_data()
    record = data.get(user_id)
    return {
        "name": await resolve_name(user_id, data),
        "avail": {day: entry["hours"] for day, entry in record["avail"].items()} if record else {},
        "off": sorted(off_dates(record)) if record else [],
        "seasonEnd": SEASON_END.isoformat(),
    }


async def dashboard_save_user(token: str, avail: dict, off: list, name: str | None) -> dict | None:
    """Replace one member's availability from the web editor.

    The token decides whose row is written, never anything in the request body,
    so a valid link can only ever edit its own owner's availability.
    """
    user_id = verify_edit_token(token)
    if user_id is None:
        return None

    cleaned: dict[str, list[int]] = {}
    for day, hours in avail.items():
        if day not in DAYS or not isinstance(hours, list):
            continue
        valid = sorted({h for h in hours if isinstance(h, int) and 0 <= h < len(HOUR_LABELS)})
        if valid:
            cleaned[day] = valid

    today = datetime.now(TZ).date().isoformat()
    away = sorted(
        {
            value
            for value in (off if isinstance(off, list) else [])
            if isinstance(value, str) and _valid_date(value) and today <= value <= SEASON_END.isoformat()
        }
    )

    data = load_data()
    chosen = (name or "").strip()[:32] if isinstance(name, str) else ""
    final_name = chosen or await resolve_name(user_id, data)

    if cleaned:
        stamp = _now_iso()
        data[user_id] = {
            "name": final_name,
            "avail": {day: {"hours": hours, "updated": stamp} for day, hours in cleaned.items()},
            "off": away,
        }
    else:
        # Clearing every slot means leaving, not lingering with an empty record.
        data.pop(user_id, None)
    save_data(data)
    log.info("%s updated availability from the web editor", final_name)
    return {"ok": True, "name": final_name, "avail": cleaned, "off": away}


def set_day(data: dict, user: discord.abc.User, day: str, hours: list[int]) -> None:
    uid = str(user.id)
    record = data.setdefault(uid, {"name": user.display_name, "avail": {}})
    record["name"] = user.display_name
    record["avail"][day] = {"hours": sorted(set(hours)), "updated": _now_iso()}


# ---------- time helpers ----------
def next_occurrence(day: str, hour_index: int) -> datetime:
    """The next upcoming `day` at that slot's start hour, in US Eastern.

    Arithmetic is done on a naive datetime and localized afterwards so that
    crossing a DST boundary keeps the wall-clock hour the team agreed on.
    """
    now = datetime.now(TZ)
    naive_now = now.replace(tzinfo=None)
    candidate = naive_now.replace(hour=START_HOUR + hour_index, minute=0, second=0, microsecond=0)
    candidate += timedelta(days=(DAYS.index(day) - candidate.weekday()) % 7)
    # Discord rejects events that start in the past; skip a week if it's too close.
    if candidate <= naive_now + timedelta(minutes=5):
        candidate += timedelta(days=7)
    return candidate.replace(tzinfo=TZ)


def slot_label(day: str, hour_index: int) -> str:
    return f"{day} {HOUR_LABELS[hour_index]}"


def people_free(data: dict, day: str, hour_index: int) -> list[str]:
    return sorted(
        rec["name"]
        for rec in data.values()
        if hour_index in rec["avail"].get(day, {}).get("hours", [])
    )


def calendar_grid(data: dict, *, only_user: str | None = None) -> str:
    """A fixed-width week grid: rows are hourly slots, columns are days.

    Rendered inside a code block so Discord's monospace font keeps the columns
    lined up. Shows per-slot head counts, or ✓/· marks for a single user.
    Kept to 30 characters wide so it doesn't wrap on phones.
    """
    if only_user:
        avail = data.get(only_user, {}).get("avail", {})
        # ASCII only inside the grid: exotic glyphs can render double-width on
        # some mobile fonts and knock the columns out of line.
        cells = {(day, h): "  #" for day, entry in avail.items() for h in entry["hours"]}
    else:
        cells = {key: f"{len(people):>3}" for key, people in rank_slots(data)}

    lines = ["TIME     " + "".join(f"{day[:2]:>3}" for day in DAYS)]
    for hour_index, label in enumerate(HOUR_LABELS):
        row = "".join(cells.get((day, hour_index), "  ·") for day in DAYS)
        lines.append(f"{label:<9}{row}")
    return "```\n" + "\n".join(lines) + "\n```"


def hour_range_label(start: int, end: int) -> str:
    """'1PM-8PM' for the inclusive slot range start..end."""
    return f"{HOUR_START_LABELS[start]}-{fmt_hour(START_HOUR + end).split('-')[1]}"


# ---------- date-aware availability ----------
# The weekly pattern in "avail" is the standing arrangement. "off" lists
# specific dates a member is away, so nobody has to re-enter their whole week
# just because they're busy one Saturday.
SEASON_END = date(2027, 2, 28)


def off_dates(record: dict) -> set[str]:
    return set(record.get("off") or [])


def free_on(data: dict, day: date) -> dict[int, list[str]]:
    """hour index -> names of everyone free at that hour on this specific date."""
    weekday = DAYS[day.weekday()]
    key = day.isoformat()
    result: dict[int, list[str]] = {}
    for record in data.values():
        if key in off_dates(record):
            continue
        entry = record["avail"].get(weekday)
        if not entry:
            continue
        for hour_index in entry["hours"]:
            result.setdefault(hour_index, []).append(record["name"])
    return {h: sorted(names) for h, names in result.items()}


def merge_hours(free: dict[int, list[str]]) -> list[dict]:
    """Collapse consecutive hours suiting the same people into windows.

    Runs only merge when the free *people* match, not just the head count --
    otherwise a window could claim hours nobody is actually free across.
    """
    blocks: list[dict] = []
    hour_index = 0
    while hour_index < len(HOUR_LABELS):
        people = free.get(hour_index)
        if not people:
            hour_index += 1
            continue
        end = hour_index
        while end + 1 < len(HOUR_LABELS) and free.get(end + 1) == people:
            end += 1
        blocks.append(
            {
                "start": hour_index,
                "end": end,
                "span": end - hour_index + 1,
                "label": hour_range_label(hour_index, end),
                "people": list(people),
            }
        )
        hour_index = end + 1
    return blocks


def date_windows(data: dict, horizon_days: int = 21) -> list[dict]:
    """Best meeting windows on concrete upcoming dates, best first.

    Ranking real dates rather than weekday patterns is what makes "I'm away
    that Saturday" actually change the answer.
    """
    now = datetime.now(TZ)
    today = now.date()
    windows: list[dict] = []
    for offset in range(horizon_days):
        day = today + timedelta(days=offset)
        if day > SEASON_END:
            break
        for block in merge_hours(free_on(data, day)):
            # Discord rejects events starting in the past, so drop today's
            # windows that have already begun.
            if datetime.combine(day, dtime(hour=START_HOUR + block["start"]), tzinfo=TZ) <= now:
                continue
            windows.append({**block, "date": day, "day": DAYS[day.weekday()]})
    windows.sort(key=lambda w: (-len(w["people"]), -w["span"], w["date"], w["start"]))
    return windows


def window_start(day: date, hour_index: int) -> datetime:
    """Wall-clock start of a slot on a concrete date, in US Eastern."""
    return datetime.combine(day, dtime(hour=START_HOUR + hour_index), tzinfo=TZ)


def day_totals(data: dict, start: date, end: date) -> dict[str, dict]:
    """Per-date summary for the calendar: peak heads and total free hours."""
    out: dict[str, dict] = {}
    day = start
    while day <= end:
        free = free_on(data, day)
        if free:
            best = max(merge_hours(free), key=lambda b: (len(b["people"]), b["span"]))
            # Just the label, not the whole block: repeating every member's name
            # for all ~200 dates was most of the payload the browser downloads.
            out[day.isoformat()] = {
                "peak": max(len(names) for names in free.values()),
                "hours": len(free),
                "label": best["label"],
            }
        day += timedelta(days=1)
    return out


def rank_slots(data: dict) -> list[tuple[tuple[str, int], list[str]]]:
    """All (day, hour) slots with at least one person, most-attended first."""
    counts: dict[tuple[str, int], list[str]] = defaultdict(list)
    for record in data.values():
        for day, entry in record["avail"].items():
            for hour_index in entry["hours"]:
                counts[(day, hour_index)].append(record["name"])
    return sorted(
        counts.items(),
        key=lambda kv: (-len(kv[1]), DAYS.index(kv[0][0]), kv[0][1]),
    )


# ---------- embeds ----------
def join_names(people: list[str], limit: int = 900) -> str:
    """Comma-join names, trimming to fit an embed field with a '+N more' tail."""
    if not people:
        return "—"
    shown: list[str] = []
    used = 0
    for name in people:
        cost = len(name) + (2 if shown else 0)
        if used + cost > limit:
            break
        shown.append(name)
        used += cost
    if not shown:  # a single pathologically long name
        return people[0][:limit]
    hidden = len(people) - len(shown)
    return ", ".join(shown) + (f" *+{hidden} more*" if hidden else "")


def base_embed(title: str, description: str = "", color: discord.Color = COLOR_INFO) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=FOOTER)
    return embed


def error_embed(message: str) -> discord.Embed:
    return base_embed("Something went wrong", message, COLOR_ERROR)


def help_embed() -> discord.Embed:
    embed = base_embed(
        "FTC Meeting Organizer",
        "Tell me when you're free, and I'll find the times that work for the most "
        "people — then turn one into a real Discord event you can RSVP to.",
    )
    embed.add_field(name="/free", value="Pick a day, then tap the hours you're free (10AM–8PM). Repeat for each day.", inline=False)
    embed.add_field(name="/editlink", value="A private link to set your whole week on the web in one go.", inline=False)
    embed.add_field(name="/my-availability", value="See everything you've currently got saved.", inline=False)
    embed.add_field(name="/dashboard", value="A link anyone can open to see the full-screen calendar, heatmap and team view — no sign-in needed.", inline=False)
    embed.add_field(name="/month", value="A real month calendar — page back and forth to see events in advance.", inline=False)
    embed.add_field(name="/events", value="Every upcoming team event, grouped by date, with RSVP counts.", inline=False)
    embed.add_field(name="/status", value="See who on the team has submitted so far.", inline=False)
    embed.add_field(name="/best", value="Rank the time slots that work for the most people. Pick one right from the results to create an event.", inline=False)
    embed.add_field(
        name="/schedule",
        value=f"Create a Discord Scheduled Event for a specific day and hour. *{' / '.join(SCHEDULE_ROLE_NAMES)} only.*"
        if SCHEDULE_ROLE_NAMES
        else "Create a Discord Scheduled Event for a specific day and hour.",
        inline=False,
    )
    embed.add_field(name="/clear", value="Clear one day (`/clear day: Tuesday`) or leave the day blank to clear everything.", inline=False)
    embed.add_field(name="/help", value="Show this message.", inline=False)
    embed.add_field(
        name="Heads up",
        value=f"Availability expires after {AVAILABILITY_TTL.days // 7} weeks so old data doesn't skew results — just re-run `/free` to refresh it.",
        inline=False,
    )
    return embed


def availability_embed(name: str, data: dict, user_id: str) -> discord.Embed:
    avail = data[user_id]["avail"]
    embed = base_embed(f"{name}'s availability", calendar_grid(data, only_user=user_id), COLOR_OK)
    total = 0
    for day in DAYS:
        entry = avail.get(day)
        if not entry:
            continue
        total += len(entry["hours"])
        embed.add_field(name=day, value=", ".join(HOUR_LABELS[h] for h in entry["hours"]), inline=False)
    embed.insert_field_at(
        0,
        name="Summary",
        value=f"**{total}** free hour{'s' if total != 1 else ''} across **{len(avail)}** day(s).",
        inline=False,
    )
    return embed


# ---------- agenda UI ----------
# An embed's color renders as a vertical accent bar down its left edge, and
# set_author() puts a small grey line above the card. Stacking one embed per
# entry -- author line only on the first of each date -- gives the grouped
# agenda look: a date header with colored event cards beneath it.
AGENDA_ACCENTS = (0xFACC15, 0xA855F7, 0x38BDF8, 0x34D399, 0xFB7185, 0xFB923C)

# Discord renders at most 10 embeds per message.
MAX_AGENDA_CARDS = 10


def accent_for(key: str) -> discord.Color:
    """Stable color per key, so the same slot keeps its bar color between runs."""
    return discord.Color(AGENDA_ACCENTS[sum(key.encode()) % len(AGENDA_ACCENTS)])


def date_header(moment: datetime) -> str:
    # Built by hand because %-d (no zero padding) isn't portable to Windows.
    return f"{moment:%A} {moment:%B} {moment.day}"


def agenda_card(item: dict, *, show_date: bool) -> discord.Embed:
    embed = discord.Embed(title=item["title"], color=accent_for(item["key"]))
    if show_date:
        embed.set_author(name=date_header(item["start"]))

    # format_dt renders in each viewer's own local timezone.
    times = f"{discord.utils.format_dt(item['start'], 't')} – {discord.utils.format_dt(item['end'], 't')}"
    lines = [f"🕐 {times}　　👥 {item['count']}"]
    if item.get("subtitle"):
        lines.append(item["subtitle"])
    if item.get("url"):
        lines.append(f"[Open in the Events tab]({item['url']})")
    embed.description = "\n".join(lines)
    return embed


def build_agenda(heading: str, items: list[dict]) -> tuple[str, list[discord.Embed]]:
    """Render items (sorted by start) as a stacked, date-grouped agenda."""
    embeds: list[discord.Embed] = []
    last_date = None
    for item in items[:MAX_AGENDA_CARDS]:
        current = item["start"].date()
        embeds.append(agenda_card(item, show_date=current != last_date))
        last_date = current

    if embeds:
        embeds[-1].set_footer(text=FOOTER)
    subtitle = f"\n**{items[0]['start']:%B %Y}**" if items else ""
    hidden = max(0, len(items) - MAX_AGENDA_CARDS)
    if hidden:
        subtitle += f"　*+{hidden} more*"
    return f"# {heading}{subtitle}", embeds


def availability_agenda_items(data: dict, limit: int = MAX_AGENDA_CARDS) -> list[dict]:
    """Top availability slots as agenda entries, resolved to real upcoming dates."""
    total = len(data)
    top = rank_slots(data)[:limit]
    items = []
    for rank, ((day, hour_index), people) in enumerate(top):
        start = next_occurrence(day, hour_index)
        medal = f"{MEDALS[rank]} " if rank < len(MEDALS) else ""
        items.append(
            {
                "title": f"{medal}{len(people)} of {total} available",
                "start": start,
                "end": start + timedelta(hours=1),
                "count": len(people),
                "subtitle": join_names(people, limit=200),
                # Keyed by day so each weekday keeps a consistent bar color.
                "key": day,
            }
        )
    items.sort(key=lambda i: i["start"])
    return items


# ---------- month calendar ----------
WEEKDAY_HEADS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


def fmt_clock(moment: datetime) -> str:
    """'2:00 PM' -- %-I isn't portable to Windows, so strip the pad by hand."""
    return moment.strftime("%I:%M %p").lstrip("0")


def month_grid(year: int, month: int, *, events: set[int], proposed: set[int], today: date) -> str:
    """A real month grid. Four characters per cell keeps every row aligned:
    a '>' marker for today, the day number, then an event/proposal marker.
    ASCII only, 28 columns, so it survives phone-width monospace.
    """
    lines = ["".join(f" {head} " for head in WEEKDAY_HEADS)]
    for week in pycal.Calendar(firstweekday=0).monthdayscalendar(year, month):
        row = ""
        for day in week:
            if day == 0:  # padding for days belonging to the adjacent month
                row += "    "
                continue
            is_today = today.year == year and today.month == month and today.day == day
            marker = "*" if day in events else "+" if day in proposed else " "
            row += f"{'>' if is_today else ' '}{day:>2}{marker}"
        lines.append(row.rstrip())
    return "```\n" + "\n".join(lines) + "\n```"


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + month - 1) + delta
    return index // 12, index % 12 + 1


def month_embed(
    year: int,
    month: int,
    events: list[discord.ScheduledEvent],
    data: dict,
) -> discord.Embed:
    today = datetime.now(TZ).date()

    in_month = sorted(
        (e for e in events if e.start_time and e.start_time.astimezone(TZ).year == year
         and e.start_time.astimezone(TZ).month == month),
        key=lambda e: e.start_time,
    )
    event_days = {e.start_time.astimezone(TZ).day for e in in_month}

    # Proposed slots only ever land in the coming week, so they naturally stop
    # appearing once you page past the current month.
    proposals = {
        item["start"]: item
        for item in availability_agenda_items(data, limit=5)
        if item["start"].year == year and item["start"].month == month
    }
    proposed_days = {start.day for start in proposals} - event_days

    embed = base_embed(
        f"{pycal.month_name[month]} {year}",
        month_grid(year, month, events=event_days, proposed=proposed_days, today=today),
        COLOR_INFO,
    )
    embed.add_field(name="Legend", value="`>` today · `*` scheduled event · `+` suggested time", inline=False)

    if in_month:
        lines = [
            f"`{e.start_time.astimezone(TZ).day:>2}` **{e.name}** — {fmt_clock(e.start_time.astimezone(TZ))}"
            for e in in_month[:12]
        ]
        if len(in_month) > 12:
            lines.append(f"*+{len(in_month) - 12} more*")
        embed.add_field(name=f"Scheduled ({len(in_month)})", value="\n".join(lines), inline=False)

    if proposals:
        lines = [
            f"`{start.day:>2}` {item['title']} — {fmt_clock(start)}"
            for start, item in sorted(proposals.items())
            if start.day in proposed_days
        ]
        if lines:
            embed.add_field(name="Suggested from availability", value="\n".join(lines), inline=False)

    if not in_month and not proposals:
        embed.add_field(name="Nothing this month", value="No events scheduled. Run `/best` to find a time.", inline=False)
    return embed


class MonthNavButton(discord.ui.Button):
    def __init__(self, delta: int | None, label: str, emoji: str | None = None, row: int = 0):
        style = discord.ButtonStyle.primary if delta == 0 else discord.ButtonStyle.secondary
        super().__init__(label=label, emoji=emoji, style=style, row=row)
        self.delta = delta  # None = refresh in place, 0 = jump to today

    async def callback(self, interaction: discord.Interaction):
        view: MonthView = self.view
        if self.delta == 0:
            now = datetime.now(TZ)
            view.year, view.month = now.year, now.month
        elif self.delta is not None:
            view.year, view.month = shift_month(view.year, view.month, self.delta)

        await interaction.response.defer()
        # Only the explicit Refresh button bypasses the cache; paging months
        # reuses what we already have.
        await view.reload(force=self.delta is None)
        await interaction.edit_original_response(embed=view.render(), view=view)


class MonthView(discord.ui.View):
    """A pageable month calendar over the guild's real scheduled events."""

    def __init__(self, guild: discord.Guild, year: int, month: int):
        super().__init__(timeout=600)
        self.guild = guild
        self.year = year
        self.month = month
        self.events: list[discord.ScheduledEvent] = []
        # ◀/▶ (U+25C0/U+25B6) have no emoji presentation and Discord rejects
        # them; the arrow emoji below are the real thing.
        self.add_item(MonthNavButton(-1, "Prev", "⬅️"))
        self.add_item(MonthNavButton(0, "Today", "📅"))
        self.add_item(MonthNavButton(1, "Next", "➡️"))
        self.add_item(MonthNavButton(None, "Refresh", "🔄"))

    async def reload(self, *, force: bool = False) -> None:
        self.events = await fetch_events(self.guild, force=force)

    def render(self) -> discord.Embed:
        return month_embed(self.year, self.month, self.events, load_data())


# ---------- permissions ----------
def can_schedule(user: discord.abc.User) -> bool:
    """True if `user` may create events. Open to all unless SCHEDULE_ROLE_NAMES is set."""
    if not SCHEDULE_ROLE_NAMES:
        return True
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.manage_events:
        return True
    wanted = {name.casefold() for name in SCHEDULE_ROLE_NAMES}
    return any(role.name.casefold() in wanted for role in user.roles)


def no_permission_embed() -> discord.Embed:
    roles = " or ".join(f"**{name}**" for name in SCHEDULE_ROLE_NAMES)
    return base_embed(
        "Only officers can create events",
        f"You need the {roles} role (or the Manage Events permission) to schedule "
        "a meeting. You can still use `/free`, `/best` and `/calendar`.",
        COLOR_WARN,
    )


# ---------- event creation ----------
async def create_meeting_event(
    guild: discord.Guild,
    day: str,
    hour_index: int,
    duration_hours: int,
    title: str,
    location: str,
    attendees: list[str],
    on_date: date | None = None,
) -> discord.ScheduledEvent:
    """Create a native Discord Scheduled Event.

    Uses on_date when the caller already knows the exact date (as /best now
    does); otherwise falls back to the next occurrence of that weekday.
    """
    start = window_start(on_date, hour_index) if on_date else next_occurrence(day, hour_index)
    end = start + timedelta(hours=duration_hours)

    window = hour_range_label(hour_index, hour_index + duration_hours - 1)
    description = f"Found by /best — {day} {window}.\n"
    if attendees:
        description += f"\n**Free at this time ({len(attendees)}):**\n" + join_names(attendees, limit=800)
    else:
        description += "\nNo one has marked this slot as free yet."

    return await guild.create_scheduled_event(
        name=title[:100],
        description=description[:1000],
        start_time=start,
        end_time=end,
        entity_type=discord.EntityType.external,
        location=location[:100],
        privacy_level=discord.PrivacyLevel.guild_only,
    )


def event_created_embed(event: discord.ScheduledEvent, attendees: list[str]) -> discord.Embed:
    embed = base_embed("Event created", f"**{event.name}**", COLOR_OK)
    # format_dt renders the timestamp in each viewer's own local timezone.
    embed.add_field(name="Starts", value=discord.utils.format_dt(event.start_time, "F"), inline=False)
    if event.end_time:
        embed.add_field(name="Ends", value=discord.utils.format_dt(event.end_time, "t"), inline=True)
    embed.add_field(name="Where", value=event.location or "—", inline=True)
    embed.add_field(
        name=f"Expected free ({len(attendees)})",
        value=join_names(attendees),
        inline=False,
    )
    embed.add_field(name="RSVP", value=f"[Open in the Events tab]({event.url})", inline=False)
    return embed


async def respond_to_event_error(interaction: discord.Interaction, exc: Exception) -> None:
    """Translate Discord API failures into something a human can act on."""
    if isinstance(exc, discord.Forbidden):
        embed = base_embed(
            "I'm missing a permission",
            "I need the **Manage Events** permission in this server to create "
            "scheduled events. Ask an admin to grant it in Server Settings → Roles.",
            COLOR_WARN,
        )
    elif isinstance(exc, discord.HTTPException):
        embed = error_embed(f"Discord rejected the event: {exc.text or exc}")
    else:
        log.exception("Unexpected failure creating a scheduled event")
        embed = error_embed("I couldn't create that event. The error is in the bot logs.")
    await send_embed(interaction, embed, ephemeral=True)


async def send_embed(interaction: discord.Interaction, embed: discord.Embed, *, ephemeral: bool = False) -> None:
    """Reply whether or not the interaction has already been responded to."""
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

_dashboard_runner = None  # set once the web dashboard binds; see on_ready

# Scheduled events change rarely but were being re-fetched from Discord on every
# dashboard poll (every 15s, per guild) and every /month or /events press, which
# is what made everything feel sluggish. Cache them briefly instead.
EVENTS_TTL = 60.0
_events_cache: dict[int, tuple[float, list[discord.ScheduledEvent]]] = {}


_refreshing: set[int] = set()


async def _refresh_events(guild: discord.Guild) -> list[discord.ScheduledEvent]:
    try:
        events = await guild.fetch_scheduled_events(with_counts=True)
    except discord.HTTPException:
        log.warning("Could not fetch scheduled events for %s; using the local cache", guild.id)
        events = list(guild.scheduled_events)
    except Exception:
        log.exception("Unexpected failure refreshing events for %s", guild.id)
        events = list(guild.scheduled_events)
    _events_cache[guild.id] = (time.monotonic(), events)
    _refreshing.discard(guild.id)
    return events


async def fetch_events(
    guild: discord.Guild, *, force: bool = False, allow_stale: bool = False
) -> list[discord.ScheduledEvent]:
    """Guild scheduled events, cached for EVENTS_TTL seconds.

    With allow_stale, a stale entry is returned immediately and refreshed in the
    background. The dashboard polls every 15s and this call can take ten seconds
    against Discord, so the web request must never wait on it.
    """
    cached = _events_cache.get(guild.id)
    fresh = cached is not None and time.monotonic() - cached[0] < EVENTS_TTL
    if not force and fresh:
        return cached[1]

    if allow_stale and cached is not None and not force:
        if guild.id not in _refreshing:
            _refreshing.add(guild.id)
            asyncio.create_task(_refresh_events(guild))
        return cached[1]

    _refreshing.add(guild.id)
    return await _refresh_events(guild)
_tunnel: PublicTunnel | None = None  # set on first /dashboard call; see the command


# ---------- availability UI ----------
class DaySelect(discord.ui.Select):
    def __init__(self, parent_view: "AvailabilityView"):
        super().__init__(
            placeholder="1. Choose a day",
            options=[discord.SelectOption(label=d) for d in DAYS],
            min_values=1,
            max_values=1,
            row=0,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        view = self.parent_view
        view.selected_day = self.values[0]
        # Preload whatever this user already saved for the day, so the buttons
        # show current state and editing is a tweak rather than a re-entry.
        record = load_data().get(str(interaction.user.id))
        view.selected_hours = list(record["avail"].get(view.selected_day, {}).get("hours", [])) if record else []
        view.refresh_hour_buttons()
        await interaction.response.edit_message(embed=view.render(), view=view)


class HourButton(discord.ui.Button):
    """One toggle per hour: green when selected, grey when not."""

    def __init__(self, parent_view: "AvailabilityView", hour_index: int, row: int):
        super().__init__(label=HOUR_START_LABELS[hour_index], style=discord.ButtonStyle.secondary, row=row)
        self.parent_view = parent_view
        self.hour_index = hour_index

    async def callback(self, interaction: discord.Interaction):
        view = self.parent_view
        if not view.selected_day:
            await interaction.response.send_message(
                embed=base_embed("Pick a day first", "Choose a day from the dropdown, then tap your free hours.", COLOR_WARN),
                ephemeral=True,
            )
            return

        if self.hour_index in view.selected_hours:
            view.selected_hours.remove(self.hour_index)
        else:
            view.selected_hours.append(self.hour_index)
        view.selected_hours.sort()
        view.refresh_hour_buttons()
        await interaction.response.edit_message(embed=view.render(), view=view)


class SelectAllButton(discord.ui.Button):
    def __init__(self, parent_view: "AvailabilityView"):
        super().__init__(label="All day", style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        view = self.parent_view
        # Toggles: a second tap clears, so one button covers both directions.
        view.selected_hours = [] if len(view.selected_hours) == len(HOUR_LABELS) else list(range(len(HOUR_LABELS)))
        view.refresh_hour_buttons()
        await interaction.response.edit_message(embed=view.render(), view=view)


class SaveButton(discord.ui.Button):
    def __init__(self, parent_view: "AvailabilityView"):
        super().__init__(label="Save this day", style=discord.ButtonStyle.green, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        view = self.parent_view
        if not view.selected_day or not view.selected_hours:
            await interaction.response.send_message(
                embed=base_embed("Not so fast", "Pick a day **and** at least one hour first.", COLOR_WARN),
                ephemeral=True,
            )
            return

        data = load_data()
        set_day(data, interaction.user, view.selected_day, view.selected_hours)
        save_data(data)
        view.saved[view.selected_day] = list(view.selected_hours)
        await interaction.response.edit_message(embed=view.render(just_saved=True), view=view)


class DoneButton(discord.ui.Button):
    def __init__(self, parent_view: "AvailabilityView"):
        super().__init__(label="Done", style=discord.ButtonStyle.blurple, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        view = self.parent_view
        if view.saved:
            embed = base_embed("Availability saved", color=COLOR_OK)
            for day in DAYS:
                if day in view.saved:
                    embed.add_field(
                        name=day,
                        value=", ".join(HOUR_LABELS[h] for h in view.saved[day]),
                        inline=False,
                    )
            embed.description = "Run `/best` to find the time that works for the most people."
        else:
            embed = base_embed("Nothing saved", "You closed without saving a day. Run `/free` again any time.", COLOR_WARN)
        await interaction.response.edit_message(embed=embed, view=None)
        view.stop()


class AvailabilityView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.selected_day: str | None = None
        self.selected_hours: list[int] = []
        self.saved: dict[str, list[int]] = {}
        self.add_item(DaySelect(self))
        # 10 hours across two rows of 5 -- Discord allows 5 buttons per row.
        for hour_index in range(len(HOUR_LABELS)):
            self.add_item(HourButton(self, hour_index, row=1 + hour_index // 5))
        self.add_item(SaveButton(self))
        self.add_item(SelectAllButton(self))
        self.add_item(DoneButton(self))

    def refresh_hour_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, HourButton):
                item.style = (
                    discord.ButtonStyle.success
                    if item.hour_index in self.selected_hours
                    else discord.ButtonStyle.secondary
                )

    def render(self, *, just_saved: bool = False) -> discord.Embed:
        if just_saved:
            embed = base_embed("Saved", f"**{self.selected_day}** is locked in.", COLOR_OK)
        else:
            embed = base_embed(
                "Set your free hours",
                "Pick a day, tap each hour you're free (green = free), then **Save this day**.",
            )

        embed.add_field(name="Day", value=f"**{self.selected_day}**" if self.selected_day else "*not picked*", inline=True)
        embed.add_field(
            name="Hours",
            value=", ".join(HOUR_LABELS[h] for h in self.selected_hours) if self.selected_hours else "*none selected*",
            inline=True,
        )
        if self.saved:
            embed.add_field(
                name="Saved so far",
                value="\n".join(
                    f"**{day}** — {', '.join(HOUR_LABELS[h] for h in self.saved[day])}"
                    for day in DAYS
                    if day in self.saved
                ),
                inline=False,
            )
        return embed


# ---------- schedule-from-best UI ----------
class SlotSelect(discord.ui.Select):
    def __init__(self, blocks: list[dict], total_people: int):
        options = [
            discord.SelectOption(
                label=(
                    f"{block['day'][:3]} {block['date']:%b} {block['date'].day} · {block['label']}"
                    if block.get("date")
                    else f"{block['day']} {block['label']}"
                )[:100],
                value=str(i),
                description=f"{len(block['people'])}/{total_people} free · "
                f"{block['span']} hour window",
                emoji=MEDALS[i] if i < len(MEDALS) else None,
            )
            for i, block in enumerate(blocks)
        ]
        super().__init__(placeholder="Pick a window to schedule…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view: ScheduleFromBestView = self.view
        view.choose(int(self.values[0]))
        await interaction.response.edit_message(embed=view.summary(), view=view)


class DurationSelect(discord.ui.Select):
    """Length options, capped to how long the chosen window actually is."""

    def __init__(self, max_hours: int = 1, selected: int = 1, enabled: bool = False):
        options = [
            discord.SelectOption(
                label=f"{h} hour{'s' if h > 1 else ''}",
                value=str(h),
                default=(h == selected),
            )
            for h in range(1, max(1, max_hours) + 1)
        ]
        super().__init__(
            placeholder="Meeting length" if enabled else "Pick a window first…",
            options=options,
            min_values=1,
            max_values=1,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction):
        view: ScheduleFromBestView = self.view
        view.duration_hours = int(self.values[0])
        view.rebuild()
        await interaction.response.edit_message(embed=view.summary(), view=view)


class CreateEventButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Create Discord event", style=discord.ButtonStyle.green, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: ScheduleFromBestView = self.view
        if view.chosen is None:
            await interaction.response.send_message(
                embed=base_embed("Pick a window", "Choose one of the time windows above first.", COLOR_WARN),
                ephemeral=True,
            )
            return

        block = view.blocks[view.chosen]
        await interaction.response.defer(ephemeral=True)
        try:
            event = await create_meeting_event(
                interaction.guild,
                block["day"],
                block["start"],
                view.duration_hours,
                DEFAULT_EVENT_TITLE,
                DEFAULT_EVENT_LOCATION,
                block["people"],
                on_date=block.get("date"),
            )
        except Exception as exc:  # surfaced to the user by respond_to_event_error
            await respond_to_event_error(interaction, exc)
            return

        view.disable_all()
        await interaction.message.edit(view=view)
        await interaction.followup.send(embed=event_created_embed(event, block["people"]))
        view.stop()


class ScheduleFromBestView(discord.ui.View):
    """Attached to /best results so a window can become an event without retyping."""

    def __init__(self, blocks: list[dict], total_people: int):
        super().__init__(timeout=600)
        self.blocks = blocks[:25]  # Discord allows at most 25 select options
        self.total_people = total_people
        self.chosen: int | None = None
        self.duration_hours = 1
        self.rebuild()

    def choose(self, index: int) -> None:
        self.chosen = index
        # Default to the whole window -- if everyone is free 1PM-7PM, offering a
        # one-hour meeting by default is the thing the old version got wrong.
        self.duration_hours = self.blocks[index]["span"]
        self.rebuild()

    def rebuild(self) -> None:
        """Re-add every component so the length options match the chosen window."""
        self.clear_items()
        self.add_item(SlotSelect(self.blocks, self.total_people))
        span = self.blocks[self.chosen]["span"] if self.chosen is not None else 1
        self.add_item(DurationSelect(span, self.duration_hours, enabled=self.chosen is not None))
        self.add_item(CreateEventButton())

    def summary(self) -> discord.Embed:
        block = self.blocks[self.chosen]
        start = (
            window_start(block["date"], block["start"])
            if block.get("date")
            else next_occurrence(block["day"], block["start"])
        )
        end = start + timedelta(hours=self.duration_hours)
        embed = base_embed("Ready to schedule", f"**{block['day']} {block['label']}**", COLOR_OK)
        embed.add_field(
            name="Event will run",
            value=f"{discord.utils.format_dt(start, 'F')} → {discord.utils.format_dt(end, 't')}"
            f"\n({self.duration_hours} of the {block['span']} free hours)",
            inline=False,
        )
        embed.add_field(
            name=f"Free then ({len(block['people'])}/{self.total_people})",
            value=join_names(block["people"]),
            inline=False,
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Re-checked on every click, not just when the message was rendered.
        if can_schedule(interaction.user):
            return True
        await interaction.response.send_message(embed=no_permission_embed(), ephemeral=True)
        return False

    def disable_all(self) -> None:
        for item in self.children:
            item.disabled = True


# ---------- dashboard state ----------
async def dashboard_state() -> dict:
    """Everything the web dashboard renders, in one JSON payload."""
    data = load_data()
    total = len(data)

    events: list[dict] = []
    seen: set[int] = set()
    cutoff = datetime.now(TZ) - timedelta(hours=1)
    # Concurrently, and never blocking on a refresh -- see fetch_events.
    per_guild = await asyncio.gather(
        *(fetch_events(guild, allow_stale=True) for guild in bot.guilds),
        return_exceptions=True,
    )
    for scheduled in per_guild:
        if isinstance(scheduled, BaseException):
            log.warning("Skipping a guild's events: %s", scheduled)
            continue
        for event in scheduled:
            if not event.start_time or event.id in seen:
                continue
            seen.add(event.id)
            start = event.start_time.astimezone(TZ)
            if start < cutoff:
                continue
            end = (event.end_time or event.start_time + timedelta(hours=1)).astimezone(TZ)
            events.append(
                {
                    "name": event.name,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "count": event.user_count or 0,
                    "location": event.location,
                    "url": event.url,
                }
            )
    events.sort(key=lambda e: e["start"])

    ranked = rank_slots(data)
    counts: dict[str, dict[int, int]] = {}
    for (day, hour_index), people in ranked:
        counts.setdefault(day, {})[hour_index] = len(people)

    best = [
        {
            "day": window["day"],
            "date": window["date"].isoformat(),
            "hour": window["start"],
            "span": window["span"],
            "label": window["label"],
            "count": len(window["people"]),
            "people": window["people"],
        }
        for window in date_windows(data)[:12]
    ]

    today = datetime.now(TZ).date()

    return {
        "generated": datetime.now(TZ).isoformat(),
        "timezone": "US Eastern",
        "days": DAYS,
        "hourLabels": HOUR_LABELS,
        "totalPeople": total,
        "today": today.isoformat(),
        "seasonEnd": SEASON_END.isoformat(),
        # Per-date peaks so the calendar can shade real dates, including the
        # ones where someone has marked themselves away.
        "dayTotals": day_totals(data, today, SEASON_END),
        "people": [
            {
                "name": rec["name"],
                "avail": {d: e["hours"] for d, e in rec["avail"].items()},
                "off": sorted(off_dates(rec)),
            }
            for rec in sorted(data.values(), key=lambda r: r["name"].casefold())
        ],
        "counts": counts,
        "best": best,
        "events": events,
    }


# ---------- events ----------
@bot.event
async def on_ready():
    # Global syncs can take up to an hour to show up in Discord. Syncing to one
    # guild is near-instant, so set GUILD_ID in .env while you're iterating.
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        log.info("Commands synced to guild %s (instant)", GUILD_ID)
    else:
        await bot.tree.sync()
        log.info("Commands synced globally (can take up to an hour to appear)")
    log.info("Logged in as %s", bot.user)

    # Guarded so a reconnect (on_ready fires again) doesn't double-bind the port.
    global _dashboard_runner
    # Warm the event cache now so the first dashboard visit doesn't pay for it.
    for guild in bot.guilds:
        asyncio.create_task(_refresh_events(guild))

    if DASHBOARD_ENABLED and _dashboard_runner is None:
        _dashboard_runner = await start_dashboard(
            dashboard_state,
            DASHBOARD_HOST,
            DASHBOARD_PORT,
            get_user=dashboard_get_user,
            save_user=dashboard_save_user,
        )


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Say hello in the first channel we're actually allowed to talk in."""
    channel = guild.system_channel
    if channel is None or not channel.permissions_for(guild.me).send_messages:
        channel = next(
            (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
            None,
        )
    if channel:
        await channel.send(embed=help_embed())


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Catch-all so users see a friendly message instead of 'application did not respond'."""
    original = getattr(error, "original", error)
    log.error("Command error in /%s: %s", getattr(interaction.command, "name", "?"), original)
    log.debug("".join(traceback.format_exception(type(original), original, original.__traceback__)))

    # Error 10062/10008 mean the interaction itself expired or vanished, so
    # there is nothing left to reply to -- trying would just raise again.
    if isinstance(original, discord.NotFound) and original.code in (10062, 10008):
        log.warning("Interaction expired before it could be answered (took over 3s)")
        return

    if isinstance(original, discord.Forbidden):
        embed = base_embed(
            "I'm missing a permission",
            "I don't have the permissions I need for that. Ask a server admin to "
            "check my role — creating events needs **Manage Events**.",
            COLOR_WARN,
        )
    elif isinstance(original, (PermissionError, OSError)):
        embed = error_embed(
            "I couldn't write to my data file, so that change wasn't saved. "
            "If this folder is synced by OneDrive, pausing the sync usually fixes it."
        )
    else:
        embed = error_embed("That command hit an unexpected error. Nothing was saved — try again in a moment.")

    try:
        await send_embed(interaction, embed, ephemeral=True)
    except discord.HTTPException:
        log.exception("Could not deliver the error message to the user")


# ---------- commands ----------
@bot.tree.command(name="help", description="What this bot does and how to use it")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(embed=help_embed(), ephemeral=True)


@bot.tree.command(name="free", description="Set your free hours (10am-8pm, any day)")
async def free(interaction: discord.Interaction):
    view = AvailabilityView()
    await interaction.response.send_message(embed=view.render(), view=view, ephemeral=True)


@bot.tree.command(name="my-availability", description="Show the availability you currently have saved")
async def my_availability(interaction: discord.Interaction):
    data = load_data()
    uid = str(interaction.user.id)
    if uid not in data:
        embed = base_embed(
            "Nothing saved yet",
            "You haven't submitted any availability. Run `/free` to add some.",
            COLOR_WARN,
        )
    else:
        embed = availability_embed(interaction.user.display_name, data, uid)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def ensure_tunnel() -> PublicTunnel:
    """Start the public tunnel if it isn't up yet, then wait briefly for its URL.

    Lazy so a bot nobody shares links from never spawns cloudflared at all.
    """
    global _tunnel
    if _tunnel is None:
        _tunnel = PublicTunnel(DASHBOARD_PORT)
        _tunnel.start()
    # Quick tunnels typically hand back a URL in 1-3 seconds; wait a little
    # rather than immediately telling the user it isn't ready.
    for _ in range(20):
        if _tunnel.url or _tunnel.error:
            break
        await asyncio.sleep(0.25)
    return _tunnel


@bot.tree.command(name="editlink", description="Get your private link for editing your availability on the web")
async def editlink(interaction: discord.Interaction):
    if _dashboard_runner is None:
        await interaction.response.send_message(
            embed=base_embed("Dashboard offline", "The web dashboard isn't running on this bot.", COLOR_WARN),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    tunnel = await ensure_tunnel()
    base = tunnel.url or f"http://localhost:{DASHBOARD_PORT}"
    url = f"{base}/?key={make_edit_token(str(interaction.user.id))}#me"

    embed = base_embed(
        "Your private edit link",
        f"Open this to set your free hours on the web:\n**{url}**",
        COLOR_OK,
    )
    embed.add_field(
        name="Keep this one to yourself",
        value="This link edits **your** availability specifically. Anyone you send it to could "
        "change your hours — share the plain `/dashboard` link instead if you just want someone to look.",
        inline=False,
    )
    if not tunnel.url:
        embed.add_field(
            name="Note",
            value="No public tunnel is running, so this link only works on the computer hosting the bot.",
            inline=False,
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="dashboard", description="Get a link to the web dashboard, viewable by anyone")
async def dashboard(interaction: discord.Interaction):
    if _dashboard_runner is None:
        await interaction.response.send_message(
            embed=base_embed("Dashboard offline", "The web dashboard isn't running on this bot.", COLOR_WARN),
            ephemeral=True,
        )
        return

    global _tunnel

    await interaction.response.defer()
    tunnel = await ensure_tunnel()

    if tunnel.url:
        embed = base_embed(
            "Web dashboard",
            f"Anyone with this link can view it — no sign-in required:\n**{tunnel.url}**",
            COLOR_OK,
        )
        embed.add_field(
            name="Heads up",
            value="This shows the whole team's names and availability to anyone who has the link.\n"
            "Give it a few seconds to start working — the address takes a moment to go live.",
            inline=False,
        )
    elif tunnel.error == "cloudflared-missing":
        embed = base_embed(
            "Public link needs one extra file",
            "To make `/dashboard` work from anywhere, download **cloudflared** "
            f"and put `cloudflared.exe` in this bot's folder, then run `/dashboard` again:\n{CLOUDFLARED_RELEASE}",
            COLOR_WARN,
        )
        embed.add_field(
            name="Just testing on this computer?",
            value=f"Open **http://localhost:{DASHBOARD_PORT}** directly instead.",
            inline=False,
        )
    else:
        embed = error_embed(
            f"Couldn't set up the public link ({tunnel.error or 'still starting'}). "
            f"You can still open **http://localhost:{DASHBOARD_PORT}** on this computer."
        )
        _tunnel = None  # let the next /dashboard call try again from scratch

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="month", description="Month calendar you can page through to see events in advance")
@app_commands.describe(ahead="Jump ahead this many months (0 = this month)")
async def month(interaction: discord.Interaction, ahead: app_commands.Range[int, 0, 24] = 0):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=base_embed("Server only", "Events live in a server, not in DMs.", COLOR_WARN),
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    now = datetime.now(TZ)
    year, month_number = shift_month(now.year, now.month, ahead)
    view = MonthView(interaction.guild, year, month_number)
    await view.reload()
    await interaction.followup.send(embed=view.render(), view=view)


@bot.tree.command(name="events", description="Upcoming team events, grouped by date")
async def events(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=base_embed("Server only", "Events live in a server, not in DMs.", COLOR_WARN),
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    # with_counts gives us the RSVP numbers shown on each card.
    scheduled = await fetch_events(interaction.guild)

    cutoff = datetime.now(TZ) - timedelta(hours=1)
    upcoming = sorted(
        (e for e in scheduled if e.start_time and e.start_time.astimezone(TZ) >= cutoff),
        key=lambda e: e.start_time,
    )
    if not upcoming:
        await interaction.followup.send(
            embed=base_embed(
                "No upcoming events",
                "Nothing on the calendar yet. Run `/best` and schedule one.",
                COLOR_WARN,
            )
        )
        return

    items = [
        {
            "title": event.name,
            "start": event.start_time.astimezone(TZ),
            "end": (event.end_time or event.start_time + timedelta(hours=1)).astimezone(TZ),
            "count": event.user_count or 0,
            "subtitle": f"📍 {event.location}" if event.location else None,
            "url": event.url,
            "key": event.name,
        }
        for event in upcoming
    ]
    content, embeds = build_agenda("📅 Upcoming Events", items)
    await interaction.followup.send(content=content, embeds=embeds)


@bot.tree.command(name="clear", description="Clear one day of your availability, or all of it")
@app_commands.describe(day="Clear only this day. Leave blank to clear every day.")
@app_commands.choices(day=[app_commands.Choice(name=d, value=d) for d in DAYS])
async def clear(interaction: discord.Interaction, day: app_commands.Choice[str] | None = None):
    data = load_data()
    uid = str(interaction.user.id)
    record = data.get(uid)

    if record is None:
        await interaction.response.send_message(
            embed=base_embed("Nothing to clear", "You didn't have any availability saved.", COLOR_WARN),
            ephemeral=True,
        )
        return

    if day is None:
        del data[uid]
        save_data(data)
        embed = base_embed("Cleared everything", "All of your saved days are gone. Run `/free` to start over.", COLOR_OK)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if record["avail"].pop(day.value, None) is None:
        await interaction.response.send_message(
            embed=base_embed("Nothing to clear", f"You had nothing saved for **{day.value}**.", COLOR_WARN),
            ephemeral=True,
        )
        return

    # A user with no days left shouldn't linger in the file and inflate the
    # "N members submitted" count that /best ranks against.
    if not record["avail"]:
        del data[uid]
    save_data(data)

    remaining = ", ".join(d for d in DAYS if d in record["avail"])
    embed = base_embed("Day cleared", f"**{day.value}** removed from your availability.", COLOR_OK)
    embed.add_field(name="Still saved", value=remaining or "*nothing — you're fully cleared*", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Show who has submitted availability")
async def status(interaction: discord.Interaction):
    data = load_data()
    if not data:
        await interaction.response.send_message(
            embed=base_embed("No submissions yet", "Nobody has run `/free` yet. Be the first!", COLOR_WARN)
        )
        return

    embed = base_embed("Availability submitted", f"**{len(data)}** member(s) have submitted.", COLOR_OK)
    # Embeds allow at most 25 fields.
    records = sorted(data.values(), key=lambda r: r["name"].casefold())
    for record in records[:25]:
        summary = ", ".join(
            f"{day} ({len(record['avail'][day]['hours'])}h)" for day in DAYS if day in record["avail"]
        )
        embed.add_field(name=record["name"], value=summary or "—", inline=False)
    if len(records) > 25:
        embed.description += f"\n*Showing the first 25 of {len(records)}.*"
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="best", description="Find the best overlapping meeting times")
@app_commands.describe(top="How many top slots to show (default 5)")
async def best(interaction: discord.Interaction, top: app_commands.Range[int, 1, 25] = 5):
    data = load_data()
    if not data:
        await interaction.response.send_message(
            embed=base_embed(
                "No availability yet",
                "Nobody has submitted times. Run `/free` to add yours, then try `/best` again.",
                COLOR_WARN,
            )
        )
        return

    blocks = date_windows(data)[:top]
    total_people = len(data)
    if not blocks:
        await interaction.response.send_message(
            embed=base_embed(
                "Nothing in the next few weeks",
                "Everyone who submitted is marked away for the whole period.",
                COLOR_WARN,
            )
        )
        return

    embed = base_embed(
        "Best meeting times",
        f"Ranked by how many of the **{total_people}** submitted members are free, "
        "on real upcoming dates. Consecutive free hours are shown as one window.",
        COLOR_OK,
    )
    for i, block in enumerate(blocks):
        medal = MEDALS[i] if i < len(MEDALS) else f"**{i + 1}.**"
        span = f" · {block['span']}h window" if block["span"] > 1 else ""
        when = f"{block['day']} {block['date']:%b %-d}" if os.name != "nt" else \
               f"{block['day']} {block['date']:%b} {block['date'].day}"
        embed.add_field(
            name=f"{medal} {when}, {block['label']} — {len(block['people'])}/{total_people} free{span}",
            value=join_names(block["people"]),
            inline=False,
        )

    view = ScheduleFromBestView(blocks, total_people) if interaction.guild else None
    if view:
        who = " / ".join(SCHEDULE_ROLE_NAMES) or "Anyone"
        embed.description += f"\n\n*{who} can pick a slot below to turn it into a Discord event.*"
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="schedule", description="Create a Discord event for a specific day and hour")
@app_commands.describe(
    day="Day of the week for the meeting",
    hour="Which hourly slot the meeting starts in",
    duration="How many hours the meeting runs (default 1)",
    title="Event title (default 'FTC Team Meeting')",
    location="Where the meeting happens",
)
@app_commands.choices(
    day=[app_commands.Choice(name=d, value=d) for d in DAYS],
    hour=[app_commands.Choice(name=HOUR_LABELS[i], value=i) for i in range(len(HOUR_LABELS))],
)
async def schedule(
    interaction: discord.Interaction,
    day: app_commands.Choice[str],
    hour: app_commands.Choice[int],
    duration: app_commands.Range[int, 1, 8] = 1,
    title: str = DEFAULT_EVENT_TITLE,
    location: str = DEFAULT_EVENT_LOCATION,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=base_embed("Server only", "Scheduled events can only be created inside a server.", COLOR_WARN),
            ephemeral=True,
        )
        return

    if not can_schedule(interaction.user):
        await interaction.response.send_message(embed=no_permission_embed(), ephemeral=True)
        return

    await interaction.response.defer()
    data = load_data()
    # Everyone free for the whole requested span, not just its first hour.
    attendees = sorted(
        set.intersection(
            *(
                set(people_free(data, day.value, h))
                for h in range(hour.value, min(hour.value + duration, len(HOUR_LABELS)))
            )
        )
    )
    try:
        event = await create_meeting_event(
            interaction.guild, day.value, hour.value, duration, title, location, attendees
        )
    except Exception as exc:  # surfaced to the user by respond_to_event_error
        await respond_to_event_error(interaction, exc)
        return

    await interaction.followup.send(embed=event_created_embed(event, attendees))


def main() -> None:
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Put it in a .env file next to bot.py.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
# 