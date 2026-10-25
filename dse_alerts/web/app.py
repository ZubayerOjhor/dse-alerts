"""The web UI: a server-rendered FastAPI app. No JavaScript framework, no external requests
from the browser (the chart library is vendored), no analytics. Everything it shows comes
from the two local databases.

Languages: English and Bangla. A visitor's choice lives in a cookie (``/lang/{code}``); a
logged-in user's saved preference (Settings) is the fallback, then ``APP_DEFAULT_LANG``.

Demo mode (``APP_DEMO=1``): the login page offers "Try the demo", which signs the visitor
into a shared account seeded by ``dse demo seed``. That account is read-only; every route
that writes refuses it with a note pointing at registration.

Run with ``dse web serve`` (uvicorn) or mount ``create_app()`` yourself.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import math
import re
import secrets
from collections import Counter
from email.utils import parseaddr
from functools import partial
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pandas as pd
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from jinja2 import pass_context
from starlette.exceptions import HTTPException as StarletteHTTPException

from dse_alerts import engine, evidence, market, portfolio
from dse_alerts.appdb import DEMO_EMAIL, AppDB, is_demo
from dse_alerts.config import Settings, get_settings
from dse_alerts.data.store import MarketStore
from dse_alerts.i18n import LANGS, fmt_date, normalize, t
from dse_alerts.i18n.alerts import localize
from dse_alerts.rules import RULES, Alert
from dse_alerts.rules.catalog import OPT_IN
from dse_alerts.web.auth import (
    PASSWORD_MAX_LENGTH,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    RateLimiter,
    Sessions,
    hash_password,
    password_tag,
    verify_password,
)

HERE = Path(__file__).parent
LANG_COOKIE = "dse_lang"
LANG_MAX_AGE = 60 * 60 * 24 * 365

# The Learn page, in reading order; each id has learn.<id>.t (title) and learn.<id>.b (body) in the catalogs.
LEARN_SECTIONS = ["what", "not", "session", "circuit", "category", "volume", "unusual", "trend", "range", "pe", "psi",
                  "dividend", "columns", "rules", "alerts", "scorecard", "portfolio", "ai"]

# Stock finder presets: query values (percentages as the form shows them) that a preset fills in.
FIND_PRESETS = {
    "all": {},
    "low": {"near": "low", "sort": "range"},
    "high": {"near": "high", "sort": "ret_20"},
    "volume": {"vol_min": 2.0, "sort": "volume"},
    "cheap": {"category": "A", "pe_max": 10.0, "sort": "pe"},
    "dividend": {"category": "A", "sort": "dy"},
    "fallen": {"ret20_max": -15.0, "sort": "ret_20", "dir": "asc"},
    "risen": {"ret20_min": 15.0, "sort": "ret_20"},
}
FIND_PAGE = 50
FIND_NUMS = ("pe_min", "pe_max", "ret20_min", "ret20_max", "vol_min", "price_max")
FIND_PAIRS = (("pe_min", "pe_max"), ("ret20_min", "ret20_max"))
FIND_COLUMNS = ("symbol", "sector", "category", "close", "ret_1", "ret_20", "volume", "range", "pe", "dy")


def _named(chart: dict, lang: str) -> dict:
    """Series names that come out of market.py in English, in the page language."""
    names = {"Book value": "chart.book_value", "Cost": "chart.cost"}
    for s in chart.get("series", []):
        if s.get("name") in names:
            s["name"] = t(lang, names[s["name"]])
    return chart


def _plain(v) -> str:
    """A preset number as a user would type it: 10 not 10.0."""
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)

LOGIN_LIMIT_PER_IP, LOGIN_LIMIT_PER_EMAIL, LOGIN_WINDOW = 30, 10, 15 * 60
REGISTER_LIMIT_PER_IP, REGISTER_WINDOW = 5, 60 * 60
DIGEST_LIMIT_PER_DAY = 6                 # AI notes written on demand per user per day
CHECK_LIMIT, CHECK_WINDOW = 3, 5 * 60    # "Check now" runs per user
CONFIRM_MAX_AGE = 3 * 24 * 3600          # emailed confirmation links
TELEGRAM_ID = re.compile(r"-?\d{1,20}")
# Sent with every response. HSTS is added by the HTTPS proxy (deploy/Caddyfile), not here.
# The page scripts and the chart JSON are inline, hence 'unsafe-inline' for scripts and styles.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                               "form-action 'self'; base-uri 'self'; object-src 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def _valid_email(addr: str) -> bool:
    _name, spec = parseaddr(addr)
    return bool(spec) and spec == addr and "@" in spec and " " not in spec and len(spec) <= 254


def _local_path(url: str | None) -> str:
    """Reduce a Referer to a path on this site, so a redirect can never leave it."""
    if not url:
        return "/"
    path = urlsplit(url)
    out = path.path or "/"
    if path.query:
        out += "?" + path.query
    return out if out.startswith("/") and not out.startswith("//") else "/"


def _isnan(x):
    return x is None or (isinstance(x, float) and pd.isna(x))


def _pct(x, digits=1):
    return "-" if _isnan(x) else f"{100 * float(x):+.{digits}f}%"


def _num(x, digits=1):
    return "-" if _isnan(x) else f"{float(x):,.{digits}f}"


def _as_date(v) -> dt.date | None:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


@pass_context
def _date_filter(ctx, v) -> str:
    """'2026-10-12', a date, or a timestamp -> '12 Oct 2026' in the page's language."""
    if _as_date(v) is None:
        return "" if v in (None, "") else str(v)
    return fmt_date(ctx.get("lang", "en"), v)


@pass_context
def _when_filter(ctx, v) -> str:
    """'2026-10-16T10:32:05' -> '16 Oct 2026, 10:32'."""
    if not v:
        return ""
    try:
        d = dt.datetime.fromisoformat(str(v))
    except ValueError:
        return str(v)
    return f"{fmt_date(ctx.get('lang', 'en'), d.date())}, {d:%H:%M}"


def _records(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    out = df.astype(object).where(pd.notna(df), None).to_dict("records")
    for r in out:
        for k, v in r.items():
            if isinstance(v, pd.Timestamp):
                r[k] = v.date()
    return out


log = logging.getLogger(__name__)

FLASH_COOKIE = "dse_flash"


def _localized(rows, lang: str) -> list[dict]:
    """Alert rows (sqlite Rows or dicts) with headline/detail in ``lang``."""
    out = []
    for r in rows:
        d = dict(r)
        d["headline"], d["detail"] = localize(d.get("rule_id", ""), d.get("symbol", ""), d.get("evidence_json") or d.get("evidence"),
                                              d.get("headline", ""), d.get("detail", ""), lang)
        out.append(d)
    return out


def create_app(settings: Settings | None = None, store: MarketStore | None = None, appdb: AppDB | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = store or MarketStore(settings.market_db, prefer_snapshot=True)   # reads the published copy
    appdb = appdb or AppDB(settings.app_db)
    secret = settings.app_secret_key
    if not secret or secret == "change-me":
        secret = secrets.token_urlsafe(48)   # never run with a guessable signing key
        log.warning("APP_SECRET_KEY is not set: using a random key, sessions will not survive a restart")
    sessions = Sessions(secret)
    confirm_tokens = URLSafeTimedSerializer(secret, salt="dse-confirm")
    limiter = RateLimiter()
    dummy_hash = hash_password(secrets.token_urlsafe(16))   # keeps login timing equal for unknown emails

    app = FastAPI(title="dse-alerts", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        head = request.method == "HEAD"
        if head:   # uptime monitors send HEAD; answer it like GET without a body
            request.scope["method"] = "GET"
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        if head:   # same headers, no body; the body's Content-Length must go with it
            headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
            return Response(status_code=resp.status_code, headers=headers, media_type=resp.media_type)
        return resp
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["pct"] = _pct
    templates.env.filters["num"] = _num
    templates.env.filters["d"] = _date_filter
    templates.env.filters["when"] = _when_filter

    # ---- helpers ----------------------------------------------------------------------

    def current_user(request: Request):
        sess = sessions.read(request.cookies.get(SESSION_COOKIE))
        if not sess:
            return None
        u = appdb.user_by_id(int(sess["uid"]))
        if not u or sess.get("pw") != password_tag(u["password_hash"]):
            return None   # unknown user, or the password changed since this cookie was issued
        request.state.csrf = sess["csrf"]
        return u

    def client_ip(request: Request) -> str:
        # The web container listens on localhost only and Caddy is the sole way in, so the
        # first X-Forwarded-For entry is the real client. Without a proxy it is the socket peer.
        fwd = request.headers.get("x-forwarded-for", "")
        return (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "?")

    def is_https(request: Request) -> bool:
        return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"

    def same_origin(request: Request) -> None:
        """Forms that have no CSRF token yet (login, sign-up) must at least come from this site."""
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower():
            raise HTTPException(status_code=400, detail="error.bad_origin")

    def send_confirmation(user_id: int, email: str) -> bool:
        """Email a signed link that marks ``email`` as a confirmed alert destination."""
        from dse_alerts.notify import deliver

        if not settings.email_enabled or not _valid_email(email):
            return False
        token = confirm_tokens.dumps({"uid": user_id, "email": email})
        link = f"{settings.base_url}/confirm/{token}"
        lang = dict(appdb.user_by_id(user_id) or {}).get("lang") or "en"
        return deliver.send_plain_email(settings, email, t(lang, "email.confirm_subject"), t(lang, "email.confirm_body", link=link))

    def require_user(request: Request):
        u = current_user(request)
        if not u:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        request.state.user = u
        return u

    def lang_of(request: Request, user=None) -> str:
        cookie = request.cookies.get(LANG_COOKIE)
        if cookie in LANGS:
            return cookie
        if user is not None:
            saved = dict(user).get("lang")
            if saved in LANGS:
                return saved
        return normalize(settings.app_default_lang)

    def writable(request: Request, user) -> None:
        """Refuse writes from the shared demo account."""
        if is_demo(user):
            raise HTTPException(status_code=403, detail="demo.readonly")

    def check_csrf(request: Request, token: str):
        if token != getattr(request.state, "csrf", None):
            raise HTTPException(status_code=400, detail="bad csrf token")

    def read_flash(request: Request, lang: str) -> dict | None:
        raw = request.cookies.get(FLASH_COOKIE)
        if not raw:
            return None
        try:
            f = json.loads(raw)
            f["text"] = t(lang, f["k"], **(f.get("a") or {}))
            return f
        except (ValueError, KeyError, TypeError):
            return None

    def flash(request: Request, resp, key: str, tone: str = "ok", undo: dict | None = None, typed: str = "", **args):
        """Show ``key`` on the next page: 'GP added', 'XXXX is not a trading code', with an undo for a removal."""
        payload = {"k": key, "a": args, "tone": tone, "undo": undo, "typed": typed[:40]}
        resp.set_cookie(FLASH_COOKIE, json.dumps(payload, ensure_ascii=False), max_age=90, samesite="lax", httponly=True, secure=is_https(request))
        return resp

    def back_to(request: Request, default: str = "/", anchor: str = "") -> RedirectResponse:
        """Return to the page the form was on (never the top of the overview), at ``anchor``."""
        path = _local_path(request.headers.get("referer")) if request.headers.get("referer") else default
        path = path.split("#")[0]
        return RedirectResponse(path + (f"#{anchor}" if anchor else ""), status_code=303)

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        u = getattr(request.state, "user", None)
        lang = lang_of(request, u)
        fl = read_flash(request, lang)
        resp = templates.TemplateResponse(request, name, {"user": u, "csrf": getattr(request.state, "csrf", ""),
                                                          "settings": settings, "now": dt.datetime.now(),
                                                          "lang": lang, "langs": LANGS, "t": partial(t, lang),
                                                          "demo": is_demo(u), "flash": fl, **ctx},
                                          status_code=status_code)
        if fl is not None:
            resp.delete_cookie(FLASH_COOKIE)
        return resp

    def resolve_symbol(text: str) -> tuple[str | None, list[str]]:
        """A trading code, or a company name typed in full or in part. Returns the code and,
        when the name fits several companies, up to five of them."""
        inst = store.instruments()
        q = text.strip().upper()
        if not q:
            return None, []
        codes = set(inst["symbol"])
        if q in codes:
            return q, []
        names = inst[["symbol", "name"]].dropna()
        names = names.assign(up=names["name"].astype(str).str.upper())
        exact = names[names["up"] == q]
        if len(exact) == 1:
            return str(exact["symbol"].iloc[0]), []
        # a part of a name counts only when it fits exactly one company: "walton" yes, "bank" no
        hit = names[names["up"].str.contains(re.escape(q), regex=True)]
        if len(hit) == 1:
            return str(hit["symbol"].iloc[0]), []
        if len(hit) > 1:
            hit = hit.assign(first=~hit["up"].str.startswith(q)).sort_values(["first", "symbol"])
            opts = [f"{r.symbol} ({r.name})" for r in hit.head(5).itertuples()]
            if len(hit) > 5:
                opts.append(f"+{len(hit) - 5}")
            return None, opts
        return None, []

    def set_session(request: Request, resp, user_id: int, password_hash: str | None):
        resp.set_cookie(SESSION_COOKIE, sessions.issue(user_id, password_hash), max_age=SESSION_MAX_AGE, httponly=True,
                        samesite="lax", secure=is_https(request))
        return resp

    def demo_available() -> bool:
        return bool(settings.app_demo and appdb.user(DEMO_EMAIL) is not None)

    def market_today():
        return {"index": market.index_last(store), "board": market.sector_board(store), "movers": market.top_movers(store, 6)}

    def market_status():
        s = store.summary()
        last = engine.latest_session(store)
        stale = (dt.date.today() - last).days > 4 if last else True
        return {"last_session": last, "rows": s["rows"], "symbols": s["symbols"], "stale": stale}

    def watch_rows(user_id: int):
        items = appdb.items(user_id)
        kinds = {i["symbol"]: i["kind"] for i in items}
        rows = _records(market.latest_rows(store, sorted(kinds)))
        held = {h["symbol"] for h in appdb.holdings(user_id)}
        for r in rows:
            r["kind"] = "owned" if r["symbol"] in held else kinds.get(r["symbol"], "watch")
        order = {"owned": 0, "target": 1, "watch": 2}
        rows.sort(key=lambda r: (order.get(r["kind"], 9), r["symbol"]))
        return items, rows

    def write_digest_if_due(user, session: dt.date | None, alerts: list, lang: str | None = None) -> None:
        """Write and store the AI note for this user and session when a provider is configured.
        A note already stored in the requested language is kept; one in another language is
        rewritten, so the overview follows the EN / বাং toggle."""
        if session is None or not alerts or not settings.ai_enabled or not user["digest_enabled"]:
            return
        lang = lang or dict(user).get("lang") or "en"
        if appdb.digest_for(user["id"], session, lang):
            return
        if limiter.hit(f"digest:{user['id']}", DIGEST_LIMIT_PER_DAY, 24 * 3600):
            return   # a provider call costs money or quota; a few per day per account is plenty
        from dse_alerts.ai import digest as ai

        try:
            text, provider = ai.write_digest_detailed(settings, session, alerts, [i["symbol"] for i in appdb.items(user["id"])],
                                                      ai.load_rule_evidence(settings), lang=lang)
        except Exception:  # a provider outage must never break a page
            log.exception("AI note failed")
            return
        if text:
            appdb.save_digest(user["id"], session, text, provider, lang)

    def _alert_from_row(row, session: dt.date) -> Alert:
        d = dict(row)
        try:
            ev = json.loads(d.get("evidence_json") or "{}")
        except ValueError:
            ev = {}
        return Alert(symbol=d["symbol"], rule_id=d["rule_id"], date=session, headline=d["headline"], detail=d["detail"],
                     severity=d.get("severity") or "info", evidence=ev)

    # ---- language ---------------------------------------------------------------------

    @app.get("/lang/{code}")
    def set_lang(request: Request, code: str):
        resp = RedirectResponse(_local_path(request.headers.get("referer")), status_code=303)
        resp.set_cookie(LANG_COOKIE, normalize(code), max_age=LANG_MAX_AGE, samesite="lax", httponly=True, secure=is_https(request))
        return resp

    # ---- auth pages -------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if not appdb.users():
            return RedirectResponse("/register", status_code=303)
        return render(request, "login.html", error=None, demo_available=demo_available())

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...)):
        same_origin(request)
        email = email.strip().lower()[:254]
        ip = client_ip(request)
        if limiter.hit("login-ip:" + ip, LOGIN_LIMIT_PER_IP, LOGIN_WINDOW) or limiter.hit("login-email:" + email, LOGIN_LIMIT_PER_EMAIL, LOGIN_WINDOW):
            return render(request, "login.html", status_code=429, error="login.too_many", demo_available=demo_available())
        u = appdb.user(email)
        if not verify_password(u["password_hash"] if u else dummy_hash, password) or not u:
            return render(request, "login.html", error="login.wrong", demo_available=demo_available())
        limiter.clear("login-email:" + email)
        return set_session(request, RedirectResponse("/", status_code=303), u["id"], u["password_hash"])

    @app.post("/demo")
    def demo_login(request: Request):
        same_origin(request)
        u = appdb.user(DEMO_EMAIL) if settings.app_demo else None
        if not u:
            raise HTTPException(status_code=404, detail="demo mode is off")
        return set_session(request, RedirectResponse("/", status_code=303), u["id"], u["password_hash"])

    def registration_open() -> bool:
        return settings.app_registration or appdb.operator_id() is None

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request):
        if not registration_open():
            raise HTTPException(status_code=404, detail="register.closed")
        return render(request, "register.html", error=None, first=not appdb.users())

    @app.post("/register")
    def register(request: Request, email: str = Form(...), password: str = Form(...), alert_to: str = Form("")):
        same_origin(request)
        if not registration_open():
            raise HTTPException(status_code=404, detail="register.closed")
        email = email.strip().lower()
        alert_to = alert_to.strip().lower()
        if len(password) < 8:
            return render(request, "register.html", error="register.short", first=not appdb.users())
        if len(password) > PASSWORD_MAX_LENGTH or not _valid_email(email) or (alert_to and not _valid_email(alert_to)):
            return render(request, "register.html", error="register.invalid", first=not appdb.users())
        if appdb.users() and limiter.hit("register-ip:" + client_ip(request), REGISTER_LIMIT_PER_IP, REGISTER_WINDOW):
            return render(request, "register.html", status_code=429, error="register.too_many", first=False)
        if appdb.user(email) or email == DEMO_EMAIL:   # an address created by the operator is set up with `dse alerts set-password`
            return render(request, "register.html", error="register.exists", first=False)
        pw_hash = hash_password(password)
        uid = appdb.ensure_user(email, pw_hash, alert_to or None)
        appdb.ensure_watchlist(uid)
        first = appdb.operator_id() == uid   # the operator's own sign-up needs no confirmation
        appdb.update_user(uid, lang=lang_of(request), email_confirmed=(alert_to or email) if first else None)
        if not first:
            send_confirmation(uid, alert_to or email)
        return set_session(request, RedirectResponse("/", status_code=303), uid, pw_hash)

    @app.get("/confirm/{token}")
    def confirm_email(request: Request, token: str):
        try:
            data = confirm_tokens.loads(token, max_age=CONFIRM_MAX_AGE)
        except BadSignature:
            raise HTTPException(status_code=400, detail="settings.confirm_bad") from None
        u = appdb.user_by_id(int(data["uid"]))
        if u and (u["alert_email"] or u["email"]) == data["email"]:
            appdb.update_user(u["id"], email_confirmed=data["email"])
        return RedirectResponse("/settings?msg=settings.confirmed", status_code=303)

    @app.post("/confirm/resend")
    def resend_confirmation(request: Request, csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        if limiter.hit(f"confirm:{user['id']}", 3, 3600):
            return RedirectResponse("/settings?msg=settings.too_many", status_code=303)
        ok = send_confirmation(user["id"], user["alert_email"] or user["email"])
        return RedirectResponse("/settings?msg=" + ("settings.confirm_sent" if ok else "settings.confirm_failed"), status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        same_origin(request)
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # ---- overview ---------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        user = current_user(request)
        if not user:
            if not appdb.users():
                return RedirectResponse("/register", status_code=303)
            return render(request, "home.html", active="home", market=market_status(), demo_available=demo_available(),
                          email_on=settings.email_enabled, telegram_on=settings.telegram_enabled,
                          registration=registration_open(), **market_today())
        request.state.user = user
        lang = lang_of(request, user)
        items, rows = watch_rows(user["id"])
        ms = market_status()
        history = appdb.alert_history(user["id"], limit=50)
        today = _localized([a for a in history if ms["last_session"] and a["session_date"] == ms["last_session"].isoformat()], lang)
        val = portfolio.valuation(store, appdb, user["id"])
        ups = sum(1 for r in rows if not _isnan(r["ret_1"]) and r["ret_1"] > 0)
        downs = sum(1 for r in rows if not _isnan(r["ret_1"]) and r["ret_1"] < 0)
        breadth = {"up": ups, "down": downs, "flat": len(rows) - ups - downs, "total": len(rows)}
        if today:  # first visit after a session (or after a language switch) writes the AI note
            raw_today = [a for a in history if a["session_date"] == ms["last_session"].isoformat()]
            write_digest_if_due(user, ms["last_session"], [_alert_from_row(a, ms["last_session"]) for a in raw_today], lang=lang)
        digest = appdb.digest_for(user["id"], ms["last_session"], lang) or appdb.digest_for(user["id"], ms["last_session"])
        mine = sorted({i["symbol"] for i in items} | {h["symbol"] for h in appdb.holdings(user["id"])})
        record_dates = market.upcoming_record_dates(store, mine) if mine else []
        record_scope = "mine" if record_dates else "market"
        if not record_dates:
            record_dates = market.upcoming_record_dates(store)[:8]
        highs = sum(1 for x in today if x.get("severity") == "high")
        notables = sum(1 for x in today if x.get("severity") == "notable")
        last_checked = dict(user).get("last_checked_at")
        added_since = sorted(i["symbol"] for i in items if i.get("added_at") and (not last_checked or i["added_at"] > last_checked))
        custom = {i["symbol"] for i in items if i.get("rules")}
        for r in rows:
            r["custom"] = r["symbol"] in custom
        check = {"at": last_checked, "session": dict(user).get("last_check_session"), "symbols": dict(user).get("last_check_symbols"),
                 "alerts": dict(user).get("last_check_alerts"), "added_since": added_since, "never": not last_checked}
        return render(request, "dashboard.html", active="dashboard", items=items, rows=rows, market=ms, alerts=today, check=check,
                      any_circuit=any(bool(r.get("hit_upper")) or bool(r.get("hit_lower")) for r in rows
                                      if not _isnan(r.get("hit_upper")) or not _isnan(r.get("hit_lower"))),
                      index=market.index_last(store), index_chart={"series": [{"name": "DSEX", "data": market.index_series(store, 130), "area": True, "digits": 0}], "options": {}},
                      total=val["total"] or None, accounts=len(val["accounts"]) if not val["accounts"].empty else 0, breadth=breadth,
                      digest=dict(digest) if digest else None, ai_on=settings.ai_enabled,
                      ai_quiet=bool(today) and highs == 0 and notables < 2,
                      board=market.sector_board(store), movers=market.top_movers(store, 6),
                      record_dates=record_dates, record_scope=record_scope)

    # ---- stock finder and the learn page ----------------------------------------------

    @app.get("/find", response_class=HTMLResponse)
    def find_page(request: Request, user=Depends(require_user)):
        """Filters, sort column and direction, and page all live in the query string, so links,
        the back button and a reload show the same result. A preset fills defaults; anything in
        the query overrides it."""
        qp = request.query_params
        preset = qp.get("preset", "")
        preset = preset if preset in FIND_PRESETS else ""
        base = FIND_PRESETS.get(preset, {})

        def pick(k: str) -> str:
            return qp.get(k, "").strip()[:40] if k in qp else _plain(base.get(k, ""))

        raw = {k: pick(k) for k in FIND_NUMS}
        vals: dict[str, float | None] = {}
        errors: dict[str, str] = {}
        for k in FIND_NUMS:
            if raw[k] == "":
                vals[k] = None
                continue
            try:
                f = float(raw[k])
            except ValueError:
                f = math.nan
            if not math.isfinite(f):          # "nan", "inf" and plain words are all refused
                errors[k], vals[k] = "error.number", None
            else:
                vals[k] = f
        for lo, hi in FIND_PAIRS:
            if vals[lo] is not None and vals[hi] is not None and vals[lo] > vals[hi]:
                errors[lo] = errors[hi] = "error.range_order"
        sectors = market.sectors(store)
        sector = pick("sector")[:60]            # an unknown sector is kept: it matches nothing, and says so
        category = pick("category").upper() if pick("category").upper() in ("A", "B", "N", "Z") else ""
        near = pick("near") if pick("near") in ("low", "high") else ""
        sort = pick("sort") if pick("sort") in market.SCREEN_SORTS else "ret_1"
        direction = pick("dir") if pick("dir") in ("asc", "desc") else market.SCREEN_SORTS[sort][1]
        try:
            page = max(1, int(qp.get("page", "1")))
        except ValueError:
            page = 1
        rows, total = [], 0
        if not errors:
            def run(p: int) -> dict:
                return market.screen(store, sector=sector or None, category=category or None, pe_min=vals["pe_min"], pe_max=vals["pe_max"],
                                     ret20_min=None if vals["ret20_min"] is None else vals["ret20_min"] / 100,
                                     ret20_max=None if vals["ret20_max"] is None else vals["ret20_max"] / 100,
                                     near=near or None, vol_min=vals["vol_min"], price_max=vals["price_max"],
                                     sort=sort, direction=direction, limit=FIND_PAGE, offset=(p - 1) * FIND_PAGE)
            res = run(page)
            total = res["total"]
            last_page = max(1, -(-total // FIND_PAGE))
            if page > last_page:                 # an old link to page 9 of what is now 3 pages
                page = last_page
                res = run(page)
            rows = res["rows"]
        pages = max(1, -(-total // FIND_PAGE))
        state = {"preset": preset, "sector": sector, "category": category, **raw, "near": near, "sort": sort, "dir": direction}

        def find_url(**over) -> str:
            s = {**state, "page": page, **over}
            s = {k: v for k, v in s.items() if v not in ("", None) and not (k == "page" and int(v) == 1)}
            return "/find" + ("?" + urlencode(s) if s else "")

        def sort_url(col: str) -> str:
            nd = ("asc" if direction == "desc" else "desc") if col == sort else market.SCREEN_SORTS[col][1]
            return find_url(sort=col, dir=nd, page=1)

        def aria_sort(col: str) -> str:
            return ("ascending" if direction == "asc" else "descending") if col == sort else "none"

        watched = {i["symbol"] for i in appdb.items(user["id"])} | {h["symbol"] for h in appdb.holdings(user["id"])}
        lang = lang_of(request, user)
        presets = [(pid, t(lang, "find.p_" + pid)) for pid in FIND_PRESETS]
        filtered = any(v for k, v in state.items() if k not in ("sort", "dir", "preset"))
        q = {"sector": sector, "category": category, "near": near, "sort": sort, "dir": direction}
        return render(request, "find.html", status_code=400 if errors else 200, active="find", rows=rows, total=total, page=page,
                      pages=pages, first=(page - 1) * FIND_PAGE + 1 if rows else 0, last=(page - 1) * FIND_PAGE + len(rows),
                      raw=raw, errors=errors, q=q, preset=preset or ("" if filtered else "all"), presets=presets, sectors=sectors,
                      watched=watched, find_url=find_url, sort_url=sort_url, aria_sort=aria_sort, columns=FIND_COLUMNS)

    @app.get("/learn", response_class=HTMLResponse)
    def learn_page(request: Request):
        request.state.user = current_user(request)   # public: no account needed to read it
        return render(request, "learn.html", active="learn", sections=LEARN_SECTIONS, rules=RULES)

    # ---- watchlist actions ------------------------------------------------------------

    @app.post("/watchlist/add")
    def add_symbol(request: Request, symbol: str = Form(...), csrf: str = Form(...), kind: str = Form("watch"), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        kind = "target" if kind == "target" else "watch"
        sym, options = resolve_symbol(symbol)
        if not sym:
            key, args = ("flash.ambiguous", {"options": "; ".join(options)}) if options else ("flash.not_code", {"symbol": symbol.strip()[:40]})
            return flash(request, back_to(request, anchor="add"), key, tone="error", typed=symbol.strip(), **args)
        existing = next((i for i in appdb.items(user["id"]) if i["symbol"] == sym), None)
        wl = appdb.ensure_watchlist(user["id"])
        if existing and existing["kind"] == kind:
            return flash(request, back_to(request, anchor="add"), "flash.already", symbol=sym)
        if existing and existing["kind"] != "owned":
            appdb.set_item_kind(wl, sym, kind)
            return flash(request, back_to(request, anchor="add"), "flash.now_" + kind, symbol=sym)
        appdb.add_item(wl, sym, kind=kind)
        return flash(request, back_to(request, anchor="add"), "flash.added_" + kind, symbol=sym)

    @app.post("/watchlist/remove")
    def remove_symbol(request: Request, symbol: str = Form(...), csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        sym = symbol.strip().upper()
        item = next((i for i in appdb.items(user["id"]) if i["symbol"] == sym and i["kind"] != "owned"), None)
        if not item:
            return flash(request, back_to(request, anchor="watched"), "flash.not_on_list", tone="error", symbol=sym)
        appdb.remove_item(appdb.ensure_watchlist(user["id"]), sym)
        undo = {"symbol": sym, "kind": item["kind"], "rules": json.dumps(item["rules"] or {}), "note": item["note"] or ""}
        # removing from the stock's own page leads back to the overview; elsewhere the page stays
        on_own_page = f"/symbol/{sym}" in (request.headers.get("referer") or "")
        resp = RedirectResponse("/#watched", status_code=303) if on_own_page else back_to(request, anchor="watched")
        return flash(request, resp, "flash.removed", undo=undo, symbol=sym)

    @app.post("/watchlist/restore")
    def restore_symbol(request: Request, symbol: str = Form(...), kind: str = Form("watch"), rules: str = Form("{}"), note: str = Form(""),
                       csrf: str = Form(...), user=Depends(require_user)):
        """Undo of a removal: the symbol comes back with the rules and note it had."""
        check_csrf(request, csrf)
        writable(request, user)
        sym, _ = resolve_symbol(symbol)
        if not sym:
            return flash(request, back_to(request, anchor="watched"), "flash.not_code", tone="error", symbol=symbol.strip()[:40])
        try:
            cfg = json.loads(rules or "{}")
        except ValueError:
            cfg = {}
        appdb.add_item(appdb.ensure_watchlist(user["id"]), sym, cfg if isinstance(cfg, dict) else {}, note.strip()[:500] or None,
                       kind="target" if kind == "target" else "watch")
        return flash(request, back_to(request, anchor="watched"), "flash.restored", symbol=sym)

    @app.post("/watchlist/kind")
    def set_kind(request: Request, symbol: str = Form(...), kind: str = Form(...), csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        if kind not in ("watch", "target"):
            return flash(request, back_to(request), "error.bad_input", tone="error")
        sym, _ = resolve_symbol(symbol)
        if not sym:
            return flash(request, back_to(request), "flash.not_code", tone="error", symbol=symbol.strip()[:40])
        wl = appdb.ensure_watchlist(user["id"])
        if sym not in {i["symbol"] for i in appdb.items(user["id"])}:
            appdb.add_item(wl, sym, kind=kind)
        else:
            appdb.set_item_kind(wl, sym, kind)
        return flash(request, back_to(request, default=f"/symbol/{sym}", anchor="watched"), "flash.now_" + kind, symbol=sym)

    # ---- portfolio & targets ----------------------------------------------------------

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio_page(request: Request, user=Depends(require_user)):
        val = portfolio.valuation(store, appdb, user["id"])
        holdings = appdb.holdings(user["id"])
        account_names = sorted({h["account"] for h in holdings})
        if not val["total"]:
            return render(request, "portfolio.html", active="portfolio", total=None, as_of=None, holdings=holdings, account_names=account_names)
        pos = val["positions"]
        sparks = market.latest_rows(store, sorted(pos["symbol"].unique()))[["symbol", "spark", "spark_dir"]]
        pos = pos.merge(sparks, on="symbol", how="left")
        groups = []
        for name, sub in pos.groupby("account", sort=True):
            groups.append({"name": name, "rows": _records(sub.sort_values("value", ascending=False))})
        hist = portfolio.value_history(store, appdb, user["id"], 250)
        ids = {(h["account"], h["symbol"]): h["account_id"] for h in holdings}
        for g in groups:
            for r in g["rows"]:
                r["account_id"] = ids.get((g["name"], r["symbol"]))
        return render(request, "portfolio.html", active="portfolio", total=val["total"], as_of=val["as_of"],
                      accounts=_records(val["accounts"]), positions_by_account=groups, holdings=holdings, account_names=account_names,
                      sectors=_records(portfolio.by_group(pos, "sector")), history_chart=_named(market.value_series(hist), lang_of(request, user)))

    @app.get("/portfolio/template.csv")
    def portfolio_template(user=Depends(require_user)):
        body = ",".join(portfolio.HOLDINGS_COLUMNS) + "\nBO 1234,GP,100,312.50,318.0,2026-06-30,,\nBO 1234,SQURPHARMA,50,210.00,,,,\n"
        return PlainTextResponse(body, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=holdings.csv"})

    @app.post("/portfolio/add")
    def portfolio_add(request: Request, csrf: str = Form(...), account: str = Form(""), symbol: str = Form(...), quantity: str = Form(...),
                      avg_cost: str = Form(...), statement_price: str = Form(""), statement_date: str = Form(""), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        sym, options = resolve_symbol(symbol)
        if not sym:
            key, args = ("flash.ambiguous", {"options": "; ".join(options)}) if options else ("flash.not_code", {"symbol": symbol.strip()[:40]})
            return flash(request, back_to(request, "/portfolio", "holdings"), key, tone="error", typed=symbol.strip(), **args)

        def num(v: str, allow_zero: bool = False) -> float | None:
            v = v.strip().replace(",", "")
            if not v:
                return None
            x = float(v)
            if x < 0 or (x == 0 and not allow_zero):
                raise ValueError(v)
            return x

        try:
            qty, cost = num(quantity), num(avg_cost, allow_zero=True)
            sp = num(statement_price)
            if qty is None or cost is None:
                raise ValueError("required")
            sd = dt.date.fromisoformat(statement_date.strip()).isoformat() if statement_date.strip() else None
        except ValueError:
            return flash(request, back_to(request, "/portfolio", "holdings"), "flash.holding_numbers", tone="error", typed=sym)
        acc = appdb.ensure_account(user["id"], account.strip()[:60] or "Default")
        appdb.upsert_holding(acc, sym, qty, cost, sp, sd)
        return flash(request, back_to(request, "/portfolio", "holdings"), "flash.holding_saved", symbol=sym, qty=f"{qty:,.0f}")

    @app.post("/portfolio/upload")
    async def portfolio_upload(request: Request, csrf: str = Form(...), file: UploadFile = File(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        raw = await file.read()
        if len(raw) > 512 * 1024:
            return flash(request, back_to(request, "/portfolio", "holdings"), "flash.csv_big", tone="error")
        try:
            df = pd.read_csv(io.BytesIO(raw), dtype=str)
            result = portfolio.import_frame(appdb, user["id"], df, known=set(store.instruments()["symbol"]))
        except (ValueError, pd.errors.ParserError, UnicodeDecodeError) as e:
            return flash(request, back_to(request, "/portfolio", "holdings"), "flash.csv_bad", tone="error", why=str(e)[:160])
        return flash(request, back_to(request, "/portfolio", "holdings"), "flash.csv_ok", rows=result["rows"], accounts=len(result["accounts"]))

    def confirm_page(request: Request, action: str, message: str, fields: dict, cancel: str):
        """The no-JavaScript path for a destructive form: nothing is deleted until this page's
        own button is pressed. With JavaScript the browser dialog adds confirmed=1 instead."""
        return render(request, "confirm.html", message=message, action=action, fields=fields,
                      cancel=_local_path(request.headers.get("referer")) if request.headers.get("referer") else cancel)

    @app.post("/portfolio/remove")
    def portfolio_remove(request: Request, csrf: str = Form(...), account_id: int = Form(...), symbol: str = Form(...),
                         confirmed: str = Form(""), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        if not any(a["id"] == account_id for a in appdb.accounts(user["id"])):
            return flash(request, back_to(request, "/portfolio", "holdings"), "error.bad_input", tone="error")
        if confirmed != "1":
            sym = symbol.strip().upper()
            return confirm_page(request, "/portfolio/remove", t(lang_of(request, user), "portfolio.remove_confirm", symbol=sym),
                                {"account_id": account_id, "symbol": sym}, "/portfolio#holdings")
        appdb.remove_holding(account_id, symbol.strip().upper())
        return flash(request, back_to(request, "/portfolio", "holdings"), "flash.holding_removed", symbol=symbol.strip().upper())

    @app.get("/targets", response_class=HTMLResponse)
    def targets_page(request: Request, user=Depends(require_user)):
        syms = [i["symbol"] for i in appdb.items(user["id"]) if i["kind"] == "target"]
        rows = evidence.target_rows(store, syms)
        for r in rows:
            closes = r.pop("closes", [])
            r["spark"], r["spark_dir"] = market.spark_path(closes), market.spark_dir(closes)
        return render(request, "targets.html", active="targets", rows=rows)

    @app.post("/accounts/rename")
    def rename_account(request: Request, account_id: int = Form(...), name: str = Form(...), csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        if any(a["id"] == account_id for a in appdb.accounts(user["id"])) and name.strip():
            appdb.rename_account(account_id, name.strip())
        return RedirectResponse("/settings", status_code=303)

    @app.post("/accounts/delete")
    def delete_account(request: Request, account_id: int = Form(...), csrf: str = Form(...), confirmed: str = Form(""),
                       user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        acc = next((a for a in appdb.accounts(user["id"]) if a["id"] == account_id), None)
        resp = RedirectResponse("/settings", status_code=303)
        if acc and confirmed != "1":
            n = sum(1 for h in appdb.holdings(user["id"]) if h["account_id"] == account_id)
            return confirm_page(request, "/accounts/delete", t(lang_of(request, user), "settings.remove_account_confirm", name=acc["name"], n=n),
                                {"account_id": account_id}, "/settings")
        if acc:
            n = appdb.delete_account(account_id)
            flash(request, resp, "flash.account_removed", name=acc["name"], n=n)
        return resp

    # ---- symbol -----------------------------------------------------------------------

    @app.get("/symbol/{symbol}", response_class=HTMLResponse)
    def symbol_page(request: Request, symbol: str, user=Depends(require_user)):
        return render(request, "symbol.html", **symbol_ctx(request, user, symbol.upper()))

    def symbol_ctx(request: Request, user, sym: str) -> dict:
        lang = lang_of(request, user)
        with store.connect(read_only=True) as con:
            inst = con.execute("SELECT * FROM instruments WHERE symbol = ?", [sym]).df()
            if inst.empty:
                raise HTTPException(status_code=404, detail="error.unknown_symbol")
            last = con.execute("SELECT date, close, ret_1, volume, vol_ratio_20 FROM features WHERE symbol = ? ORDER BY date DESC LIMIT 1", [sym]).df()
            stats = con.execute("SELECT * FROM symbol_stats WHERE symbol = ?", [sym]).df() \
                if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='symbol_stats'").fetchone()[0] else pd.DataFrame()
            fund = con.execute("SELECT * FROM fundamentals WHERE symbol = ? ORDER BY as_of DESC LIMIT 1", [sym]).df()
            news = con.execute("SELECT * FROM news WHERE symbol = ? ORDER BY date DESC LIMIT 8", [sym]).df()
            divs = con.execute("SELECT * FROM dividends WHERE symbol = ? ORDER BY declared_on DESC LIMIT 5", [sym]).df()
        inst_d = _records(inst)[0]
        stats_s = stats.iloc[0] if not stats.empty else None
        fund_s = fund.iloc[0] if not fund.empty else None
        ev = evidence.evidence(store, sym, stats_s, fund_s, inst_d.get("sector"), news, divs)
        facts = [evidence.fact_text(f, lang) for f in ev["facts"]]
        item = next((i for i in appdb.items(user["id"]) if i["symbol"] == sym), None)
        held = [h for h in appdb.holdings(user["id"]) if h["symbol"] == sym]
        holding = None
        if held:
            q = sum(h["quantity"] for h in held)
            holding = {"quantity": q, "avg_cost": sum(h["quantity"] * h["avg_cost"] for h in held) / q if q else None}
        return {"active": "", "symbol": sym, "inst": inst_d, "last": _records(last)[0] if not last.empty else {},
                    "ev": ev, "facts": facts, "stats": _records(stats)[0] if not stats.empty else None, "fund": _records(fund)[0] if not fund.empty else None,
                    "news": _records(news), "divs": _records(divs), "item": item, "holding": holding, "rules": RULES, "opt_in": OPT_IN,
                    "price_chart": market.ohlc(store, sym, "1y"), "rel_chart": market.relative_series(store, sym, 250),
                    "alerts": _localized([a for a in appdb.alert_history(user["id"], 500) if a["symbol"] == sym][:20], lang),
                    "changed": _changed_rules(item["rules"] if item else {}, lang)}

    def _changed_rules(cfg: dict, lang: str) -> list[str]:
        """'Big drop: your own threshold, in %: 8', 'Volume spike: off' for the rules summary."""
        out = []
        for r in RULES:
            v = cfg.get(r.id)
            if v is False:
                out.append(f"{t(lang, 'rule.' + r.id + '.name')}: {t(lang, 'settings.off')}")
            elif isinstance(v, dict) and v:
                parts = [f"{t(lang, 'param.' + k)} {round(x * 100, 2) if k == 'pct' else x}" for k, x in v.items()]
                out.append(f"{t(lang, 'rule.' + r.id + '.name')}: {', '.join(parts)}")
        return out

    @app.post("/symbol/{symbol}/rules")
    async def save_rules(request: Request, symbol: str, user=Depends(require_user)):
        form = await request.form()
        check_csrf(request, str(form.get("csrf", "")))
        writable(request, user)
        sym = symbol.upper()
        if sym not in set(store.instruments()["symbol"]):
            raise HTTPException(status_code=404, detail=t(lang_of(request, user), "error.not_code", symbol=sym))
        cfg: dict = {}
        errors = _parse_rule_form(form, cfg)
        if errors:
            # the page comes back with every typed value in place and the message beside the field
            typed = {k: str(v) for k, v in form.items() if k != "csrf"}
            return render(request, "symbol.html", status_code=400, form=typed, errors=errors, open_rules=True,
                          **symbol_ctx(request, user, sym))
        note = str(form.get("note", "")).strip()[:500] or None
        appdb.add_item(appdb.ensure_watchlist(user["id"]), sym, cfg, note)
        return flash(request, RedirectResponse(f"/symbol/{sym}#rules", status_code=303), "flash.rules_saved", symbol=sym)

    def _parse_rule_form(form, cfg: dict) -> dict[str, str]:
        """Fill ``cfg`` from the form; return {field: error key} for values that are not numbers."""
        errors: dict[str, str] = {}
        for r in RULES:
            enabled = form.get(f"on_{r.id}") == "on"
            params = {}
            for k, default in r.params.items():
                field = f"{r.id}__{k}"
                raw = str(form.get(field, "")).strip()
                if raw == "":
                    continue
                try:
                    if k == "pct":
                        params[k] = float(raw) / 100.0
                    elif isinstance(default, bool):
                        params[k] = raw.lower() in ("1", "true", "on", "yes")
                    elif isinstance(default, int) and not isinstance(default, bool):
                        params[k] = int(raw)
                    elif isinstance(default, float) or k in ("min", "max"):
                        params[k] = float(raw)
                    elif k == "direction":
                        if raw.lower() not in ("up", "down", "both"):
                            raise ValueError(raw)
                        params[k] = raw.lower()
                    else:
                        params[k] = raw
                    if isinstance(params[k], (int, float)) and not isinstance(params[k], bool) and params[k] <= 0:
                        errors[field] = "error.positive"          # a 0% move or 0 sessions would fire on everything, or never
                except ValueError:
                    errors[field] = "error.number"
            if not enabled:
                if r.id not in OPT_IN:
                    cfg[r.id] = False
            elif params or r.id in OPT_IN:
                cfg[r.id] = params
        return errors

    # ---- JSON for charts --------------------------------------------------------------

    @app.get("/api/ohlc/{symbol}")
    def api_ohlc(symbol: str, range: str = "1y", user=Depends(require_user)):
        if range not in market.RANGE_SESSIONS:
            return JSONResponse({"error": "unknown range"}, status_code=400)
        return JSONResponse({**market.ohlc(store, symbol.upper(), range), "range": range})

    @app.get("/api/index")
    def api_index(range: str = "6m", user=Depends(require_user)):
        return JSONResponse({"series": [{"name": "DSEX", "data": market.index_series(store, market.RANGE_SESSIONS.get(range, 130))}]})

    @app.get("/instruments.json")
    def instruments_json(user=Depends(require_user)):
        inst = store.instruments()
        inst = inst[inst["tradeable"].fillna(False).astype(bool)]
        return [{"symbol": r.symbol, "name": "" if r.name is None or pd.isna(r.name) else str(r.name)} for r in inst.itertuples()]

    # ---- alerts, scorecard, rules, settings -------------------------------------------

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts_page(request: Request, user=Depends(require_user)):
        lang = lang_of(request, user)
        return render(request, "alerts.html", active="alerts", alerts=_localized(appdb.alert_history(user["id"], 300), lang),
                      digests={d["session_date"]: dict(d) for d in appdb.digests(user["id"], 60)})

    @app.post("/alerts/check")
    def check_now(request: Request, csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        lang = lang_of(request, user)
        resp = RedirectResponse("/#alerts", status_code=303)
        if limiter.hit(f"check:{user['id']}", CHECK_LIMIT, CHECK_WINDOW):
            return flash(request, resp, "flash.check_recent", tone="error")   # the scheduler runs it daily anyway
        session, results = engine.run(store, appdb, record=True, user_ids=[user["id"]])
        if not results or session is None:
            return flash(request, resp, "flash.check_nothing", tone="error")
        r = results[0]
        write_digest_if_due(user, session, r.alerts, lang=lang)
        return flash(request, resp, "flash.checked", n=len(r.symbols), alerts=len(r.alerts), new=len(r.new_alerts), date=fmt_date(lang, session))

    @app.get("/scorecard", response_class=HTMLResponse)
    def scorecard_page(request: Request, user=Depends(require_user)):
        from dse_alerts import scorecard as sc

        lang = lang_of(request, user)
        df = sc.outcomes(store, appdb, user["id"])
        expectation = sc.load_expectation(settings)
        agg = sc.by_rule(df, expectation)
        chart = [{"id": r.id, "share": v["fwd20_positive_share"], "per_year": v.get("per_250_sessions"), "median": v.get("fwd20_median")}
                 for r in RULES if (v := expectation.get(r.id)) and v.get("fwd20_positive_share") is not None]
        by_rule = _records(agg)
        base = _base_rate(expectation)
        # the axis hugs the dots: a 25-75% track left half the width empty
        pts = [100 * c["share"] for c in chart] + ([base["positive"]] if base else [])
        lo = int(min(pts) - 3) // 5 * 5 if pts else 25
        hi = -(-int(max(pts) + 3) // 5) * 5 if pts else 75
        axis = {"lo": lo, "hi": max(hi, lo + 10)}
        return render(request, "scorecard.html", active="scorecard", rows=_localized(_records(df.head(200)), lang), by_rule=by_rule,
                      chart=chart, axis=axis, any_enough=any(r.get("enough") for r in by_rule), base=base,
                      pending=int((df["status"] == "pending").sum()) if not df.empty else 0, total=len(df),
                      episodes=int(df["episode_start"].sum()) if not df.empty else 0, min_episodes=sc.MIN_EPISODES)

    @app.get("/rules", response_class=HTMLResponse)
    def rules_page(request: Request, user=Depends(require_user)):
        from dse_alerts.analysis.calibrate import load_calibration, rule_evidence

        study = load_calibration(settings)
        cal = rule_evidence(settings)
        return render(request, "rules.html", active="rules", rules=RULES, opt_in=OPT_IN, cal=cal, base=_base_rate(cal),
                      study={"first": str(study.get("first", ""))[:10], "last": str(study.get("last", ""))[:10],
                             "symbols": study.get("symbols"), "sessions": study.get("sessions")} if study else None)

    def _base_rate(cal: dict) -> dict | None:
        """What any session looked like 20 sessions later, the comparison for every "rose afterwards"."""
        for v in cal.values():
            if v.get("base_fwd20_positive_share") is not None:
                return {"positive": round(100 * v["base_fwd20_positive_share"]), "median": v.get("base_fwd20_median")}
        return None

    def settings_ctx(user):
        from dse_alerts.notify import deliver

        admin = user["id"] == appdb.operator_id()
        return {"active": "settings", "runs": appdb.last_runs(10) if admin else [], "market": market_status() if admin else None, "admin": admin,
                "accounts": appdb.accounts(user["id"]), "email_on": settings.email_enabled, "telegram_on": settings.telegram_enabled,
                "holding_counts": Counter(h["account_id"] for h in appdb.holdings(user["id"])),
                "bot": deliver.telegram_bot_name(settings) if settings.telegram_enabled and user["telegram_code"] else ""}

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, user=Depends(require_user), msg: str = ""):
        return render(request, "settings.html", msg=msg if msg.startswith("settings.") and len(msg) < 40 else None, **settings_ctx(user))

    @app.post("/settings")
    def save_settings(request: Request, csrf: str = Form(...), alert_to: str = Form(""), digest: str = Form(""),
                      password: str = Form(""), telegram_chat_id: str = Form(""), lang: str = Form(""), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        chat = telegram_chat_id.strip()
        fields = {"alert_email": alert_to.strip().lower() or user["email"], "digest_enabled": 1 if digest == "on" else 0,
                  "telegram_chat_id": chat or None, "lang": normalize(lang) if lang else dict(user).get("lang") or "en"}
        if not _valid_email(fields["alert_email"]) or (chat and not TELEGRAM_ID.fullmatch(chat)):
            return render(request, "settings.html", msg="settings.invalid", **settings_ctx(user))
        if password:
            if len(password) < 8 or len(password) > PASSWORD_MAX_LENGTH:
                return render(request, "settings.html", msg="settings.password_short", **settings_ctx(user))
            fields["password_hash"] = hash_password(password)
        msg = "settings.saved"
        # a new destination is confirmed through an emailed link before anything is sent there
        if fields["alert_email"] != user["email_confirmed"] and send_confirmation(user["id"], fields["alert_email"]):
            msg = "settings.confirm_sent"
        if not chat:
            fields["telegram_confirmed"] = fields["telegram_code"] = None
        elif chat != user["telegram_confirmed"]:
            fields["telegram_confirmed"] = None
            fields["telegram_code"] = user["telegram_code"] if user["telegram_code"] and chat == user["telegram_chat_id"] else secrets.token_hex(3).upper()
        appdb.update_user(user["id"], **fields)
        resp = RedirectResponse("/settings?msg=" + msg, status_code=303)
        resp.set_cookie(LANG_COOKIE, fields["lang"], max_age=LANG_MAX_AGE, samesite="lax", httponly=True, secure=is_https(request))
        if password:   # this browser stays signed in; every other session is now invalid
            set_session(request, resp, user["id"], fields["password_hash"])
        return resp

    @app.post("/telegram/verify")
    def telegram_verify(request: Request, csrf: str = Form(...), user=Depends(require_user)):
        """The user sent the code shown on the settings page to the bot; match it to the chat id."""
        from dse_alerts.notify import deliver

        check_csrf(request, csrf)
        writable(request, user)
        code, chat = user["telegram_code"], user["telegram_chat_id"]
        if not (code and chat and settings.telegram_enabled) or limiter.hit(f"tg:{user['id']}", 10, 3600):
            return RedirectResponse("/settings?msg=settings.telegram_not_found", status_code=303)
        if any(m["chat_id"] == chat and code in m["text"].upper() for m in deliver.telegram_messages(settings)):
            appdb.update_user(user["id"], telegram_confirmed=chat, telegram_code=None)
            return RedirectResponse("/settings?msg=settings.telegram_ok", status_code=303)
        return RedirectResponse("/settings?msg=settings.telegram_not_found", status_code=303)

    @app.exception_handler(StarletteHTTPException)   # the router's own 404/405 are Starlette's class
    async def _redirects(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        request.state.user = current_user(request)
        lang = lang_of(request, request.state.user)
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        if exc.status_code in (404, 405) and detail in ("Not Found", "Method Not Allowed"):
            detail = f"error.{exc.status_code}"
        detail = t(lang, detail)   # catalog keys translate; plain text passes through
        return render(request, "error.html", status_code=exc.status_code, status=exc.status_code, detail=detail)

    return app
