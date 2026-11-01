"""StackWealth Dashboard - Client Details & Insights (single page file)."""
import io, json, logging, re
import pandas as pd
import streamlit as st
from app import (APP_NAME, CSS, GREEN, RED, TEXT, POS_SQL, authed, bars_fig, bench_section, card, chips, concentration, db, donut_fig,
                 enrich, fmt_cr, fmt_inr, gauge_fig, load_positions, normalize_symbol, pct, pnl_bar_fig, refresh_ui, section, short,
                 show, tone, tone_css, totals, treemap_fig, value_series, period_return)

log = logging.getLogger("stackwealth.client")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE = re.compile(r"^\+?[0-9][0-9 \-()]{6,18}$")


def audit(conn, action, entity_id, details):
    conn.execute("INSERT INTO sw_audit_log(actor,action,entity,entity_id,details) VALUES (%s,%s,'client',%s,%s::jsonb)",
                 (st.session_state.get("sw_user", "admin"), action, str(entity_id), json.dumps(details, default=str)))


def clean_contact(name, email, phone, notes):
    name = (name or "").strip()
    if not 1 <= len(name) <= 120: raise ValueError("Client name is required (max 120 characters).")
    if (email or "").strip() and not EMAIL.match(email.strip()): raise ValueError("Email looks invalid.")
    if (phone or "").strip() and not PHONE.match(phone.strip()): raise ValueError("Phone looks invalid.")
    return name, (email or "").strip() or None, (phone or "").strip() or None, (notes or "").strip() or None


def positions_for(client_id) -> pd.DataFrame:
    q = POS_SQL.replace("WHERE c.status='active'", "WHERE c.id=%s")
    with db() as conn:
        cur = conn.execute(q, (client_id,))
        cols = [d.name for d in cur.description]
        return enrich([dict(zip(cols, r)) for r in cur.fetchall()])


def page():
    with st.sidebar:
        st.markdown("<div class='brand' style='font-size:1.2rem;text-align:left'>Stack<span>Wealth</span> Dashboard</div>", unsafe_allow_html=True)
        present = st.toggle("Presentation mode", help="Hides editing controls and contact details.")
        st.page_link("app.py", label="← Home dashboard")
    with db() as conn:
        clients = conn.execute("SELECT id, name, email, phone, notes, status FROM clients ORDER BY status, name").fetchall()
    names = ["Overview"] + ([] if present else ["Holdings", "Client details", "New client"])
    tabs = st.tabs(names)
    if not clients:
        with tabs[0]: st.info("No clients yet.")
        if not present:
            with tabs[-1]: new_client_form()
        return
    ids = [c[0] for c in clients]
    idx = ids.index(st.session_state["open_client"]) if st.session_state.get("open_client") in ids else 0
    sel = st.selectbox("Client", clients, index=idx, format_func=lambda c: c[1] + ("  (archived)" if c[5] == "archived" else ""))
    st.session_state["open_client"] = sel[0]
    with db() as conn:   # re-validate selection server-side
        row = conn.execute("SELECT id, name, email, phone, notes, status FROM clients WHERE id=%s", (sel[0],)).fetchone()
    if not row: st.error("Client not found."); return
    cid, name, email, phone, notes, status = row
    df = positions_for(cid)
    with tabs[0]: overview(df, cid, name, status, email, phone, present)
    if not present:
        with tabs[1]: holdings_tab(cid, name, status, df)
        with tabs[2]: details_tab(cid, name, email, phone, notes, status)
        with tabs[3]: new_client_form()


def overview(df, cid, name, status, email, phone, present):
    closes = st.session_state.get("closes")
    if not present:
        a, b = st.columns([4, 1])
        with b:
            if st.button("Refresh market data", type="primary", use_container_width=True): refresh_ui()
        with a:
            if st.session_state.get("refresh_msg"): st.caption(st.session_state["refresh_msg"])
    contact = "" if present else f" · {email or 'no email'} · {phone or 'no phone'}"
    if df.empty:
        st.markdown(f"<div class='hero'><div class='app'>Stack<span>Wealth</span> Dashboard · Client</div><div class='lbl'>{name} · {status}{contact}</div></div>", unsafe_allow_html=True)
        st.info("This client has no holdings yet."); return
    chips(df, closes)
    t = totals(df)
    series = value_series(df[df.priced], closes) if t["n_priced"] else pd.Series(dtype=float)
    day = float((series.iloc[-1] / series.iloc[-2] - 1) * 100) if len(series) > 1 and series.iloc[-2] else None
    colr = tone(day if day is not None else (t["pnl"] if t["n_priced"] else None))
    big = fmt_inr(t["value"]) if t["n_priced"] else fmt_inr(t["invested"])
    sub = (f"{'▲' if day >= 0 else '▼'} {pct(day)} last session &nbsp;·&nbsp; " if day is not None else "") + \
          (f"Unrealized P&L {fmt_inr(t['pnl'], sign=True)} ({pct(t['ret'])})" if t["n_priced"] else "Prices not loaded - showing invested capital")
    st.markdown(f"<div class='hero'><div class='app'>Stack<span>Wealth</span> Dashboard · Client</div>"
                f"<div class='lbl'>{name} · {status}{contact}</div><div class='lbl'>{'Portfolio value' if t['n_priced'] else 'Invested capital'} · {fmt_cr(t['value'] if t['n_priced'] else t['invested'])}</div>"
                f"<div class='big' style='color:{colr if t['n_priced'] else TEXT}'>{big}</div><div class='sub'>{sub}</div></div>", unsafe_allow_html=True)
    if t["n_priced"] < t["n"]:
        st.markdown(f"<div class='warn'>{t['n'] - t['n_priced']} position(s) have no price and are excluded from value and P&L.</div>", unsafe_allow_html=True)

    allp = load_positions()
    tot_all = totals(allp)["value"] if not allp.empty else 0
    share = (t["value"] / tot_all * 100) if tot_all and t["n_priced"] else None
    basis = "Value" if t["n_priced"] else "Invested"
    top5, hhi = concentration(df, basis)
    pr = df[df.priced]
    bestp = pr.loc[pr.Ret.idxmax()] if len(pr) and pr.Ret.notna().any() else None
    worstp = pr.loc[pr.Ret.idxmin()] if len(pr) and pr.Ret.notna().any() else None
    k = st.columns(4)
    for col, c in zip(k, [("Invested capital", fmt_inr(t["invested"]), "all holdings", TEXT),
                          ("Market value", fmt_inr(t["value"]) if t["n_priced"] else "—", "priced holdings", TEXT),
                          ("Unrealized P&L", fmt_inr(t["pnl"], sign=True) if t["n_priced"] else "—", pct(t["ret"]), tone(t["pnl"] if t["n_priced"] else None)),
                          ("Positions", str(t["n"]), f"{t['n_priced']} priced", TEXT)]):
        col.markdown(card(*c), unsafe_allow_html=True)
    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    k2 = st.columns(4)
    for col, c in zip(k2, [("Share of all-client value", f"{share:.1f}%" if share is not None else "—", "of the combined portfolio", TEXT),
                           ("Top-5 concentration", f"{top5:.1f}%" if top5 is not None else "—", f"HHI {hhi:,.0f}" if hhi else "", TEXT),
                           ("Best position", bestp.Symbol if bestp is not None else "—", pct(bestp.Ret) if bestp is not None else "", GREEN),
                           ("Weakest position", worstp.Symbol if worstp is not None else "—", pct(worstp.Ret) if worstp is not None else "", tone(worstp.Ret) if worstp is not None else TEXT)]):
        col.markdown(card(*c), unsafe_allow_html=True)

    section("Allocation")
    a, b = st.columns([2, 3])
    with a: show(donut_fig(df, "Symbol", basis, "By stock", 440))
    with b: show(treemap_fig(df, 440))
    c1, c2, c3 = st.columns(3)
    with c1: show(donut_fig(df, "Sector", basis, "By sector", 340))
    with c2: show(donut_fig(df, "Cap", basis, "By market cap", 340))
    with c3: show(pnl_bar_fig(pr.Symbol, pr.PnL, "Unrealized P&L by stock"), "Load prices to see P&L.")

    bench_section(df, closes, f"c{cid}", name)

    section("Holdings")
    q = st.text_input("Search symbol", placeholder="Search symbol", label_visibility="collapsed")
    tbl = df.copy()
    if q: tbl = tbl[tbl.Symbol.str.contains(q.upper(), regex=False)]
    show_ = tbl.assign(Weight=tbl[basis] / tbl[basis].sum() * 100 if tbl[basis].sum() else None)[
        ["Symbol", "Qty", "Avg", "Invested", "Price", "Value", "PnL", "Ret", "Weight", "price_date", "Sector", "Cap"]].rename(
        columns={"Avg": "Avg price", "PnL": "Unrealized P&L", "Ret": "Return %", "price_date": "Price date", "Weight": "Weight %"})
    st.dataframe(show_.style.format({"Qty": "{:,.2f}", "Avg price": "₹{:,.2f}", "Invested": "₹{:,.0f}", "Price": "₹{:,.2f}", "Value": "₹{:,.0f}",
                 "Unrealized P&L": "₹{:+,.0f}", "Return %": "{:+.2f}%", "Weight %": "{:.1f}%"}, na_rep="—").map(tone_css, subset=["Unrealized P&L", "Return %"]),
                 hide_index=True, use_container_width=True)
    st.caption("Prices: Yahoo Finance, delayed / end-of-day. Missing price shows —. Unrealized P&L only.")
    if not present:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw: show_.to_excel(xw, sheet_name="Holdings", index=False)
        st.download_button("Download holdings (Excel)", buf.getvalue(), f"StackWealth_Dashboard_{re.sub(r'[^A-Za-z0-9]+', '_', name)}.xlsx")


def holdings_tab(cid, name, status, df):
    if status != "active": st.warning("Archived client - reactivate to edit holdings."); return
    with st.form("hold", clear_on_submit=True):
        a, b, c = st.columns(3)
        sym = a.text_input("Symbol (e.g. RELIANCE, BSE:500325)")
        qty = b.number_input("Quantity", min_value=0.0, step=1.0, format="%.4f")
        avg = c.number_input("Average purchase price (₹)", min_value=0.0, step=0.05, format="%.4f")
        ok = st.form_submit_button("Add / update holding", type="primary")
    if ok:
        try:
            s = normalize_symbol(sym)
            if qty <= 0: raise ValueError("Quantity must be greater than zero.")
            with db() as conn:
                created = conn.execute("""INSERT INTO current_holdings(client_id,symbol,quantity,avg_price) VALUES (%s,%s,%s,%s)
                    ON CONFLICT (client_id,symbol) DO UPDATE SET quantity=EXCLUDED.quantity, avg_price=EXCLUDED.avg_price, updated_at=now()
                    RETURNING (xmax = 0)""", (cid, s, str(qty), str(avg))).fetchone()[0]
                audit(conn, "holding.created" if created else "holding.updated", cid, {"symbol": s, "quantity": qty, "avg_price": avg})
            st.success(("Added " if created else "Updated existing holding ") + short(s) + ". Click Refresh market data on the Overview tab to price it."); st.rerun()
        except ValueError as e: st.error(str(e))
    if not df.empty:
        t = st.selectbox("Remove holding", [""] + list(df.sym), format_func=lambda s: short(s) if s else "Select…")
        if t and st.checkbox(f"Confirm removal of {short(t)} from {name}") and st.button("Remove holding"):
            with db() as conn:
                n = conn.execute("DELETE FROM current_holdings WHERE client_id=%s AND symbol=%s", (cid, t)).rowcount
                if n == 1: audit(conn, "holding.removed", cid, {"symbol": t})
            st.success("Removed."); st.rerun()


def details_tab(cid, name, email, phone, notes, status):
    with st.form("edit"):
        n = st.text_input("Name", name); e = st.text_input("Email", email or ""); p = st.text_input("Phone", phone or ""); no = st.text_area("Notes", notes or "")
        if st.form_submit_button("Save changes", type="primary"):
            try:
                n, e, p, no = clean_contact(n, e, p, no)
                with db() as conn:
                    conn.execute("UPDATE clients SET name=%s,email=%s,phone=%s,notes=%s,updated_at=now() WHERE id=%s", (n, e, p, no, cid))
                    audit(conn, "client.updated", cid, {"name": n})
                st.success("Saved."); st.rerun()
            except ValueError as ex: st.error(str(ex))
    new = "archived" if status == "active" else "active"
    if st.button("Archive client" if status == "active" else "Reactivate client"):
        with db() as conn:
            conn.execute("UPDATE clients SET status=%s, updated_at=now() WHERE id=%s", (new, cid)); audit(conn, f"client.{new}", cid, {})
        st.rerun()
    st.caption("Archiving keeps all holdings and history; archived clients are excluded from home-page totals.")


def new_client_form():
    with st.form("new", clear_on_submit=True):
        n = st.text_input("Name"); e = st.text_input("Email (optional)"); p = st.text_input("Phone (optional)"); no = st.text_area("Notes (optional)")
        if st.form_submit_button("Create client", type="primary"):
            try:
                n, e, p, no = clean_contact(n, e, p, no)
                with db() as conn:
                    cid = conn.execute("INSERT INTO clients(name,email,phone,notes,status) VALUES (%s,%s,%s,%s,'active') RETURNING id", (n, e, p, no)).fetchone()[0]
                    audit(conn, "client.created", cid, {"name": n})
                st.success("Client created.")
            except ValueError as ex: st.error(str(ex))


def main():
    st.set_page_config(page_title=APP_NAME, page_icon="📈", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    if not authed(): st.warning("Please sign in on the Home page first."); st.stop()
    try: page()
    except RuntimeError as e: st.error(str(e))
    except Exception: log.exception("client page error"); st.error("Something went wrong. See the app logs for details.")


main()
