"""Discord bot that collects team availability and turns overlaps into real
Discord Scheduled Events.

Storage stays as a plain JSON file (see DATA_FILE) on purpose -- no database.
"""

from __future__ import annotations

import asyncio
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
# date_cls is the same class under a second name: /reschedule takes a parameter
# called "date" (that is what Discord shows the user), which shadows it locally.
from datetime import date, date as date_cls, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from dashboard import start_dashboard

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

# Render's filesystem is wiped on every deploy, so availability lives in the
# linked Key Value store when one is configured. Falls back to DATA_FILE for
# local runs, keeping the same JSON shape either way.
REDIS_URL = (
    os.environ.get("REDIS_URL")
    or os.environ.get("KEY_VALUE_URL")
    or os.environ.get("RENDER_REDIS_URL")
    or ""
)
REDIS_KEY = os.environ.get("REDIS_KEY", "ftc:availability")
REDIS_WRITE_RETRIES = 3
PRUNE_INTERVAL = 3600  # seconds between expiry sweeps when Redis-backed

# Render (and most hosts) require binding 0.0.0.0 on the port they hand you in
# $PORT. Locally that means the dashboard is also reachable from your LAN; set
# DASHBOARD_HOST=127.0.0.1 in .env if you'd rather keep it to your own machine.
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT") or "8080")
DASHBOARD_ENABLED = os.environ.get("DASHBOARD", "1") != "0"

# Render exposes the service's permanent URL as RENDER_EXTERNAL_URL. Everything
# that hands out a link uses this, falling back to localhost for local runs.
PUBLIC_URL = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL") or "").rstrip("/")


def public_base_url() -> str:
    return PUBLIC_URL or f"http://localhost:{DASHBOARD_PORT}"

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

# Events are scheduled on a finer grid than availability is collected on: people
# tell us which *hours* they are free, but a meeting can start at 8:15 and run
# for 45 minutes. SLOT_MINUTES is the granularity every event time snaps to.
SLOT_MINUTES = 15
MINUTE_CHOICES = list(range(0, 60, SLOT_MINUTES))

# Lengths offered when creating or editing an event, in minutes. Fine-grained
# near the bottom where quarter-hours actually matter, coarser further out.
DURATION_MINUTES = [15, 30, 45, 60, 75, 90, 105, 120, 150, 180, 210, 240, 300, 360, 420, 480]


def fmt_clock(hour: int, minute: int = 0) -> str:
    """'8:15 AM' for a wall-clock time."""
    suffix = "AM" if hour % 24 < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def fmt_duration(minutes: int) -> str:
    """'1 hour 30 minutes' for a length in minutes."""
    hours, mins = divmod(int(minutes), 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours > 1 else ''}")
    if mins:
        parts.append(f"{mins} minutes")
    return " ".join(parts) or "0 minutes"


def clock_range(start: datetime, end: datetime) -> str:
    """'8:15 AM – 9:45 AM' for a concrete event window."""
    return f"{fmt_clock(start.hour, start.minute)} – {fmt_clock(end.hour, end.minute)}"


# ---------- storage ----------
# On disk:
#   { "<user_id>": {
#       "name": str,
#       "avail": { "Monday": {"hours": [0, 1, 2], "updated": "<iso8601>"} },
#       "dates": { "2026-08-18": [7, 8, 9] },
#       "off":   ["2026-08-25"]
#   } }
# The ints in "hours" and in "dates" are indices into HOUR_LABELS.
#
# "avail" is the standing weekly pattern. "dates" overrides it for one specific
# date -- free 5-8 this Tuesday but only 3-4 the next -- and an empty list there
# means "free for nothing that date". "off" is the shorthand for a whole day
# away and wins over both. See free_on for the precedence.
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


def _clean_hours(raw: Any) -> list[int]:
    """Sorted, de-duplicated, in-range hour indices from untrusted input."""
    if not isinstance(raw, list):
        return []
    return sorted({h for h in raw if isinstance(h, int) and 0 <= h < len(HOUR_LABELS)})


def _migrate_record(record: dict) -> dict:
    """Coerce one user record into the current shape, dropping junk."""
    avail: dict[str, dict] = {}
    for day, value in (record.get("avail") or {}).items():
        if day not in DAYS:
            continue
        raw_hours = value if isinstance(value, list) else (value or {}).get("hours", [])
        updated = None if isinstance(value, list) else (value or {}).get("updated")
        hours = _clean_hours(raw_hours)
        if hours:
            avail[day] = {"hours": hours, "updated": updated}

    # Keep only well-formed, still-relevant off dates and per-date overrides, so
    # neither can grow without bound as the season goes by.
    today = datetime.now(TZ).date().isoformat()
    off = sorted(
        {
            value
            for value in (record.get("off") or [])
            if isinstance(value, str) and _valid_date(value) and value >= today
        }
    )
    dates = {
        key: _clean_hours(value)
        for key, value in sorted((record.get("dates") or {}).items())
        if isinstance(key, str) and _valid_date(key) and key >= today
    }
    return {
        "name": str(record.get("name") or "Unknown"),
        "avail": avail,
        "dates": dates,
        "off": off,
    }


def _valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _prune_expired(data: dict) -> bool:
    """Drop day entries past the TTL and overrides for dates already gone.

    Returns True if anything changed.
    """
    cutoff = datetime.now(TZ) - AVAILABILITY_TTL
    today = datetime.now(TZ).date().isoformat()
    changed = False
    for uid in list(data):
        record = data[uid]
        avail = record["avail"]
        for day in list(avail):
            ts = _parse_ts(avail[day].get("updated"))
            if ts and ts < cutoff:
                del avail[day]
                changed = True
        # A date override is self-expiring: once the date is past it can never
        # apply again. Same for a day marked away.
        for key in [k for k in record.get("dates", {}) if k < today]:
            del record["dates"][key]
            changed = True
        stale_off = [k for k in record.get("off", []) if k < today]
        if stale_off:
            record["off"] = [k for k in record["off"] if k >= today]
            changed = True
        # Someone may have only date-specific hours and no weekly pattern.
        if not avail and not record.get("dates"):
            del data[uid]
            changed = True
    return changed


# Commands run on the asyncio event loop and Discord kills an interaction that
# isn't answered within 3 seconds, so disk access has to stay off the hot path.
# We keep the parsed data in memory and only re-read when the file changes.
_cache: dict | None = None
_cache_stamp: tuple[int, int] | None = None

# Key Value plumbing. Writes are queued rather than awaited so that saving never
# blocks a slash command on a network round trip; _drain_writes coalesces bursts
# and retries, and storage_close() flushes anything outstanding on shutdown.
_redis: Any = None
_pending_payload: str | None = None
_write_task: "asyncio.Task | None" = None
_last_prune = 0.0


def _queue_redis_write(payload: str) -> None:
    global _pending_payload, _write_task
    _pending_payload = payload
    if _write_task is None or _write_task.done():
        try:
            _write_task = asyncio.get_running_loop().create_task(_drain_writes())
        except RuntimeError:
            # No loop (tests, or a save during shutdown) -- write synchronously.
            _write_task = None
            log.debug("No running loop; skipping async key-value write")


async def _drain_writes() -> None:
    global _pending_payload
    while _pending_payload is not None:
        payload, _pending_payload = _pending_payload, None
        for attempt in range(REDIS_WRITE_RETRIES):
            try:
                await _redis.set(REDIS_KEY, payload)
                break
            except Exception:
                if attempt == REDIS_WRITE_RETRIES - 1:
                    log.exception("Could not save availability to the key-value store")
                else:
                    await asyncio.sleep(0.5 * (attempt + 1))


async def storage_init() -> None:
    """Connect to the Key Value store and load availability into memory.

    Falls back to the local JSON file when no store is configured, so running
    the bot on your own machine needs no extra setup.
    """
    global _redis, _cache, _cache_stamp, _last_prune

    if not REDIS_URL:
        log.info("No key-value store configured; using %s", DATA_FILE)
        return

    try:
        import redis.asyncio as redis_async
    except ImportError:
        log.error("REDIS_URL is set but the 'redis' package isn't installed; falling back to %s", DATA_FILE)
        return

    try:
        client = redis_async.from_url(REDIS_URL, decode_responses=True)
        await client.ping()
    except Exception:
        log.exception("Could not reach the key-value store; falling back to %s", DATA_FILE)
        return

    _redis = client
    _last_prune = time.monotonic()

    raw_text = None
    from_store = True
    try:
        raw_text = await _redis.get(REDIS_KEY)
    except Exception:
        log.exception("Could not read availability from the key-value store")

    if not raw_text:
        # First run against an empty store: adopt any local file so existing
        # availability carries over instead of silently starting from scratch.
        from_store = False
        try:
            raw_text = DATA_FILE.read_text(encoding="utf-8")
            log.info("Key-value store was empty; seeding it from %s", DATA_FILE)
        except OSError:
            raw_text = None

    parsed: dict = {}
    if raw_text:
        try:
            loaded = json.loads(raw_text)
            if isinstance(loaded, dict):
                parsed = loaded
            else:
                raise ValueError("stored value is not a JSON object")
        except (json.JSONDecodeError, ValueError):
            log.exception("Stored availability was unreadable; keeping a copy under %s.corrupt", REDIS_KEY)
            try:
                await _redis.set(f"{REDIS_KEY}.corrupt", raw_text)
            except Exception:
                log.warning("Could not stash the unreadable value")

    data, _ = _normalise(parsed)
    _cache, _cache_stamp = data, None
    log.info("Loaded %d member(s) from the key-value store", len(data))

    # Write back whenever the store doesn't already hold exactly this — covers
    # seeding from a local file, migrating an older shape, and pruning.
    serialised = json.dumps(data, indent=2)
    if not from_store or serialised != raw_text:
        _queue_redis_write(serialised)


async def storage_close() -> None:
    """Flush any queued write, then close the connection."""
    if _redis is None:
        return
    if _write_task is not None and not _write_task.done():
        try:
            await asyncio.wait_for(_write_task, timeout=10)
        except (asyncio.TimeoutError, Exception):
            log.warning("Timed out flushing the final availability write")
    if _pending_payload is not None:
        await _drain_writes()
    try:
        await _redis.aclose()
    except Exception:
        pass


def _file_stamp() -> tuple[int, int] | None:
    try:
        stat = DATA_FILE.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _normalise(raw: dict) -> tuple[dict, bool]:
    """Migrate every record to the current shape and drop expired entries."""
    data = {str(uid): _migrate_record(rec) for uid, rec in raw.items() if isinstance(rec, dict)}
    # Keep anyone with a weekly pattern *or* date-specific hours.
    data = {uid: rec for uid, rec in data.items() if rec["avail"] or rec["dates"]}
    pruned = _prune_expired(data)
    if pruned:
        log.info("Pruned availability older than %s", AVAILABILITY_TTL)
    return data, pruned


def load_data() -> dict:
    """Read, migrate and prune the availability data.

    Returns a private copy: callers mutate what they get back, and that must
    not corrupt the cache. Never raises -- corrupt storage is set aside so a
    stray character can't take every command down.
    """
    global _cache, _cache_stamp, _last_prune

    if _redis is not None:
        # Key Value backed: this process is the only writer, so the in-memory
        # copy stays authoritative between saves. It's filled by storage_init().
        if _cache is None:
            return {}
        # Nothing re-reads storage here, so sweep expiries on a timer instead.
        if time.monotonic() - _last_prune > PRUNE_INTERVAL:
            _last_prune = time.monotonic()
            if _prune_expired(_cache):
                log.info("Pruned availability older than %s", AVAILABILITY_TTL)
                _queue_redis_write(json.dumps(_cache, indent=2))
        return copy.deepcopy(_cache)

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

    data, _ = _normalise(raw)

    # Only write when migrating or pruning actually changed something. Writing
    # on every read is what previously made commands slow enough to time out.
    if json.dumps(data, indent=2) != text:
        save_data(data)
    else:
        _cache, _cache_stamp = copy.deepcopy(data), stamp
    return data


def save_data(data: dict) -> None:
    """Persist availability, then refresh the in-memory cache.

    With a Key Value store the write is queued and flushed on the event loop so
    a slash command never waits on the network. Otherwise it goes to disk,
    preferring an atomic temp-file swap -- Windows raises PermissionError
    (WinError 5) from os.replace whenever the destination is momentarily locked
    (OneDrive, antivirus, open editors), so we retry then write in place, which
    is less safe but far better than losing the save.
    """
    global _cache, _cache_stamp

    payload = json.dumps(data, indent=2)

    if _redis is not None:
        _cache, _cache_stamp = copy.deepcopy(data), None
        _queue_redis_write(payload)
        return

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
    """Key for signing edit links.

    Prefer DASHBOARD_SECRET from the environment: hosts like Render give each
    deploy a fresh filesystem, so a secret written to disk would change on every
    deploy and invalidate everyone's saved edit links.
    """
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
            pass
        if PUBLIC_URL:
            log.warning(
                "DASHBOARD_SECRET is not set. On a hosted deploy the generated key does not "
                "survive a restart, so every /dashboard URL will stop working. Set it as an "
                "environment variable."
            )
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
        "dates": dict(record.get("dates") or {}) if record else {},
        "off": sorted(off_dates(record)) if record else [],
        "today": datetime.now(TZ).date().isoformat(),
        "seasonEnd": SEASON_END.isoformat(),
    }


async def dashboard_save_user(
    token: str, avail: dict, off: list, name: str | None, dates: dict | None = None
) -> dict | None:
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
        valid = _clean_hours(hours)
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

    # An override with an empty list is meaningful ("free for nothing that
    # date"), so unlike the weekly pattern it is kept rather than dropped.
    overrides: dict[str, list[int]] = {}
    for key, hours in (dates or {}).items():
        if not isinstance(key, str) or not _valid_date(key):
            continue
        if not (today <= key <= SEASON_END.isoformat()) or not isinstance(hours, list):
            continue
        overrides[key] = _clean_hours(hours)
    overrides = dict(sorted(overrides.items()))

    data = load_data()
    chosen = (name or "").strip()[:32] if isinstance(name, str) else ""
    final_name = chosen or await resolve_name(user_id, data)

    if cleaned or overrides:
        stamp = _now_iso()
        data[user_id] = {
            "name": final_name,
            "avail": {day: {"hours": hours, "updated": stamp} for day, hours in cleaned.items()},
            "dates": overrides,
            "off": away,
        }
    else:
        # Clearing every slot means leaving, not lingering with an empty record.
        data.pop(user_id, None)
    save_data(data)
    log.info("%s updated availability from the web editor", final_name)
    return {
        "ok": True,
        "name": final_name,
        "avail": cleaned,
        "dates": overrides,
        "off": away,
    }


# ---------- time helpers ----------
def next_occurrence(day: str, hour: int, minute: int = 0) -> datetime:
    """The next upcoming `day` at that wall-clock time, in US Eastern.

    Arithmetic is done on a naive datetime and localized afterwards so that
    crossing a DST boundary keeps the wall-clock time the team agreed on.
    """
    now = datetime.now(TZ)
    naive_now = now.replace(tzinfo=None)
    candidate = naive_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidate += timedelta(days=(DAYS.index(day) - candidate.weekday()) % 7)
    # Discord rejects events that start in the past; skip a week if it's too close.
    if candidate <= naive_now + timedelta(minutes=5):
        candidate += timedelta(days=7)
    return candidate.replace(tzinfo=TZ)


def hour_range_label(start: int, end: int) -> str:
    """'1PM-8PM' for the inclusive slot range start..end."""
    return f"{HOUR_START_LABELS[start]}-{fmt_hour(START_HOUR + end).split('-')[1]}"


# ---------- date-aware availability ----------
# The weekly pattern in "avail" is the standing arrangement. "off" lists
# specific dates a member is away, so nobody has to re-enter their whole week
# just because they're busy one Saturday. "dates" overrides the weekly hours on
# one date, which is what makes "free 5-8 this Tuesday, 3-4 the next" possible.
SEASON_END = date(2027, 2, 28)


def off_dates(record: dict) -> set[str]:
    return set(record.get("off") or [])


def hours_on(record: dict, day: date) -> list[int]:
    """The hours one member is free on a concrete date.

    Precedence: a day marked away beats everything, then a date-specific
    override, then the standing weekly pattern.
    """
    key = day.isoformat()
    if key in off_dates(record):
        return []
    override = (record.get("dates") or {}).get(key)
    if override is not None:
        return list(override)
    return list(record["avail"].get(DAYS[day.weekday()], {}).get("hours", []))


def has_override(record: dict, day: date) -> bool:
    return day.isoformat() in (record.get("dates") or {})


def free_on(data: dict, day: date) -> dict[int, list[str]]:
    """hour index -> names of everyone free at that hour on this specific date."""
    result: dict[int, list[str]] = {}
    for record in data.values():
        for hour_index in hours_on(record, day):
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
            if window_start(day, START_HOUR + block["start"]) <= now:
                continue
            windows.append({**block, "date": day, "day": DAYS[day.weekday()]})
    windows.sort(key=lambda w: (-len(w["people"]), -w["span"], w["date"], w["start"]))
    return windows


def window_start(day: date, hour: int, minute: int = 0) -> datetime:
    """A concrete date at a wall-clock time, in US Eastern."""
    return datetime.combine(day, dtime(hour=hour, minute=minute), tzinfo=TZ)


def slot_start(day: date, hour_index: int, minute: int = 0) -> datetime:
    """Start of an availability slot on a concrete date, in US Eastern."""
    return window_start(day, START_HOUR + hour_index, minute)


def overlapped_slots(hour: int, minute: int, duration_minutes: int) -> list[int]:
    """Availability hour indices an event at this time actually overlaps.

    Events live on a 15-minute grid while availability is collected per hour, so
    "who is free then" means "who is free for every hour the event touches".
    Hours outside the collected 10am-8pm range simply have no data.
    """
    start = hour * 60 + minute
    end = start + duration_minutes
    return [
        i
        for i in range(len(HOUR_LABELS))
        if (START_HOUR + i) * 60 < end and (START_HOUR + i + 1) * 60 > start
    ]


def attendees_for(
    data: dict, on_date: date, hour: int, minute: int, duration_minutes: int
) -> list[str]:
    """Everyone free for *every* availability hour the event runs across.

    Resolved against the concrete date, so per-date overrides and days marked
    away are both respected.
    """
    slots = overlapped_slots(hour, minute, duration_minutes)
    if not slots:
        return []
    free = free_on(data, on_date)
    return sorted(set.intersection(*(set(free.get(h, [])) for h in slots)))


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


def full_date_label(day: date) -> str:
    """'Monday, August 10, 2026' -- %-d isn't portable to Windows, hence day."""
    return f"{DAYS[day.weekday()]}, {day:%B} {day.day}, {day.year}"


def day_detail(data: dict, day: date) -> dict:
    """Everything the dashboard shows for one *specific* date.

    Free hours are computed for this exact date rather than for its weekday, so
    two Mondays with different away-lists give different answers.
    """
    free = free_on(data, day)
    key = day.isoformat()
    away = sorted(rec["name"] for rec in data.values() if key in off_dates(rec))
    # Who is on date-specific hours here rather than their usual weekly ones --
    # this is what explains two Tuesdays looking different. Being away wins over
    # an override, so those people are reported as away and not listed twice.
    custom = sorted(
        rec["name"]
        for rec in data.values()
        if has_override(rec, day) and key not in off_dates(rec)
    )
    hours = [
        {
            "index": i,
            "label": HOUR_LABELS[i],
            "start": fmt_clock(START_HOUR + i),
            "end": fmt_clock(START_HOUR + i + 1),
            "people": free.get(i, []),
            "count": len(free.get(i, [])),
        }
        for i in range(len(HOUR_LABELS))
    ]
    windows = [
        {
            **block,
            "count": len(block["people"]),
            "startLabel": fmt_clock(START_HOUR + block["start"]),
            "endLabel": fmt_clock(START_HOUR + block["end"] + 1),
        }
        for block in merge_hours(free)
    ]
    return {
        "date": key,
        "weekday": DAYS[day.weekday()],
        "label": full_date_label(day),
        "totalPeople": len(data),
        "freeHours": sum(1 for hour in hours if hour["count"]),
        "peak": max((hour["count"] for hour in hours), default=0),
        "hours": hours,
        "windows": windows,
        "away": away,
        "custom": custom,
        "isPast": day < datetime.now(TZ).date(),
        "inSeason": day <= SEASON_END,
    }


async def dashboard_day(raw_date: str) -> dict | None:
    """Free hours for one date, for the dashboard's date selector."""
    if not _valid_date(raw_date or ""):
        return None
    return day_detail(load_data(), date.fromisoformat(raw_date))


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
    embed.add_field(
        name="/dashboard",
        value="**Start here.** Your private link to the web dashboard: set your weekly hours, "
        "give a single date its own hours (free 5-8 this Tuesday but 3-4 the next), "
        "mark days you're away, and see the calendar, heatmap and team views.",
        inline=False,
    )
    embed.add_field(name="/best", value="Rank the times that work for the most people. Pick one right from the results to create an event.", inline=False)
    embed.add_field(name="/events", value="Every upcoming team event, grouped by date, with RSVP counts.", inline=False)
    only = f" *{' / '.join(SCHEDULE_ROLE_NAMES)} only.*" if SCHEDULE_ROLE_NAMES else ""
    embed.add_field(
        name="/schedule",
        value="Create a Discord Scheduled Event. Start times come in 15-minute steps "
        "(8:00, 8:15, 8:30, 8:45…) and the length can be as short as 15 minutes." + only,
        inline=False,
    )
    embed.add_field(
        name="/reschedule",
        value="Move an existing event to a new date and start time, on the same "
        "15-minute grid." + only,
        inline=False,
    )
    embed.add_field(name="/help", value="Show this message.", inline=False)
    embed.add_field(
        name="Heads up",
        value=f"Availability expires after {AVAILABILITY_TTL.days // 7} weeks so old data doesn't skew "
        "results — open `/dashboard` to refresh it.",
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


# ---------- month calendar ----------
WEEKDAY_HEADS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


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
        "a meeting. You can still use `/dashboard` and `/best`.",
        COLOR_WARN,
    )


# ---------- event creation ----------
async def create_meeting_event(
    guild: discord.Guild,
    day: str,
    hour: int,
    minute: int,
    duration_minutes: int,
    title: str,
    location: str,
    attendees: list[str],
    on_date: date | None = None,
) -> discord.ScheduledEvent:
    """Create a native Discord Scheduled Event.

    hour/minute are wall-clock, on the SLOT_MINUTES grid, so a meeting can start
    at 8:15 rather than only on the hour. Uses on_date when the caller already
    knows the exact date (as /best now does); otherwise falls back to the next
    occurrence of that weekday.
    """
    start = (
        window_start(on_date, hour, minute) if on_date else next_occurrence(day, hour, minute)
    )
    end = start + timedelta(minutes=duration_minutes)

    when = f"{full_date_label(start.date())} · {clock_range(start, end)}"
    description = f"{when} ({fmt_duration(duration_minutes)}).\n"
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

_dashboard_runner = None  # set once the web dashboard binds; see main()
_synced = False           # command tree synced once per process; see on_ready

# Scheduled events change rarely but were being re-fetched from Discord on every
# dashboard poll (every 15s, per guild) and every /events press, which
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
    if not force and fresh and cached is not None:
        return cached[1]

    if allow_stale and cached is not None and not force:
        if guild.id not in _refreshing:
            _refreshing.add(guild.id)
            asyncio.create_task(_refresh_events(guild))
        return cached[1]

    _refreshing.add(guild.id)
    return await _refresh_events(guild)


# ---------- availability UI ----------


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
        super().__init__(
            placeholder="Pick a window to schedule…",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view = cast(ScheduleFromBestView, self.view)
        view.choose(int(self.values[0]))
        await interaction.response.edit_message(embed=view.summary(), view=view)


def grid_step(total_minutes: int, limit: int = 25) -> int:
    """Finest step (from SLOT_MINUTES up) that keeps a select within `limit`."""
    for step in (SLOT_MINUTES, 30, 60, 120):
        if total_minutes // step <= limit:
            return step
    return 120


class StartSelect(discord.ui.Select):
    """Quarter-hour start times inside the chosen window."""

    def __init__(self, base: datetime | None = None, span_minutes: int = 60,
                 selected: int = 0, enabled: bool = False):
        options: list[discord.SelectOption] = []
        if base is not None:
            # Leave room for at least one slot of meeting after the start.
            usable = max(SLOT_MINUTES, span_minutes - SLOT_MINUTES)
            step = grid_step(usable)
            for offset in range(0, usable + 1, step):
                moment = base + timedelta(minutes=offset)
                options.append(
                    discord.SelectOption(
                        label=fmt_clock(moment.hour, moment.minute),
                        value=str(offset),
                        default=(offset == selected),
                    )
                )
        if not options:
            options = [discord.SelectOption(label="—", value="0")]
        super().__init__(
            placeholder="Start time" if enabled else "Pick a window first…",
            options=options,
            min_values=1,
            max_values=1,
            disabled=not enabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view = cast(ScheduleFromBestView, self.view)
        view.start_offset = int(self.values[0])
        view.rebuild()
        await interaction.response.edit_message(embed=view.summary(), view=view)


class DurationSelect(discord.ui.Select):
    """Length options in 15-minute steps, capped to what's left of the window."""

    def __init__(self, max_minutes: int = 60, selected: int = 60, enabled: bool = False):
        step = grid_step(max(SLOT_MINUTES, max_minutes))
        options = [
            discord.SelectOption(
                label=fmt_duration(minutes),
                value=str(minutes),
                default=(minutes == selected),
            )
            for minutes in range(step, max(step, max_minutes) + 1, step)
        ]
        super().__init__(
            placeholder="Meeting length" if enabled else "Pick a window first…",
            options=options,
            min_values=1,
            max_values=1,
            disabled=not enabled,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        view = cast(ScheduleFromBestView, self.view)
        view.duration_minutes = int(self.values[0])
        view.rebuild()
        await interaction.response.edit_message(embed=view.summary(), view=view)


class CreateEventButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Create Discord event", style=discord.ButtonStyle.green, row=3)

    async def callback(self, interaction: discord.Interaction):
        view = cast(ScheduleFromBestView, self.view)
        if view.chosen is None:
            await interaction.response.send_message(
                embed=base_embed("Pick a window", "Choose one of the time windows above first.", COLOR_WARN),
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                embed=base_embed("Server only", "Events can only be created inside a server.", COLOR_WARN),
                ephemeral=True,
            )
            return

        block = view.blocks[view.chosen]
        start = view.start_time()
        await interaction.response.defer(ephemeral=True)
        try:
            event = await create_meeting_event(
                interaction.guild,
                block["day"],
                start.hour,
                start.minute,
                view.duration_minutes,
                DEFAULT_EVENT_TITLE,
                DEFAULT_EVENT_LOCATION,
                block["people"],
                on_date=start.date(),
            )
        except Exception as exc:  # surfaced to the user by respond_to_event_error
            await respond_to_event_error(interaction, exc)
            return

        view.disable_all()
        if interaction.message is not None:
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
        self.start_offset = 0          # minutes past the window's first hour
        self.duration_minutes = 60
        self.rebuild()

    def choose(self, index: int) -> None:
        self.chosen = index
        # Default to the whole window -- if everyone is free 1PM-7PM, offering a
        # one-hour meeting by default is the thing the old version got wrong.
        self.start_offset = 0
        self.duration_minutes = self.blocks[index]["span"] * 60
        self.rebuild()

    def window_base(self) -> datetime:
        """Wall-clock start of the chosen free window."""
        block = self.blocks[cast(int, self.chosen)]
        return (
            slot_start(block["date"], block["start"])
            if block.get("date")
            else next_occurrence(block["day"], START_HOUR + block["start"])
        )

    def start_time(self) -> datetime:
        return self.window_base() + timedelta(minutes=self.start_offset)

    def rebuild(self) -> None:
        """Re-add every component so the time options match the chosen window."""
        base = None
        span_minutes = 60
        if self.chosen is not None:
            base = self.window_base()
            span_minutes = self.blocks[self.chosen]["span"] * 60
            # Keep the selection inside the window after either select changes.
            self.start_offset = min(self.start_offset, max(0, span_minutes - SLOT_MINUTES))
            remaining = span_minutes - self.start_offset
            step = grid_step(max(SLOT_MINUTES, remaining))
            self.duration_minutes = min(
                max(step, self.duration_minutes - self.duration_minutes % step), remaining
            )

        self.clear_items()
        self.add_item(SlotSelect(self.blocks, self.total_people))
        self.add_item(
            StartSelect(base, span_minutes, self.start_offset, enabled=self.chosen is not None)
        )
        self.add_item(
            DurationSelect(
                span_minutes - self.start_offset,
                self.duration_minutes,
                enabled=self.chosen is not None,
            )
        )
        self.add_item(CreateEventButton())

    def summary(self) -> discord.Embed:
        # Only ever called once a window has been picked.
        assert self.chosen is not None
        block = self.blocks[self.chosen]
        start = self.start_time()
        end = start + timedelta(minutes=self.duration_minutes)
        embed = base_embed(
            "Ready to schedule",
            f"**{full_date_label(start.date())}**\n{clock_range(start, end)}",
            COLOR_OK,
        )
        embed.add_field(
            name="Event will run",
            value=f"{discord.utils.format_dt(start, 'F')} → {discord.utils.format_dt(end, 't')}"
            f"\n({fmt_duration(self.duration_minutes)} of the {block['span']}h free window "
            f"{block['label']})",
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
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
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
        "todayLabel": full_date_label(today),
        "seasonEnd": SEASON_END.isoformat(),
        # Wall-clock start of each availability hour, so the browser never has
        # to re-derive "10AM" from an index.
        "hourStarts": [fmt_clock(START_HOUR + i) for i in range(len(HOUR_LABELS))],
        # Per-date peaks so the calendar can shade real dates, including the
        # ones where someone has marked themselves away.
        "dayTotals": day_totals(data, today, SEASON_END),
        "people": [
            {
                "name": rec["name"],
                "avail": {d: e["hours"] for d, e in rec["avail"].items()},
                "dates": dict(rec.get("dates") or {}),
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
    log.info("Logged in as %s", bot.user)

    # on_ready fires again after every reconnect, and syncing the command tree
    # is one of the most rate-limited things a bot can do -- repeating it on
    # each resume is a reliable way to earn a Cloudflare block that then looks
    # like the bot itself is broken. Once per process is enough.
    global _synced
    if not _synced:
        _synced = True
        try:
            # Global syncs can take up to an hour to show up in Discord. Syncing
            # to one guild is near-instant, so set GUILD_ID in .env while you're
            # iterating.
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                log.info("Commands synced to guild %s (instant)", GUILD_ID)
            else:
                await bot.tree.sync()
                log.info("Commands synced globally (can take up to an hour to appear)")
        except discord.HTTPException as exc:
            # A failed sync leaves the previously registered commands in place,
            # so the bot is still usable -- don't take it down over this.
            _synced = False
            log.error(
                "Could not sync commands (HTTP %s): %s",
                exc.status,
                summarise_response(exc.text),
            )

    # Warm the event cache now so the first dashboard visit doesn't pay for it.
    for guild in bot.guilds:
        asyncio.create_task(_refresh_events(guild))


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


@bot.tree.command(name="dashboard", description="Open the web dashboard and set your availability")
async def dashboard(interaction: discord.Interaction):
    if _dashboard_runner is None:
        await interaction.response.send_message(
            embed=base_embed("Dashboard offline", "The web dashboard isn't running on this bot.", COLOR_WARN),
            ephemeral=True,
        )
        return

    url = f"{public_base_url()}/?key={make_edit_token(str(interaction.user.id))}#me"
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
    if not PUBLIC_URL:
        embed.add_field(
            name="Note",
            value="No public address is configured, so this link only works on the computer running the bot.",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


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


@bot.tree.command(name="best", description="Find the best overlapping meeting times")
@app_commands.describe(top="How many top slots to show (default 5)")
async def best(interaction: discord.Interaction, top: app_commands.Range[int, 1, 25] = 5):
    data = load_data()
    if not data:
        await interaction.response.send_message(
            embed=base_embed(
                "No availability yet",
                "Nobody has submitted times. Run `/dashboard` to add yours, then try `/best` again.",
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

    # MISSING rather than None: discord.py's sentinel for "no view supplied".
    view: discord.ui.View = discord.utils.MISSING
    if interaction.guild:
        view = ScheduleFromBestView(blocks, total_people)
        who = " / ".join(SCHEDULE_ROLE_NAMES) or "Anyone"
        embed.description = (embed.description or "") + (
            f"\n\n*{who} can pick a slot below to turn it into a Discord event.*"
        )
    await interaction.response.send_message(embed=embed, view=view)


# Reused by /schedule and /reschedule so both offer exactly the same grid.
HOUR_CHOICES = [app_commands.Choice(name=fmt_clock(h), value=h) for h in range(24)]
MINUTE_OPTIONS = [
    app_commands.Choice(name=f":{m:02d}", value=m) for m in MINUTE_CHOICES
]
DURATION_CHOICES = [
    app_commands.Choice(name=fmt_duration(m), value=m) for m in DURATION_MINUTES
]


@bot.tree.command(name="schedule", description="Create a Discord event at any 15-minute start time")
@app_commands.describe(
    day="Day of the week for the meeting",
    hour="Hour the meeting starts",
    minute="Minutes past the hour (15-minute steps)",
    duration="How long the meeting runs",
    title="Event title (default 'FTC Team Meeting')",
    location="Where the meeting happens",
)
# Split across two decorators: choices() binds a single type variable per call,
# so str choices and int choices can't share one invocation.
@app_commands.choices(day=[app_commands.Choice(name=d, value=d) for d in DAYS])
@app_commands.choices(hour=HOUR_CHOICES, minute=MINUTE_OPTIONS, duration=DURATION_CHOICES)
async def schedule(
    interaction: discord.Interaction,
    day: app_commands.Choice[str],
    hour: app_commands.Choice[int],
    minute: app_commands.Choice[int] | None = None,
    duration: app_commands.Choice[int] | None = None,
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

    start_minute = minute.value if minute else 0
    duration_minutes = duration.value if duration else 60

    await interaction.response.defer()
    data = load_data()
    # Resolve the concrete date up front: attendees are date-specific (someone
    # may have overridden their hours for that Tuesday), and passing the same
    # date on to create_meeting_event keeps the two from disagreeing.
    start = next_occurrence(day.value, hour.value, start_minute)
    # Everyone free for every hour the meeting touches, not just its first.
    attendees = attendees_for(data, start.date(), hour.value, start_minute, duration_minutes)
    try:
        event = await create_meeting_event(
            interaction.guild,
            day.value,
            hour.value,
            start_minute,
            duration_minutes,
            title,
            location,
            attendees,
            on_date=start.date(),
        )
    except Exception as exc:  # surfaced to the user by respond_to_event_error
        await respond_to_event_error(interaction, exc)
        return

    await interaction.followup.send(embed=event_created_embed(event, attendees))


async def event_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Upcoming events, labelled with their exact date and start time."""
    if interaction.guild is None:
        return []
    cutoff = datetime.now(TZ) - timedelta(hours=1)
    # allow_stale: autocomplete has the same ~3s budget as any interaction, and
    # fetch_events can take far longer than that against the Discord API.
    upcoming = sorted(
        (
            event
            for event in await fetch_events(interaction.guild, allow_stale=True)
            if event.start_time and event.start_time.astimezone(TZ) >= cutoff
        ),
        key=lambda e: e.start_time,
    )
    needle = current.casefold()
    out = []
    for event in upcoming:
        start = event.start_time.astimezone(TZ)
        label = f"{event.name} — {start:%a %b} {start.day} · {fmt_clock(start.hour, start.minute)}"
        if needle in label.casefold():
            out.append(app_commands.Choice(name=label[:100], value=str(event.id)))
    return out[:25]


@bot.tree.command(name="reschedule", description="Move an existing event to a new 15-minute start time")
@app_commands.describe(
    event="Which upcoming event to move",
    hour="New start hour",
    minute="Minutes past the hour (15-minute steps)",
    duration="New length (leave blank to keep the current one)",
    date="New date as YYYY-MM-DD (leave blank to keep the current one)",
)
@app_commands.choices(hour=HOUR_CHOICES, minute=MINUTE_OPTIONS, duration=DURATION_CHOICES)
@app_commands.autocomplete(event=event_autocomplete)
async def reschedule(
    interaction: discord.Interaction,
    event: str,
    hour: app_commands.Choice[int],
    minute: app_commands.Choice[int] | None = None,
    duration: app_commands.Choice[int] | None = None,
    date: str = "",
):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=base_embed("Server only", "Scheduled events live in a server, not in DMs.", COLOR_WARN),
            ephemeral=True,
        )
        return

    if not can_schedule(interaction.user):
        await interaction.response.send_message(embed=no_permission_embed(), ephemeral=True)
        return

    if not event.isdigit():
        await interaction.response.send_message(
            embed=base_embed(
                "Pick an event from the list",
                "Start typing and choose one of the suggestions so I know which event to move.",
                COLOR_WARN,
            ),
            ephemeral=True,
        )
        return

    if date and not _valid_date(date):
        await interaction.response.send_message(
            embed=base_embed("That date didn't parse", "Use the `YYYY-MM-DD` form, for example `2026-08-10`.", COLOR_WARN),
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    scheduled = await fetch_events(interaction.guild, force=True)
    target = discord.utils.get(scheduled, id=int(event))
    if target is None or target.start_time is None:
        await interaction.followup.send(
            embed=base_embed("I couldn't find that event", "It may have been cancelled already.", COLOR_WARN)
        )
        return

    current_start = target.start_time.astimezone(TZ)
    current_end = (target.end_time or current_start + timedelta(hours=1)).astimezone(TZ)
    on_date = date_cls.fromisoformat(date) if date else current_start.date()
    duration_minutes = (
        duration.value
        if duration
        else max(SLOT_MINUTES, round((current_end - current_start).total_seconds() / 60))
    )

    start = window_start(on_date, hour.value, minute.value if minute else 0)
    end = start + timedelta(minutes=duration_minutes)
    if start <= datetime.now(TZ) + timedelta(minutes=1):
        await interaction.followup.send(
            embed=base_embed(
                "That time has already passed",
                f"{full_date_label(on_date)} at {fmt_clock(start.hour, start.minute)} is in the past — "
                "Discord only accepts future start times.",
                COLOR_WARN,
            )
        )
        return

    try:
        await target.edit(start_time=start, end_time=end)
    except Exception as exc:  # surfaced to the user by respond_to_event_error
        await respond_to_event_error(interaction, exc)
        return

    _events_cache.pop(interaction.guild.id, None)  # next read picks up the new time
    data = load_data()
    attendees = attendees_for(data, on_date, start.hour, start.minute, duration_minutes)
    embed = base_embed("Event moved", f"**{target.name}**", COLOR_OK)
    embed.add_field(
        name="Was",
        value=f"{full_date_label(current_start.date())}\n{clock_range(current_start, current_end)}",
        inline=True,
    )
    embed.add_field(
        name="Now",
        value=f"{full_date_label(on_date)}\n{clock_range(start, end)} ({fmt_duration(duration_minutes)})",
        inline=True,
    )
    embed.add_field(name="Starts", value=discord.utils.format_dt(start, "F"), inline=False)
    embed.add_field(
        name=f"Expected free ({len(attendees)})", value=join_names(attendees), inline=False
    )
    embed.add_field(name="RSVP", value=f"[Open in the Events tab]({target.url})", inline=False)
    await interaction.followup.send(embed=embed)


# Reconnect backoff for a Discord login that fails for a reason that might
# clear up on its own -- a Cloudflare challenge on the host's IP, a 5xx, or the
# network being briefly unavailable during a deploy.
CONNECT_RETRY_BASE = 30
CONNECT_RETRY_MAX = 600


def summarise_response(text: str | None, limit: int = 300) -> str:
    """Collapse an error body to one line.

    Discord's API sometimes answers with an HTML page instead of JSON -- most
    often a Cloudflare challenge aimed at the host's IP address. Dumping that
    whole page into the logs buries the actual problem, so name it instead.
    """
    body = (text or "").strip()
    if not body:
        return "(empty response)"
    lowered = body.lower()
    if "<html" in lowered or "<!doctype html" in lowered:
        if "challenge-platform" in lowered or "cloudflare" in lowered or "cf-ray" in lowered:
            return (
                "a Cloudflare challenge page -- Discord is challenging this host's IP "
                "rather than answering the API call"
            )
        return "an HTML error page instead of a JSON API response"
    return " ".join(body[:limit].split())


def reset_client() -> bool:
    """Put the client back into a state where start() can be called again.

    close() leaves the client unusable in two ways: it marks it closed, and it
    drops the loop reference. login() only re-runs its setup hook when it sees
    the "no loop yet" sentinel, so both have to be put back or the next start()
    would run against a half-initialised client. Returns False if anything about
    that is not exactly as expected, in which case the caller should let the
    process restart instead of pressing on with a broken client.
    """
    sentinel = getattr(discord.client, "_loop", None)
    if sentinel is None:      # discord.py changed its internals
        return False
    try:
        bot.clear()
        bot.loop = sentinel
    except Exception:
        log.exception("Could not reset the Discord client for another attempt")
        return False
    return not bot.is_closed()


async def connect_discord() -> None:
    """Run the Discord client, retrying failures that may be temporary.

    Exiting the process on a transient failure just hands Render a crash loop,
    and takes the dashboard down with it. A bad token is different: no amount of
    retrying fixes that, so it is reported and gives up.
    """
    delay = CONNECT_RETRY_BASE
    while True:
        try:
            await bot.start(cast(str, TOKEN))
            return                              # clean shutdown
        except discord.LoginFailure:
            log.error(
                "Discord rejected the bot token. Check DISCORD_BOT_TOKEN -- if you "
                "regenerated it in the Developer Portal, update it here too."
            )
            return
        except discord.PrivilegedIntentsRequired:
            log.error(
                "Discord refused the requested intents. Enable them for this bot in the "
                "Developer Portal under Bot -> Privileged Gateway Intents."
            )
            return
        except discord.HTTPException as exc:
            log.error(
                "Discord returned HTTP %s while connecting: %s",
                exc.status,
                summarise_response(exc.text),
            )
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            log.error("Could not reach Discord: %s", exc)
        finally:
            # Without this the client's aiohttp connector is left open, which is
            # what produced the "Unclosed connector" error on shutdown.
            await bot.close()

        if not reset_client():
            log.error("Cannot reuse the Discord client; exiting so the host starts a fresh process")
            return

        log.info("Retrying the Discord connection in %ss (the dashboard stays up)", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, CONNECT_RETRY_MAX)


async def run_bot() -> None:
    """Bind the web port first, then connect to Discord.

    Render health-checks the port shortly after boot and will mark the deploy
    failed if nothing is listening, so the dashboard must not wait on the
    Discord login to finish.
    """
    global _dashboard_runner
    await storage_init()
    if DASHBOARD_ENABLED:
        _dashboard_runner = await start_dashboard(
            dashboard_state,
            DASHBOARD_HOST,
            DASHBOARD_PORT,
            get_user=dashboard_get_user,
            save_user=dashboard_save_user,
            get_day=dashboard_day,
        )
    try:
        await connect_discord()
    finally:
        # Flush the last save before the process goes away.
        await storage_close()
        if _dashboard_runner is not None:
            await _dashboard_runner.cleanup()


def main() -> None:
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Set it in .env locally, or as an environment variable.")
    if PUBLIC_URL:
        log.info("Public dashboard URL: %s", PUBLIC_URL)
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
# 