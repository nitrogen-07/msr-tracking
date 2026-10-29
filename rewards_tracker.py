#!/usr/bin/env python3
"""
Microsoft Rewards 24/7 Stock Monitor (rewards_tracker.py)
Target SKU: Flipkart Gift Card (https://rewards.bing.com/redeem/sku/000885000034)

Features:
- TLS fingerprint bypass via curl_cffi (Chrome desktop impersonation)
- Continuous ~90s polling loop with human-like random jitter (85-110s)
- Graceful ~5.5-hour execution window inside a 6-hour GitHub Actions runner
- Multi-layer DOM & text validation (HTTP 200 guardrail, Cloudflare/bot checks,
  "So popular we're restocking!" warning check, and "Redeem now" button state)
- Instant HTML-formatted Telegram alerts for restocks, site errors, and recovery
- Atomic JSON state persistence (state_sku.json) with signal trapping
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import os
import random
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------

SKU_ID = os.getenv("REWARDS_SKU_ID", "000885000034")
SKU_URL = os.getenv(
    "REWARDS_SKU_URL",
    f"https://rewards.bing.com/redeem/sku/{SKU_ID}",
)
STATE_FILE = Path(os.getenv("STATE_FILE_PATH", "state_sku.json"))

# Telegram Configuration (loaded from GitHub Secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Optional Microsoft Rewards session cookie string (e.g., "_U=...; MUID=...")
# Note: When unauthenticated ("Not signed in"), Microsoft Rewards renders the
# "Redeem now" button as disabled even when the item is in stock. Supplying
# REWARDS_COOKIE allows authenticated DOM inspection of the live button state.
REWARDS_COOKIE = os.getenv("REWARDS_COOKIE", "").strip()

# Run Duration & Polling Interval Settings
# Default max runtime is 5 hours 40 minutes (20,400s) so the job exits cleanly
# well before GitHub Actions' hard 6-hour (21,600s) job limit, leaving ample
# time to commit state_sku.json and trigger the self-relay job.
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", str(5 * 3600 + 40 * 60)))
JITTER_MIN_SECONDS = float(os.getenv("JITTER_MIN_SECONDS", "85"))
JITTER_MAX_SECONDS = float(os.getenv("JITTER_MAX_SECONDS", "110"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))

# Alert Throttling Thresholds
CONSECUTIVE_ERROR_ALERT_THRESHOLD = int(os.getenv("ERROR_ALERT_THRESHOLD", "3"))
ERROR_ALERT_COOLDOWN_SECONDS = int(os.getenv("ERROR_ALERT_COOLDOWN_SECONDS", "1800"))
RESTOCK_REMINDER_COOLDOWN_SECONDS = int(
    os.getenv("RESTOCK_REMINDER_COOLDOWN_SECONDS", "900")
)

# Standard Desktop Chrome Headers (No mobile / BMS headers)
DESKTOP_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://rewards.bing.com/redeem/",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Restock / Out-of-stock Markers
OUT_OF_STOCK_PHRASES = (
    "so popular we're restocking!",
    "so popular we’re restocking!",
    "so popular we're restocking",
    "so popular we’re restocking",
    "out of stock",
    "currently unavailable",
)

CLOUDFLARE_OR_BOT_MARKERS = (
    "attention required! | cloudflare",
    "just a moment...",
    "cf-browser-verification",
    "challenge-platform",
    "access denied",
    "verify you are human",
)

# Global flag for graceful shutdown on SIGTERM / SIGINT
SHUTDOWN_REQUESTED = False

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)
logging.Formatter.converter = time.gmtime
logger = logging.getLogger("rewards_tracker")


def _handle_shutdown_signal(signum: int, _frame: Any) -> None:
    """Handle SIGTERM/SIGINT cleanly so state_sku.json is saved before exit."""
    global SHUTDOWN_REQUESTED
    sig_name = signal.Signals(signum).name
    logger.warning("Received signal %s. Initiating graceful shutdown...", sig_name)
    SHUTDOWN_REQUESTED = True


signal.signal(signal.SIGTERM, _handle_shutdown_signal)
signal.signal(signal.SIGINT, _handle_shutdown_signal)


# ---------------------------------------------------------------------------
# State Persistence Helpers
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def default_state() -> Dict[str, Any]:
    """Return the default schema for state_sku.json."""
    return {
        "sku_id": SKU_ID,
        "sku_url": SKU_URL,
        "item_name": "Flipkart Gift Card",
        "points_cost": None,
        "status": "unknown",  # "in_stock" | "out_of_stock" | "error" | "unknown"
        "is_signed_in": False,
        "button_enabled": False,
        "restock_warning_present": None,
        "last_checked": None,
        "last_status_change": None,
        "last_restock_alert_epoch": 0,
        "last_error_alert_epoch": 0,
        "consecutive_errors": 0,
        "last_error": None,
        "total_polls": 0,
    }


def load_state(path: Path = STATE_FILE) -> Dict[str, Any]:
    """Load persisted state from disk or initialize a fresh state dictionary."""
    state = default_state()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                state.update(loaded)
                logger.info(
                    "Loaded existing state from %s (status=%s, consecutive_errors=%s)",
                    path,
                    state.get("status"),
                    state.get("consecutive_errors"),
                )
        except Exception as exc:
            logger.error("Failed to read state file %s: %s. Using fresh state.", path, exc)
    else:
        logger.info("No existing state file at %s. Initializing fresh state.", path)
    return state


def save_state(state: Dict[str, Any], path: Path = STATE_FILE) -> None:
    """Atomically write state_sku.json to prevent partial/corrupted writes."""
    tmp_path = path.with_suffix(".json.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=False)
            fh.write("\n")
        tmp_path.replace(path)
    except Exception as exc:
        logger.error("Failed to save state to %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Telegram Notification Helper
# ---------------------------------------------------------------------------


def send_telegram_notification(message: str) -> bool:
    """
    Send an HTML-formatted alert via the Telegram Bot API.
    Uses curl_cffi with standard timeout and retry handling.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning(
            "Telegram credentials (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) are not set. "
            "Skipping notification."
        )
        return False

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    for attempt in range(1, 4):
        try:
            resp = cffi_requests.post(
                api_url,
                json=payload,
                impersonate="chrome",
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info("Telegram notification delivered successfully.")
                return True
            logger.error(
                "Telegram API error (attempt %d/3): HTTP %d - %s",
                attempt,
                resp.status_code,
                resp.text[:300],
            )
        except Exception as exc:
            logger.error(
                "Telegram request exception (attempt %d/3): %s",
                attempt,
                exc,
            )
        time.sleep(2 * attempt)

    return False


# ---------------------------------------------------------------------------
# DOM & Stock Parsing Logic
# ---------------------------------------------------------------------------


def _is_element_disabled(tag: Any) -> bool:
    """Check whether a button/anchor element is disabled or greyed out in HTML."""
    if tag is None:
        return True

    # 1. Explicit HTML disabled attribute
    if tag.has_attr("disabled"):
        return True

    # 2. ARIA disabled state
    aria_disabled = str(tag.get("aria-disabled", "")).strip().lower()
    if aria_disabled in ("true", "1", "disabled"):
        return True

    # 3. Class-based disabled indicators used by Microsoft Fluent / Rewards UI
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    lowered_classes = {c.lower() for c in classes}
    disabled_class_tokens = {
        "disabled",
        "is-disabled",
        "c-disabled",
        "btn-disabled",
        "redeem-disabled",
    }
    if lowered_classes & disabled_class_tokens:
        return True
    if any("disabled" in c for c in lowered_classes):
        return True

    # 4. Inline style pointer-events: none
    style = str(tag.get("style", "")).lower()
    if "pointer-events: none" in style or "pointer-events:none" in style:
        return True

    return False


def analyze_sku_page(html_text: str) -> Dict[str, Any]:
    """
    Parse the Microsoft Rewards SKU page HTML and evaluate restock criteria.

    Restock Detection Rules:
    - Out of stock state:
        The page contains the warning text "So popular we're restocking!"
        AND/OR the "Redeem now" button element is disabled/greyed out.
    - Restocked state:
        The warning text disappears AND the "Redeem now" button element becomes
        active/enabled.
    """
    soup = BeautifulSoup(html_text, "lxml")
    visible_text = soup.get_text(separator=" ", strip=True)
    lowered_text = visible_text.lower()
    lowered_html = html_text.lower()

    # 1. Check for Cloudflare / WAF / Bot block challenge pages
    for marker in CLOUDFLARE_OR_BOT_MARKERS:
        if marker in lowered_html and "microsoft rewards" not in lowered_text:
            return {
                "valid_dom": False,
                "error_reason": f"Bot protection / challenge page detected ({marker})",
            }

    # 2. Verify that expected Microsoft Rewards SKU DOM structure exists
    has_rewards_branding = (
        "microsoft rewards" in lowered_text or "rewards.bing.com" in lowered_html
    )
    has_flipkart_or_sku = (
        "flipkart" in lowered_text
        or SKU_ID in html_text
        or "redeem" in lowered_text
    )
    if not (has_rewards_branding and has_flipkart_or_sku):
        return {
            "valid_dom": False,
            "error_reason": (
                "Unexpected DOM structure: missing Microsoft Rewards / Flipkart SKU content"
            ),
        }

    # 3. Extract product title and points cost (if present)
    item_name = "Flipkart Gift Card"
    for heading in soup.find_all(["h1", "h2", "h3"]):
        text = heading.get_text(strip=True)
        if "flipkart" in text.lower() or "gift card" in text.lower():
            item_name = text
            break

    points_cost: Optional[str] = None
    pts_match = re.search(r"([\d,]{3,9})\s*pts", visible_text, re.IGNORECASE)
    if pts_match:
        points_cost = f"{pts_match.group(1)} pts"

    # 4. Check authentication banner ("Not signed in. Login with your Microsoft account...")
    is_signed_in = "not signed in. login with your microsoft account" not in lowered_text

    # 5. Check for Out-of-Stock warning text ("So popular we're restocking!")
    restock_warning_present = any(
        phrase in lowered_text or phrase in lowered_html
        for phrase in OUT_OF_STOCK_PHRASES
    )

    # Also check embedded JSON/script variables for explicit stock flags if present
    json_out_of_stock = bool(
        re.search(r'"(?:isOutOfStock|outOfStock|SoldOut)"\s*:\s*true', html_text, re.I)
    )
    if json_out_of_stock:
        restock_warning_present = True

    # 6. Locate the "Redeem now" button / control element
    redeem_candidates = []
    for elem in soup.find_all(["button", "a", "input", "div", "span"]):
        elem_text = elem.get_text(strip=True).lower()
        elem_value = str(elem.get("value", "")).strip().lower()
        elem_id = str(elem.get("id", "")).strip().lower()
        elem_bi = str(elem.get("data-bi-id", "") or elem.get("data-bi-name", "")).lower()

        if (
            elem_text == "redeem now"
            or elem_value == "redeem now"
            or "redeem now" in elem_text
            or "redeem" in elem_id
            or "redeem" in elem_bi
        ):
            # Prefer interactive elements (button, a, input) over wrapper divs
            if elem.name in ("button", "a", "input"):
                redeem_candidates.append(elem)

    redeem_button = redeem_candidates[0] if redeem_candidates else None
    button_found = redeem_button is not None
    button_disabled = _is_element_disabled(redeem_button) if button_found else True
    button_enabled = button_found and not button_disabled

    # 7. Evaluate Final Stock Status per Core Specification:
    # - Out of stock state: warning text "So popular we're restocking!" present
    #   AND/OR "Redeem now" button is disabled/greyed out.
    # - Restocked state: warning text disappears AND "Redeem now" button is active/enabled.
    if not restock_warning_present and button_enabled:
        stock_status = "in_stock"
    else:
        stock_status = "out_of_stock"

    return {
        "valid_dom": True,
        "stock_status": stock_status,
        "item_name": item_name,
        "points_cost": points_cost,
        "is_signed_in": is_signed_in,
        "restock_warning_present": restock_warning_present,
        "button_found": button_found,
        "button_enabled": button_enabled,
        "button_disabled": button_disabled,
    }


# ---------------------------------------------------------------------------
# Network Fetch via curl_cffi
# ---------------------------------------------------------------------------


def fetch_sku_page(session: cffi_requests.Session) -> Tuple[Optional[int], str, Optional[str]]:
    """
    Fetch the SKU URL using curl_cffi with Chrome desktop impersonation.
    Returns (status_code, html_body, error_message).
    """
    headers = dict(DESKTOP_HEADERS)
    if REWARDS_COOKIE:
        headers["Cookie"] = REWARDS_COOKIE

    try:
        response = session.get(
            SKU_URL,
            headers=headers,
            impersonate="chrome",
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        return response.status_code, response.text, None
    except Exception as exc:
        return None, "", f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Alert Formatting Helpers
# ---------------------------------------------------------------------------


def build_restock_message(parsed: Dict[str, Any]) -> str:
    """Format an HTML Telegram message for a confirmed restock event."""
    item_name = html.escape(str(parsed.get("item_name") or "Flipkart Gift Card"))
    points_cost = html.escape(str(parsed.get("points_cost") or "N/A"))
    timestamp = utc_now_iso()
    return (
        f"🟢 <b>RESTOCK ALERT: Microsoft Rewards!</b>\n\n"
        f"🎁 <b>Item:</b> {item_name}\n"
        f"💎 <b>Cost:</b> {points_cost}\n"
        f"🆔 <b>SKU:</b> <code>{html.escape(SKU_ID)}</code>\n"
        f"✅ <b>Status:</b> IN STOCK (Warning gone &amp; Redeem button active)\n"
        f"🕒 <b>Time:</b> <code>{timestamp}</code>\n\n"
        f"🔗 <a href=\"{html.escape(SKU_URL)}\">Click Here to Redeem Immediately</a>"
    )


def build_error_message(error_detail: str, consecutive_errors: int) -> str:
    """Format an HTML Telegram message for repeated HTTP/DOM/Cloudflare errors."""
    timestamp = utc_now_iso()
    safe_error = html.escape(error_detail[:500])
    return (
        f"⚠️ <b>SITE / MONITOR ERROR: Microsoft Rewards Tracker</b>\n\n"
        f"🆔 <b>SKU:</b> <code>{html.escape(SKU_ID)}</code>\n"
        f"❌ <b>Consecutive Failures:</b> {consecutive_errors}\n"
        f"📋 <b>Error Detail:</b> <code>{safe_error}</code>\n"
        f"🕒 <b>Time:</b> <code>{timestamp}</code>\n\n"
        f"🔗 <a href=\"{html.escape(SKU_URL)}\">Inspect SKU Page</a>"
    )


def build_recovery_message(previous_errors: int, current_status: str) -> str:
    """Format an HTML Telegram message when the monitor recovers from site errors."""
    timestamp = utc_now_iso()
    return (
        f"🔄 <b>MONITOR RECOVERED: Microsoft Rewards Tracker</b>\n\n"
        f"🆔 <b>SKU:</b> <code>{html.escape(SKU_ID)}</code>\n"
        f"✅ <b>Resolved After:</b> {previous_errors} error(s)\n"
        f"📦 <b>Current Stock State:</b> <code>{html.escape(current_status.upper())}</code>\n"
        f"🕒 <b>Time:</b> <code>{timestamp}</code>"
    )


# ---------------------------------------------------------------------------
# Main Polling Loop
# ---------------------------------------------------------------------------


def run_monitor() -> int:
    """Run the continuous polling loop for the duration of the 6-hour runner window."""
    start_time = time.monotonic()
    state = load_state(STATE_FILE)
    unauth_notice_logged = False

    logger.info("============================================================")
    logger.info("Starting Microsoft Rewards 24/7 Stock Monitor")
    logger.info("Target URL: %s", SKU_URL)
    logger.info(
        "Max runtime: %d seconds (~%.1f hours) | Jitter: %.0f-%.0fs",
        MAX_RUNTIME_SECONDS,
        MAX_RUNTIME_SECONDS / 3600.0,
        JITTER_MIN_SECONDS,
        JITTER_MAX_SECONDS,
    )
    logger.info(
        "Session cookie configured: %s",
        "YES" if bool(REWARDS_COOKIE) else "NO (unauthenticated mode)",
    )
    logger.info("============================================================")

    with cffi_requests.Session() as session:
        while not SHUTDOWN_REQUESTED:
            elapsed = time.monotonic() - start_time
            if elapsed >= MAX_RUNTIME_SECONDS:
                logger.info(
                    "Reached target runner duration (%.1f mins). Exiting cleanly for relay handoff.",
                    elapsed / 60.0,
                )
                break

            now_epoch = int(time.time())
            now_iso = utc_now_iso()
            state["total_polls"] = int(state.get("total_polls", 0)) + 1
            state["last_checked"] = now_iso

            status_code, html_body, net_error = fetch_sku_page(session)

            # ---------------------------------------------------------------
            # Validation Guardrail: Check HTTP 200 OK before parsing stock
            # ---------------------------------------------------------------
            error_reason: Optional[str] = None
            if net_error is not None:
                error_reason = f"Network error: {net_error}"
            elif status_code != 200:
                if status_code == 403:
                    error_reason = "HTTP 403 Forbidden (WAF/TLS or IP block)"
                elif status_code == 429:
                    error_reason = "HTTP 429 Too Many Requests (Rate limited)"
                elif status_code == 503:
                    error_reason = "HTTP 503 Service Unavailable"
                else:
                    error_reason = f"Unexpected HTTP status code: {status_code}"
            else:
                parsed = analyze_sku_page(html_body)
                if not parsed.get("valid_dom"):
                    error_reason = str(parsed.get("error_reason", "Invalid DOM structure"))

            # ---------------------------------------------------------------
            # Branch A: Site / HTTP / DOM Error Handling
            # ---------------------------------------------------------------
            if error_reason is not None:
                state["consecutive_errors"] = int(state.get("consecutive_errors", 0)) + 1
                state["last_error"] = error_reason
                c_errors = state["consecutive_errors"]
                logger.warning(
                    "Poll #%d failed (%d consecutive): %s",
                    state["total_polls"],
                    c_errors,
                    error_reason,
                )

                last_err_alert = int(state.get("last_error_alert_epoch", 0))
                if (
                    c_errors >= CONSECUTIVE_ERROR_ALERT_THRESHOLD
                    and (now_epoch - last_err_alert) >= ERROR_ALERT_COOLDOWN_SECONDS
                ):
                    if send_telegram_notification(build_error_message(error_reason, c_errors)):
                        state["last_error_alert_epoch"] = now_epoch
                        state["status"] = "error"

                save_state(state, STATE_FILE)

            # ---------------------------------------------------------------
            # Branch B: Valid HTTP 200 OK & Valid DOM Response
            # ---------------------------------------------------------------
            else:
                prev_errors = int(state.get("consecutive_errors", 0))
                prev_status = str(state.get("status", "unknown"))
                new_status = str(parsed["stock_status"])

                # If recovering from a previously alerted error streak, notify recovery
                if (
                    prev_errors >= CONSECUTIVE_ERROR_ALERT_THRESHOLD
                    and int(state.get("last_error_alert_epoch", 0)) > 0
                ):
                    send_telegram_notification(
                        build_recovery_message(prev_errors, new_status)
                    )

                state["consecutive_errors"] = 0
                state["last_error"] = None
                state["item_name"] = parsed.get("item_name") or state.get("item_name")
                state["points_cost"] = parsed.get("points_cost") or state.get("points_cost")
                state["is_signed_in"] = bool(parsed.get("is_signed_in"))
                state["button_enabled"] = bool(parsed.get("button_enabled"))
                state["restock_warning_present"] = bool(parsed.get("restock_warning_present"))

                if not state["is_signed_in"] and not unauth_notice_logged:
                    logger.info(
                        "Note: Page indicates 'Not signed in'. On Microsoft Rewards, the "
                        "'Redeem now' button remains disabled until signed in. Set the "
                        "optional REWARDS_COOKIE secret if you want authenticated button state."
                    )
                    unauth_notice_logged = True

                logger.info(
                    "Poll #%d OK | SKU=%s | Status=%s | Warning=%s | ButtonEnabled=%s | SignedIn=%s",
                    state["total_polls"],
                    SKU_ID,
                    new_status.upper(),
                    state["restock_warning_present"],
                    state["button_enabled"],
                    state["is_signed_in"],
                )

                # Detect transition to IN_STOCK or periodic reminder while still in stock
                last_restock_alert = int(state.get("last_restock_alert_epoch", 0))
                should_alert_restock = False

                if new_status == "in_stock":
                    if prev_status != "in_stock":
                        should_alert_restock = True
                    elif (now_epoch - last_restock_alert) >= RESTOCK_REMINDER_COOLDOWN_SECONDS:
                        should_alert_restock = True

                if should_alert_restock:
                    logger.info("RESTOCK DETECTED! Sending Telegram notification...")
                    if send_telegram_notification(build_restock_message(parsed)):
                        state["last_restock_alert_epoch"] = now_epoch

                if new_status != prev_status:
                    state["last_status_change"] = now_iso
                    state["status"] = new_status

                save_state(state, STATE_FILE)

            # ---------------------------------------------------------------
            # Sleep with Human-Like Jitter (85-110s) unless near runner cutoff
            # ---------------------------------------------------------------
            remaining = MAX_RUNTIME_SECONDS - (time.monotonic() - start_time)
            if remaining <= 0 or SHUTDOWN_REQUESTED:
                break

            jitter_sleep = random.uniform(JITTER_MIN_SECONDS, JITTER_MAX_SECONDS)
            actual_sleep = min(jitter_sleep, remaining)
            logger.info("Sleeping for %.2f seconds before next poll...", actual_sleep)

            # Sleep in 1-second increments so SIGTERM/SIGINT is handled immediately
            sleep_end = time.monotonic() + actual_sleep
            while not SHUTDOWN_REQUESTED and time.monotonic() < sleep_end:
                time.sleep(min(1.0, sleep_end - time.monotonic()))

    save_state(state, STATE_FILE)
    logger.info("Monitor loop finished cleanly. State saved to %s.", STATE_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(run_monitor())