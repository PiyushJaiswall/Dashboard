"""StackWealth Dashboard - Client Details & Insights (single page file)."""
import io, logging, re
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from app import (APP_NAME, CSS, GREEN, RED, MUTED, authed, card, db, donut, enrich, fmt_inr, normalize_symbol,
                 pct, refresh_market_data, short, tone, totals, POS_SQL)

log = logging.getLogger("stackwealth.client")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE = re.compile(r"^\+?[0-9][0-9 \-()]{6,18}$")


def audit(conn, action, entity_id, details):
    conn.execute("INSERT INTO sw_audit_log(actor,action,entity,entity_id,details) VALUES (%s,%s,'client',%s,%s::jsonb)",
                 (st.session_state.get("sw_user", "admin"), action, str(entity_id), __import__("json").dumps(details, default=str)))


def clean_contact(name, email, phone, notes):
    name = (name or "").strip()
    if not 1 <= len(name) <= 120: raise ValueError("Client name is required (max 120 characters).")
    if email.strip() and not EMAIL.match(email.strip()): raise ValueError("Email looks invalid.")
    if phone.strip() and not PHONE.match(phone.strip()): raise ValueError("Phone looks invalid.")
    return name, email.strip() or None, phone.strip() or None, notes.strip() or None


def positions_for(client_id) -> pd.DataFrame:
    q = POS_SQL.replace("WHERE c.status='active'", "WHERE c.id=%s")
    with db() as conn:
        cur = conn.execute(q, (client_id,))
        cols = [d.name for d in cur.description]
        return enrich([dict(zip(cols, r)) for r in cur.fetchall()])


def page():
    with st.sidebar:
        present = st.toggle("Presentation mode", help="Hides editing controls and contact details.")
    with db() as conn:
        clients = conn.execute("SELECT id, name, email, phone, notes, status FROM clients ORDER BY status, name").fetchall()
    tabs = ["Overview"] + ([] if present else ["Holdings", "Client details", "New client"])
    tab_objs = st.tabs(tabs)
    if not clients:
        with tab_objs[0]: st.info("No clients yet.")
        if not present:
            with tab_objs[-1]: new_client_form()
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
    with tab_objs[0]: overview(df, name, status, email, phone, present)
    if not present:
        with tab_objs[1]: holdings_tab(cid, name, status, df)
        with tab_objs[2]: details_tab(cid, name, email, phone, notes, status)
        with tab_objs[3]: new_client_form()


def overview(df, name, status, email, phone, present):
    t = totals(df) if not df.empty else None
    contact = "" if present else f" · {email or 'no email'} · {phone or 'no phone'}"
    colr = tone(t["pnl"] if t and t["n_priced"] else None)
    big = fmt_inr(t["value"]) if t and t["n_priced"] else fmt_inr(t["invested"] if t else 0)
    lbl = "Portfolio value" if t and t["n_priced"] else "Invested capital (prices not loaded)"
    st.markdown(f"<div class='hero'><div class='app'>Stack<span>Wealth</span> Dashboard · Client</div>"
                f"<div class='lbl'>{name} · {status}{contact}</div><div class='lbl'>{lbl}</div>"
                f"<div class='big' style='color:{colr}'>{big}</div>"
                f"<div class='sub'>{'Unrealized P&L ' + fmt_inr(t['pnl'], sign=True) + ' (' + pct(t['ret']) + ')' if t and t['n_priced'] else ''}</div></div>", unsafe_allow_html=True)
    if not present and st.button("Refresh market data (all clients)", type="primary"):
        with st.spinner("Fetching…"):
            try: r = refresh_market_data(); st.success(f"{r['ok']} symbols updated" + (f"; no data: {', '.join(short(s) for s in r['missing'])}" if r["missing"] else ""))
            except Exception: log.exception("refresh"); st.error("Refresh failed; previous prices kept.")
        st.rerun()
    if df.empty: st.info("This client has no holdings yet."); return
    k = st.columns(4)
    for col, c in zip(k, [("Invested capital", fmt_inr(t["invested"]), "all holdings", "#e6edf7"),
                          ("Market value", fmt_inr(t["value"]) if t["n_priced"] else "—", "priced holdings", "#e6edf7"),
                          ("Unrealized P&L", fmt_inr(t["pnl"], sign=True) if t["n_priced"] else "—", pct(t["ret"]), colr),
                          ("Positions", str(t["n"]), f"{t['n_priced']} priced", "#e6edf7")]):
        col.markdown(card(*c), unsafe_allow_html=True)
    if t["n_priced"] < t["n"]:
        st.markdown(f"<div class='warn'>{t['n'] - t['n_priced']} position(s) have no price and are excluded from value and P&L.</div>", unsafe_allow_html=True)
    basis = "Value" if t["n_priced"] else "Invested"
    a, b = st.columns(2)
    with a: donut(df, "Symbol", basis, "Allocation by stock")
    with b: donut(df, "Sector", basis, "Allocation by sector")
    c1, c2 = st.columns(2)
    with c1: donut(df, "Cap", basis, "Allocation by market cap")
    with c2:
        pr = df[df.priced]
        if not pr.empty:
            f = go.Figure(go.Bar(x=pr.Symbol, y=pr.PnL, marker_color=[GREEN if v >= 0 else RED for v in pr.PnL]))
            f.update_layout(template="plotly_dark", title="Unrealized P&L by stock", height=380, margin=dict(t=50, l=0, r=0, b=0), paper_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(f, use_container_width=True)
    show = df.assign(**{"Return %": (df.PnL / df.Invested * 100).where(df.Invested > 0), "Price date": df.price_date})
    show = show[["Symbol", "Qty", "Avg", "Invested", "Price", "Value", "PnL", "Return %", "Price date", "Sector", "Cap"]].rename(columns={"Avg": "Avg price", "PnL": "Unrealized P&L"})
    st.dataframe(show.style.format({"Qty": "{:,.2f}", "Avg price": "₹{:,.2f}", "Invested": "₹{:,.0f}", "Price": "₹{:,.2f}", "Value": "₹{:,.0f}",
                 "Unrealized P&L": "₹{:+,.0f}", "Return %": "{:+.2f}%"}, na_rep="—"), hide_index=True, use_container_width=True)
    st.caption("Prices: Yahoo Finance, delayed / end-of-day. Missing price shows —. Unrealized P&L only.")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw: show.to_excel(xw, sheet_name="Holdings", index=False)
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
                cur = conn.execute("""INSERT INTO current_holdings(client_id,symbol,quantity,avg_price) VALUES (%s,%s,%s,%s)
                    ON CONFLICT (client_id,symbol) DO UPDATE SET quantity=EXCLUDED.quantity, avg_price=EXCLUDED.avg_price, updated_at=now()
                    RETURNING (xmax = 0)""", (cid, s, str(qty), str(avg)))
                created = cur.fetchone()[0]
                audit(conn, "holding.created" if created else "holding.updated", cid, {"symbol": s, "quantity": qty, "avg_price": avg})
            st.success(("Added " if created else "Updated existing holding ") + short(s)); st.rerun()
        except ValueError as e: st.error(str(e))
    if not df.empty:
        t = st.selectbox("Remove holding", [""] + list(df.sym))
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
                n, e, p, no = clean_contact(n, e or "", p or "", no or "")
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
    st.caption("Archiving keeps all holdings and history; archived clients are excluded from the home page totals.")


def new_client_form():
    with st.form("new", clear_on_submit=True):
        n = st.text_input("Name"); e = st.text_input("Email (optional)"); p = st.text_input("Phone (optional)"); no = st.text_area("Notes (optional)")
        if st.form_submit_button("Create client", type="primary"):
            try:
                n, e, p, no = clean_contact(n, e, p, no)
                with db() as conn:
                    cid = conn.execute("INSERT INTO clients(name,email,phone,notes,status) VALUES (%s,%s,%s,%s,'active') RETURNING id", (n, e, p, no)).fetchone()[0]
                    audit(conn, "client.created", cid, {"name": n})
                st.success("Client created."); 
            except ValueError as ex: st.error(str(ex))


def main():
    st.set_page_config(page_title=APP_NAME, page_icon="📈", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    if not authed(): st.warning("Please sign in on the Home page first."); st.stop()
    try: page()
    except RuntimeError as e: st.error(str(e))
    except Exception: log.exception("client page error"); st.error("Something went wrong. See the app logs for details.")


main()
