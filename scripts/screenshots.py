"""Screenshot every page of the web UI (and the email rendering) with a headless browser.

Usage: python scripts/screenshots.py --base http://127.0.0.1:8765 --email you@example.com --password ... --out docs/screens
Console errors from the pages are printed and make the script exit non-zero, so this doubles
as a smoke test of the charts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PAGES = [("overview", "/"), ("portfolio", "/portfolio"), ("targets", "/targets"), ("symbol", "/symbol/GP"),
         ("alerts", "/alerts"), ("scorecard", "/scorecard"), ("rules", "/rules"), ("settings", "/settings")]


def render_email(out: Path, email: str) -> Path:
    from dse_alerts import engine
    from dse_alerts.appdb import AppDB
    from dse_alerts.config import get_settings
    from dse_alerts.data.store import MarketStore
    from dse_alerts.notify import render
    from dse_alerts.notify.context import build

    s = get_settings()
    store, db = MarketStore(s.market_db), AppDB(s.app_db)
    u = db.user(email)
    session, results = engine.run(store, db, record=False, user_ids=[u["id"]])
    ctx = build(store, db, u["id"], session or dt.date.today(), results[0].alerts if results else [],
                "This is where the optional written summary appears when an AI provider is configured.", base_url="http://127.0.0.1:8000")
    p = out / "email.html"
    p.write_text("<!doctype html><html><head><meta charset='utf-8'></head>" + render.html_body(ctx) + "</html>", encoding="utf-8")
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--out", default="docs/screens")
    ap.add_argument("--themes", default="light,midnight")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.goto(args.base + "/login")
        page.fill("input[name=email]", args.email)
        page.fill("input[name=password]", args.password)
        page.click("button.primary")
        page.wait_for_url(args.base + "/")
        for theme in args.themes.split(","):
            page.evaluate("t => { localStorage.setItem('dse-theme', t); }", theme)
            for name, path in PAGES:
                page.goto(args.base + path)
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(600)
                page.screenshot(path=str(out / f"{name}-{theme}.png"), full_page=True)
                print("shot", name, theme)
        # the email, rendered at a mail-client width
        email_file = render_email(out, args.email)
        page.set_viewport_size({"width": 720, "height": 900})
        page.goto(email_file.resolve().as_uri())
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / "email.png"), full_page=True)
        print("shot email")
        browser.close()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
