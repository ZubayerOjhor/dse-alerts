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
import json
import logging
import re
import secrets
from email.utils import parseaddr
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from dse_alerts import engine, evidence, market, portfolio
from dse_alerts.appdb import DEMO_EMAIL, AppDB, is_demo
from dse_alerts.config import Settings, get_settings
from dse_alerts.data.store import MarketStore
from dse_alerts.i18n import LANGS, normalize, t
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
    "fallen": {"ret20_max": -15.0, "sort": "ret_20"},
    "risen": {"ret20_min": 15.0, "sort": "ret_20"},
}
FIND_LIMIT = 150

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
    store = store or MarketStore(settings.market_db)
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
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["pct"] = _pct
    templates.env.filters["num"] = _num

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

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        u = getattr(request.state, "user", None)
        lang = lang_of(request, u)
        return templates.TemplateResponse(request, name, {"user": u, "csrf": getattr(request.state, "csrf", ""),
                                                          "settings": settings, "now": dt.datetime.now(),
                                                          "lang": lang, "langs": LANGS, "t": partial(t, lang),
                                                          "demo": is_demo(u), **ctx},
                                          status_code=status_code)

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

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request):
        return render(request, "register.html", error=None, first=not appdb.users())

    @app.post("/register")
    def register(request: Request, email: str = Form(...), password: str = Form(...), alert_to: str = Form("")):
        same_origin(request)
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
        first = len(appdb.users()) == 1   # the operator's own sign-up needs no confirmation
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
            return render(request, "home.html", active="home", market=market_status(), demo_available=demo_available(), **market_today())
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
        return render(request, "dashboard.html", active="dashboard", items=items, rows=rows, market=ms, alerts=today,
                      index=market.index_last(store), index_chart={"series": [{"name": "DSEX", "data": market.index_series(store, 130), "area": True, "digits": 0}], "options": {}},
                      total=val["total"] or None, accounts=len(val["accounts"]) if not val["accounts"].empty else 0, breadth=breadth,
                      digest=dict(digest) if digest else None, ai_on=settings.ai_enabled,
                      board=market.sector_board(store), movers=market.top_movers(store, 6),
                      record_dates=record_dates, record_scope=record_scope)

    # ---- stock finder and the learn page ----------------------------------------------

    @app.get("/find", response_class=HTMLResponse)
    def find_page(request: Request, user=Depends(require_user), preset: str = "", sector: str = "", category: str = "",
                  pe_min: str = "", pe_max: str = "", ret20_min: str = "", ret20_max: str = "", vol_min: str = "",
                  price_max: str = "", near: str = "", sort: str = "ret_1"):
        lang = lang_of(request, user)

        def num(v: str):
            try:
                return float(v) if v.strip() else None
            except ValueError:
                return None

        q = {"sector": sector or None, "category": category.upper() or None, "pe_min": num(pe_min), "pe_max": num(pe_max),
             "ret20_min": num(ret20_min), "ret20_max": num(ret20_max), "vol_min": num(vol_min), "price_max": num(price_max),
             "near": near if near in ("low", "high") else None, "sort": sort if sort in market.SCREEN_SORTS else "ret_1"}
        if preset in FIND_PRESETS:
            q.update(FIND_PRESETS[preset])
        rows = market.screen(store, sector=q["sector"], category=q["category"], pe_min=q["pe_min"], pe_max=q["pe_max"],
                             ret20_min=None if q["ret20_min"] is None else q["ret20_min"] / 100,
                             ret20_max=None if q["ret20_max"] is None else q["ret20_max"] / 100,
                             near=q["near"], vol_min=q["vol_min"], price_max=q["price_max"], sort=q["sort"], limit=FIND_LIMIT)
        watched = {i["symbol"] for i in appdb.items(user["id"])} | {h["symbol"] for h in appdb.holdings(user["id"])}
        presets = [(pid, t(lang, "find.p_" + pid)) for pid in FIND_PRESETS]
        return render(request, "find.html", active="find", rows=rows, q=q, preset=preset if preset in FIND_PRESETS else ("" if any(
            v for k, v in q.items() if k != "sort") else "all"), presets=presets, sectors=market.sectors(store), watched=watched, limit=FIND_LIMIT)

    @app.get("/learn", response_class=HTMLResponse)
    def learn_page(request: Request):
        request.state.user = current_user(request)   # public: no account needed to read it
        return render(request, "learn.html", active="learn", sections=LEARN_SECTIONS, rules=RULES)

    # ---- watchlist actions ------------------------------------------------------------

    @app.post("/watchlist/add")
    def add_symbol(request: Request, symbol: str = Form(...), csrf: str = Form(...), kind: str = Form("watch"), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        sym = symbol.strip().upper()
        if sym not in set(store.instruments()["symbol"]):
            raise HTTPException(status_code=404, detail=t(lang_of(request, user), "error.not_code", symbol=sym))
        appdb.add_item(appdb.ensure_watchlist(user["id"]), sym, kind="target" if kind == "target" else "watch")
        return RedirectResponse(_local_path(request.headers.get("referer")), status_code=303)

    @app.post("/watchlist/remove")
    def remove_symbol(request: Request, symbol: str = Form(...), csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        appdb.remove_item(appdb.ensure_watchlist(user["id"]), symbol.strip().upper())
        return RedirectResponse("/", status_code=303)

    @app.post("/watchlist/kind")
    def set_kind(request: Request, symbol: str = Form(...), kind: str = Form(...), csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        sym = symbol.strip().upper()
        if kind not in ("watch", "target"):
            raise HTTPException(status_code=400, detail="error.bad_input")
        if sym not in set(store.instruments()["symbol"]):
            raise HTTPException(status_code=404, detail=t(lang_of(request, user), "error.not_code", symbol=sym))
        wl = appdb.ensure_watchlist(user["id"])
        if sym not in {i["symbol"] for i in appdb.items(user["id"])}:
            appdb.add_item(wl, sym, kind=kind)
        else:
            appdb.set_item_kind(wl, sym, "target" if kind == "target" else "watch")
        return RedirectResponse(request.headers.get("referer") or f"/symbol/{sym}", status_code=303)

    # ---- portfolio & targets ----------------------------------------------------------

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio_page(request: Request, user=Depends(require_user)):
        val = portfolio.valuation(store, appdb, user["id"])
        if not val["total"]:
            return render(request, "portfolio.html", active="portfolio", total=None, as_of=None)
        pos = val["positions"]
        sparks = market.latest_rows(store, sorted(pos["symbol"].unique()))[["symbol", "spark"]]
        pos = pos.merge(sparks, on="symbol", how="left")
        groups = []
        for name, sub in pos.groupby("account", sort=True):
            groups.append({"name": name, "rows": _records(sub.sort_values("value", ascending=False))})
        hist = portfolio.value_history(store, appdb, user["id"], 250)
        return render(request, "portfolio.html", active="portfolio", total=val["total"], as_of=val["as_of"],
                      accounts=_records(val["accounts"]), positions_by_account=groups,
                      sectors=_records(portfolio.by_group(pos, "sector")), history_chart=market.value_series(hist))

    @app.get("/targets", response_class=HTMLResponse)
    def targets_page(request: Request, user=Depends(require_user)):
        syms = [i["symbol"] for i in appdb.items(user["id"]) if i["kind"] == "target"]
        rows = evidence.target_rows(store, syms)
        for r in rows:
            r["spark"] = market.spark_path(r.pop("closes", []))
        return render(request, "targets.html", active="targets", rows=rows)

    @app.post("/accounts/rename")
    def rename_account(request: Request, account_id: int = Form(...), name: str = Form(...), csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        if any(a["id"] == account_id for a in appdb.accounts(user["id"])) and name.strip():
            appdb.rename_account(account_id, name.strip())
        return RedirectResponse("/settings", status_code=303)

    # ---- symbol -----------------------------------------------------------------------

    @app.get("/symbol/{symbol}", response_class=HTMLResponse)
    def symbol_page(request: Request, symbol: str, user=Depends(require_user)):
        lang = lang_of(request, user)
        sym = symbol.upper()
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
        return render(request, "symbol.html", active="", symbol=sym, inst=inst_d, last=_records(last)[0] if not last.empty else {},
                      ev=ev, facts=facts, stats=_records(stats)[0] if not stats.empty else None, fund=_records(fund)[0] if not fund.empty else None,
                      news=_records(news), divs=_records(divs), item=item, holding=holding, rules=RULES, opt_in=OPT_IN,
                      price_chart=market.ohlc(store, sym, "1y"), rel_chart=market.relative_series(store, sym, 250),
                      alerts=_localized([a for a in appdb.alert_history(user["id"], 500) if a["symbol"] == sym][:20], lang))

    @app.post("/symbol/{symbol}/rules")
    async def save_rules(request: Request, symbol: str, user=Depends(require_user)):
        form = await request.form()
        check_csrf(request, str(form.get("csrf", "")))
        writable(request, user)
        sym = symbol.upper()
        if sym not in set(store.instruments()["symbol"]):
            raise HTTPException(status_code=404, detail=t(lang_of(request, user), "error.not_code", symbol=sym))
        cfg: dict = {}
        try:
            _parse_rule_form(form, cfg)
        except ValueError:
            raise HTTPException(status_code=400, detail="error.bad_input") from None
        note = str(form.get("note", "")).strip()[:500] or None
        appdb.add_item(appdb.ensure_watchlist(user["id"]), sym, cfg, note)
        return RedirectResponse(f"/symbol/{sym}", status_code=303)

    def _parse_rule_form(form, cfg: dict) -> None:
        for r in RULES:
            enabled = form.get(f"on_{r.id}") == "on"
            params = {}
            for k, default in r.params.items():
                raw = str(form.get(f"{r.id}__{k}", "")).strip()
                if raw == "":
                    continue
                if k == "pct":
                    params[k] = float(raw) / 100.0
                elif isinstance(default, bool):
                    params[k] = raw.lower() in ("1", "true", "on", "yes")
                elif isinstance(default, int) and not isinstance(default, bool):
                    params[k] = int(raw)
                elif isinstance(default, float):
                    params[k] = float(raw)
                else:
                    try:
                        params[k] = float(raw) if k in ("min", "max") else raw
                    except ValueError:
                        params[k] = raw
            if not enabled:
                if r.id not in OPT_IN:
                    cfg[r.id] = False
            elif params or r.id in OPT_IN:
                cfg[r.id] = params

    # ---- JSON for charts --------------------------------------------------------------

    @app.get("/api/ohlc/{symbol}")
    def api_ohlc(symbol: str, range: str = "1y", user=Depends(require_user)):
        return JSONResponse(market.ohlc(store, symbol.upper(), range if range in market.RANGE_SESSIONS else "1y"))

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
        if limiter.hit(f"check:{user['id']}", CHECK_LIMIT, CHECK_WINDOW):
            return RedirectResponse("/", status_code=303)   # the scheduler runs it daily anyway
        session, results = engine.run(store, appdb, record=True, user_ids=[user["id"]])
        if results:
            write_digest_if_due(user, session, results[0].alerts, lang=lang_of(request, user))
        return RedirectResponse("/", status_code=303)

    @app.get("/scorecard", response_class=HTMLResponse)
    def scorecard_page(request: Request, user=Depends(require_user)):
        from dse_alerts import scorecard as sc

        lang = lang_of(request, user)
        df = sc.outcomes(store, appdb, user["id"])
        agg = sc.by_rule(df, sc.load_expectation(settings))
        return render(request, "scorecard.html", active="scorecard", rows=_localized(_records(df.head(200)), lang), by_rule=_records(agg),
                      pending=int((df["status"] == "pending").sum()) if not df.empty else 0, total=len(df))

    @app.get("/rules", response_class=HTMLResponse)
    def rules_page(request: Request, user=Depends(require_user)):
        cal = {}
        p = settings.dse_data_dir / "calibration.json"
        if p.exists():
            try:
                cal = {e["event"]: e for e in json.loads(p.read_text(encoding="utf-8")).get("event_stats", [])}
            except (ValueError, KeyError):
                cal = {}
        return render(request, "rules.html", active="rules", rules=RULES, opt_in=OPT_IN, cal=cal)

    def settings_ctx(user):
        from dse_alerts.notify import deliver

        admin = user["id"] == 1
        return {"active": "settings", "runs": appdb.last_runs(10) if admin else [], "market": market_status(), "admin": admin,
                "accounts": appdb.accounts(user["id"]), "email_on": settings.email_enabled,
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

    @app.exception_handler(HTTPException)
    async def _redirects(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        request.state.user = current_user(request)
        lang = lang_of(request, request.state.user)
        detail = t(lang, exc.detail) if isinstance(exc.detail, str) else exc.detail   # catalog keys translate; plain text passes through
        return render(request, "error.html", status_code=exc.status_code, status=exc.status_code, detail=detail)

    return app
