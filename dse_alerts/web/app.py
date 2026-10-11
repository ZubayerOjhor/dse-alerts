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
from functools import partial
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dse_alerts import engine, evidence, market, portfolio
from dse_alerts.appdb import DEMO_EMAIL, AppDB, is_demo
from dse_alerts.config import Settings, get_settings
from dse_alerts.data.store import MarketStore
from dse_alerts.i18n import LANGS, normalize, t
from dse_alerts.i18n.alerts import localize
from dse_alerts.rules import RULES, Alert
from dse_alerts.rules.catalog import OPT_IN
from dse_alerts.web.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    Sessions,
    hash_password,
    verify_password,
)

HERE = Path(__file__).parent
LANG_COOKIE = "dse_lang"
LANG_MAX_AGE = 60 * 60 * 24 * 365


def _isnan(x):
    return x is None or (isinstance(x, float) and pd.isna(x))


def _pct(x, digits=1):
    return "–" if _isnan(x) else f"{100 * float(x):+.{digits}f}%"


def _num(x, digits=1):
    return "–" if _isnan(x) else f"{float(x):,.{digits}f}"


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
    sessions = Sessions(settings.app_secret_key)

    app = FastAPI(title="dse-alerts", docs_url=None, redoc_url=None, openapi_url=None)
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
        if not u:
            return None
        request.state.csrf = sess["csrf"]
        return u

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

    def set_session(resp, user_id: int):
        resp.set_cookie(SESSION_COOKIE, sessions.issue(user_id), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
        return resp

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
        existing = appdb.digest_for(user["id"], session)
        if existing and (existing["lang"] or "en") == lang:
            return
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
        resp = RedirectResponse(request.headers.get("referer") or "/", status_code=303)
        resp.set_cookie(LANG_COOKIE, normalize(code), max_age=LANG_MAX_AGE, samesite="lax")
        return resp

    # ---- auth pages -------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if not appdb.users():
            return RedirectResponse("/register", status_code=303)
        return render(request, "login.html", error=None, demo_available=settings.app_demo and appdb.user(DEMO_EMAIL) is not None)

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...)):
        u = appdb.user(email.strip().lower())
        if not u or not verify_password(u["password_hash"], password):
            return render(request, "login.html", error="login.wrong", demo_available=settings.app_demo and appdb.user(DEMO_EMAIL) is not None)
        return set_session(RedirectResponse("/", status_code=303), u["id"])

    @app.api_route("/demo", methods=["GET", "POST"])
    def demo_login(request: Request):
        u = appdb.user(DEMO_EMAIL) if settings.app_demo else None
        if not u:
            raise HTTPException(status_code=404, detail="demo mode is off")
        return set_session(RedirectResponse("/", status_code=303), u["id"])

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request):
        return render(request, "register.html", error=None, first=not appdb.users())

    @app.post("/register")
    def register(request: Request, email: str = Form(...), password: str = Form(...), alert_to: str = Form("")):
        email = email.strip().lower()
        if len(password) < 8:
            return render(request, "register.html", error="register.short", first=not appdb.users())
        existing = appdb.user(email)
        if (existing and existing["password_hash"]) or email == DEMO_EMAIL:
            return render(request, "register.html", error="register.exists", first=False)
        uid = appdb.ensure_user(email, hash_password(password), alert_to.strip() or None)
        appdb.ensure_watchlist(uid)
        appdb.update_user(uid, lang=lang_of(request))
        return set_session(RedirectResponse("/", status_code=303), uid)

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # ---- overview ---------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user=Depends(require_user)):
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
        digest = appdb.digest_for(user["id"], ms["last_session"])
        return render(request, "dashboard.html", active="dashboard", items=items, rows=rows, market=ms, alerts=today,
                      index=market.index_last(store), index_chart={"series": [{"name": "DSEX", "data": market.index_series(store, 130), "area": True, "digits": 0}], "options": {}},
                      total=val["total"] or None, accounts=len(val["accounts"]) if not val["accounts"].empty else 0, breadth=breadth,
                      digest=dict(digest) if digest else None, ai_on=settings.ai_enabled)

    # ---- watchlist actions ------------------------------------------------------------

    @app.post("/watchlist/add")
    def add_symbol(request: Request, symbol: str = Form(...), csrf: str = Form(...), kind: str = Form("watch"), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        sym = symbol.strip().upper()
        if sym not in set(store.instruments()["symbol"]):
            raise HTTPException(status_code=404, detail=t(lang_of(request, user), "error.not_code", symbol=sym))
        appdb.add_item(appdb.ensure_watchlist(user["id"]), sym, kind="target" if kind == "target" else "watch")
        return RedirectResponse(request.headers.get("referer") or "/", status_code=303)

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
        cfg: dict = {}
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
        note = str(form.get("note", "")).strip() or None
        appdb.add_item(appdb.ensure_watchlist(user["id"]), sym, cfg, note)
        return RedirectResponse(f"/symbol/{sym}", status_code=303)

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
        session, results = engine.run(store, appdb, record=True, user_ids=[user["id"]])
        if results:
            write_digest_if_due(user, session, results[0].alerts)
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

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, user=Depends(require_user)):
        return render(request, "settings.html", active="settings", runs=appdb.last_runs(10), market=market_status(), msg=None,
                      accounts=appdb.accounts(user["id"]))

    @app.post("/settings")
    def save_settings(request: Request, csrf: str = Form(...), alert_to: str = Form(""), digest: str = Form(""),
                      password: str = Form(""), telegram_chat_id: str = Form(""), lang: str = Form(""), user=Depends(require_user)):
        check_csrf(request, csrf)
        writable(request, user)
        fields = {"alert_email": alert_to.strip() or user["email"], "digest_enabled": 1 if digest == "on" else 0,
                  "telegram_chat_id": telegram_chat_id.strip() or None, "lang": normalize(lang) if lang else dict(user).get("lang") or "en"}
        if password:
            if len(password) < 8:
                return render(request, "settings.html", active="settings", runs=appdb.last_runs(10), market=market_status(),
                              msg="settings.password_short", accounts=appdb.accounts(user["id"]))
            fields["password_hash"] = hash_password(password)
        appdb.update_user(user["id"], **fields)
        resp = RedirectResponse("/settings", status_code=303)
        resp.set_cookie(LANG_COOKIE, fields["lang"], max_age=LANG_MAX_AGE, samesite="lax")
        return resp

    @app.exception_handler(HTTPException)
    async def _redirects(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        request.state.user = current_user(request)
        lang = lang_of(request, request.state.user)
        detail = t(lang, exc.detail) if isinstance(exc.detail, str) else exc.detail   # catalog keys translate; plain text passes through
        return render(request, "error.html", status_code=exc.status_code, status=exc.status_code, detail=detail)

    return app
