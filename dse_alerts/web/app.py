"""The web UI: a server-rendered FastAPI app. No JavaScript framework, no external requests
from the browser (the chart library is vendored), no analytics. Everything it shows comes
from the two local databases.

Run with ``dse web serve`` (uvicorn) or mount ``create_app()`` yourself.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dse_alerts import engine, evidence, market, portfolio
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
        existing = appdb.user(email)
        if existing and existing["password_hash"]:
            return render(request, "register.html", error="That email already has an account. Log in instead.", first=False)
        uid = appdb.ensure_user(email, hash_password(password), alert_to.strip() or None)
        appdb.ensure_watchlist(uid)
        return set_session(RedirectResponse("/", status_code=303), uid)

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # ---- overview ---------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user=Depends(require_user)):
        items, rows = watch_rows(user["id"])
        ms = market_status()
        history = appdb.alert_history(user["id"], limit=50)
        today = [a for a in history if ms["last_session"] and a["session_date"] == ms["last_session"].isoformat()]
        val = portfolio.valuation(store, appdb, user["id"])
        ups = sum(1 for r in rows if not _isnan(r["ret_1"]) and r["ret_1"] > 0)
        downs = sum(1 for r in rows if not _isnan(r["ret_1"]) and r["ret_1"] < 0)
        breadth = {"up": ups, "down": downs, "flat": len(rows) - ups - downs, "total": len(rows)}
        return render(request, "dashboard.html", active="dashboard", items=items, rows=rows, market=ms, alerts=today,
                      index=market.index_last(store), index_chart={"series": [{"name": "DSEX", "data": market.index_series(store, 130), "area": True, "digits": 0}], "options": {}},
                      total=val["total"] or None, accounts=len(val["accounts"]) if not val["accounts"].empty else 0, breadth=breadth)

    # ---- watchlist actions ------------------------------------------------------------

    @app.post("/watchlist/add")
    def add_symbol(request: Request, symbol: str = Form(...), csrf: str = Form(...), kind: str = Form("watch"), user=Depends(require_user)):
        check_csrf(request, csrf)
        sym = symbol.strip().upper()
        if sym not in set(store.instruments()["symbol"]):
            raise HTTPException(status_code=404, detail=f"{sym} is not a DSE trading code")
        appdb.add_item(appdb.ensure_watchlist(user["id"]), sym, kind="target" if kind == "target" else "watch")
        return RedirectResponse(request.headers.get("referer") or "/", status_code=303)

    @app.post("/watchlist/remove")
    def remove_symbol(request: Request, symbol: str = Form(...), csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        appdb.remove_item(appdb.ensure_watchlist(user["id"]), symbol.strip().upper())
        return RedirectResponse("/", status_code=303)

    @app.post("/watchlist/kind")
    def set_kind(request: Request, symbol: str = Form(...), kind: str = Form(...), csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
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
        if any(a["id"] == account_id for a in appdb.accounts(user["id"])) and name.strip():
            appdb.rename_account(account_id, name.strip())
        return RedirectResponse("/settings", status_code=303)

    # ---- symbol -----------------------------------------------------------------------

    @app.get("/symbol/{symbol}", response_class=HTMLResponse)
    def symbol_page(request: Request, symbol: str, user=Depends(require_user)):
        sym = symbol.upper()
        with store.connect(read_only=True) as con:
            inst = con.execute("SELECT * FROM instruments WHERE symbol = ?", [sym]).df()
            if inst.empty:
                raise HTTPException(status_code=404, detail="unknown symbol")
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
        item = next((i for i in appdb.items(user["id"]) if i["symbol"] == sym), None)
        held = [h for h in appdb.holdings(user["id"]) if h["symbol"] == sym]
        holding = None
        if held:
            q = sum(h["quantity"] for h in held)
            holding = {"quantity": q, "avg_cost": sum(h["quantity"] * h["avg_cost"] for h in held) / q if q else None}
        return render(request, "symbol.html", active="", symbol=sym, inst=inst_d, last=_records(last)[0] if not last.empty else {},
                      ev=ev, stats=_records(stats)[0] if not stats.empty else None, fund=_records(fund)[0] if not fund.empty else None,
                      news=_records(news), divs=_records(divs), item=item, holding=holding, rules=RULES, opt_in=OPT_IN,
                      price_chart=market.ohlc(store, sym, "1y"), rel_chart=market.relative_series(store, sym, 250),
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
        return render(request, "alerts.html", active="alerts", alerts=appdb.alert_history(user["id"], 300))

    @app.post("/alerts/check")
    def check_now(request: Request, csrf: str = Form(...), user=Depends(require_user)):
        check_csrf(request, csrf)
        engine.run(store, appdb, record=True, user_ids=[user["id"]])
        return RedirectResponse("/", status_code=303)

    @app.get("/scorecard", response_class=HTMLResponse)
    def scorecard_page(request: Request, user=Depends(require_user)):
        from dse_alerts import scorecard as sc

        df = sc.outcomes(store, appdb, user["id"])
        agg = sc.by_rule(df, sc.load_expectation(settings))
        return render(request, "scorecard.html", active="scorecard", rows=_records(df.head(200)), by_rule=_records(agg),
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
                      password: str = Form(""), telegram_chat_id: str = Form(""), user=Depends(require_user)):
        check_csrf(request, csrf)
        fields = {"alert_email": alert_to.strip() or user["email"], "digest_enabled": 1 if digest == "on" else 0,
                  "telegram_chat_id": telegram_chat_id.strip() or None}
        if password:
            if len(password) < 8:
                return render(request, "settings.html", active="settings", runs=appdb.last_runs(10), market=market_status(),
                              msg="Password too short.", accounts=appdb.accounts(user["id"]))
            fields["password_hash"] = hash_password(password)
        appdb.update_user(user["id"], **fields)
        return RedirectResponse("/settings", status_code=303)

    @app.exception_handler(HTTPException)
    async def _redirects(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        request.state.user = current_user(request)
        return render(request, "error.html", status_code=exc.status_code, status=exc.status_code, detail=exc.detail)

    return app
