"""Streamlit pages. Business logic lives in portfolio/repositories/market_data/exports."""
from __future__ import annotations
import logging
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import components as ui
import exports, market_data, repositories as repo
from auth import require_auth
from database import DatabaseUnavailable, connect, get_database_url
from migrations.runner import SchemaError, migrate, validate_schema
from portfolio import concentration, fmt_inr, fmt_pct, group_summary, position_metrics, summarize

log = logging.getLogger("stackwealth.views")
TEMPLATE = "plotly_dark"


def _secrets():
    try:
        return dict(st.secrets)
    except Exception:
        return {}


def _url() -> str:
    return get_database_url(_secrets())


def actor() -> str:
    return st.session_state.get("sw_user", "admin")


@st.cache_resource(show_spinner=False)
def _ensure_schema(url: str):
    with connect(url) as conn:
        migrate(conn)
        return validate_schema(conn)


def route(page: str, presentation: bool):
    require_auth(st.session_state)
    try:
        problems = _ensure_schema(_url())
        if problems:
            st.error("Database schema problems: " + "; ".join(problems)); return
        {"Dashboard": dashboard, "Client Portfolio": client_portfolio, "Clients": clients_page,
         "Import / Export": import_export, "Audit & Diagnostics": diagnostics}[page](presentation)
    except DatabaseUnavailable as e:
        st.error(str(e))
    except SchemaError as e:
        st.error(f"Schema check failed: {e}")
    except (ValueError, repo.NotFound) as e:
        st.error(str(e))
    except Exception:
        log.exception("unhandled error on page %s", page)
        st.error("Something went wrong. Details were written to the server log (Streamlit Cloud: Manage app > Logs).")


def _positions(client_id=None):
    with connect(_url()) as conn:
        return repo.list_positions(conn, client_id)


def refresh_button(presentation: bool):
    if presentation:
        return
    if st.sidebar.button("Refresh Prices", use_container_width=True, help="Fetches latest available prices from Yahoo Finance."):
        require_auth(st.session_state)
        with st.spinner("Fetching prices…"):
            with connect(_url()) as conn:
                syms = repo.active_symbols(conn)
            res = market_data.fetch_prices(syms)
            with connect(_url()) as conn:
                repo.save_prices(conn, res.quotes)
                repo.audit(conn, actor(), "prices.refresh", "price_cache", None,
                           {**res.summary(), "failed_symbols": list(res.failed)[:50], "no_data_symbols": res.no_data[:50]})
        s = res.summary()
        st.sidebar.success(f"Refreshed {s['refreshed']}, failed {s['failed']}, no data {s['no_data']}.") if not (s["failed"] or s["no_data"]) \
            else st.sidebar.warning(f"Refreshed {s['refreshed']}, failed {s['failed']}, no data {s['no_data']}. Previous prices were kept.")


def _freshness_line(pos):
    dates = [p["price_date"] for p in pos if p.get("price_date")]
    ts = [p["retrieved_at"] for p in pos if p.get("retrieved_at")]
    if not dates:
        st.caption("No cached prices yet. Use Refresh Prices. Data comes from Yahoo Finance and may be delayed or end-of-day.")
        return
    stale = sum(1 for p in pos if market_data.freshness(p.get("price_date")) == "Stale")
    st.caption(f"Latest provider price date: {max(dates):%d %b %Y} · last retrieval: {max(ts):%d %b %Y %H:%M} · "
               f"{stale} stale · prices are delayed/end-of-day, not real-time (Yahoo Finance).")


def _charts(pos, by_client=True):
    enriched = []
    for p in pos:
        m = position_metrics(p["quantity"], p["avg_price"], p.get("price"))
        enriched.append({"Client": p["client_name"], "Symbol": market_data.display_symbol(p["symbol"]),
                         **{k: (float(v) if v is not None else None) for k, v in m.items() if k != "priced"}, "priced": m["priced"]})
    df = pd.DataFrame(enriched)
    priced = df[df.priced]
    if not priced.empty:
        st_df = priced.groupby("Symbol", as_index=False).agg(mv=("market_value", "sum"), pnl=("pnl", "sum"), invested=("invested", "sum"))
        st_df["share"] = st_df.mv / st_df.mv.sum() * 100
        fig = px.treemap(st_df, path=["Symbol"], values="mv", color="Symbol", custom_data=["share", "pnl"],
                         color_discrete_sequence=px.colors.qualitative.Safe, template=TEMPLATE, title="Allocation by stock (market value)")
        fig.update_traces(texttemplate="%{label}<br>%{percentRoot:.1%}", hovertemplate="%{label}<br>Value ₹%{value:,.0f}<br>Share %{customdata[0]:.2f}%<br>P&L ₹%{customdata[1]:,.0f}<extra></extra>")
        fig.update_layout(margin=dict(t=40, l=0, r=0, b=0), height=420)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No priced positions yet - refresh prices to see allocation.")
    return df, priced


def _pnl_bar(frame, x, title):
    colors = [ui.EMERALD if v >= 0 else ui.RED for v in frame["pnl"]]
    fig = go.Figure(go.Bar(x=frame[x], y=frame["pnl"], marker_color=colors, hovertemplate="%{x}<br>₹%{y:,.0f}<extra></extra>"))
    fig.update_layout(template=TEMPLATE, title=title, height=340, margin=dict(t=40, l=0, r=0, b=0))
    st.plotly_chart(fig, use_container_width=True)


def dashboard(presentation: bool):
    st.title("StackWealth Dashboard")
    refresh_button(presentation)
    pos = _positions()
    st.caption("Scope: all active clients" + (" · presentation mode" if presentation else ""))
    if not pos:
        st.info("No holdings yet. Add a client under Clients, then add holdings in Client Portfolio."); return
    s = summarize(pos)
    ui.kpis(s, {"Active clients": str(len({p['client_id'] for p in pos})), "Positions (client-stock)": str(s["positions"])})
    _freshness_line(pos)
    df, priced = _charts(pos)
    names = {p["client_id"]: p["client_name"] for p in pos}
    cs = group_summary(pos, "client_id")
    ct = pd.DataFrame([{"Client": names[k], "Invested (all)": float(v["invested_total"]), "Invested (priced)": float(v["invested_priced"]),
                        "Market value": float(v["market_value"]), "pnl": float(v["pnl"]),
                        "Return %": None if v["return_pct"] is None else round(float(v["return_pct"]), 2),
                        "Holdings": v["positions"], "Price coverage %": round(float(v["coverage_pct"]), 0)} for k, v in cs.items()])
    st.subheader("Clients")
    q = st.text_input("Search clients", key="dash_search")
    view = ct[ct.Client.str.contains(q, case=False, regex=False)] if q else ct
    st.dataframe(view.rename(columns={"pnl": "Unrealized P&L"}), use_container_width=True, hide_index=True)
    if not presentation:
        sel = st.selectbox("Open client portfolio", [""] + list(ct.Client), key="dash_open")
        if sel:
            st.session_state["open_client"] = next(k for k, v in names.items() if v == sel)
            st.info("Select 'Client Portfolio' in the sidebar to view this client.")
    c1, c2 = st.columns(2)
    with c1:
        f = go.Figure([go.Bar(name="Invested (priced)", x=ct.Client, y=ct["Invested (priced)"], marker_color="#64748b"),
                       go.Bar(name="Market value", x=ct.Client, y=ct["Market value"], marker_color=ui.EMERALD)])
        f.update_layout(template=TEMPLATE, barmode="group", title="Invested vs current value (priced positions)", height=340, margin=dict(t=40, l=0, r=0, b=0))
        st.plotly_chart(f, use_container_width=True)
    with c2:
        _pnl_bar(ct, "Client", "Unrealized P&L by client (priced positions)")
    rt = ct.dropna(subset=["Return %"])
    if not rt.empty:
        f = go.Figure(go.Bar(x=rt.Client, y=rt["Return %"], marker_color=[ui.EMERALD if v >= 0 else ui.RED for v in rt["Return %"]]))
        f.update_layout(template=TEMPLATE, title="Return % by client (priced capital only)", height=300, margin=dict(t=40, l=0, r=0, b=0))
        st.plotly_chart(f, use_container_width=True)
    st.subheader("Insights")
    if not priced.empty:
        t1, t2, t3 = st.tabs(["Largest holdings", "Top gainers / losers", "Concentration"])
        agg = priced.groupby("Symbol", as_index=False).agg(market_value=("market_value", "sum"), pnl=("pnl", "sum"))
        t1.dataframe(agg.nlargest(10, "market_value"), hide_index=True, use_container_width=True)
        t2.dataframe(priced.nlargest(5, "pnl")[["Client", "Symbol", "pnl"]], hide_index=True, use_container_width=True)
        t2.dataframe(priced.nsmallest(5, "pnl")[["Client", "Symbol", "pnl"]], hide_index=True, use_container_width=True)
        c = concentration(pos)
        t3.metric("Top-5 stocks share of market value", fmt_pct(c["top_share_pct"], sign=False))
        t3.metric("HHI (0-10,000)", f"{c['hhi']:.0f}" if c["hhi"] is not None else "—")
    miss = df[~df.priced]
    if not miss.empty:
        st.warning("Positions with missing prices")
        st.dataframe(miss[["Client", "Symbol", "invested"]].rename(columns={"invested": "Invested"}), hide_index=True, use_container_width=True)


def _pick_client(presentation):
    with connect(_url()) as conn:
        cl = repo.list_clients(conn, "active")
    if not cl:
        st.info("No active clients."); return None
    ids = [c["id"] for c in cl]
    idx = ids.index(st.session_state["open_client"]) if st.session_state.get("open_client") in ids else 0
    sel = st.selectbox("Client", cl, index=idx, format_func=lambda c: c["name"])
    st.session_state["open_client"] = sel["id"]
    with connect(_url()) as conn:
        return repo.get_client(conn, sel["id"])  # server-side validation


def client_portfolio(presentation: bool):
    c = _pick_client(presentation)
    if not c:
        return
    st.title(f"{c['name']} · StackWealth Dashboard")
    refresh_button(presentation)
    pos = _positions(c["id"])
    st.caption(f"Scope: {c['name']} · status: {c['status']}" + ("" if presentation else
               f" · {c.get('email') or 'no email'} · {c.get('phone') or 'no phone'}"))
    if pos:
        s = summarize(pos); ui.kpis(s, {"Positions": str(s["positions"])}); _freshness_line(pos)
        df, priced = _charts(pos)
        if not priced.empty:
            _pnl_bar(priced.groupby("Symbol", as_index=False).agg(pnl=("pnl", "sum")), "Symbol", "Unrealized P&L by stock")
        hf = exports.holdings_frame(pos)
        q = st.text_input("Search symbol", key="hold_q")
        if q: hf = hf[hf.Symbol.str.contains(q.upper(), regex=False)]
        sort = st.selectbox("Sort by", ["Symbol", "Market Value (INR)", "Unrealized P&L (INR)", "Return %", "Invested Capital (INR)"])
        st.dataframe(hf.sort_values(sort, ascending=sort == "Symbol"), hide_index=True, use_container_width=True)
        st.download_button("Download holdings (Excel)", exports.to_excel_bytes({"Holdings": exports.holdings_frame(pos)}),
                           f"StackWealth_Dashboard_{c['name']}.xlsx")
    else:
        st.info("This client has no holdings yet.")
    if presentation:
        return
    st.subheader("Manage holdings")
    with st.form("holding_form", clear_on_submit=True):
        a, b, d = st.columns(3)
        sym = a.text_input("Symbol (e.g. RELIANCE, BSE:500325)")
        qty = b.number_input("Quantity", min_value=0.0, step=1.0, format="%.4f")
        avg = d.number_input("Average purchase price (₹)", min_value=0.0, step=0.05, format="%.4f")
        go_ = st.form_submit_button("Add / update holding", type="primary")
    if go_:
        require_auth(st.session_state)
        with connect(_url()) as conn:
            existed = repo.holding_exists(conn, c["id"], sym)
            out = repo.upsert_holding(conn, actor(), c["id"], sym, str(qty), str(avg))
        st.success(f"{'Updated existing' if existed else 'Added new'} holding ({out}).")
    if pos:
        target = st.selectbox("Remove holding", [""] + [p["symbol"] for p in pos])
        if target:
            sure = st.checkbox(f"Confirm removal of {market_data.display_symbol(target)} from {c['name']}")
            if st.button("Remove holding", disabled=not sure):
                require_auth(st.session_state)
                with connect(_url()) as conn:
                    repo.remove_holding(conn, actor(), c["id"], target)
                st.success("Holding removed."); st.rerun()


def clients_page(presentation: bool):
    st.title("Clients")
    tab_a, tab_r, tab_n = st.tabs(["Active", "Archived", "New client"])
    for tab, status in ((tab_a, "active"), (tab_r, "archived")):
        with tab:
            q = st.text_input("Search", key=f"cq_{status}")
            with connect(_url()) as conn:
                cl = repo.list_clients(conn, status, q)
            if not cl:
                st.info(f"No {status} clients."); continue
            for c in cl:
                with st.container(border=True):
                    st.markdown(f"**{c['name']}**")
                    with st.form(f"edit_{c['id']}"):
                        n = st.text_input("Name", c["name"]); e = st.text_input("Email", c["email"] or "")
                        p = st.text_input("Phone", c["phone"] or ""); no = st.text_area("Notes", c["notes"] or "")
                        save = st.form_submit_button("Save changes")
                    if save:
                        with connect(_url()) as conn: repo.update_client(conn, actor(), c["id"], name=n, email=e, phone=p, notes=no)
                        st.success("Client updated."); st.rerun()
                    lbl = "Archive" if status == "active" else "Reactivate"
                    if st.button(lbl, key=f"st_{c['id']}"):
                        with connect(_url()) as conn:
                            repo.set_client_status(conn, actor(), c["id"], "archived" if status == "active" else "active")
                        st.rerun()
    with tab_n:
        with st.form("new_client", clear_on_submit=True):
            n = st.text_input("Name"); e = st.text_input("Email (optional)"); p = st.text_input("Phone (optional)"); no = st.text_area("Notes (optional)")
            if st.form_submit_button("Create client", type="primary"):
                with connect(_url()) as conn: repo.create_client(conn, actor(), name=n, email=e, phone=p, notes=no)
                st.success("Client created.")


def import_export(presentation: bool):
    require_auth(st.session_state)
    st.title("Import / Export")
    pos = _positions()
    st.subheader("Export")
    if pos:
        miss = [p for p in pos if p.get("price") is None]
        sheets = {"Client Summary": exports.client_summary_frame(pos), "Holdings": exports.holdings_frame(pos),
                  "Missing Prices": exports.holdings_frame(miss)}
        st.download_button("Portfolio report (Excel)", exports.to_excel_bytes(sheets), "StackWealth_Dashboard_Portfolio_Report.xlsx")
        st.download_button("Client summary (CSV)", exports.to_csv_bytes(sheets["Client Summary"]), "StackWealth_Dashboard_Client_Summary.csv")
        st.download_button("Holdings (CSV)", exports.to_csv_bytes(sheets["Holdings"]), "StackWealth_Dashboard_Holdings.csv")
    else:
        st.info("Nothing to export yet.")
    st.subheader("Bulk holdings import")
    st.caption("Clients are matched by exact name among active clients; unknown clients are rejected, never created. "
               "Existing client-symbol positions are updated (quantity and average price replaced). Any invalid row rejects the whole file.")
    st.download_button("Download import template (CSV)", exports.import_template_bytes(), "StackWealth_Dashboard_Import_Template.csv")
    up = st.file_uploader("Upload CSV or Excel", type=["csv", "xlsx"])
    if up:
        try:
            df = pd.read_csv(up) if up.name.lower().endswith(".csv") else pd.read_excel(up)
        except Exception:
            st.error("Could not read the file. Use the template format."); return
        if len(df) > 5000:
            st.error("File too large (max 5,000 rows)."); return
        with connect(_url()) as conn:
            clients = repo.list_clients(conn, "active")
        valid, errors = exports.validate_import(df, clients)
        if errors:
            st.error(f"{len(errors)} row error(s). Nothing was imported."); st.dataframe(pd.DataFrame(errors), hide_index=True)
        else:
            st.success(f"{len(valid)} valid row(s).")
            st.dataframe(pd.DataFrame(valid).drop(columns=["client_id"]), hide_index=True)
            if st.button("Commit import", type="primary"):
                with connect(_url()) as conn:
                    r = repo.import_holdings(conn, actor(), valid)
                st.success(f"Import complete: {r['created']} created, {r['updated']} updated.")


def diagnostics(presentation: bool):
    require_auth(st.session_state)
    st.title("Audit & Diagnostics")
    from auth import is_valid_bcrypt_hash, load_auth_config
    cfg = load_auth_config(_secrets())
    st.write({"Auth configured": cfg is not None, "Password hash valid bcrypt": bool(cfg and is_valid_bcrypt_hash(cfg.password_hash))})
    with connect(_url()) as conn:
        st.write({"Schema problems": validate_schema(conn) or "none"})
        st.dataframe(pd.DataFrame(repo.recent_audit(conn)), hide_index=True, use_container_width=True)
