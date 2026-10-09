"""
Background sync: pull MFP data into SQLite cache.

Usage:
    python -m mfp_mcp.sync --days 7                          # last 7 days
    python -m mfp_mcp.sync --days 14 --end-date 2025-06-01   # 14 days up to June 1
    python -m mfp_mcp.sync --days 30 --end-date today         # last 30 days inclusive
    python -m mfp_mcp.sync --days 7 --force                   # re-sync already synced days

Called from cron 2-3x/day.  Uses the same authentication path as the
MCP server (Camoufox persistent context -> cookies.json fallback).

Sync flow:
  1. Auth once via cookies or Camoufox
  2. Determine which dates in range need syncing (not in cache, stale, or --force)
  3. Fetch each date from MFP API
  4. Save to SQLite: diary entries, daily totals, goals, water, notes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar, Cookie
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure we can import sibling modules
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from mfp_mcp.cache import MFPCache, date_range

# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mfp_sync")

CONFIG_DIR = Path.home() / ".mfp_mcp"
COOKIES_FILE = CONFIG_DIR / "cookies.json"
CACHE_DIR = CONFIG_DIR

# ml per cup (US customary)
_ML_PER_CUP = 237.0


def _load_cookiejar(path: Path) -> CookieJar | None:
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("Cannot read cookies: %s", e)
        return None

    saved_at = data.get("saved_at", "2000-01-01")
    if datetime.now() - datetime.fromisoformat(saved_at) > timedelta(days=30):
        logger.warning("Cookies expired (>30 days old)")
        return None

    cookies = data.get("cookies", {})
    jar = CookieJar()
    for name, value in cookies.items():
        cookie = Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=".myfitnesspal.com",
            domain_specified=True,
            domain_initial_dot=True,
            path="/",
            path_specified=True,
            secure=True,
            expires=int(time.time()) + 86400 * 30,
            discard=False,
            comment=None,
            comment_url=None,
            rest={"HttpOnly": None},
            rfc2109=False,
        )
        jar.set_cookie(cookie)
    return jar


def _get_client(cookies_path: Path):
    """Return an authenticated myfitnesspal.Client, trying cookies first."""
    import myfitnesspal

    # 1. Try stored cookies
    jar = _load_cookiejar(cookies_path)
    if jar:
        try:
            client = myfitnesspal.Client(cookiejar=jar)
            _ = client.get_date(date.today())
            logger.info("Authenticated via stored cookies")
            return client
        except Exception as e:
            logger.warning("Cookies auth failed: %s", e)

    # 2. Fallback: Camoufox (requires Xvfb / VNC)
    logger.info("Cookies unusable, attempting Camoufox auth…")
    _username = os.environ.get("MFP_USERNAME")
    _password = os.environ.get("MFP_PASSWORD")

    # Lazy import to avoid circular dep
    import importlib
    server_mod = importlib.import_module("mfp_mcp.server")
    auth_fn = getattr(server_mod, "authenticate_with_camoufox_async")
    save_fn = getattr(server_mod, "save_cookies")

    import asyncio

    ck = asyncio.run(auth_fn(_username, _password))
    save_fn(ck)
    jar = _load_cookiejar(cookies_path)
    if jar:
        client = myfitnesspal.Client(cookiejar=jar)
        logger.info("Authenticated via Camoufox (fresh cookies saved)")
        return client

    raise RuntimeError("Could not authenticate — no cookies, no Camoufox.")


_NUTRITION_FIELDS = ("calories", "protein", "carbohydrates", "fat", "fiber", "sugar", "sodium")


def _float_or_none(value) -> float | None:
    """Convert a pint Quantity or plain number to float, or None."""
    if value is None:
        return None
    if hasattr(value, "magnitude"):
        return round(float(value.magnitude), 1)
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _meal_to_dict(meal) -> dict:
    """Serialize a myfitnesspal Meal object into a plain dict."""
    entries = []
    for e in meal.entries:
        entry = {
            "name": e.name,
            "brand": getattr(e, "brand", None),
        }
        # Quantity / unit
        entry["quantity"] = getattr(e, "quantity", None)
        entry["unit"] = str(e.unit) if getattr(e, "unit", None) else None

        # Nutrition fields — take from e.totals dict (myfitnesspal lib doesn't expose
        # calories/protein/etc as direct attributes, only inside the totals dict)
        totals = getattr(e, "totals", {})
        for attr in _NUTRITION_FIELDS:
            v = totals.get(attr)
            entry[attr] = _float_or_none(v)

        entries.append(entry)
    return {"name": meal.name, "entries": entries}


def _totals_dict(meal_or_day) -> dict:
    """Extract macronutrient totals as a plain float dict."""
    result = {}
    totals = getattr(meal_or_day, "totals", {})
    for k in _NUTRITION_FIELDS:
        v = totals.get(k, 0)
        fv = _float_or_none(v)
        if fv:
            result[k] = fv
    return result


def _goals_dict(day) -> dict:
    """Extract nutrition goals from a day object."""
    goals = getattr(day, "goals", {})
    if not goals:
        return {}
    result = {}
    for k in _NUTRITION_FIELDS:
        v = goals.get(k)
        if v is not None:
            fv = _float_or_none(v)
            if fv is not None:
                result[k] = fv
    return result


def sync_days(
    client,
    cache: MFPCache,
    days_to_sync: list[date],
    force: bool = False,
) -> tuple[int, int, int]:
    """
    Sync a list of dates into the cache.

    Returns (synced_count, skipped_count, failed_count).
    Skips dates that are already 'complete' in sync_meta (unless force=True).
    Re-syncs dates with status 'stale' or 'missing'.
    """
    synced = 0
    skipped = 0
    failed = 0

    for d in days_to_sync:
        if not force and cache.is_synced(d):
            skipped += 1
            continue

        try:
            day = client.get_date(d)
        except Exception as e:
            logger.warning("  %s — fetch failed: %s", d, e)
            cache.mark_synced(d, status="missing")
            failed += 1
            continue

        # Serialize meals with full nutrition data
        meals_data = [_meal_to_dict(m) for m in day.meals if m.entries]

        # Daily totals (from sum of all meals)
        daily_totals: dict = {}
        for m in day.meals:
            for k, v in _totals_dict(m).items():
                daily_totals[k] = round(daily_totals.get(k, 0) + v, 1)

        # Water: convert ml → cups
        water_cups = None
        try:
            w = day.water
            ml = float(w.ml) if hasattr(w, "ml") else float(w)
            if ml and ml > 0:
                water_cups = round(ml / _ML_PER_CUP, 1)
        except Exception:
            pass

        # Notes
        notes = getattr(day, "notes", None) or ""

        # Goals
        goals = _goals_dict(day)

        cache.upsert_diary_day(
            d,
            meals_data,
            daily_totals,
            water_cups=water_cups,
            notes=notes,
            goals=goals if goals else None,
        )
        cache.mark_synced(d)
        synced += 1
        entry_count = sum(len(m.get("entries", [])) for m in meals_data)
        logger.info("  %s — synced (%d entries, %d meals)", d, entry_count, len(meals_data))

    return synced, skipped, failed


def days_with_activity(client, start: date, end: date) -> list[date]:
    """
    Determine which dates in [start, end] have diary data by iterating
    and checking for meals.
    """
    result = []
    for d in date_range(start, end):
        try:
            day = client.get_date(d)
            if any(m.entries for m in day.meals):
                result.append(d)
        except Exception:
            pass
    return result


def main():
    parser = argparse.ArgumentParser(description="Sync MFP data into local SQLite cache")
    parser.add_argument("--days", type=int, default=14, help="Number of days to sync (default: 14)")
    parser.add_argument(
        "--end-date",
        type=str,
        default="today",
        help="End date (YYYY-MM-DD or 'today'). Default: today",
    )
    parser.add_argument("--force", action="store_true", help="Re-sync all days regardless of status")
    parser.add_argument("--cache-dir", type=str, default=None, help="Cache directory (default: ~/.mfp_mcp)")
    args = parser.parse_args()

    # Resolve dates
    if args.end_date.lower() == "today":
        end = date.today()
    else:
        end = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    start = end - timedelta(days=args.days - 1)

    # Resolve cache dir
    cache_dir = Path(args.cache_dir) if args.cache_dir else CACHE_DIR
    cookies_path = cache_dir / "cookies.json"

    logger.info(
        "MFP sync starting — range %s → %s (%d days, force=%s)",
        start, end, (end - start).days + 1, args.force,
    )

    # Auth (one session for all days)
    t0 = time.time()
    try:
        client = _get_client(cookies_path)
    except RuntimeError as e:
        logger.error("Authentication failed: %s", e)
        sys.exit(1)
    logger.info("Auth took %.1fs", time.time() - t0)

    # Cache
    cache = MFPCache(cache_dir)

    # Determine which days to sync
    if args.force:
        days = list(date_range(start, end))
    else:
        # Check each day: sync if never synced OR status is not 'complete'
        days = []
        for d in date_range(start, end):
            if cache.needs_resync(d):
                days.append(d)
        if not days:
            # If all are already complete, check for days with activity
            # that might have been missed (e.g. new dates not in sync_meta)
            logger.info("All %d days already synced; checking for new activity...", (end - start).days + 1)
            days = days_with_activity(client, start, end)
            # Filter out already-complete ones
            days = [d for d in days if cache.needs_resync(d)]

    if not days:
        logger.info("No days to sync in range %s → %s", start, end)
    else:
        logger.info("Syncing %d days...", len(days))
        synced, skipped, failed = sync_days(client, cache, days, force=args.force)
        elapsed = time.time() - t0
        logger.info(
            "Sync done — %d synced, %d skipped, %d failed in %.1fs (%.1fs/day)",
            synced, skipped, failed, elapsed, elapsed / max(synced, 1),
        )

    logger.info("Total time: %.1fs", time.time() - t0)


if __name__ == "__main__":
    main()
