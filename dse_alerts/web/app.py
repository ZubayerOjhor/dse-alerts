"""The web UI: a small server-rendered FastAPI app. No JavaScript framework, no external
requests from the browser, no analytics. Everything it shows comes from the two local
databases.

Run with ``dse web serve`` (uvicorn) or mount ``create_app()`` yourself.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dse_alerts import engine
from dse_alerts.appdb import AppDB
from dse_alerts.config import Settings, get_settings
from dse_alerts.data.store import MarketStore
from dse_alerts.rules import RULES
from dse_alerts.rules.catalog import OPT_IN
from dse_alerts.web.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    Sessions,
    hash_password,
    verify_password,
)

HERE = Path(__file__).parent


def _pct(x, digits=1):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "–"
    return f"{100 * float(x):+.{digits}f}%"


def _num(x, digits=1):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "–"
    return f"{float(x):,.{digits}f}"


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
        return u

    def check_csrf(request: Request, token: str):
        if token != getattr(request.state, "csrf", None):
            raise HTTPException(status_code=400, detail="bad csrf token")

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        u = getattr(request.state, "user", None)
        return templates.TemplateResponse(request, name, {"user": u, "csrf": getattr(request.state, "csrf", ""),
                                                          "settings": settings, "now": dt.datetime.now(), **ctx},
                                          status_code=status_code)

    def set_session(resp, user_id: int):
        resp.set_cookie(SESSION_COOKIE, sessions.issue(user_id), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
        return resp

    def market_status():
        s = store.summary()
        last = engine.latest_session(store)
        stale = (dt.date.today() - last).days > 4 if last else True
        return {"last_session": last, "rows": s["rows"], "symbols": s["symbols"], "stale": stale}

    def latest_rows(symbols: list[str]) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()
        ph = ",".join("?" * len(symbols))
        with store.connect(read_only=True) as con:
            return con.execute(
                f"""
                SELECT f.symbol, f.date, f.close, f.ret_1, f.ret_10, f.vol_ratio_20, f.hit_upper, f.hit_lower,
                       f.upper_streak, f.lower_streak, i.name, i.category, i.sector
                FROM features f JOIN instruments i USING (symbol)
                WHERE f.symbol IN ({ph})
                QUALIFY row_number() OVER (PARTITION BY f.symbol ORDER BY f.date DESC) = 1
                ORDER BY f.symbol
                """,
                symbols,
            ).df()

    # ---- auth pages -------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if not appdb.users():
            return RedirectResponse("/register", status_code=303)
        return render(request, "login.html", error=None)

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...)):
        u = appdb.user(email.strip().lower())
        if not u or not verify_password(u["password_hash"], password):
            return render(request, "login.html", error="Wrong email or password.")
        return set_session(RedirectResponse("/", status_code=303), u["id"])

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request):
        return render(request, "register.html", error=None, first=not appdb.users())

    @app.post("/register")
    def register(request: Request, email: str = Form(...), password: str = Form(...), alert_to: str = Form("")):
        email = email.strip().lower()
        if len(password) < 8:
            return render(request, "register.html", error="Use at least 8 characters.", first=not appdb.users())
        if appdb.user(email) and appdb.user(email)["password_hash"]:
            return render(request, "register.html", error="That email already has an account. Log in instead.", first=False)
        uid = appdb.ensure_user(email, hash_password(password), alert_to.strip() or None)
        appdb.ensure_watchlist(uid)
        return set_session(RedirectResponse("/", status_code=303), uid)

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # ---- app pages --------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user=Depends(require_user)):
        request.state.user = user
        items = appdb.items(user["id"])
        rows = latest_rows([i["symbol"] for i in items])
        today = appdb.alert_history(user["id"], limit=50)
        ms = market_status()
        recent_alerts = [a for a in today if ms["last_session"] and a["session_date"] == ms["last_session"].isoformat()]
        return render(request, "dashboard.html", items=items, rows=rows.to_dict("records"), market=ms,
                      alerts=recent_alerts, history=today[:20])

    @app.post("/watchlist/add")
    def add_symbol(request: Request, symbol: str = Form(...), csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        sym = symbol.strip().upper()
        inst = store.instruments()
        if sym not in set(inst["symbol"]):
            raise HTTPException(status_code=404, detail=f"{sym} is not a DSE trading code")
        appdb.add_item(appdb.ensure_watchlist(user["id"]), sym)
        return RedirectResponse("/", status_code=303)

    @app.post("/watchlist/remove")
    def remove_symbol(request: Request, symbol: str = Form(...), csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        appdb.remove_item(appdb.ensure_watchlist(user["id"]), symbol.strip().upper())
        return RedirectResponse("/", status_code=303)

    @app.get("/symbol/{symbol}", response_class=HTMLResponse)
    def symbol_page(request: Request, symbol: str, user=Depends(require_user)):
        request.state.user = user
        sym = symbol.upper()
        with store.connect(read_only=True) as con:
            hist = con.execute(
                "SELECT date, open, high, low, close, volume, ret_1, ret_10, vol_ratio_20, hit_upper, hit_lower, sma_20, sma_50 "
                "FROM features WHERE symbol = ? ORDER BY date DESC LIMIT 60", [sym]).df()
            inst = con.execute("SELECT * FROM instruments WHERE symbol = ?", [sym]).df()
            stats = con.execute("SELECT * FROM symbol_stats WHERE symbol = ?", [sym]).df() \
                if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='symbol_stats'").fetchone()[0] else pd.DataFrame()
            fund = con.execute("SELECT * FROM fundamentals WHERE symbol = ? ORDER BY as_of DESC LIMIT 1", [sym]).df()
            news = con.execute("SELECT * FROM news WHERE symbol = ? ORDER BY date DESC LIMIT 10", [sym]).df()
            divs = con.execute("SELECT * FROM dividends WHERE symbol = ? ORDER BY declared_on DESC LIMIT 5", [sym]).df()
        if inst.empty:
            raise HTTPException(status_code=404, detail="unknown symbol")
        item = next((i for i in appdb.items(user["id"]) if i["symbol"] == sym), None)
        closes = hist["close"].tolist()[::-1]
        return render(request, "symbol.html", symbol=sym, inst=inst.iloc[0].to_dict(),
                      stats=stats.iloc[0].to_dict() if not stats.empty else None,
                      fund=fund.iloc[0].to_dict() if not fund.empty else None,
                      hist=hist.to_dict("records"), news=news.to_dict("records"), divs=divs.to_dict("records"),
                      item=item, rules=RULES, opt_in=OPT_IN, spark=_sparkline(closes),
                      alerts=[a for a in appdb.alert_history(user["id"], 500) if a["symbol"] == sym][:20])

    @app.post("/symbol/{symbol}/rules")
    async def save_rules(request: Request, symbol: str, user=Depends(require_user)):
        form = await request.form()
        check_csrf(request, str(form.get("csrf", "")))
        sym = symbol.upper()
        cfg: dict = {}
        for r in RULES:
            enabled = form.get(f"on_{r.id}") == "on"
            params = {}
            for k, default in r.params.items():
                raw = str(form.get(f"{r.id}__{k}", "")).strip()
                if raw == "":
                    continue
                if k in ("pct",):
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

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts_page(request: Request, user=Depends(require_user)):
        request.state.user = user
        return render(request, "alerts.html", alerts=appdb.alert_history(user["id"], 300))

    @app.post("/alerts/check")
    def check_now(request: Request, csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        engine.run(store, appdb, record=True, user_ids=[user["id"]])
        return RedirectResponse("/", status_code=303)

    @app.get("/rules", response_class=HTMLResponse)
    def rules_page(request: Request, user=Depends(require_user)):
        request.state.user = user
        cal = {}
        p = settings.dse_data_dir / "calibration.json"
        if p.exists():
            try:
                cal = {e["event"]: e for e in json.loads(p.read_text(encoding="utf-8")).get("event_stats", [])}
            except (ValueError, KeyError):
                cal = {}
        return render(request, "rules.html", rules=RULES, opt_in=OPT_IN, cal=cal)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, user=Depends(require_user)):
        request.state.user = user
        return render(request, "settings.html", runs=appdb.last_runs(10), market=market_status(), msg=None)

    @app.post("/settings")
    def save_settings(request: Request, csrf: str = Form(...), alert_to: str = Form(""), digest: str = Form(""),
                      password: str = Form(""), user=Depends(require_user)):
        check_csrf(request, csrf)
        fields = {"alert_email": alert_to.strip() or user["email"], "digest_enabled": 1 if digest == "on" else 0}
        if password:
            if len(password) < 8:
                request.state.user = user
                return render(request, "settings.html", runs=appdb.last_runs(10), market=market_status(), msg="Password too short.")
            fields["password_hash"] = hash_password(password)
        appdb.update_user(user["id"], **fields)
        return RedirectResponse("/settings", status_code=303)

    @app.get("/instruments.json")
    def instruments_json(user=Depends(require_user)):
        inst = store.instruments()
        inst = inst[inst["tradeable"].fillna(False)]
        return [{"symbol": r.symbol, "name": r.name or ""} for r in inst.itertuples()]

    @app.exception_handler(HTTPException)
    async def _redirects(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        request.state.user = current_user(request)
        return render(request, "error.html", status_code=exc.status_code, status=exc.status_code, detail=exc.detail)

    return app


def _sparkline(values: list[float], w: int = 240, h: int = 48) -> str:
    """Inline SVG path for a list of closes (oldest first)."""
    vals = [v for v in values if v is not None and not pd.isna(v)]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = w / (len(vals) - 1)
    pts = [f"{i * step:.1f},{h - (v - lo) / span * (h - 4) - 2:.1f}" for i, v in enumerate(vals)]
    return "M" + " L".join(pts)
