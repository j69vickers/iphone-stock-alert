#!/usr/bin/env python3
"""Apple pickup-stock alert for one store (default: Barton Creek, Austin TX).

Usage:
  python check.py                 normal run (used by GitHub Actions)
  python check.py --probe PART    print what Apple returns for PART (verify the part number)
  python check.py --test-notify   send a test push to your ntfy topic
  python check.py --dry-run       don't send notifications or write state

Env: NTFY_TOPIC (required for notifications), NTFY_SERVER (default https://ntfy.sh)
Needs: pip install playwright (uses installed Google Chrome).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
WARMUP_URL = "https://www.apple.com/shop/buy-iphone/iphone-18-pro"
HEADED = bool(os.environ.get("HEADED"))  # set in CI; run under xvfb to avoid headless detection
UA_PLATFORM = "X11; Linux x86_64" if sys.platform.startswith("linux") else "Windows NT 10.0; Win64; x64"


class SchemaError(Exception):
    """Response was received but doesn't look like what we expect."""


class FetchError(Exception):
    """Network / HTTP failure after retries."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- fetching
def _browser_fetch(path):
    """Apple rejects plain HTTP clients and 'HeadlessChrome' (HTTP 541), so we load an
    apple.com page in real Chrome and call the endpoint from inside it."""
    from playwright.sync_api import sync_playwright, Error as PWError
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(
                channel="chrome", headless=not HEADED,
                ignore_default_args=["--enable-automation"] + ([] if HEADED else ["--headless"]),
                args=["--disable-blink-features=AutomationControlled"] + ([] if HEADED else ["--headless=new"]))
            try:
                major = b.version.split(".")[0]
                ua = (f"Mozilla/5.0 ({UA_PLATFORM}) AppleWebKit/537.36 (KHTML, like Gecko) "
                      f"Chrome/{major}.0.0.0 Safari/537.36")
                page = b.new_context(locale="en-US", user_agent=ua).new_page()
                page.goto(WARMUP_URL, wait_until="load", timeout=45000)
                page.wait_for_timeout(3000)
                return page.evaluate(
                    "async p => { const r = await fetch(p, {credentials:'include'});"
                    " return [r.status, await r.text(), r.headers.get('retry-after')]; }", path)
            finally:
                b.close()
    except PWError as e:
        raise FetchError(f"browser: {str(e).splitlines()[0][:150]}")


def fetch(parts, zip_code, retries=3):
    q = [("pl", "true"), ("mts.0", "regular"), ("mts.1", "compact"),
         ("searchNearby", "true"), ("location", zip_code)]
    q += [(f"parts.{i}", p) for i, p in enumerate(parts)]
    path = f"/shop/fulfillment-messages?{urllib.parse.urlencode(q, safe='/')}"
    last = "unknown"
    for attempt in range(retries):
        wait = 5 * (attempt + 1)
        try:
            status, text, retry_after = _browser_fetch(path)
        except FetchError as e:
            last = str(e)
        else:
            if status == 200:
                try:
                    return json.loads(text)
                except ValueError:
                    raise SchemaError("200 response was not JSON")
            last = f"HTTP {status}"
            if status == 429 or status == 503:
                try:
                    wait = min(float(retry_after), 60)
                except (TypeError, ValueError):
                    wait = 10 * (attempt + 1)
        if attempt < retries - 1:
            time.sleep(wait)
    raise FetchError(last)


def find_store(data, name_part):
    try:
        stores = data["body"]["content"]["pickupMessage"]["stores"]
    except (KeyError, TypeError):
        raise SchemaError("missing body.content.pickupMessage.stores")
    if not isinstance(stores, list):
        raise SchemaError("'stores' is not a list")
    if not stores:
        raise SchemaError("no stores returned (invalid part numbers or format change)")
    for s in stores:
        if name_part.lower() in str(s.get("storeName", "")).lower():
            return s
    raise SchemaError(f"no store matching '{name_part}' among "
                      f"{[s.get('storeName') for s in stores]}")


def parse(data, cfg):
    store = find_store(data, cfg["store_name_contains"])
    avail = store.get("partsAvailability")
    if not isinstance(avail, dict):
        raise SchemaError("store has no partsAvailability dict")
    out = {}
    for p in cfg["parts"]:
        info = avail.get(p["part"])
        if info is None:
            raise SchemaError(f"part {p['part']} missing from response (wrong part number?)")
        if "pickupDisplay" not in info:
            raise SchemaError(f"part {p['part']} has no pickupDisplay field")
        out[p["part"]] = {
            "available": info["pickupDisplay"] == "available",
            "title": info.get("storePickupProductTitle") or p.get("label") or p["part"],
        }
    return store, out


# ---------------------------------------------------------------- notifying
def notify(title, message, click=None, priority="default", tags=None, dry=False):
    topic = os.environ.get("NTFY_TOPIC")
    print(f"[notify] {title} | {message}")
    if dry:
        return
    if not topic:
        print("NTFY_TOPIC not set - skipping push", file=sys.stderr)
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
        headers["Actions"] = f"view, Reserve, {click}"
    req = urllib.request.Request(f"{server}/{topic}", data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # never let a notify failure crash the run
        print(f"ntfy send failed: {e}", file=sys.stderr)


def reserve_link(part, store):
    """Best-effort deep link to Apple's reserve flow for this store+part."""
    num = store.get("storeNumber")
    if not num:
        return "https://www.apple.com/shop/buy-iphone"
    q = urllib.parse.urlencode({"partNumber": part, "channel": "1", "rv": "0", "path": "",
                                "sourceID": "", "iPhone": "", "appleCare": "", "store": num})
    return f"https://reserve-us.apple.com/US/en_US/reserve/A?{q}"


# ---------------------------------------------------------------- state
def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- main logic
def run(cfg, dry=False, fetcher=fetch):
    state = load_state()
    health = state.setdefault("_health", {"fails": 0, "alerted": False})
    parts = state.setdefault("parts", {})
    threshold = cfg.get("failure_alert_threshold", 6)  # 6 x 5min = 30min

    try:
        data = fetcher([p["part"] for p in cfg["parts"]], cfg["zip"])
        store, result = parse(data, cfg)
    except (FetchError, SchemaError) as e:
        kind = "format" if isinstance(e, SchemaError) else "fetch"
        health["fails"] += 1
        health["last_error"] = f"{kind}: {e}"
        print(f"ERROR ({health['fails']}x): {kind}: {e}", file=sys.stderr)
        # Schema problems are rarely transient: alert after 2; fetch errors after threshold.
        limit = 2 if kind == "format" else threshold
        if health["fails"] >= limit and not health["alerted"]:
            notify("Stock checker is failing",
                   f"{health['fails']} consecutive failures. Last: {kind}: {e}",
                   priority="high", tags="warning", dry=dry)
            health["alerted"] = True
        if not dry:
            save_state(state)
        return 0  # keep workflow green; failures are surfaced via ntfy

    if health["alerted"]:
        notify("Stock checker recovered", "Checks are working again.", tags="white_check_mark", dry=dry)
    health.update(fails=0, alerted=False)
    health.pop("last_error", None)

    for part, r in result.items():
        prev = parts.get(part, {}).get("available", False)
        if r["available"] and not prev:
            notify(f"IN STOCK at {store.get('storeName', 'store')}",
                   f"{r['title']} ({part}) is available for pickup.",
                   click=reserve_link(part, store), priority="urgent",
                   tags="iphone,tada", dry=dry)
        print(f"{part}: {'AVAILABLE' if r['available'] else 'unavailable'} (was {'available' if prev else 'unavailable'})")
        since = parts.get(part, {}).get("since") if r["available"] == prev and part in parts else now()
        parts[part] = {"available": r["available"], "title": r["title"], "since": since}
    if not dry:
        save_state(state)
    return 0


def probe(part, cfg):
    data = fetch([part], cfg["zip"])
    store = find_store(data, cfg["store_name_contains"])
    info = store.get("partsAvailability", {}).get(part)
    print(f"Store: {store.get('storeName')} ({store.get('storeNumber')})")
    print(json.dumps(info, indent=2) if info else f"Part {part} NOT recognised for this store.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", metavar="PART")
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = json.loads(CONFIG_PATH.read_text())
    if a.test_notify:
        notify("Test alert", "ntfy is wired up correctly.", tags="white_check_mark")
        return 0
    if a.probe:
        probe(a.probe, cfg)
        return 0
    return run(cfg, dry=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())

