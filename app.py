"""StackWealth Dashboard - login + home page (single file).
Run: streamlit run app.py
Uses tables created earlier: clients, current_holdings, price_cache, sw_audit_log."""
from __future__ import annotations
import hmac, html, io, json, logging, re, time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

log = logging.getLogger("stackwealth")
APP_NAME = "StackWealth Dashboard"
GREEN, RED, AMBER, NAVY, PANEL, MUTED, TEXT = "#10b981", "#ef4444", "#f59e0b", "#0b1426", "#111d36", "#9fb0cc", "#e6edf7"
PALETTE = px.colors.qualitative.Safe + px.colors.qualitative.Pastel
# AMFI/SEBI classify by rank (top 100 large, 101-250 mid). Approximate INR cut-offs from the July 2026 AMFI list;
# update every six months.
LARGE_CAP_MIN_CR, MID_CAP_MIN_CR = 106300, 33500
BENCHMARKS = {"NIFTY 50": "^NSEI", "SENSEX": "^BSESN", "NIFTY Midcap 100": "^CNXMIDCAP", "NIFTY Bank": "^NSEBANK", "NIFTY IT": "^CNXIT"}
DEFAULT_BENCH = ["NIFTY 50", "SENSEX"]
PERIODS = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365}
SESSION_TTL, STALE_DAYS = 8 * 3600, 4


# ============================================================ pure helpers
def fmt_inr(v, dec: int = 0, sign: bool = False) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    d = Decimal(str(v)); neg = d < 0
    ip, _, fp = f"{abs(d):.{dec}f}".partition(".")
    if len(ip) > 3:
        head, tail, parts = ip[:-3], ip[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head: parts.insert(0, head)
        ip = ",".join(parts + [tail])
    return ("-" if neg else "+" if sign and d > 0 else "") + "₹" + ip + (("." + fp) if fp else "")


def fmt_cr(v) -> str:
    """Compact Indian units for big numbers: Cr / L."""
    if v is None or v != v: return "—"
    a = abs(v)
    s = f"₹{a / 1e7:,.2f} Cr" if a >= 1e7 else f"₹{a / 1e5:,.2f} L" if a >= 1e5 else f"₹{a:,.0f}"
    return ("-" if v < 0 else "") + s


def pct(v, sign: bool = True) -> str:
    return "—" if v is None or v != v else (f"{v:+.2f}%" if sign else f"{v:.2f}%")


def normalize_symbol(raw: str) -> str:
    s = (raw or "").strip().upper().replace(" ", "")
    exch = ".NS"
    if ":" in s:
        pre, s = s.split(":", 1)
        if pre not in ("NSE", "BSE"): raise ValueError("Use NSE: or BSE: prefix.")
        exch = ".NS" if pre == "NSE" else ".BO"
    for suf in (".NS", ".BO"):
        if s.endswith(suf): s, exch = s[:-3], suf
    if not re.match(r"^[A-Z0-9][A-Z0-9&\-]{0,24}$", s): raise ValueError(f"Invalid symbol '{(raw or '')[:30]}'.")
    return s + exch


def short(sym: str) -> str:
    return sym.rsplit(".", 1)[0]


def cap_bucket(market_cap_inr) -> str:
    if not market_cap_inr: return "Unclassified"
    cr = float(market_cap_inr) / 1e7
    return "Large Cap" if cr >= LARGE_CAP_MIN_CR else "Mid Cap" if cr >= MID_CAP_MIN_CR else "Small Cap"


def enrich(rows: list[dict]) -> pd.DataFrame:
    """One row per client-stock position. Missing price => Value/PnL are NaN (never zero)."""
    cols = ["client_id", "Client", "Symbol", "sym", "Qty", "Avg", "Price", "Invested", "Value", "PnL", "Ret", "price_date", "Sector", "Cap", "priced"]
    out = []
    for r in rows:
        q, a = Decimal(str(r["quantity"])), Decimal(str(r["avg_price"]))
        inv = q * a
        p = Decimal(str(r["price"])) if r.get("price") else None
        mv = q * p if p else None
        d = r.get("diagnostics") or {}
        pnl = float(mv - inv) if mv is not None else None
        out.append({"client_id": r["client_id"], "Client": r["client_name"], "Symbol": short(r["symbol"]), "sym": r["symbol"],
                    "Qty": float(q), "Avg": float(a), "Price": float(p) if p else None, "Invested": float(inv),
                    "Value": float(mv) if mv is not None else None, "PnL": pnl,
                    "Ret": (pnl / float(inv) * 100) if pnl is not None and inv else None,
                    "price_date": r.get("price_date"), "Sector": d.get("sector") or "Unclassified",
                    "Cap": cap_bucket(d.get("market_cap")), "priced": mv is not None})
    return pd.DataFrame(out, columns=cols)


def totals(df: pd.DataFrame) -> dict:
    pr = df[df["priced"].astype(bool)]
    inv_all, inv_pr, mv = float(df.Invested.sum()), float(pr.Invested.sum()), float(pr.Value.sum())
    return {"invested": inv_all, "invested_priced": inv_pr, "value": mv, "pnl": mv - inv_pr,
            "ret": (mv - inv_pr) / inv_pr * 100 if inv_pr else None, "n": len(df), "n_priced": len(pr),
            "clients": int(df.client_id.nunique()) if len(df) else 0}


def concentration(df: pd.DataFrame, basis: str):
    s = df.groupby("Symbol")[basis].sum().dropna()
    s = s[s > 0]
    if s.empty: return None, None
    w = s / s.sum()
    return float(w.nlargest(5).sum() * 100), float((w ** 2).sum() * 10000)


def value_series(df: pd.DataFrame, closes: pd.DataFrame) -> pd.Series:
    """Current holdings valued at historical closes (hypothetical, not actual past performance)."""
    if closes is None or df.empty: return pd.Series(dtype=float)
    q = df.groupby("sym").Qty.sum()
    q = q[q.index.isin(closes.columns)]
    if q.empty: return pd.Series(dtype=float)
    c = closes[list(q.index)].ffill().dropna()
    return sum(c[s] * v for s, v in q.items())


def period_return(s, days: int):
    if s is None or len(s) < 2: return None
    target = s.index[-1] - pd.Timedelta(days=days)
    if target < s.index[0]:
        if (s.index[0] - target).days > 7: return None
        start = s.iloc[0]
    else:
        start = s.asof(target)
    if pd.isna(start) or not start: return None
    return float((s.iloc[-1] / start - 1) * 100)


def freshness(df: pd.DataFrame) -> dict:
    d = [x for x in df.price_date if x is not None and x == x]
    if not d: return {"latest": None, "stale": 0, "missing": int((~df.priced).sum())}
    return {"latest": max(d), "stale": sum(1 for x in d if (date.today() - x).days > STALE_DAYS), "missing": int((~df.priced).sum())}


def client_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (cid, name), x in df.groupby(["client_id", "Client"]):
        pr = x[x.priced]
        inv_pr = pr.Invested.sum()
        rows.append({"client_id": cid, "Client": name, "Invested": x.Invested.sum(),
                     "Market value": pr.Value.sum() if len(pr) else None, "P&L (priced)": pr.PnL.sum() if len(pr) else None,
                     "Return %": (pr.PnL.sum() / inv_pr * 100) if len(pr) and inv_pr else None,
                     "Holdings": len(x), "Priced": f"{len(pr)}/{len(x)}"})
    return pd.DataFrame(rows, columns=["client_id", "Client", "Invested", "Market value", "P&L (priced)", "Return %", "Holdings", "Priced"])


def tone(x) -> str:
    return MUTED if x is None or x != x or x == 0 else GREEN if x > 0 else RED


def tone_css(v) -> str:
    return f"color:{GREEN};font-weight:600" if isinstance(v, (int, float)) and v > 0 else \
           f"color:{RED};font-weight:600" if isinstance(v, (int, float)) and v < 0 else ""


# ============================================================ figure builders (return figures; testable)
def _layout(f, h, title=None):
    f.update_layout(template="plotly_dark", height=h, margin=dict(t=48 if title else 10, l=0, r=0, b=0), title=title,
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(color=TEXT))
    return f


def donut_fig(df, name, val, title, h=380):
    d = df[df[val].fillna(0) > 0].groupby(name, as_index=False)[val].sum()
    if d.empty: return None
    f = px.pie(d, names=name, values=val, hole=0.52, color_discrete_sequence=PALETTE)
    f.update_traces(textinfo="percent+label", pull=[0.03] * len(d), hovertemplate="%{label}<br>₹%{value:,.0f}<br>%{percent}<extra></extra>",
                    marker=dict(line=dict(color=NAVY, width=2)))
    f.update_layout(showlegend=False)
    return _layout(f, h, title)


def treemap_fig(df, h=460):
    """Sector -> stock; tile size = value, colour = return % (red loss / green profit)."""
    pr = df[df.priced & (df.Value > 0)]
    if pr.empty:
        d = df[df.Invested > 0].groupby(["Sector", "Symbol"], as_index=False).Invested.sum()
        if d.empty: return None
        f = px.treemap(d, path=[px.Constant("All"), "Sector", "Symbol"], values="Invested", color_discrete_sequence=PALETTE)
        return _layout(f, h, "Sector → stock (invested capital)")
    d = pr.groupby(["Sector", "Symbol"], as_index=False).agg(Value=("Value", "sum"), PnL=("PnL", "sum"), Invested=("Invested", "sum"))
    d["Ret"] = d.PnL / d.Invested * 100
    f = px.treemap(d, path=[px.Constant("Portfolio"), "Sector", "Symbol"], values="Value", color="Ret",
                   color_continuous_scale=[RED, "#1f2937", GREEN], color_continuous_midpoint=0, range_color=[-25, 25],
                   custom_data=["PnL", "Ret"])
    f.update_traces(texttemplate="%{label}<br>%{customdata[1]:+.1f}%", hovertemplate="%{label}<br>Value ₹%{value:,.0f}<br>P&L ₹%{customdata[0]:,.0f} (%{customdata[1]:+.2f}%)<extra></extra>",
                    marker=dict(line=dict(color=NAVY, width=2)))
    f.update_layout(coloraxis_colorbar=dict(title="Return %"))
    return _layout(f, h, "Sector → stock: size = value, colour = return")


def gauge_fig(top5):
    if top5 is None: return None
    f = go.Figure(go.Indicator(mode="gauge+number", value=top5, number=dict(suffix="%", font=dict(color=TEXT)),
        title=dict(text="Top-5 stocks share of value", font=dict(size=15)),
        gauge=dict(axis=dict(range=[0, 100]), bar=dict(color="#e6edf7"),
                   steps=[dict(range=[0, 40], color="#065f46"), dict(range=[40, 65], color="#92400e"), dict(range=[65, 100], color="#7f1d1d")])))
    return _layout(f, 300)


def bars_fig(ct):
    d = ct.dropna(subset=["Market value"])
    if d.empty: return None
    inv_pr = d["Market value"] - d["P&L (priced)"]
    f = go.Figure([go.Bar(name="Invested (priced)", x=d.Client, y=inv_pr, marker_color="#64748b"),
                   go.Bar(name="Market value", x=d.Client, y=d["Market value"], marker_color=GREEN)])
    f.update_layout(barmode="group", legend=dict(orientation="h"))
    return _layout(f, 340, "Invested vs current value by client")


def pnl_bar_fig(x, y, title, h=340):
    d = pd.DataFrame({"x": x, "y": y}).dropna()
    if d.empty: return None
    f = go.Figure(go.Bar(x=d.x, y=d.y, marker_color=[GREEN if v >= 0 else RED for v in d.y], hovertemplate="%{x}<br>₹%{y:,.0f}<extra></extra>"))
    return _layout(f, h, title)


def bench_fig(series, closes, picks, days, label):
    """Rebased-to-100 comparison. Returns (fig, table, missing_names)."""
    lines, missing = {label: series}, []
    for n in picks:
        tk = BENCHMARKS[n]
        if closes is not None and tk in closes.columns and closes[tk].notna().any(): lines[n] = closes[tk].dropna()
        else: missing.append(n)
    frame = pd.concat(lines, axis=1).ffill().dropna()
    table = pd.DataFrame([{"Series": n, **{k: period_return(s, d) for k, d in PERIODS.items()}} for n, s in lines.items()]).set_index("Series")
    view = frame[frame.index >= frame.index[-1] - pd.Timedelta(days=days)]
    reb = view / view.iloc[0] * 100
    f = go.Figure()
    for i, c in enumerate(reb.columns):
        f.add_trace(go.Scatter(x=reb.index, y=reb[c], name=c, line=dict(width=3.5 if i == 0 else 1.8, color=GREEN if i == 0 else PALETTE[i + 2]),
                               fill="tozeroy" if i == 0 else None, fillcolor="rgba(16,185,129,0.07)"))
    f.add_hline(y=100, line_dash="dot", line_color="#475569")
    f.update_layout(yaxis_title="Rebased to 100", legend=dict(orientation="h"), hovermode="x unified")
    return _layout(f, 400), table, missing


def build_report(ct: pd.DataFrame, df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    hold = df.drop(columns=["client_id", "sym", "priced"]).rename(columns={"Avg": "Avg price (INR)", "Price": "Latest price (INR)", "Invested": "Invested (INR)",
                                                                           "Value": "Market value (INR)", "PnL": "Unrealized P&L (INR)", "Ret": "Return %"})
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame({"Report": [APP_NAME], "Generated": [time.strftime("%Y-%m-%d %H:%M:%S")],
                      "Note": ["Unrealized P&L only. Yahoo Finance prices are delayed / end-of-day. Missing prices excluded from value and P&L."]}).to_excel(xw, sheet_name="About", index=False)
        ct.drop(columns="client_id").to_excel(xw, sheet_name="Clients", index=False)
        hold.to_excel(xw, sheet_name="Holdings", index=False)
    return buf.getvalue()


# ============================================================ database
def _dsn() -> str:
    try:
        return st.secrets["DATABASE_URL"]
    except Exception:
        raise RuntimeError("DATABASE_URL is missing in Streamlit secrets.") from None


@contextmanager
def db():
    import psycopg
    try:
        conn = psycopg.connect(_dsn(), connect_timeout=10)
    except Exception as e:
        log.error("db connect failed: %s", type(e).__name__)
        raise RuntimeError("Cannot connect to the database. Check DATABASE_URL (use the Supabase pooler string) and that the project isn't paused.") from None
    try:
        yield conn; conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


POS_SQL = """SELECT c.id AS client_id, c.name AS client_name, h.symbol, h.quantity, h.avg_price,
  p.price, p.price_date, p.retrieved_at, p.diagnostics
  FROM current_holdings h JOIN clients c ON c.id=h.client_id
  LEFT JOIN price_cache p ON p.symbol=h.symbol WHERE c.status='active' ORDER BY c.name, h.symbol"""


def load_positions() -> pd.DataFrame:
    with db() as conn:
        cur = conn.execute(POS_SQL)
        cols = [d.name for d in cur.description]
        return enrich([dict(zip(cols, r)) for r in cur.fetchall()])


# ============================================================ market data (explicit refresh only)
def download_closes(tickers: list[str], period: str = "1y") -> pd.DataFrame:
    import yfinance as yf
    tickers = sorted(set(tickers))
    data = yf.download(tickers, period=period, interval="1d", auto_adjust=False, progress=False, group_by="column", threads=True)
    if data is None or data.empty: return pd.DataFrame()
    c = data["Close"]
    if isinstance(c, pd.Series): c = c.to_frame(tickers[0])
    return c.dropna(axis=1, how="all")


def fetch_info(sym: str) -> dict:
    try:
        import yfinance as yf
        i = yf.Ticker(sym).info or {}
        return {"sector": i.get("sector") or i.get("industry"), "market_cap": i.get("marketCap")}
    except Exception as e:
        log.warning("info failed %s: %s", sym, type(e).__name__)
        return {}


def refresh_market_data() -> dict:
    syms = sorted(set(load_positions().sym))
    if not syms: return {"ok": 0, "missing": []}
    closes = download_closes(syms + list(BENCHMARKS.values()))
    ok = [s for s in syms if s in closes.columns and closes[s].notna().any() and float(closes[s].dropna().iloc[-1]) > 0]
    with db() as conn:
        for s in ok:
            col = closes[s].dropna()
            conn.execute("""INSERT INTO price_cache(symbol,price,price_date,retrieved_at,source,status) VALUES (%s,%s,%s,now(),'yfinance','ok')
              ON CONFLICT (symbol) DO UPDATE SET price=EXCLUDED.price, price_date=EXCLUDED.price_date, retrieved_at=now(), source='yfinance', status='ok'""",
                         (s, round(float(col.iloc[-1]), 4), col.index[-1].date()))
    with ThreadPoolExecutor(max_workers=4) as ex:
        infos = dict(zip(ok, ex.map(fetch_info, ok)))
    with db() as conn:
        for s, i in infos.items():
            i = {k: v for k, v in i.items() if v}
            if i: conn.execute("UPDATE price_cache SET diagnostics = COALESCE(diagnostics,'{}'::jsonb) || %s::jsonb WHERE symbol=%s", (json.dumps(i), s))
        conn.execute("INSERT INTO sw_audit_log(actor,action,entity,details) VALUES (%s,'prices.refresh','price_cache',%s::jsonb)",
                     (st.session_state.get("sw_user", "admin"), json.dumps({"requested": len(syms), "refreshed": len(ok), "missing": [s for s in syms if s not in ok][:50]})))
    st.session_state["closes"] = closes
    return {"ok": len(ok), "missing": [s for s in syms if s not in ok]}


def refresh_ui():
    with st.spinner("Fetching market data…"):
        try:
            r = refresh_market_data()
            st.session_state["refresh_msg"] = f"{r['ok']} symbols updated" + (f"; no data for: {', '.join(short(s) for s in r['missing'])}" if r["missing"] else "")
        except Exception:
            log.exception("refresh failed")
            st.session_state["refresh_msg"] = "Refresh failed (provider unavailable or rate-limited). Existing prices were kept."
    st.rerun()


# ============================================================ authentication
def login_ok(user: str, pw: str):
    try:
        sec = st.secrets["auth"]; u, h = sec["username"], str(sec["password_hash"]).strip()
    except Exception:
        log.error("AUTH CONFIG ERROR: [auth] username/password_hash missing"); return False, "config"
    if not re.match(r"^\$2[abxy]\$\d{2}\$[./A-Za-z0-9]{53}$", h):
        log.error("AUTH CONFIG ERROR: password_hash is not a valid bcrypt hash"); return False, "config"
    import bcrypt
    pw_ok = bcrypt.checkpw((pw or "").encode()[:72], h.encode())
    if hmac.compare_digest((user or "").encode(), u.encode()) and pw_ok: return True, None
    log.warning("AUTH FAILURE: invalid credentials"); return False, "invalid"


def authed() -> bool:
    s = st.session_state
    return bool(s.get("sw_auth")) and time.time() - s.get("sw_login_at", 0) < SESSION_TTL


# ============================================================ UI pieces
CSS = f"""<style>
.stApp {{background:{NAVY};}} [data-testid="stSidebar"] {{background:{PANEL};}}
html,body,.stApp,label,p,span,h1,h2,h3,h4 {{color:{TEXT};}}
.block-container {{padding-top:1.4rem;}}
.stButton>button[kind="primary"],.stFormSubmitButton>button[kind="primary"] {{background:{GREEN};border-color:{GREEN};color:#04130d;font-weight:600;}}
.brand {{font-size:2rem;font-weight:700;color:#fff;text-align:center}} .brand span,.hero .app span {{color:{GREEN}}}
.tag {{text-align:center;color:{MUTED};margin:.2rem 0 1.2rem}}
.hero {{background:linear-gradient(135deg,{PANEL},#0e1a30);border:1px solid #1f2f52;border-radius:12px;padding:22px 26px;margin-bottom:14px}}
.hero .app {{font-size:1.1rem;font-weight:600;color:#fff;letter-spacing:.3px}} .hero .lbl {{color:{MUTED};margin-top:10px;font-size:.9rem}}
.hero .big {{font-size:3.1rem;font-weight:700;line-height:1.15}} .hero .sub {{color:{MUTED};margin-top:4px}}
.card {{background:{PANEL};border:1px solid #1f2f52;border-radius:10px;padding:14px 16px;height:100%}}
.card .k {{color:{MUTED};font-size:.78rem;text-transform:uppercase;letter-spacing:.4px}} .card .v {{font-size:1.4rem;font-weight:650;margin-top:4px}}
.card .s {{color:{MUTED};font-size:.8rem;margin-top:2px}}
.warn {{border-left:3px solid {AMBER};background:#2a2310;color:#fde68a;padding:.6rem .9rem;border-radius:4px;margin:8px 0}}
.chip {{display:inline-block;padding:3px 10px;border-radius:999px;font-size:.78rem;border:1px solid #1f2f52;color:{MUTED};margin-right:6px}}
.sec {{font-size:1.15rem;font-weight:650;margin:22px 0 6px;padding-left:10px;border-left:3px solid {GREEN}}}
</style>"""


def card(k: str, v: str, s: str = "", color: str = TEXT) -> str:
    return f"<div class='card'><div class='k'>{html.escape(k)}</div><div class='v' style='color:{color}'>{html.escape(v)}</div><div class='s'>{html.escape(s)}</div></div>"


def section(title: str):
    st.markdown(f"<div class='sec'>{html.escape(title)}</div>", unsafe_allow_html=True)


def show(fig, empty="Nothing to chart yet."):
    st.plotly_chart(fig, use_container_width=True) if fig is not None else st.info(empty)


def chips(df, closes):
    f = freshness(df)
    c = [f"<span class='chip'>Prices as of {f['latest']:%d %b %Y}</span>" if f["latest"] else "<span class='chip'>No prices cached</span>",
         f"<span class='chip' style='color:{AMBER}'>{f['stale']} stale</span>" if f["stale"] else "",
         f"<span class='chip' style='color:{AMBER}'>{f['missing']} missing</span>" if f["missing"] else "",
         "<span class='chip'>History loaded</span>" if closes is not None else "<span class='chip'>History not loaded</span>",
         "<span class='chip'>Yahoo Finance · delayed / end-of-day</span>"]
    st.markdown("".join(c), unsafe_allow_html=True)


def bench_section(df, closes, key, label):
    section("Performance vs benchmarks")
    series = value_series(df[df.priced], closes) if len(df) else pd.Series(dtype=float)
    if series.empty:
        st.info("Click **Refresh market data** to load 1-year history for this portfolio and the indices."); return None
    a, b = st.columns([3, 2])
    picks = a.multiselect("Benchmarks", list(BENCHMARKS), DEFAULT_BENCH, key=f"bm_{key}")
    per = b.radio("Period", list(PERIODS), index=3, horizontal=True, key=f"per_{key}")
    fig, tbl, missing = bench_fig(series, closes, picks, PERIODS[per], label)
    st.plotly_chart(fig, use_container_width=True)
    for m in missing: st.caption(f"No data returned for {m}.")
    st.dataframe(tbl.style.format("{:+.2f}%", na_rep="—").map(tone_css), use_container_width=True)
    st.caption("The portfolio line values TODAY's holdings at past closing prices (hypothetical: ignores past trades, additions and dividends). "
               "Benchmarks are price indices. Not a record of actual client returns.")
    return tbl


# ============================================================ pages
def login_page():
    st.markdown("<div style='height:8vh'></div>", unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown("<div class='brand'>Stack<span>Wealth</span> Dashboard</div><div class='tag'>Multi-client Indian equity portfolio monitoring</div>", unsafe_allow_html=True)
        with st.form("login"):
            u = st.text_input("Username"); p = st.text_input("Password", type="password")
            go_ = st.form_submit_button("Sign in", type="primary", use_container_width=True)
        if go_:
            ok, err = login_ok(u, p)
            if ok:
                st.session_state.clear(); st.session_state.update(sw_auth=True, sw_user=u, sw_login_at=time.time()); st.rerun()
            elif err == "config": st.error("Sign-in is not configured correctly. Check the [auth] section of your Streamlit secrets.")
            else: st.error("Invalid username or password.")


def home_page():
    with st.sidebar:
        st.markdown("<div class='brand' style='font-size:1.2rem;text-align:left'>Stack<span>Wealth</span> Dashboard</div>", unsafe_allow_html=True)
        present = st.toggle("Presentation mode", help="Hides admin controls.")
        st.page_link("pages/Client_Details.py", label="Client details & insights")
        if st.button("Sign out", use_container_width=True): st.session_state.clear(); st.rerun()
    df = load_positions()
    closes = st.session_state.get("closes")

    if not present:
        a, b = st.columns([4, 1])
        with b:
            if st.button("Refresh market data", type="primary", use_container_width=True,
                         help="Prices, sectors, market caps, 1-year history and benchmarks (Yahoo Finance, delayed / end-of-day)."):
                refresh_ui()
        with a:
            if st.session_state.get("refresh_msg"): st.caption(st.session_state["refresh_msg"])
    if df.empty:
        st.markdown("<div class='hero'><div class='app'>Stack<span>Wealth</span> Dashboard</div></div>", unsafe_allow_html=True)
        st.info("No holdings yet. Add clients and holdings on the Client Details page."); return
    chips(df, closes)

    t = totals(df)
    series = value_series(df[df.priced], closes) if t["n_priced"] else pd.Series(dtype=float)
    day = float((series.iloc[-1] / series.iloc[-2] - 1) * 100) if len(series) > 1 and series.iloc[-2] else None
    day_abs = float(series.iloc[-1] - series.iloc[-2]) if day is not None else None
    colr = tone(day if day is not None else (t["pnl"] if t["n_priced"] else None))
    big = fmt_inr(t["value"]) if t["n_priced"] else fmt_inr(t["invested"])
    arrow = "" if day is None else ("▲ " if day >= 0 else "▼ ")
    sub = (f"{arrow}{pct(day)} ({fmt_inr(day_abs, sign=True)}) last session &nbsp;·&nbsp; " if day is not None else "") + \
          (f"Unrealized P&L {fmt_inr(t['pnl'], sign=True)} ({pct(t['ret'])})" if t["n_priced"] else "Prices not loaded - showing invested capital")
    st.markdown(f"<div class='hero'><div class='app'>Stack<span>Wealth</span> Dashboard</div>"
                f"<div class='lbl'>{'Total portfolio value' if t['n_priced'] else 'Total invested capital'} · {fmt_cr(t['value'] if t['n_priced'] else t['invested'])}</div>"
                f"<div class='big' style='color:{colr if t['n_priced'] else TEXT}'>{big}</div><div class='sub'>{sub}</div></div>", unsafe_allow_html=True)
    if t["n_priced"] < t["n"]:
        cov = t["invested_priced"] / t["invested"] * 100 if t["invested"] else 0
        st.markdown(f"<div class='warn'>{t['n'] - t['n_priced']} of {t['n']} positions have no price and are excluded from value and P&L "
                    f"(priced coverage: {cov:.0f}% of invested capital). Click Refresh market data.</div>", unsafe_allow_html=True)

    ct = client_table(df)
    pr_ct = ct.dropna(subset=["Return %"])
    pos = df[df.priced].groupby("Symbol").agg(PnL=("PnL", "sum"), Inv=("Invested", "sum"))
    pos["Ret"] = pos.PnL / pos.Inv * 100
    basis = "Value" if t["n_priced"] else "Invested"
    lab = "market value" if basis == "Value" else "invested capital"
    top5, hhi = concentration(df, basis)

    k = st.columns(5)
    for col, c in zip(k, [("Invested capital", fmt_inr(t["invested"]), "all holdings", TEXT),
                          ("Market value", fmt_inr(t["value"]) if t["n_priced"] else "—", "priced holdings", TEXT),
                          ("Unrealized P&L", fmt_inr(t["pnl"], sign=True) if t["n_priced"] else "—", pct(t["ret"]) + " on priced capital", tone(t["pnl"] if t["n_priced"] else None)),
                          ("Active clients", str(t["clients"]), "", TEXT), ("Positions", str(t["n"]), f"{t['n_priced']} priced", TEXT)]):
        col.markdown(card(*c), unsafe_allow_html=True)
    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    k2 = st.columns(5)
    best = pr_ct.loc[pr_ct["Return %"].idxmax()] if len(pr_ct) else None
    worst = pr_ct.loc[pr_ct["Return %"].idxmin()] if len(pr_ct) else None
    ty = period_return(series, 365) if len(series) else None
    nifty = closes["^NSEI"].dropna() if closes is not None and "^NSEI" in closes.columns else None
    ny = period_return(nifty, 365) if nifty is not None else None
    for col, c in zip(k2, [
        ("Best client", best.Client if best is not None else "—", pct(best["Return %"]) if best is not None else "", GREEN),
        ("Weakest client", worst.Client if worst is not None else "—", pct(worst["Return %"]) if worst is not None else "", RED if worst is not None and worst["Return %"] < 0 else TEXT),
        ("Top gainer", pos.PnL.idxmax() if len(pos) else "—", fmt_inr(pos.PnL.max(), sign=True) if len(pos) else "", tone(pos.PnL.max() if len(pos) else None)),
        ("Top loser", pos.PnL.idxmin() if len(pos) else "—", fmt_inr(pos.PnL.min(), sign=True) if len(pos) else "", tone(pos.PnL.min() if len(pos) else None)),
        ("1Y vs NIFTY 50", pct(ty - ny) if ty is not None and ny is not None else "—", "hypothetical, current holdings" if ty is not None else "load market data", tone(ty - ny if ty is not None and ny is not None else None))]):
        col.markdown(card(*c), unsafe_allow_html=True)

    section("Where client money is invested")
    a, b = st.columns([2, 3])
    with a:
        show(donut_fig(df, "Symbol", basis, f"Allocation by stock ({lab})", 460))
    with b:
        show(treemap_fig(df, 460))
    c1, c2, c3 = st.columns(3)
    with c1: show(donut_fig(df, "Sector", basis, f"By sector ({lab})", 340))
    with c2: show(donut_fig(df, "Cap", basis, f"By market cap ({lab})", 340))
    with c3:
        show(gauge_fig(top5), "Load prices to see concentration.")
        if hhi is not None: st.caption(f"HHI {hhi:,.0f} (below 1,500 diversified · above 2,500 concentrated)")
    if (df.Sector == "Unclassified").any():
        st.caption(f"'Unclassified' = sector / market cap not available yet (loaded by Refresh market data). Cap buckets use approximate AMFI cut-offs: large ≥ ₹{LARGE_CAP_MIN_CR:,} cr, mid ≥ ₹{MID_CAP_MIN_CR:,} cr.")

    bench_section(df, closes, "home", "All clients (combined)")

    section("Winners and losers")
    w, l = st.columns(2)
    fmt = {"PnL": "₹{:+,.0f}", "Ret": "{:+.2f}%", "Inv": "₹{:,.0f}"}
    with w:
        st.markdown("**Top 5 gainers**")
        g_ = pos[pos.PnL > 0].nlargest(5, "PnL")
        st.dataframe(g_.style.format(fmt).map(tone_css, subset=["PnL", "Ret"]), use_container_width=True) if len(g_) else st.caption("No gainers.")
    with l:
        st.markdown("**Top 5 losers**")
        l_ = pos[pos.PnL < 0].nsmallest(5, "PnL")
        st.dataframe(l_.style.format(fmt).map(tone_css, subset=["PnL", "Ret"]), use_container_width=True) if len(l_) else st.caption("No losers.")

    section("Clients — click a row to open details")
    q = st.text_input("Search clients", label_visibility="collapsed", placeholder="Search clients")
    g = ct[ct.Client.str.contains(q, case=False, regex=False)] if q else ct
    g = g.reset_index(drop=True)
    styled = g.drop(columns="client_id").style.map(tone_css, subset=["P&L (priced)", "Return %"]).format(
        {"Invested": "₹{:,.0f}", "Market value": "₹{:,.0f}", "P&L (priced)": "₹{:+,.0f}", "Return %": "{:+.2f}%"}, na_rep="—")
    ev = st.dataframe(styled, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row", key="client_table")
    if ev.selection.rows:
        st.session_state["open_client"] = g.iloc[ev.selection.rows[0]]["client_id"]
        st.switch_page("pages/Client_Details.py")
    x1, x2 = st.columns(2)
    with x1: show(bars_fig(ct), "Load prices to compare clients.")
    with x2: show(pnl_bar_fig(ct.Client, ct["P&L (priced)"], "Unrealized P&L by client"), "Load prices to see P&L.")

    if t["n_priced"] < t["n"]:
        section("Data quality — positions without a price")
        st.dataframe(df[~df.priced][["Client", "Symbol", "Qty", "Invested"]], hide_index=True, use_container_width=True)
    if not present:
        st.download_button("Download portfolio report (Excel)", build_report(ct, df), "StackWealth_Dashboard_Report.xlsx")


def main():
    st.set_page_config(page_title=APP_NAME, page_icon="📈", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    if not authed():
        st.session_state.pop("sw_auth", None); return login_page()
    try:
        home_page()
    except RuntimeError as e:
        st.error(str(e))
    except Exception:
        log.exception("home page error"); st.error("Something went wrong. See the app logs (Manage app > Logs) for details.")


if __name__ == "__main__":
    main()
