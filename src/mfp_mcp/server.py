"""
MyFitnessPal MCP Server

A Model Context Protocol (MCP) server that provides tools for interacting
with MyFitnessPal data including food diary, exercises, measurements, goals,
water intake, and food search.

Authentication Methods (in order of priority):
1. Persistent Browser Profile: ~/.mfp_mcp/browser_profile (Camoufox/Firefox)
2. Environment variables: MFP_USERNAME and MFP_PASSWORD (via Camoufox)
3. Stored session cookies: ~/.mfp_mcp/cookies.json
"""

import asyncio
import json
import logging
import os
import secrets
import sys
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar, Cookie
from pathlib import Path
from typing import Optional, Dict, Any, List
from enum import Enum
from collections import OrderedDict
import time

import httpx
from pydantic import BaseModel, Field, ConfigDict, ValidationError
from starlette.requests import Request

from mfp_mcp.cache import MFPCache


# ---------------------------------------------------------------------------
try:
    from mcp.server.transport_security import TransportSecurityMiddleware as _TSM

    def _always_valid_host(self, host):
        return True

    def _always_valid_origin(self, origin):
        return True

    _TSM._validate_host = _always_valid_host    # type: ignore[method-assign]
    _TSM._validate_origin = _always_valid_origin  # type: ignore[method-assign]
except Exception as _patch_err:
    import logging as _log
    _log.getLogger("mfp_mcp").warning("Could not patch transport_security: %s", _patch_err)

# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("mfp_mcp")

_HOST = os.environ.get("MFP_HOST", "0.0.0.0")
_PORT = int(os.environ.get("MFP_PORT", "8000"))
REST_API_TOKEN = os.environ.get("API_TOKEN", "")

# Window size for Camoufox (useful for VNC compatibility)
_WINDOW_SIZE = os.environ.get("MFP_WINDOW_SIZE", "1280,720")
try:
    _WIDTH, _HEIGHT = map(int, _WINDOW_SIZE.split(","))
except Exception:
    _WIDTH, _HEIGHT = 1280, 720

mcp = FastMCP("myfitnesspal_mcp", host=_HOST, port=_PORT)

# Add a simple health check endpoint for Docker/Render. The REST bridge
# reports whether its connector token has been configured, without exposing it.
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({"ok": True, "status": "ok", "configured": bool(REST_API_TOKEN)})



# ---------------------------------------------------------------------------
# Nicolle REST bridge
#
# This is separate from the MCP transports above. Muse reaches these routes
# through a custom connector whose Bearer token is injected by authd. The
# bridge then calls the MyFitnessPal tools from this same process.

def _require_rest_token(request: Request) -> JSONResponse | None:
    if not REST_API_TOKEN:
        return JSONResponse({"error": "Bridge API_TOKEN is not configured"}, status_code=500)
    expected = f"Bearer {REST_API_TOKEN}"
    supplied = request.headers.get("authorization", "")
    if not secrets.compare_digest(supplied, expected):
        return JSONResponse({"error": "Invalid token"}, status_code=401)
    return None


def _parse_rest_date(value: str | None) -> date:
    return parse_date(value)


def _json_payload(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text}


async def _execute_rest(factory):
    """Run one upstream MyFitnessPal call and convert failures to clean JSON."""
    try:
        result = await factory()
    except ValidationError as exc:
        return JSONResponse(
            {"error": "Invalid request", "detail": exc.errors(include_url=False)},
            status_code=400,
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": f"Invalid request: {exc}"}, status_code=400)
    except Exception as exc:  # Upstream login/network/API failures.
        logger.exception("MyFitnessPal REST request failed")
        return JSONResponse(
            {"error": "MyFitnessPal request failed", "detail": str(exc)[:500]},
            status_code=502,
        )

    payload = _json_payload(result)
    if isinstance(payload, dict) and isinstance(payload.get("raw"), str):
        raw = payload["raw"].strip()
        if raw.startswith("Error "):
            return JSONResponse({"error": raw}, status_code=502)
    return JSONResponse(payload)


class BearerAuthMiddleware:
    """Protect every HTTP route except health/OpenAPI with the bridge token.

    The upstream server also exposes MCP transports. Those routes must not be
    public just because the REST bridge is hosted on a public URL.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") in {"/health", "/openapi.json"}:
            await self.app(scope, receive, send)
            return

        if not REST_API_TOKEN:
            response = JSONResponse(
                {"error": "Bridge API_TOKEN is not configured"}, status_code=500
            )
            await response(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        expected = f"Bearer {REST_API_TOKEN}"
        if not secrets.compare_digest(headers.get("authorization", ""), expected):
            response = JSONResponse({"error": "Invalid token"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


@mcp.custom_route("/openapi.json", methods=["GET"])
async def rest_openapi(request: Request):
    return JSONResponse({
        "openapi": "3.0.0",
        "info": {"title": "MyFitnessPal API for Muse", "version": "1.0.0"},
        "paths": {
            "/health": {"get": {"summary": "Health check"}},
            "/diary": {"get": {"summary": "Read diary for a date"}},
            "/foods/search": {"get": {"summary": "Search foods"}},
            "/foods/{food_id}": {"get": {"summary": "Get food details"}},
            "/diary/entries": {"post": {"summary": "Add a food to the diary"}},
            "/diary/entries/remove": {"post": {"summary": "Remove a food entry"}},
            "/water": {"get": {"summary": "Read water"}, "post": {"summary": "Set water"}},
            "/goals": {"get": {"summary": "Read goals"}, "post": {"summary": "Update goals"}},
            "/measurements": {"get": {"summary": "Read measurements"}, "post": {"summary": "Log a measurement"}},
        },
    })


@mcp.custom_route("/diary", methods=["GET"])
async def rest_get_diary(request: Request):
    denied = _require_rest_token(request)
    if denied:
        return denied
    return await _execute_rest(lambda: mfp_get_diary(GetDiaryInput(date=request.query_params.get("date"), response_format=ResponseFormat.JSON)))


@mcp.custom_route("/foods/search", methods=["GET"])
async def rest_search_food(request: Request):
    denied = _require_rest_token(request)
    if denied:
        return denied
    return await _execute_rest(lambda: mfp_search_food(SearchFoodInput(query=request.query_params.get("q", ""), limit=int(request.query_params.get("limit", "10")), response_format=ResponseFormat.JSON)))


@mcp.custom_route("/foods/{food_id}", methods=["GET"])
async def rest_get_food(request: Request):
    denied = _require_rest_token(request)
    if denied:
        return denied
    return await _execute_rest(lambda: mfp_get_food_details(GetFoodDetailsInput(mfp_id=request.path_params["food_id"], response_format=ResponseFormat.JSON)))


@mcp.custom_route("/diary/entries", methods=["POST"])
async def rest_add_food(request: Request):
    denied = _require_rest_token(request)
    if denied:
        return denied
    body = await request.json()
    return await _execute_rest(lambda: mfp_add_food_to_diary(AddFoodToDiaryInput(
        mfp_id=str(body.get("food_id") or body.get("mfp_id") or ""),
        meal=body.get("meal", "Breakfast"),
        date=body.get("date"),
        quantity=float(body.get("quantity", 1.0)),
        unit=body.get("unit"),
    )))


@mcp.custom_route("/diary/entries/remove", methods=["POST"])
async def rest_remove_food(request: Request):
    denied = _require_rest_token(request)
    if denied:
        return denied
    body = await request.json()
    return await _execute_rest(lambda: mfp_remove_food_from_diary(RemoveFoodFromDiaryInput(
        date=body.get("date"),
        entry_id=body.get("entry_id"),
        name_contains=body.get("name_contains"),
        meal=body.get("meal"),
        max_matches=int(body.get("max_matches", 1)),
    )))


@mcp.custom_route("/water", methods=["GET"])
async def rest_get_water(request: Request):
    denied = _require_rest_token(request)
    if denied:
        return denied
    return await _execute_rest(lambda: mfp_get_water(GetWaterInput(date=request.query_params.get("date"))))


@mcp.custom_route("/water", methods=["POST"])
async def rest_set_water(request: Request):
    denied = _require_rest_token(request)
    if denied:
        return denied
    body = await request.json()
    return await _execute_rest(lambda: mfp_set_water(SetWaterInput(cups=float(body.get("cups", 0)), date=body.get("date"))))


@mcp.custom_route("/goals", methods=["GET"])
async def rest_get_goals(request: Request):
    denied = _require_rest_token(request)
    if denied:
        return denied
    return await _execute_rest(lambda: mfp_get_goals(GetGoalsInput(date=request.query_params.get("date"), response_format=ResponseFormat.JSON)))


@mcp.custom_route("/goals", methods=["POST"])
async def rest_set_goals(request: Request):
    denied = _require_rest_token(request)
    if denied:
        return denied
    body = await request.json()
    return await _execute_rest(lambda: mfp_set_goals(SetGoalsInput(
        calories=body.get("calories"),
        protein=body.get("protein"),
        carbohydrates=body.get("carbohydrates"),
        fat=body.get("fat"),
    )))


@mcp.custom_route("/measurements", methods=["GET"])
async def rest_get_measurements(request: Request):
    denied = _require_rest_token(request)
    if denied:
        return denied
    return await _execute_rest(lambda: mfp_get_measurements(GetMeasurementsInput(
        measurement=request.query_params.get("measurement", "Weight"),
        start_date=request.query_params.get("start_date"),
        end_date=request.query_params.get("end_date"),
        response_format=ResponseFormat.JSON,
    )))


@mcp.custom_route("/measurements", methods=["POST"])
async def rest_set_measurement(request: Request):
    denied = _require_rest_token(request)
    if denied:
        return denied
    body = await request.json()
    return await _execute_rest(lambda: mfp_set_measurement(SetMeasurementInput(
        measurement=body.get("measurement", "Weight"),
        value=float(body.get("value")),
    )))

CONFIG_DIR = Path.home() / ".mfp_mcp"
COOKIES_FILE = CONFIG_DIR / "cookies.json"

# MyFitnessPal's v2 JSON API, used for diary writes. The legacy HTML form
# endpoint (/food/diary/{user}/add) was removed by MFP and now returns 404.
MFP_API_BASE = "https://api.myfitnesspal.com"
MFP_CLIENT_ID = "mfp-main-js"
VALID_MEALS = ("Breakfast", "Lunch", "Dinner", "Snacks")
MFP_WEB_BASE = "https://www.myfitnesspal.com"

# canonical arg -> MFP nutritional_contents key
_NUTRIENT_KEYS = {
    "fat": "fat",
    "saturated_fat": "saturated_fat",
    "polyunsaturated_fat": "polyunsaturated_fat",
    "monounsaturated_fat": "monounsaturated_fat",
    "trans_fat": "trans_fat",
    "cholesterol": "cholesterol",
    "sodium": "sodium",
    "potassium": "potassium",
    "carbs": "carbohydrates",
    "fiber": "fiber",
    "sugar": "sugar",
    "protein": "protein",
    "vitamin_a": "vitamin_a",
    "vitamin_c": "vitamin_c",
    "calcium": "calcium",
    "iron": "iron",
}

BROWSER_PROFILE_DIR = CONFIG_DIR / "browser_profile"

# SQLite cache instance (lazily initialised on first use)
_cache: Optional[MFPCache] = None

def get_cache() -> MFPCache:
    global _cache
    if _cache is None:
        _cache = MFPCache(CONFIG_DIR)
    return _cache


def _refresh_cache_for_date(client, target_date: date) -> None:
    """After a write, re-pull the day into SQLite so reads stay fresh.

    Write tools hit the live MFP API directly and do not touch the cache.
    Without this, the next mfp_get_diary for the same date would return the
    stale cached copy until the background sync runs. Calling sync_days with
    force=True for that one date keeps the read path honest.
    """
    try:
        from mfp_mcp.sync import sync_days
        cache = get_cache()
        synced, skipped, failed = sync_days(client, cache, [target_date], force=True)
        logger.info("Cache refreshed for %s after write (synced=%d, failed=%d)", target_date, synced, failed)
    except Exception as e:
        logger.warning("Cache refresh failed for %s after write: %s", target_date, e)

# Detect if Xvfb virtual display is available
_DISPLAY = os.environ.get("DISPLAY", "")
_USE_HEADED = bool(_DISPLAY)

# Headed browser mode (needed only for manual/VNC login) must be requested
# explicitly with MFP_HEADED=1. Default is headless: a headed Firefox under
# Xvfb uses noticeably more RAM, which matters on small instances.
_HEADED = _USE_HEADED and os.environ.get("MFP_HEADED", "0") == "1"


def _rss_mb() -> float:
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return -1.0


# Cached authenticated client + lock so concurrent requests share ONE
# browser authentication instead of each launching its own Camoufox.
_client = None
_client_lock = asyncio.Lock()

def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def save_cookies(cookies: Dict[str, str]):
    ensure_config_dir()
    cookie_data = {
        "cookies": cookies,
        "saved_at": datetime.now().isoformat(),
    }
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookie_data, f, indent=2)
    logger.info(f"Saved session cookies to {COOKIES_FILE}")


def load_cookies() -> Optional[Dict[str, str]]:
    if not COOKIES_FILE.exists():
        return None
    try:
        with open(COOKIES_FILE, "r") as f:
            cookie_data = json.load(f)
        saved_at = datetime.fromisoformat(cookie_data.get("saved_at", "2000-01-01"))
        if datetime.now() - saved_at > timedelta(days=30):
            logger.info("Stored cookies are expired (>30 days old)")
            return None
        return cookie_data.get("cookies")
    except Exception as e:
        logger.warning(f"Failed to load cookies: {e}")
        return None


def dict_to_cookiejar(cookies_dict: Dict[str, str], domain: str = ".myfitnesspal.com") -> CookieJar:
    jar = CookieJar()
    for name, value in cookies_dict.items():
        cookie = Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith('.'),
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


async def _dismiss_consent_popup(page) -> None:
    """Remove GDPR/consent popup iframe that intercepts pointer events."""
    try:
        await page.wait_for_selector(
            '[id^="sp_message_container"], [id^="sp_message_iframe"]',
            timeout=5000,
            state="attached",
        )
        await page.evaluate("""
            document.querySelectorAll('[id^="sp_message_container"]').forEach(el => el.remove());
            document.querySelectorAll('[id^="sp_message_iframe"]').forEach(el => el.remove());
        """)
        logger.info("Camoufox: dismissed consent popup")
    except Exception:
        pass


async def authenticate_with_camoufox_async(username: Optional[str] = None, password: Optional[str] = None) -> Dict[str, str]:
    """
    Authenticate with MyFitnessPal using Camoufox (hardened Firefox fork).
    Uses Persistent Context to save the session in a volume.
    """
    logger.info("Authenticating with Camoufox (Persistent Context)")
    ensure_config_dir()

    try:
        from camoufox import AsyncCamoufox
    except ImportError:
        raise RuntimeError("Camoufox library is not installed.")

    # Camoufox settings for maximum stealth
    camoufox_args = {
        "headless": not _HEADED,
        "persistent_context": True,
        "user_data_dir": str(BROWSER_PROFILE_DIR),
        "humanize": True,
        "window": (_WIDTH, _HEIGHT),
    }
    # Bypass Camoufox's GitHub release lookup: the browser is fetched at
    # image build time, and the runtime check hits api.github.com, which
    # rate-limits shared hosting IPs (403). Supplying the local binary
    # path skips that lookup entirely.
    try:
        from camoufox.pkgman import launch_path

        camoufox_args["executable_path"] = launch_path()
    except Exception as e:
        logger.warning(f"Could not resolve local Camoufox binary: {e}")

    logger.info(
        "Camoufox launch: headed=%s rss=%.0fMB", _HEADED, _rss_mb()
    )

    async with AsyncCamoufox(**camoufox_args) as browser:
        # We don't use 'context' here because Camoufox manages the persistent context internally
        page = await browser.new_page()
        
        try:
            logger.info("Camoufox: checking session validity by visiting home page")
            await page.goto("https://www.myfitnesspal.com/", timeout=60000)
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            
            # Check if we are already logged in
            current_url = page.url
            if "myfitnesspal.com/account/login" not in current_url and "login" not in current_url:
                # Try to find a logged-in indicator
                logged_in = await page.query_selector("a[href*='logout'], .user-avatar, .nav-item-user")
                if logged_in:
                    logger.info("Camoufox: persistent session is valid")
                    raw_cookies = await browser.cookies()
                    return {c["name"]: c["value"] for c in raw_cookies}

            if not username or not password:
                logger.warning("No valid session and no credentials provided. Please log in via VNC.")
                # If in headed mode, we can wait for manual login
                if _HEADED:
                    logger.info("WAITING FOR MANUAL LOGIN VIA VNC (300s timeout)...")
                    await page.goto("https://www.myfitnesspal.com/account/login")
                    try:
                        await page.wait_for_url(lambda u: "login" not in u.lower(), timeout=300000)
                        logger.info("Manual login detected!")
                        raw_cookies = await browser.cookies()
                        return {c["name"]: c["value"] for c in raw_cookies}
                    except Exception as te:
                        raise RuntimeError(f"Manual login timeout: {te}")
                else:
                    raise RuntimeError("Persistent session invalid and no credentials provided for headless login.")

            # Headless or Headed automated login
            logger.info(f"Camoufox: navigating to login page for {username}")
            await page.goto("https://www.myfitnesspal.com/account/login", timeout=60000)
            await _dismiss_consent_popup(page)

            await page.wait_for_selector('input[name="email"], input[name="username"]', timeout=30000)
            await page.fill('input[name="email"], input[name="username"]', username)
            await page.fill('input[name="password"]', password)
            
            logger.info("Camoufox: submitting login form")
            await page.click('button[type="submit"], input[type="submit"]', force=True)

            # Wait for navigation away from login
            try:
                await page.wait_for_url(lambda u: "login" not in u.lower(), timeout=60000)
            except Exception:
                # Might be stuck on a captcha
                if _HEADED:
                    logger.info("Stuck on login page? Check VNC for Captcha. Waiting 60s...")
                    await asyncio.sleep(60)
                else:
                    raise RuntimeError("Login failed or blocked by Captcha in headless mode.")

            raw_cookies = await browser.cookies()
            cookie_dict = {c["name"]: c["value"] for c in raw_cookies}
            
            if "__Secure-next-auth.session-token" not in cookie_dict:
                raise RuntimeError("Login completed but session token is missing.")

            logger.info("Camoufox: authentication successful")
            return cookie_dict

        except Exception as e:
            logger.error(f"Camoufox error: {e}")
            raise

def _client_from_cookies(cookies):
    import myfitnesspal

    cookiejar = dict_to_cookiejar(cookies)
    client = myfitnesspal.Client(cookiejar=cookiejar)
    # Test client
    _ = client.get_date(date.today())
    return client


async def get_mfp_client_async():
    """Return a cached authenticated client.

    Order matters for small instances:
    1. Reuse the in-process client (no browser, no HTTP auth test).
    2. Try stored cookies (cheap HTTP only).
    3. Only then launch Camoufox to log in, and cache the result.

    A lock serializes authentication so concurrent requests cannot each
    launch their own browser at the same time.
    """
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is not None:
            return _client

        last_error = None

        # 1. Stored cookies first — no browser launch needed.
        stored_cookies = load_cookies()
        if stored_cookies:
            logger.info("Attempting auth from stored cookies.json (no browser)")
            try:
                _client = _client_from_cookies(stored_cookies)
                logger.info("Authenticated from stored cookies")
                return _client
            except Exception as e:
                last_error = e
                logger.warning(f"Stored cookie auth failed: {e}")

        # 2. Browser login as the last resort.
        username = os.environ.get("MFP_USERNAME")
        password = os.environ.get("MFP_PASSWORD")
        logger.info(
            "Attempting authentication via Camoufox Persistent Context (rss=%.0fMB)",
            _rss_mb(),
        )
        try:
            cookies = await authenticate_with_camoufox_async(username, password)
            save_cookies(cookies)
            _client = _client_from_cookies(cookies)
            logger.info(
                "Successfully authenticated with Camoufox session (rss=%.0fMB)",
                _rss_mb(),
            )
            return _client
        except Exception as e:
            last_error = e
            logger.warning(f"Camoufox authentication failed: {e}")

        raise RuntimeError(
            f"All authentication methods failed. Last error: {str(last_error)}"
        )


def get_mfp_client():
    """Sync wrapper."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            future = asyncio.run_coroutine_threadsafe(get_mfp_client_async(), loop)
            return future.result(timeout=180) # Longer timeout for Camoufox
        else:
            return loop.run_until_complete(get_mfp_client_async())
    except RuntimeError:
        return asyncio.run(get_mfp_client_async())

def parse_date(date_str: Optional[str] = None) -> date:
    if date_str is None:
        return date.today()
    return datetime.strptime(date_str, "%Y-%m-%d").date()

def format_nutrition_dict(nutrition: Dict[str, Any]) -> Dict[str, Any]:
    formatted = {}
    for key, value in nutrition.items():
        if hasattr(value, "magnitude"):
            formatted[key] = float(value.magnitude)
        else:
            formatted[key] = value
    return formatted

def format_meal_entry(entry) -> Dict[str, Any]:
    return {
        "name": entry.name,
        "short_name": getattr(entry, "short_name", None),
        "quantity": getattr(entry, "quantity", None),
        "unit": getattr(entry, "unit", None),
        "nutrition": format_nutrition_dict(entry.totals),
    }

def format_exercise(exercise) -> Dict[str, Any]:
    entries = exercise.get_as_list()
    return {"name": exercise.name, "entries": entries}

def ordered_dict_to_dict(od: OrderedDict) -> Dict[str, Any]:
    return {str(k): v for k, v in od.items()}

class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"

def format_response(data: Any, format_type: ResponseFormat, title: str = "") -> str:
    if format_type == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)
    lines = []
    if title:
        lines.append(f"## {title}\n")
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                lines.append(f"### {key}")
                for k, v in value.items():
                    lines.append(f"- **{k}**: {v}")
            elif isinstance(value, list):
                lines.append(f"### {key}")
                for item in value:
                    if isinstance(item, dict):
                        lines.append(f"- {item.get('name', str(item))}")
                        for k, v in item.items():
                            if k != "name":
                                lines.append(f"  - {k}: {v}")
                    else:
                        lines.append(f"- {item}")
            else:
                lines.append(f"- **{key}**: {value}")
    else:
        lines.append(str(data))
    return "\n".join(lines)

class GetDiaryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    date: Optional[str] = Field(default=None, description="Date in YYYY-MM-DD format.", pattern=r"^\d{4}-\d{2}-\d{2}$")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format")

class SearchFoodInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    query: str = Field(..., description="Search query for food items")
    limit: int = Field(default=10, ge=1, le=50)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

class GetFoodDetailsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    mfp_id: str = Field(..., description="MyFitnessPal food item ID")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

class GetMeasurementsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    measurement: str = Field(default="Weight")
    start_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

class SetMeasurementInput(BaseModel):
    """Input model for setting a measurement."""

    model_config = ConfigDict(str_strip_whitespace=True)

    measurement: str = Field(
        default="Weight",
        description="Type of measurement to set (e.g., 'Weight', 'Body Fat', 'Waist')",
    )
    value: float = Field(
        ...,
        description="Measurement value (e.g., 185.5 for weight in lbs)",
        gt=0,
    )

class SetGoalsInput(BaseModel):
    """Input model for setting nutrition goals."""

    model_config = ConfigDict(str_strip_whitespace=True)

    calories: Optional[int] = Field(
        default=None,
        description="Daily calorie goal (e.g., 2000)",
        ge=500,
        le=10000,
    )
    protein: Optional[int] = Field(
        default=None,
        description="Daily protein goal in grams (e.g., 150)",
        ge=0,
        le=1000,
    )
    carbohydrates: Optional[int] = Field(
        default=None,
        description="Daily carbohydrate goal in grams (e.g., 200)",
        ge=0,
        le=2000,
    )
    fat: Optional[int] = Field(
        default=None,
        description="Daily fat goal in grams (e.g., 65)",
        ge=0,
        le=500,
    )

class AddFoodToDiaryInput(BaseModel):
    """Input model for adding food to diary."""

    model_config = ConfigDict(str_strip_whitespace=True)

    mfp_id: str = Field(
        ...,
        description="MyFitnessPal food item ID (obtained from mfp_search_food)",
        min_length=1,
    )
    meal: str = Field(
        default="Breakfast",
        description="Meal name (e.g., 'Breakfast', 'Lunch', 'Dinner', 'Snacks')",
    )
    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    quantity: float = Field(
        default=1.0,
        description="Quantity/servings (e.g., 1.5 for 1.5 servings)",
        gt=0,
        le=100,
    )
    unit: Optional[str] = Field(
        default=None,
        description="Unit/serving size description (e.g., '1 cup', '100g'). If not provided, uses default serving size from food item.",
    )

class CreateCustomFoodInput(BaseModel):
    """Input model for creating a private custom food."""

    model_config = ConfigDict(str_strip_whitespace=True)

    description: str = Field(
        ..., description="Food name as it appears in MFP", min_length=1, max_length=200
    )
    brand_name: str = Field(
        default="Generic",
        description="Brand. Packaged food = label brand; restaurant = venue name; homemade = 'Generic'.",
        max_length=200,
    )
    serving_amount: float = Field(
        default=100, description="Serving size number (e.g. 100 for '100 g')", gt=0
    )
    serving_unit: str = Field(
        default="g", description="Serving unit (e.g. 'g', 'ml', 'piece', 'box (100 g)')"
    )
    calories: float = Field(..., description="Calories per serving", ge=0)

    carbs: Optional[float] = Field(
        default=None,
        description="NET carbs in g (MFP adds fiber itself to report total; never pre-add fiber)",
        ge=0,
    )
    fiber: Optional[float] = Field(default=None, description="Fiber, g", ge=0)
    sugar: Optional[float] = Field(default=None, description="Sugars, g", ge=0)
    protein: Optional[float] = Field(default=None, description="Protein, g", ge=0)
    fat: Optional[float] = Field(default=None, description="Total fat, g", ge=0)
    saturated_fat: Optional[float] = Field(default=None, description="Saturated fat, g", ge=0)
    polyunsaturated_fat: Optional[float] = Field(default=None, description="Polyunsaturated fat, g", ge=0)
    monounsaturated_fat: Optional[float] = Field(default=None, description="Monounsaturated fat, g", ge=0)
    trans_fat: Optional[float] = Field(default=None, description="Trans fat, g", ge=0)
    cholesterol: Optional[float] = Field(default=None, description="Cholesterol, mg", ge=0)
    sodium: Optional[float] = Field(default=None, description="Sodium, mg", ge=0)
    potassium: Optional[float] = Field(default=None, description="Potassium, mg", ge=0)
    vitamin_a: Optional[float] = Field(default=None, description="Vitamin A, %DV", ge=0)
    vitamin_c: Optional[float] = Field(default=None, description="Vitamin C, %DV", ge=0)
    calcium: Optional[float] = Field(default=None, description="Calcium, %DV", ge=0)
    iron: Optional[float] = Field(default=None, description="Iron, %DV", ge=0)

    country_code: str = Field(
        default="NL",
        description=(
            "Label convention, and it changes carb meaning. 'NL'/EU: `carbs` is read as NET "
            "(MFP reports total = carbs + fiber). Omitting/US: `carbs` is read as TOTAL. "
            "Keep 'NL' unless deliberately entering a US-style total-carb label."
        ),
        min_length=2, max_length=2,
    )
    public: bool = Field(
        default=False, description="Share publicly. Keep False for personal entries."
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )

class DeleteCustomFoodInput(BaseModel):
    """Input model for deleting a custom food."""

    model_config = ConfigDict(str_strip_whitespace=True)

    food_id: str = Field(..., description="Food id (from mfp_create_custom_food or mfp_list_own_foods)", min_length=1)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )

class SetWaterInput(BaseModel):
    """Input model for setting water intake."""

    model_config = ConfigDict(str_strip_whitespace=True)

    cups: float = Field(
        ...,
        description="Number of cups of water (e.g., 2.5 for 2.5 cups). Note: MyFitnessPal uses cups as the unit.",
        ge=0,
        le=50,
    )
    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )

class GetExercisesInput(BaseModel):
    """Input model for getting exercises."""

    model_config = ConfigDict(str_strip_whitespace=True)

    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )

class GetGoalsInput(BaseModel):
    """Input model for getting nutrition goals."""

    model_config = ConfigDict(str_strip_whitespace=True)

    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )

class GetWaterInput(BaseModel):
    """Input model for getting water intake."""

    model_config = ConfigDict(str_strip_whitespace=True)

    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )

class GetReportInput(BaseModel):
    """Input model for getting nutrition reports."""

    model_config = ConfigDict(str_strip_whitespace=True)

    report_name: str = Field(
        default="Net Calories",
        description="Report name (e.g., 'Net Calories', 'Total Calories', 'Protein', 'Fat', 'Carbs')",
    )
    start_date: Optional[str] = Field(
        default=None,
        description="Start date in YYYY-MM-DD format. Defaults to 7 days ago.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    end_date: Optional[str] = Field(
        default=None,
        description="End date in YYYY-MM-DD format. Defaults to today.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )

class ListOwnFoodsInput(BaseModel):
    """Input model for listing the user's own custom foods."""

    model_config = ConfigDict(str_strip_whitespace=True)

    search: str = Field(default="", description="Optional substring filter on the food name")
    limit: int = Field(default=25, description="Max foods to return", gt=0, le=200)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )

class RemoveFoodFromDiaryInput(BaseModel):
    """Input model for removing food entries from diary."""

    model_config = ConfigDict(extra="forbid")

    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    entry_id: Optional[str] = Field(
        default=None,
        description=(
            "Specific food_entry_id to remove. If omitted, name_contains is "
            "used to match by name."
        ),
    )
    name_contains: Optional[str] = Field(
        default=None,
        description=(
            "Case-insensitive substring to match against entry names "
            "(e.g. 'banana' or '0.5 cup rice'). Ignored if entry_id is set."
        ),
    )
    meal: Optional[str] = Field(
        default=None,
        description=(
            "Restrict matching to a meal: Breakfast, Lunch, Dinner, Snacks."
        ),
    )
    max_matches: int = Field(
        default=1,
        gt=0,
        le=50,
        description=(
            "Safety cap on how many matching entries to delete in one call."
        ),
    )






def _mfp_api_headers(client, json_body: bool = False) -> Dict[str, str]:
    """
    Build auth headers for MyFitnessPal's v2 JSON API.

    The v2 API backs the current MFP web client. It requires the session's
    OAuth bearer token plus an mfp-client-id identifying the calling client.
    """
    headers = {
        "Authorization": f"Bearer {client.access_token}",
        "mfp-client-id": MFP_CLIENT_ID,
        "mfp-user-id": str(client.user_id),
        "Accept": "application/json",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def get_food_v2(client, mfp_id: str) -> Dict[str, Any]:
    """
    Fetch a food's full v2 record, including its version and serving sizes.

    The diary API rejects entries whose food version does not match the
    current stored version, so this must be read fresh rather than cached.

    Args:
        client: Authenticated myfitnesspal.Client instance
        mfp_id: MyFitnessPal food item ID

    Returns:
        The food object as returned by the v2 API

    Raises:
        RuntimeError: If the food cannot be retrieved
    """
    response = client.session.get(
        f"{MFP_API_BASE}/v2/foods",
        params={"ids": str(mfp_id)},
        headers=_mfp_api_headers(client),
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Could not look up food {mfp_id}: HTTP {response.status_code}"
        )

    items = response.json().get("items") or []
    if not items:
        raise RuntimeError(f"No food found with ID {mfp_id}")
    return items[0]


def select_serving_size(food: Dict[str, Any], unit: Optional[str] = None) -> Dict[str, Any]:
    """
    Choose which of a food's serving sizes to log against.

    Args:
        food: Food object from get_food_v2
        unit: Optional unit to match (e.g. "oz", "medium breast"). Matching is
            case-insensitive and accepts a substring. Falls back to the food's
            default (first) serving size when omitted or unmatched.

    Returns:
        The serving size dict, trimmed to the fields the diary API permits

    Raises:
        RuntimeError: If the food declares no serving sizes
    """
    serving_sizes = food.get("serving_sizes") or []
    if not serving_sizes:
        raise RuntimeError(f"Food {food.get('id')} has no serving sizes")

    chosen = serving_sizes[0]
    if unit:
        wanted = unit.strip().lower()
        for size in serving_sizes:
            size_unit = str(size.get("unit", "")).lower()
            if size_unit == wanted or wanted in size_unit:
                chosen = size
                break
        else:
            logger.warning(
                f"Unit {unit!r} not found for food {food.get('id')}; "
                f"using default serving {chosen.get('unit')!r}"
            )

    # The diary endpoint rejects any serving_size field beyond these three.
    return {
        "value": chosen["value"],
        "unit": chosen["unit"],
        "nutrition_multiplier": chosen["nutrition_multiplier"],
    }



# ============================================================================
# Custom-food creation (web BFF /api/services/foods)
#
# MFP exposes no custom-food create on the v2 OAuth API, but its own web client
# does it with three plain HTTP calls, all cookie-authenticated. Since this
# server already holds a logged-in cookie jar, it can call them directly — no
# browser automation, no Chrome running.
#
#   GET  /api/auth/csrf                  -> { csrfToken }
#   GET  /api/services/users/foods/mine  -> [ ...foods ]   (source of user_id)
#   POST /api/services/foods             -> { item: {...} } (201/200)
#   DELETE /api/services/foods/{id}      -> 204
#
# CARBS + COUNTRY_CODE (verified 2026-07-26, both directions):
# `country_code` decides how MFP reads nutritional_contents.carbohydrates.
#   country_code="NL" (EU labels exclude fibre):
#       sent value = NET  -> stores net_carbs=sent, carbohydrates=sent+fiber
#   country_code omitted (US default, labels include fibre):
#       sent value = TOTAL -> stores carbohydrates=sent, net_carbs=sent-fiber
# Sending carbohydrates=42, fiber=8 gives 50/42 with "NL" and 42/34 without.
# So country_code is NOT cosmetic: omitting it silently shifts every carb value
# by the fibre amount. Default "NL" and pass NET carbs.
# ============================================================================

MFP_WEB_BASE = "https://www.myfitnesspal.com"

# canonical arg -> MFP nutritional_contents key
_NUTRIENT_KEYS = {
    "fat": "fat",
    "saturated_fat": "saturated_fat",
    "polyunsaturated_fat": "polyunsaturated_fat",
    "monounsaturated_fat": "monounsaturated_fat",
    "trans_fat": "trans_fat",
    "cholesterol": "cholesterol",
    "sodium": "sodium",
    "potassium": "potassium",
    "carbs": "carbohydrates",
    "fiber": "fiber",
    "sugar": "sugar",
    "protein": "protein",
    "vitamin_a": "vitamin_a",
    "vitamin_c": "vitamin_c",
    "calcium": "calcium",
    "iron": "iron",
}


def _web_headers(csrf: Optional[str] = None, json_body: bool = False) -> Dict[str, str]:
    """Headers for the cookie-authenticated web BFF."""
    headers = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    if csrf:
        headers["x-csrf-token"] = csrf
    return headers


def _api_error_detail(response) -> str:
    """Pull MFP's structured error text out of a failed response.

    Mirrors how add_food_to_diary surfaces v2 errors, so we report the API's own
    message rather than echoing an arbitrary slice of the response body.
    """
    try:
        body = response.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    return str(
        body.get("error_description")
        or body.get("error_details", {}).get("item_error")
        or body.get("error")
        or ""
    )


def _get_csrf_token(client) -> Optional[str]:
    """Fetch a CSRF token. Returns None if unavailable; POST will then 403."""
    try:
        r = client.session.get(
            f"{MFP_WEB_BASE}/api/auth/csrf", headers=_web_headers(), timeout=30
        )
        if r.status_code == 200:
            return r.json().get("csrfToken")
    except Exception as e:
        logger.warning(f"Could not fetch CSRF token: {e}")
    return None


def list_own_foods(client, search: str = "") -> List[Dict[str, Any]]:
    """List the user's own custom foods (newest first), optionally filtered."""
    r = client.session.get(
        f"{MFP_WEB_BASE}/api/services/users/foods/mine",
        params={"search": search},
        headers=_web_headers(),
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"Could not list own foods: HTTP {r.status_code}. "
            "The stored session may have expired — run refresh_browser_cookies."
        )
    return r.json() or []


def _serving_sizes(amount: float, unit: str) -> List[Dict[str, Any]]:
    """Primary serving plus the container wrapper MFP's own client sends."""
    return [
        {
            "value": amount,
            "unit": unit,
            "nutrition_multiplier": 1,
            "gram_weight": 1,
            "fraction": False,
            "index": 0,
        },
        {
            "value": 1,
            "unit": f"container ({amount} {unit} ea.)",
            "nutrition_multiplier": 1,
            "gram_weight": 1,
            "fraction": False,
            "index": 1,
        },
    ]


def create_custom_food(client, spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a private custom food via the web BFF.

    Args:
        client: authenticated myfitnesspal.Client
        spec: validated CreateCustomFoodInput as a dict

    Returns:
        {"id": str|None, "status": int, "description": str}
    """
    csrf = _get_csrf_token(client)

    nutrition: Dict[str, Any] = {
        "energy": {"unit": "calories", "value": spec["calories"]},
        "grams": 1,
    }
    for arg, api_key in _NUTRIENT_KEYS.items():
        value = spec.get(arg)
        if value is not None:
            nutrition[api_key] = value

    item = {
        "description": spec["description"],
        "brand_name": spec.get("brand_name") or "Generic",
        "public": bool(spec.get("public", False)),
        "type": "food",
        "nutritional_contents": nutrition,
        "serving_sizes": _serving_sizes(
            spec.get("serving_amount", 100), spec.get("serving_unit", "g")
        ),
    }
    item["country_code"] = spec.get("country_code") or "NL"
    # Ownership is assigned by MyFitnessPal from the session, so user_id is not
    # sent: posting without it returns 200 with the correct owner. Sending it
    # would also mean an extra request, and would fail for an account that has
    # no custom foods yet.

    r = client.session.post(
        f"{MFP_WEB_BASE}/api/services/foods",
        headers=_web_headers(csrf, json_body=True),
        data=json.dumps({"item": item}),
        timeout=30,
    )

    if r.status_code not in (200, 201):
        hint = "" if csrf else " (no CSRF token acquired)"
        detail = _api_error_detail(r)
        raise RuntimeError(
            f"Failed to create custom food: HTTP {r.status_code}{hint}"
            + (f" - {detail}" if detail else "")
        )

    # MFP returns a bare list of the created food object(s); older docs/clients
    # assumed {"item": {...}}. Accept both.
    new_id = None
    try:
        body = r.json()
        if isinstance(body, list) and body:
            new_id = body[0].get("id")
        elif isinstance(body, dict):
            new_id = (body.get("item") or body).get("id")
    except Exception:
        pass
    if new_id is None:
        logger.warning("Food created but MyFitnessPal returned no id")

    logger.info(f"Created custom food {new_id}: {spec['description']}")
    return {"id": new_id, "status": r.status_code, "description": spec["description"]}


def delete_custom_food(client, food_id: str) -> int:
    """Delete a custom food by id. MFP has no update endpoint — recreate + delete."""
    csrf = _get_csrf_token(client)
    r = client.session.delete(
        f"{MFP_WEB_BASE}/api/services/foods/{food_id}",
        headers=_web_headers(csrf),
        timeout=30,
    )
    if r.status_code not in (200, 204):
        detail = _api_error_detail(r)
        raise RuntimeError(
            f"Failed to delete custom food {food_id}: HTTP {r.status_code}"
            + (f" - {detail}" if detail else "")
        )
    logger.info(f"Deleted custom food {food_id}")
    return r.status_code


def add_food_to_diary(
    client, mfp_id: str, meal: str, target_date: date, quantity: float = 1.0, unit: Optional[str] = None
) -> Optional[str]:
    """
    Add a food item to the diary for a specific date and meal.

    Args:
        client: Authenticated myfitnesspal.Client instance
        mfp_id: MyFitnessPal food item ID
        meal: Meal name (Breakfast, Lunch, Dinner, Snacks)
        target_date: Date to add the food entry
        quantity: Number of servings (default 1.0)
        unit: Optional serving unit to log against (e.g. "oz")

    Returns:
        The new entry's UUID, or None if MFP did not return one

    Raises:
        RuntimeError: If the operation fails
    """
    food = get_food_v2(client, mfp_id)
    serving_size = select_serving_size(food, unit)

    meal_name = meal.strip().capitalize()
    if meal_name not in VALID_MEALS:
        raise RuntimeError(
            f"Invalid meal {meal!r}. Expected one of: {', '.join(VALID_MEALS)}"
        )

    entry = {
        "type": "food_entry",
        "date": target_date.strftime("%Y-%m-%d"),
        "meal_name": meal_name,
        "servings": float(quantity),
        "food": {"id": str(food["id"]), "version": str(food["version"])},
        "serving_size": serving_size,
    }

    response = client.session.post(
        f"{MFP_API_BASE}/v2/diary",
        headers=_mfp_api_headers(client, json_body=True),
        data=json.dumps({"items": [entry]}),
        timeout=30,
    )

    if response.status_code not in (200, 201):
        detail = ""
        try:
            body = response.json()
            detail = body.get("error_details", {}).get("item_error") or body.get(
                "error_description", ""
            )
        except Exception:
            pass
        raise RuntimeError(
            f"Failed to add food to diary: HTTP {response.status_code}"
            + (f" - {detail}" if detail else "")
        )

    logger.info(
        f"Added food {mfp_id} ({serving_size['value']} {serving_size['unit']} "
        f"x{quantity}) to {meal_name} for {target_date}"
    )

    # MFP returns the new entry's id here and nowhere else - the diary page
    # exposes only legacy numeric ids, which the v2 API does not accept.
    try:
        return response.json()["items"][0]["id"]
    except (ValueError, KeyError, IndexError):
        logger.warning("Entry created but MyFitnessPal returned no entry id")
        return None


def list_diary_entries(client, target_date: date) -> List[Dict[str, str]]:
    """
    Scrape the diary page for the given date and return a list of entries
    with their internal food_entry_id (needed for deletion), name, and meal.

    Returns:
        List of {"entry_id", "name", "meal"} dicts in display order.
    """
    import re

    date_str = target_date.strftime("%Y-%m-%d")
    diary_url = f"{client.BASE_URL_SECURE}food/diary?date={date_str}"
    # Use the library's session to ensure cookies/CSRF/etc. are aligned
    response = client.session.get(diary_url)
    src = response.text

    entries: List[Dict[str, str]] = []
    # Walk through the HTML linearly. Track the most recent meal header.
    current_meal = None
    pattern = re.compile(
        r'(class="meal_header"[^>]*>\s*<[^>]+>([^<]+)</[^>]+>)|'
        r'(<a[^>]+data-food-entry-id="(\d+)"[^>]+class="js-show-edit-food"[^>]*>'
        r'([^<]+)</a>)',
        re.DOTALL,
    )
    for m in pattern.finditer(src):
        if m.group(2):
            current_meal = m.group(2).strip()
        elif m.group(4):
            entries.append({
                "entry_id": m.group(4),
                "name": m.group(5).strip(),
                "meal": current_meal or "",
            })
    return entries


def remove_food_entry(client, entry_id: str) -> None:
    """
    Delete a food diary entry by its food_entry_id.

    Uses the legacy /food/remove/{id} endpoint with X-CSRF-Token from
    the diary page meta tag.
    """
    import re

    # Need a fresh CSRF token from the diary page
    diary_resp = client.session.get(
        f"{client.BASE_URL_SECURE}food/diary"
    )
    csrf_match = re.search(
        r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
        diary_resp.text,
    )
    if not csrf_match:
        raise RuntimeError("Could not extract csrf-token from diary page")
    csrf = csrf_match.group(1)

    response = client.session.request(
        "DELETE",
        f"{client.BASE_URL_SECURE}food/remove/{entry_id}",
        headers={
            "Referer": f"{client.BASE_URL_SECURE}food/diary",
            "X-CSRF-Token": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        allow_redirects=False,
    )
    # Success = 302 redirect to diary, or 200 with empty body
    if response.status_code in (200, 204, 302, 303):
        logger.info(f"Removed diary entry {entry_id}")
        return
    raise RuntimeError(
        f"Remove failed for entry {entry_id}: HTTP {response.status_code}"
    )


def set_water_intake(client, target_date: date, cups: float) -> None:
    """
    Set water intake for a specific date via the v2 JSON API.

    Uses POST /v2/diary/water (the same endpoint the MFP web client uses).
    Semantics are REPLACE, not additive -- each POST overwrites the day's
    cumulative total wholesale. Pass cups=0 to clear the day.

    Note: MFP truncates fractional cups when posting units="cups" (1.5 cups
    becomes 240ml, losing the .5). We always convert cups -> milliliters
    client-side and post units="milliliters" to preserve fractions. MFP uses
    its own ~240ml/cup "metric cup" constant.

    Args:
        client: Authenticated myfitnesspal.Client instance
        target_date: Date to set water intake
        cups: Number of cups of water (0 clears the day)
    """
    date_str = target_date.strftime("%Y-%m-%d")
    ml = round(cups * 240.0)
    payload = {
        "date": date_str,
        "units": "milliliters",
        "value": ml,
    }
    response = client.session.post(
        f"{MFP_API_BASE}/v2/diary/water",
        headers=_mfp_api_headers(client, json_body=True),
        data=json.dumps(payload),
        timeout=30,
    )
    if response.status_code not in (200, 201):
        detail = ""
        try:
            body = response.json()
            detail = body.get("error_description") or body.get("message") or ""
        except Exception:
            pass
        raise RuntimeError(
            f"Failed to set water intake: HTTP {response.status_code}"
            + (f" - {detail}" if detail else "")
        )
    logger.info(f"Successfully set water intake to {cups} cups ({ml} ml) for {target_date}")
    return response.json() if response.content else None


def _build_diary_from_cache(target_date: date, daily: Dict, entries: List[Dict], goals: Dict) -> Dict[str, Any]:
    """Build the standard diary response dict from cached data."""
    meals: Dict[str, Any] = {}
    meal_totals: Dict[str, Dict[str, float]] = {}

    for e in entries:
        meal_name = e["meal"]
        if meal_name not in meals:
            meals[meal_name] = {"entries": [], "totals": {}}
            meal_totals[meal_name] = {}

        entry = {
            "name": e["food_name"],
            "brand": e.get("brand"),
            "quantity": e.get("quantity"),
            "unit": e.get("unit"),
            "nutrition": {
                "calories": e["calories"],
                "protein": e["protein"],
                "carbohydrates": e["carbs"],
                "fat": e["fat"],
                "fiber": e["fiber"],
                "sugar": e["sugar"],
                "sodium": e["sodium"],
            },
        }
        meals[meal_name]["entries"].append(entry)

        # Accumulate per-meal totals from cache columns (carbs, not carbohydrates)
        CACHE_NUTRITION_KEYS = ["calories", "protein", "carbs", "fat", "fiber", "sugar", "sodium"]
        MFP_OUTPUT_KEYS = ["calories", "protein", "carbohydrates", "fat", "fiber", "sugar", "sodium"]
        for cache_k, mfp_k in zip(CACHE_NUTRITION_KEYS, MFP_OUTPUT_KEYS):
            v = e.get(cache_k) or 0
            meal_totals[meal_name][mfp_k] = meal_totals[meal_name].get(mfp_k, 0) + v

    # Normalize keys to match live MFP format (cache uses carbs, output uses carbohydrates)
    daily_totals = {mfp_k: daily.get(cache_k, 0) or 0 for cache_k, mfp_k in zip(CACHE_NUTRITION_KEYS, MFP_OUTPUT_KEYS)}

    for meal_name in meals:
        meals[meal_name]["totals"] = meal_totals.get(meal_name, {})

    # Water: stored as cups in cache
    water_cups = daily.get("water_cups", 0) or 0

    data = {
        "date": str(target_date),
        "meals": meals,
        "daily_totals": daily_totals,
        "daily_goals": goals,
        "water": {"cups": water_cups},
        "notes": daily.get("notes") or "",
        "source": "cache",
    }
    return data


@mcp.tool(name="mfp_get_diary")
async def mfp_get_diary(params: GetDiaryInput) -> str:
    """Get the food diary for a specific date.

    Reads from the local SQLite cache when available (fast, ~10ms).
    Falls back to live MFP API only when the date hasn't been synced yet.
    """
    try:
        target_date = parse_date(params.date)
        cache = get_cache()

        # 1. Try cache first
        daily = cache.get_diary_date(target_date)
        if daily is not None:
            entries = cache.get_diary_entries(target_date)
            goals = cache.get_daily_goals(target_date)
            data = _build_diary_from_cache(target_date, daily, entries, goals)
            return format_response(data, params.response_format, f"Food Diary for {target_date}")

        # 2. Fallback: live MFP (slow — authenticate + fetch)
        client = await get_mfp_client_async()
        day = client.get_date(target_date)
        data = {
            "date": str(target_date),
            "meals": {},
            "daily_totals": {},
            "daily_goals": day.goals,
            "water": day.water,
            "notes": day.notes or "",
            "source": "live",
        }
        for meal in day.meals:
            data["meals"][meal.name] = {
                "entries": [format_meal_entry(entry) for entry in meal.entries],
                "totals": format_nutrition_dict(meal.totals),
            }
        totals = {}
        for entry in day.entries:
            for key, value in entry.totals.items():
                val = float(value.magnitude) if hasattr(value, "magnitude") else value
                totals[key] = totals.get(key, 0) + val
        data["daily_totals"] = totals
        return format_response(data, params.response_format, f"Food Diary for {target_date}")
    except Exception as e:
        return f"Error retrieving diary: {str(e)}"

@mcp.tool(name="mfp_search_food")
async def mfp_search_food(params: SearchFoodInput) -> str:
    """Search the MyFitnessPal food database."""
    try:
        client = await get_mfp_client_async()
        results = client.get_food_search_results(params.query)[: params.limit]
        data = {"query": params.query, "results": [{"name": i.name, "brand": i.brand, "serving": i.serving, "calories": i.calories, "mfp_id": i.mfp_id} for i in results]}
        return format_response(data, params.response_format, f"Food Search Results for '{params.query}'")
    except Exception as e: return f"Error searching foods: {str(e)}"

@mcp.tool(name="mfp_get_food_details")
async def mfp_get_food_details(params: GetFoodDetailsInput) -> str:
    """Get detailed nutritional information for a specific food item."""
    try:
        client = await get_mfp_client_async()
        item = client.get_food_item_details(params.mfp_id)
        data = {"mfp_id": params.mfp_id, "description": getattr(item, "description", "N/A"), "nutrition": {"protein": getattr(item, "protein", None), "carbohydrates": getattr(item, "carbohydrates", None), "fat": getattr(item, "fat", None)}, "servings": [str(s) for s in getattr(item, "servings", [])]}
        return format_response(data, params.response_format, "Food Item Details")
    except Exception as e: return f"Error getting food details: {str(e)}"

@mcp.tool(name="mfp_get_measurements")
async def mfp_get_measurements(params: GetMeasurementsInput) -> str:
    """Get body measurements over a date range.

    Reads from the local SQLite cache when available (fast, ~10ms).
    Falls back to live MFP API when the data hasn't been synced.
    """
    try:
        end = parse_date(params.end_date)
        start = parse_date(params.start_date) if params.start_date else end - timedelta(days=30)
        cache = get_cache()

        # 1. Try cache first
        cached = cache.get_measurements(params.measurement, start, end)
        if cached:
            values = {r["date"]: r["value"] for r in cached}
            return format_response(
                {"measurement": params.measurement, "values": values, "source": "cache"},
                params.response_format,
                f"{params.measurement} History",
            )

        # 2. Fallback: live MFP
        client = await get_mfp_client_async()
        measurements = client.get_measurements(params.measurement, start, end)
        return format_response(
            {"measurement": params.measurement, "values": ordered_dict_to_dict(measurements), "source": "live"},
            params.response_format,
            f"{params.measurement} History",
        )
    except Exception as e:
        return f"Error getting measurements: {str(e)}"



@mcp.tool(
    name="mfp_set_measurement",
    annotations={
        "title": "Log Body Measurement",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mfp_set_measurement(params: SetMeasurementInput) -> str:
    """
    Log a new body measurement (weight, body fat, etc.) for today.

    Records the measurement value in MyFitnessPal for tracking progress.

    Args:
        params: SetMeasurementInput containing:
            - measurement (str): Type of measurement (default 'Weight')
            - value (float): Measurement value (e.g., 185.5)

    Returns:
        str: Confirmation message with the logged value
    """
    try:
        client = await get_mfp_client_async()
        # POST /v2/measurements (upsert). myfitnesspal's set_measurements
        # targets the removed legacy HTML form (/measurements/new -> 404),
        # so post the v2 JSON endpoint the web client uses instead.
        payload = {
            "items": [
                {
                    "type": params.measurement.title(),
                    "value": params.value,
                    "date": date.today().strftime("%Y-%m-%d"),
                    "unit": "pounds",
                }
            ]
        }
        r = client.session.post(
            f"{MFP_API_BASE}/v2/measurements",
            headers=_mfp_api_headers(client, json_body=True),
            data=json.dumps(payload),
            timeout=30,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Failed to set measurement: HTTP {r.status_code} - {r.text[:200]}")

        # Upsert the value into cache so mfp_get_measurements is instantly consistent
        try:
            get_cache().upsert_measurements(
                {date.today(): params.value}, params.measurement.title()
            )
        except Exception as _ce:
            logger.warning("Measurement cache upsert failed: %s", _ce)

        return json.dumps(
            {
                "success": True,
                "message": f"Successfully logged {params.measurement}: {params.value}",
                "measurement": params.measurement,
                "value": params.value,
                "unit": "pounds",
                "date": str(date.today()),
            },
            indent=2,
        )

    except Exception as e:
        return f"Error setting measurement: {str(e)}"


@mcp.tool(
    name="mfp_get_exercises",
    annotations={
        "title": "Get Exercise Log",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_get_exercises(params: GetExercisesInput) -> str:
    """
    Get logged exercises for a specific date.

    Returns both cardiovascular and strength training exercises with their
    details (duration, calories burned, sets, reps, weight, etc.).

    Args:
        params: GetExercisesInput containing:
            - date (str, optional): Date in YYYY-MM-DD format, defaults to today
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: List of exercises with details and calories burned
    """
    try:
        client = await get_mfp_client_async()
        target_date = parse_date(params.date)
        day = client.get_date(target_date)

        data = {"date": str(target_date), "exercises": []}

        for exercise in day.exercises:
            data["exercises"].append(format_exercise(exercise))

        # Calculate total calories burned
        total_burned = 0
        for ex in data["exercises"]:
            for entry in ex.get("entries", []):
                if "nutrition_information" in entry:
                    total_burned += entry["nutrition_information"].get(
                        "calories burned", 0
                    )

        data["total_calories_burned"] = total_burned

        return format_response(
            data, params.response_format, f"Exercise Log for {target_date}"
        )

    except Exception as e:
        return f"Error getting exercises: {str(e)}"


@mcp.tool(
    name="mfp_get_goals",
    annotations={
        "title": "Get Nutrition Goals",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_get_goals(params: GetGoalsInput) -> str:
    """
    Get the user's daily nutrition goals (calories, protein, carbs, fat, etc.).

    Returns the configured daily targets for all tracked nutrients.

    Args:
        params: GetGoalsInput containing:
            - date (str, optional): Date in YYYY-MM-DD format, defaults to today
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Daily nutrition goals and targets
    """
    try:
        client = await get_mfp_client_async()
        target_date = parse_date(params.date)
        day = client.get_date(target_date)

        data = {"date": str(target_date), "goals": day.goals}

        return format_response(data, params.response_format, "Daily Nutrition Goals")

    except Exception as e:
        return f"Error getting goals: {str(e)}"


@mcp.tool(
    name="mfp_set_goals",
    annotations={
        "title": "Update Nutrition Goals",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_set_goals(params: SetGoalsInput) -> str:
    """
    Update daily nutrition goals (calories, protein, carbs, fat).

    Sets new daily targets for the specified nutrients. Only updates the
    values that are provided; others remain unchanged.

    Args:
        params: SetGoalsInput containing:
            - calories (int, optional): Daily calorie goal
            - protein (int, optional): Daily protein goal in grams
            - carbohydrates (int, optional): Daily carb goal in grams
            - fat (int, optional): Daily fat goal in grams

    Returns:
        str: Confirmation message with updated goals
    """
    try:
        # Check that at least one goal is provided
        if not any(
            [params.calories, params.protein, params.carbohydrates, params.fat]
        ):
            return "Error: Please provide at least one goal to update (calories, protein, carbohydrates, or fat)"

        client = await get_mfp_client_async()

        # Build kwargs for set_new_goal
        kwargs = {}
        if params.calories:
            kwargs["energy"] = params.calories
        if params.protein:
            kwargs["protein"] = params.protein
        if params.carbohydrates:
            kwargs["carbohydrates"] = params.carbohydrates
        if params.fat:
            kwargs["fat"] = params.fat

        client.set_new_goal(**kwargs)
        # Goals are global but cached per-day; refresh today's entry
        _refresh_cache_for_date(client, date.today())

        return json.dumps(
            {
                "success": True,
                "message": "Successfully updated nutrition goals",
                "updated_goals": {
                    "calories": params.calories,
                    "protein": params.protein,
                    "carbohydrates": params.carbohydrates,
                    "fat": params.fat,
                },
            },
            indent=2,
        )

    except Exception as e:
        return f"Error setting goals: {str(e)}"


@mcp.tool(
    name="mfp_get_water",
    annotations={
        "title": "Get Water Intake",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_get_water(params: GetWaterInput) -> str:
    """
    Get water intake for a specific date.

    Returns the number of cups/glasses of water logged for the day.

    Args:
        params: GetWaterInput containing:
            - date (str, optional): Date in YYYY-MM-DD format, defaults to today

    Returns:
        str: Water intake amount for the specified date
    """
    try:
        client = await get_mfp_client_async()
        target_date = parse_date(params.date)
        day = client.get_date(target_date)

        data = {
            "date": str(target_date),
            "water_cups": day.water,
            "water_ml": day.water * 236.588,  # Convert cups to ml
        }

        return json.dumps(data, indent=2)

    except Exception as e:
        return f"Error getting water intake: {str(e)}"


@mcp.tool(
    name="mfp_add_food_to_diary",
    annotations={
        "title": "Add Food to Diary",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mfp_add_food_to_diary(params: AddFoodToDiaryInput) -> str:
    """
    Add a food item to your MyFitnessPal food diary for a specific date and meal.

    This tool adds a food entry to your diary. You can search for foods using
    mfp_search_food to find the food ID (mfp_id) needed for this tool.

    Args:
        params: AddFoodToDiaryInput containing:
            - mfp_id (str): MyFitnessPal food item ID (from mfp_search_food)
            - meal (str): Meal name - 'Breakfast', 'Lunch', 'Dinner', or 'Snacks' (default: 'Breakfast')
            - date (str, optional): Date in YYYY-MM-DD format, defaults to today
            - quantity (float): Number of servings (default: 1.0)
            - unit (str, optional): Unit/serving size (e.g., '1 cup', '100g')

    Returns:
        str: Confirmation message with details of the added food entry
    """
    try:
        client = await get_mfp_client_async()
        target_date = parse_date(params.date)
        
        # Normalize meal name (capitalize first letter)
        meal = params.meal.strip().capitalize()
        if meal.lower() == "snack":
            meal = "Snacks"
        
        # Add food to diary
        entry_id = add_food_to_diary(
            client=client,
            mfp_id=params.mfp_id,
            meal=meal,
            target_date=target_date,
            quantity=params.quantity,
            unit=params.unit,
        )

        # Refresh cache so the next read reflects the change immediately
        _refresh_cache_for_date(client, target_date)

        # Get food details for confirmation
        try:
            food_item = client.get_food_item_details(params.mfp_id)
            food_name = getattr(food_item, "description", "Unknown Food")
        except Exception:
            food_name = "Food item"

        return json.dumps(
            {
                "success": True,
                "message": f"Successfully added {food_name} to {meal}",
                "entry_id": entry_id,
                "date": str(target_date),
                "meal": meal,
                "food_id": params.mfp_id,
                "food_name": food_name,
                "quantity": params.quantity,
                "unit": params.unit,
            },
            indent=2,
        )
        
    except Exception as e:
        return f"Error adding food to diary: {str(e)}"


@mcp.tool(
    name="mfp_remove_food_from_diary",
    annotations={
        "title": "Remove Food From Diary",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mfp_remove_food_from_diary(params: RemoveFoodFromDiaryInput) -> str:
    """
    Remove (delete) one or more food entries from your diary.

    Two modes:

    1. By entry_id (precise): delete exactly the entry whose
       food_entry_id matches. Use this when you already know the ID.

    2. By name_contains (fuzzy): list the day's entries, find ones whose
       name contains the given substring (case-insensitive), optionally
       restricted to a meal, and delete up to max_matches of them.

    Args:
        params: RemoveFoodFromDiaryInput with one of:
            - entry_id: exact food_entry_id to delete
            - name_contains: substring match against entry names
            - meal: restrict matching to one meal
            - max_matches: safety cap for fuzzy matches (default 1)
            - date: date to operate on (default today)

    Returns:
        JSON describing each entry that was removed.
    """
    try:
        client = await get_mfp_client_async()
        target_date = parse_date(params.date)

        # Mode 1: delete a single entry by ID
        if params.entry_id:
            remove_food_entry(client, params.entry_id)
            _refresh_cache_for_date(client, target_date)
            return json.dumps({
                "success": True,
                "removed": [{"entry_id": params.entry_id}],
                "date": str(target_date),
            }, indent=2)

        # Mode 2: fuzzy match by name (+ optional meal filter)
        if not params.name_contains:
            return ("Error removing food: provide either entry_id or "
                    "name_contains")

        entries = list_diary_entries(client, target_date)
        needle = params.name_contains.lower()
        meal_filter = (
            params.meal.lower() if params.meal else None
        )

        matches = []
        for e in entries:
            if needle not in e["name"].lower():
                continue
            if meal_filter and meal_filter not in e["meal"].lower():
                continue
            matches.append(e)

        if not matches:
            return json.dumps({
                "success": False,
                "removed": [],
                "message": (
                    f"No entries matched '{params.name_contains}'"
                    + (f" in {params.meal}" if params.meal else "")
                ),
            }, indent=2)

        to_remove = matches[: params.max_matches]
        removed = []
        for e in to_remove:
            remove_food_entry(client, e["entry_id"])
            removed.append({
                "entry_id": e["entry_id"],
                "name": e["name"],
                "meal": e["meal"],
            })

        _refresh_cache_for_date(client, target_date)

        return json.dumps({
            "success": True,
            "removed": removed,
            "matched_count": len(matches),
            "remaining_matches_skipped": max(
                0, len(matches) - len(to_remove)
            ),
            "date": str(target_date),
        }, indent=2, ensure_ascii=False)

    except Exception as e:
        return f"Error removing food: {e}"


@mcp.tool(
    name="mfp_set_water",
    annotations={
        "title": "Log Water Intake",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mfp_set_water(params: SetWaterInput) -> str:
    """
    Log water intake for a specific date.

    Sets the number of cups of water consumed for the day. MyFitnessPal uses
    cups as the unit (1 cup = ~237ml).

    Args:
        params: SetWaterInput containing:
            - cups (float): Number of cups of water (e.g., 2.5 for 2.5 cups)
            - date (str, optional): Date in YYYY-MM-DD format, defaults to today

    Returns:
        str: Confirmation message with the logged water amount
    """
    try:
        client = await get_mfp_client_async()
        target_date = parse_date(params.date)
        
        # Set water intake
        set_water_intake(client=client, target_date=target_date, cups=params.cups)
        _refresh_cache_for_date(client, target_date)
        
        return json.dumps(
            {
                "success": True,
                "message": f"Successfully logged {params.cups} cups of water",
                "date": str(target_date),
                "cups": params.cups,
                "milliliters": round(params.cups * 236.588, 2),
            },
            indent=2,
        )
        
    except Exception as e:
        return f"Error setting water intake: {str(e)}"


@mcp.tool(
    name="mfp_get_report",
    annotations={
        "title": "Get Nutrition Report",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_get_report(params: GetReportInput) -> str:
    """
    Get a nutrition report over a date range.

    Returns daily values for the specified nutrient/metric over the date range.
    Useful for analyzing trends and patterns in nutrition intake.

    Args:
        params: GetReportInput containing:
            - report_name (str): Report type (e.g., 'Net Calories', 'Protein')
            - start_date (str, optional): Start date, defaults to 7 days ago
            - end_date (str, optional): End date, defaults to today
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Daily values and summary statistics for the report period
    """
    try:
        client = await get_mfp_client_async()

        end = parse_date(params.end_date)
        if params.start_date:
            start = parse_date(params.start_date)
        else:
            start = end - timedelta(days=7)

        report = client.get_report(
            report_name=params.report_name,
            report_category="Nutrition",
            lower_bound=start,
            upper_bound=end,
        )

        data = {
            "report_name": params.report_name,
            "start_date": str(start),
            "end_date": str(end),
            "values": (
                ordered_dict_to_dict(report) if isinstance(report, OrderedDict) else report
            ),
        }

        # Calculate summary stats
        if report:
            values = list(report.values())
            numeric_values = [v for v in values if isinstance(v, (int, float))]
            if numeric_values:
                data["summary"] = {
                    "total": sum(numeric_values),
                    "average": round(sum(numeric_values) / len(numeric_values), 2),
                    "min": min(numeric_values),
                    "max": max(numeric_values),
                }

        return format_response(
            data, params.response_format, f"{params.report_name} Report"
        )

    except Exception as e:
        return f"Error getting report: {str(e)}"


# ============================================================================
# Cookie Management Tool
# ============================================================================


def _verify_cookies_and_format(cookies: Dict[str, str], source: str) -> str:
    """Verify cookies via a live MFP round-trip, then persist on success.

    Persisting only after verification matches the auto-discovery path's
    anti-poisoning behavior — a stale/expired session can't clobber a
    previously good `cookies.json`.
    """
    if not _has_real_mfp_session(cookies):
        return (
            f"No MyFitnessPal session token found in {source}. "
            "Make sure you are logged into myfitnesspal.com in that browser, "
            "then try again."
        )
    try:
        import myfitnesspal
        cookiejar = dict_to_cookiejar(cookies)
        client = myfitnesspal.Client(cookiejar=cookiejar)
        _ = client.get_date(date.today())
    except Exception as e:
        return (
            f"Cookies were extracted from {source} but verification failed: "
            f"{e}. The session may have expired — log in again and retry. "
            f"(cookies.json was NOT overwritten.)"
        )
    save_cookies(cookies)
    return (
        f"Successfully extracted and verified {len(cookies)} cookies "
        f"from {source}. Authentication is now working."
    )


@mcp.tool()
def refresh_browser_cookies(browser: str = "auto") -> str:
    """
    Extract and save session cookies from your web browser.

    Use this tool when authentication fails and you need to refresh your
    MyFitnessPal session. You must be logged into myfitnesspal.com in the
    target browser.

    Args:
        browser: Source to extract cookies from. Options:
                 - 'auto' (default): scan every installed Chromium-based
                   browser on macOS (Arc, Chrome, Edge, Brave, Vivaldi,
                   Opera, ...) and use the first one with a valid session.
                 - 'arc', 'chrome', 'chromium', 'edge', 'brave', 'vivaldi',
                   'opera': force a specific Chromium browser (macOS).
                 - 'firefox': use browser_cookie3 (Firefox is not Chromium).

    Returns:
        Success message or error description.
    """
    browser_key = browser.lower().strip()

    # 'auto' — discover every Chromium browser via keychain Safe Storage
    if browser_key == "auto":
        result = try_chromium_browsers_for_session_cookies()
        if not result:
            return (
                "Auto-discovery did not find a Chromium browser with a "
                "valid MyFitnessPal session. Log into myfitnesspal.com in "
                "Arc, Chrome, Edge, Brave, Vivaldi, or Opera, then retry. "
                "(macOS only — on Linux/Windows, pass 'chrome' or "
                "'firefox' instead.)"
            )
        browser_name, cookies = result
        return _verify_cookies_and_format(cookies, browser_name)

    # Explicit Chromium browser
    if browser_key in _CHROMIUM_BROWSER_ALIASES:
        canonical = _CHROMIUM_BROWSER_ALIASES[browser_key]
        if sys.platform == "darwin":
            service_name = f"{canonical} Safe Storage"
            cookies = _try_extract_from_chromium_browser(service_name)
            if cookies is None:
                return (
                    f"Could not read cookies from {canonical}. Make sure "
                    "the browser is installed and you have logged in at "
                    "least once."
                )
            return _verify_cookies_and_format(cookies, canonical)
        # Non-macOS: keychain-based path doesn't apply. browser_cookie3
        # handles chrome/chromium on Linux/Windows via their default
        # profile paths; other Chromium browsers aren't supported there.
        if browser_key in ("chrome", "chromium"):
            try:
                import browser_cookie3
                cj = browser_cookie3.chrome(domain_name=".myfitnesspal.com")
                cookies = {c.name: c.value for c in cj}
            except Exception as e:
                return f"Error extracting cookies from {browser_key}: {e}"
            return _verify_cookies_and_format(cookies, browser_key)
        return (
            f"{canonical} cookie extraction requires macOS (keychain-backed "
            f"Safe Storage). On this platform, use 'chrome' or 'firefox'."
        )

    # Firefox via browser_cookie3 (it has its own format, not Chromium)
    if browser_key == "firefox":
        try:
            import browser_cookie3
            cj = browser_cookie3.firefox(domain_name=".myfitnesspal.com")
            cookies = {c.name: c.value for c in cj}
        except Exception as e:
            return f"Error extracting cookies from firefox: {e}"
        return _verify_cookies_and_format(cookies, "firefox")

    valid_options = sorted({*_CHROMIUM_BROWSER_ALIASES, "firefox", "auto"})
    return (
        f"Unsupported browser: {browser!r}. Use 'auto' to scan all installed "
        f"Chromium browsers, or one of: {', '.join(valid_options)}."
    )


# ============================================================================
# Main Entry Point
# ============================================================================



@mcp.tool(
    name="mfp_create_custom_food",
    annotations={
        "title": "Create Custom Food",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mfp_create_custom_food(params: CreateCustomFoodInput) -> str:
    """
    Create a private custom food in the user's MyFitnessPal account.

    Fills the full nutrition panel MFP supports (macros, fats breakdown,
    cholesterol, sodium, potassium, fiber, sugars, and the four %DV micros).
    Uses the cookie-authenticated web endpoint, so no browser needs to be
    running. Returns the new food's id, which mfp_add_food_to_diary accepts.

    CARBS ARE NET (with the default country_code="NL"): pass net carbs in
    `carbs`; MFP stores net_carbs as given and reports total = carbs + fiber.
    Never pre-add fiber. Verified: carbs=42/fiber=8 stores 50/42 under "NL" but
    42/34 with country_code omitted, so the field is load-bearing, not cosmetic.

    MFP has no update endpoint. To correct a food, create the corrected version
    then mfp_delete_custom_food the old one.

    Args:
        params: CreateCustomFoodInput (description, brand_name, serving_amount,
            serving_unit, calories + optional nutrients, public, response_format)

    Returns:
        str: The created food's id, description and HTTP status
    """
    try:
        client = await get_mfp_client_async()
        result = create_custom_food(client, params.model_dump())
        return format_response(result, params.response_format, "Custom Food Created")
    except Exception as e:
        return f"Error creating custom food: {str(e)}"


@mcp.tool(
    name="mfp_list_own_foods",
    annotations={
        "title": "List Own Custom Foods",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_list_own_foods(params: ListOwnFoodsInput) -> str:
    """
    List the user's own custom foods, newest first.

    Private custom foods do not reliably surface in mfp_search_food, so this is
    the way to find the id of something previously created.

    Args:
        params: ListOwnFoodsInput (search, limit, response_format)

    Returns:
        str: Matching custom foods with id, description, brand and calories
    """
    try:
        client = await get_mfp_client_async()
        foods = list_own_foods(client, params.search)[: params.limit]
        data = {
            "count": len(foods),
            "foods": [
                {
                    "id": f.get("id"),
                    "description": f.get("description"),
                    "brand_name": f.get("brand_name"),
                    "calories": (f.get("nutritional_contents", {}).get("energy", {}) or {}).get("value"),
                    "serving": (f.get("serving_sizes") or [{}])[0].get("unit"),
                    "public": f.get("public"),
                }
                for f in foods
            ],
        }
        return format_response(data, params.response_format, "My Custom Foods")
    except Exception as e:
        return f"Error listing own foods: {str(e)}"


@mcp.tool(
    name="mfp_delete_custom_food",
    annotations={
        "title": "Delete Custom Food",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_delete_custom_food(params: DeleteCustomFoodInput) -> str:
    """
    Delete one of the user's custom foods by id.

    Destructive and not recoverable. A food actively referenced by a logged
    diary entry may be refused by MyFitnessPal.

    Args:
        params: DeleteCustomFoodInput (food_id)

    Returns:
        str: Confirmation with the HTTP status
    """
    try:
        client = await get_mfp_client_async()
        status = delete_custom_food(client, params.food_id)
        data = {"food_id": params.food_id, "deleted": True, "status": status}
        return format_response(data, params.response_format, "Custom Food Deleted")
    except Exception as e:
        return f"Error deleting custom food: {str(e)}"

if __name__ == "__main__":
    transport = os.environ.get("MFP_TRANSPORT", "streamable-http")
    logger.info(f"Starting MyFitnessPal bridge — transport={transport}, host={_HOST}, port={_PORT}")
    if transport == "streamable-http":
        import uvicorn

        uvicorn.run(
            BearerAuthMiddleware(mcp.streamable_http_app()),
            host=_HOST,
            port=_PORT,
            log_level="info",
        )
    elif transport == "sse":
        import uvicorn

        uvicorn.run(
            BearerAuthMiddleware(mcp.sse_app()),
            host=_HOST,
            port=_PORT,
            log_level="info",
        )
    else:
        mcp.run(transport=transport)
