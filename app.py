import io
import uuid
from datetime import datetime, timezone

import bcrypt
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg
import streamlit as st
import yfinance as yf

from psycopg.rows import dict_row


# =========================================================
# CONFIGURATION
# =========================================================

st.set_page_config(
    page_title="Wealth Desk",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

GREEN = "#16A34A"
RED = "#DC2626"
NAVY = "#14213D"
BLUE = "#2563EB"
MUTED = "#64748B"

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap');

    html, body, [class*="css"] {
        font-family: 'DM Sans', sans-serif;
    }

    .stApp {
        background: #F5F7FB;
    }

    .block-container {
        max-width: 1500px;
        padding-top: 1.5rem;
        padding-bottom: 3rem;
    }

    h1, h2, h3 {
        color: #14213D;
    }

    div[data-testid="stMetric"] {
        background: white;
        border: 1px solid #E5EAF2;
        border-radius: 14px;
        padding: 18px;
        box-shadow: 0 3px 12px rgba(20, 33, 61, 0.035);
    }

    div[data-testid="stMetricLabel"] {
        color: #64748B;
        font-size: 0.88rem;
    }

    div[data-testid="stMetricValue"] {
        color: #14213D;
        font-weight: 700;
    }

    div.stButton > button {
        border-radius: 9px;
        font-weight: 600;
    }

    section[data-testid="stSidebar"] {
        background: #FFFFFF;
    }

    .small-note {
        color: #64748B;
        font-size: 0.82rem;
    }

    .status-pill {
        background: #E8F7EE;
        color: #15803D;
        padding: 5px 10px;
        border-radius: 20px;
        font-size: 0.82rem;
        font-weight: 600;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# DATABASE
# =========================================================

def get_connection():
    return psycopg.connect(
        st.secrets["DATABASE_URL"],
        connect_timeout=10,
        row_factory=dict_row,
    )


def initialize_database():
    """
    Reuses the existing clients table.
    Creates separate holdings, cached prices, and audit tables.
    Does not modify or delete the existing transactions table.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS wealth_holdings (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    client_id UUID NOT NULL REFERENCES clients(id),
                    ticker TEXT NOT NULL,
                    quantity NUMERIC(20, 6) NOT NULL
                        CHECK (quantity > 0),
                    average_cost NUMERIC(20, 6) NOT NULL
                        CHECK (average_cost >= 0),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (client_id, ticker)
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS wealth_price_cache (
                    ticker TEXT PRIMARY KEY,
                    price NUMERIC(20, 6) NOT NULL,
                    price_date TEXT,
                    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    source TEXT NOT NULL DEFAULT 'Yahoo Finance'
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS wealth_audit_logs (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_wealth_holdings_client
                ON wealth_holdings(client_id);
            """)


def audit(action, entity_type, entity_id=None, details=None):
    import json

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wealth_audit_logs
                    (action, entity_type, entity_id, details)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (
                    action,
                    entity_type,
                    str(entity_id) if entity_id else None,
                    json.dumps(details or {}),
                ),
            )


def read_clients():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, email, phone, notes, is_archived
                FROM clients
                ORDER BY name;
            """)
            rows = cur.fetchall()

    return pd.DataFrame(rows)


def read_holdings():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    h.id,
                    h.client_id,
                    c.name AS client_name,
                    h.ticker,
                    h.quantity,
                    h.average_cost,
                    h.updated_at
                FROM wealth_holdings h
                JOIN clients c ON c.id = h.client_id
                WHERE COALESCE(c.is_archived, FALSE) = FALSE
                ORDER BY c.name, h.ticker;
            """)
            rows = cur.fetchall()

    return pd.DataFrame(rows)


def read_prices():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ticker, price, price_date, fetched_at, source
                FROM wealth_price_cache;
            """)
            rows = cur.fetchall()

    return pd.DataFrame(rows)


# =========================================================
# PRICE REFRESH
# =========================================================

def normalize_ticker(ticker):
    ticker = ticker.strip().upper()

    # Default Indian exchange suffix when user enters a bare symbol.
    if "." not in ticker:
        ticker += ".NS"

    return ticker


def refresh_market_prices(tickers):
    """
    Fetches prices only when the user explicitly refreshes.
    Stores only the latest successful price per ticker.
    """
    results = []
    failures = []

    unique_tickers = sorted({
        normalize_ticker(t)
        for t in tickers
        if str(t).strip()
    })

    progress = st.progress(0, text="Refreshing market prices...")

    for index, ticker in enumerate(unique_tickers, start=1):
        try:
            hist = yf.Ticker(ticker).history(
                period="5d",
                interval="1d",
                auto_adjust=False,
            )

            if hist.empty or "Close" not in hist:
                failures.append(ticker)
            else:
                closes = hist["Close"].dropna()

                if closes.empty:
                    failures.append(ticker)
                else:
                    price = float(closes.iloc[-1])
                    price_date = str(closes.index[-1].date())

                    if price <= 0:
                        failures.append(ticker)
                    else:
                        results.append(
                            (ticker, price, price_date)
                        )

        except Exception:
            failures.append(ticker)

        progress.progress(
            index / max(len(unique_tickers), 1),
            text=f"Refreshing {ticker}...",
        )

    progress.empty()

    if results:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for ticker, price, price_date in results:
                    cur.execute(
                        """
                        INSERT INTO wealth_price_cache
                            (ticker, price, price_date, fetched_at, source)
                        VALUES (%s, %s, %s, NOW(), 'Yahoo Finance')
                        ON CONFLICT (ticker)
                        DO UPDATE SET
                            price = EXCLUDED.price,
                            price_date = EXCLUDED.price_date,
                            fetched_at = NOW(),
                            source = EXCLUDED.source;
                        """,
                        (ticker, price, price_date),
                    )

    return len(results), failures


# =========================================================
# PORTFOLIO CALCULATIONS
# =========================================================

def prepare_portfolio(holdings, prices):
    if holdings.empty:
        return pd.DataFrame()

    df = holdings.copy()

    df["ticker"] = df["ticker"].astype(str).str.upper()
    df["quantity"] = pd.to_numeric(df["quantity"])
    df["average_cost"] = pd.to_numeric(df["average_cost"])

    if prices.empty:
        df["current_price"] = float("nan")
        df["price_fetched_at"] = pd.NaT
        df["price_date"] = None
    else:
        prices = prices.copy()
        prices["ticker"] = prices["ticker"].astype(str).str.upper()
        prices["price"] = pd.to_numeric(prices["price"])

        df = df.merge(
            prices[["ticker", "price", "fetched_at", "price_date"]],
            on="ticker",
            how="left",
        )

        df = df.rename(
            columns={
                "price": "current_price",
                "fetched_at": "price_fetched_at",
            }
        )

    df["invested"] = df["quantity"] * df["average_cost"]

    df["market_value"] = (
        df["quantity"] * df["current_price"]
    )

    df["pnl"] = df["market_value"] - df["invested"]

    df["return_pct"] = (
        df["pnl"] / df["invested"].replace(0, float("nan")) * 100
    )

    df["price_available"] = df["current_price"].notna()

    return df


def money(value):
    if pd.isna(value):
        return "—"

    return f"₹{value:,.2f}"


def signed_money(value):
    if pd.isna(value):
        return "—"

    sign = "+" if value > 0 else ""
    return f"{sign}₹{value:,.2f}"


def signed_pct(value):
    if pd.isna(value):
        return "—"

    return f"{value:+.2f}%"


def metric(label, value, delta=None):
    st.metric(label, value, delta=delta)


def portfolio_totals(df):
    if df.empty:
        return {
            "invested": 0.0,
            "market_value": 0.0,
            "pnl": 0.0,
            "return_pct": 0.0,
            "missing": 0,
        }

    priced = df[df["price_available"]].copy()

    invested = float(priced["invested"].sum())
    market_value = float(priced["market_value"].sum())
    pnl = market_value - invested
    ret = (pnl / invested * 100) if invested else 0.0

    return {
        "invested": invested,
        "market_value": market_value,
        "pnl": pnl,
        "return_pct": ret,
        "missing": int((~df["price_available"]).sum()),
    }


# =========================================================
# LOGIN
# =========================================================

def login_screen():
    st.markdown("# ◈ Wealth Desk")
    st.caption("Portfolio intelligence for every client")
    st.divider()

    _, center, _ = st.columns([1, 1.1, 1])

    with center:
        st.subheader("Admin sign in")

        username = st.text_input("Username")
        password = st.text_input("Password", type="password")

        if st.button("Sign in", type="primary", use_container_width=True):
            try:
                expected_username = st.secrets["ADMIN_USERNAME"]
                expected_hash = st.secrets["ADMIN_PASSWORD_HASH"].encode()

                valid_password = bcrypt.checkpw(
                    password.encode(),
                    expected_hash,
                )

                if username == expected_username and valid_password:
                    st.session_state["authenticated"] = True
                    st.rerun()
                else:
                    st.error("Incorrect username or password.")

            except Exception:
                st.error(
                    "Authentication settings are missing or invalid. "
                    "Check your Streamlit Secrets."
                )


if not st.session_state.get("authenticated", False):
    login_screen()
    st.stop()


# =========================================================
# INITIALIZE AND LOAD DATA
# =========================================================

try:
    initialize_database()

    clients_df = read_clients()
    holdings_df = read_holdings()
    prices_df = read_prices()

except Exception as exc:
    st.error(
        "Wealth Desk could not load its database. "
        "Check DATABASE_URL and your Supabase connection."
    )
    st.caption(f"Error type: {type(exc).__name__}")
    st.stop()


# =========================================================
# HEADER
# =========================================================

header_left, header_right = st.columns([3, 1])

with header_left:
    st.markdown("# ◈ Wealth Desk")
    st.caption("A consolidated view of your clients' portfolios")

with header_right:
    if st.button("↻ Refresh Prices", type="primary", use_container_width=True):
        if holdings_df.empty:
            st.info("Add holdings before refreshing prices.")
        else:
            updated, failed = refresh_market_prices(
                holdings_df["ticker"].dropna().tolist()
            )

            st.session_state["last_refresh_result"] = (
                updated,
                failed,
            )
            st.rerun()

    if st.button("Sign out", use_container_width=True):
        st.session_state["authenticated"] = False
        st.rerun()

st.divider()

if clients_df.empty:
    clients_df = pd.DataFrame(
        columns=["id", "name", "email", "phone", "notes", "is_archived"]
    )

if not clients_df.empty:
    clients_df["is_archived"] = (
        clients_df["is_archived"].fillna(False).astype(bool)
    )

active_clients = clients_df[
    ~clients_df["is_archived"]
].copy()

portfolio_df = prepare_portfolio(holdings_df, prices_df)

# Show the last successful price refresh, if available.
if not prices_df.empty:
    latest_refresh = pd.to_datetime(
        prices_df["fetched_at"], utc=True, errors="coerce"
    ).max()

    if pd.notna(latest_refresh):
        st.caption(
            "Cached market prices · Last successful update: "
            + latest_refresh.strftime("%d %b %Y, %H:%M UTC")
        )
else:
    st.warning(
        "No cached market prices yet. Add holdings and click "
        "'Refresh Prices' to fetch market data."
    )

if "last_refresh_result" in st.session_state:
    updated, failed = st.session_state["last_refresh_result"]

    if failed:
        st.warning(
            f"Updated {updated} ticker(s). "
            f"Prices unavailable for: {', '.join(failed)}"
        )
    else:
        st.success(f"Successfully updated {updated} ticker(s).")


# =========================================================
# CLIENT SELECTOR
# =========================================================

client_options = ["All Clients"] + active_clients["name"].tolist()

selected_client_name = st.selectbox(
    "View portfolio",
    client_options,
    key="selected_client",
)

if selected_client_name == "All Clients":
    selected_df = portfolio_df.copy()
else:
    selected_df = portfolio_df[
        portfolio_df["client_name"] == selected_client_name
    ].copy()

totals = portfolio_totals(selected_df)


# =========================================================
# KPI CARDS
# =========================================================

st.markdown("## Portfolio overview")

k1, k2, k3, k4, k5 = st.columns(5)

with k1:
    metric(
        "Current Value",
        money(totals["market_value"]),
    )

with k2:
    metric(
        "Invested Capital",
        money(totals["invested"]),
    )

with k3:
    metric(
        "Unrealized P&L",
        signed_money(totals["pnl"]),
        signed_pct(totals["return_pct"]),
    )

with k4:
    metric(
        "Return",
        signed_pct(totals["return_pct"]),
    )

with k5:
    if selected_client_name == "All Clients":
        metric("Active Clients", str(len(active_clients)))
    else:
        metric("Stock Holdings", str(len(selected_df)))


if totals["missing"] > 0:
    st.warning(
        f"{totals['missing']} holding(s) have no cached market price. "
        "Their values are excluded from current-value and P&L totals."
    )


# =========================================================
# CHARTS
# =========================================================

st.markdown("## Portfolio analytics")

priced_df = selected_df[
    selected_df["price_available"]
].copy()

if priced_df.empty:
    st.info(
        "No priced holdings to chart yet. Add holdings and click "
        "'Refresh Prices' to load market prices."
    )

else:
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### Allocation by stock")

        allocation = (
            priced_df.groupby("ticker", as_index=False)["market_value"]
            .sum()
            .sort_values("market_value", ascending=False)
        )

        fig = px.treemap(
            allocation,
            path=["ticker"],
            values="market_value",
            color="market_value",
            color_continuous_scale="Blues",
        )

        fig.update_layout(
            margin=dict(t=10, l=5, r=5, b=5),
            height=380,
            coloraxis_showscale=False,
        )

        fig.update_traces(
            texttemplate="%{label}<br>₹%{value:,.0f}",
            textfont_size=13,
        )

        st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.markdown("### Invested vs current value")

        comparison = (
            priced_df.groupby("ticker", as_index=False)
            .agg(
                Invested=("invested", "sum"),
                CurrentValue=("market_value", "sum"),
            )
        )

        comparison = comparison.sort_values(
            "CurrentValue", ascending=True
        ).tail(12)

        comparison_long = comparison.melt(
            id_vars="ticker",
            value_vars=["Invested", "CurrentValue"],
            var_name="Type",
            value_name="Value",
        )

        fig = px.bar(
            comparison_long,
            x="Value",
            y="ticker",
            color="Type",
            barmode="group",
            orientation="h",
            color_discrete_map={
                "Invested": "#94A3B8",
                "CurrentValue": "#2563EB",
            },
        )

        fig.update_layout(
            height=380,
            margin=dict(t=10, l=5, r=5, b=5),
            legend_title_text="",
            yaxis_title="",
            xaxis_title="Value (INR)",
        )

        st.plotly_chart(fig, use_container_width=True)

    col3, col4 = st.columns(2)

    with col3:
        st.markdown("### Profit and loss by stock")

        stock_pnl = (
            priced_df.groupby("ticker", as_index=False)["pnl"]
            .sum()
            .sort_values("pnl", ascending=True)
        )

        stock_pnl["Color"] = stock_pnl["pnl"].apply(
            lambda x: "Profit" if x >= 0 else "Loss"
        )

        fig = px.bar(
            stock_pnl,
            x="pnl",
            y="ticker",
            orientation="h",
            color="Color",
            color_discrete_map={
                "Profit": GREEN,
                "Loss": RED,
            },
        )

        fig.update_layout(
            height=350,
            margin=dict(t=10, l=5, r=5, b=5),
            showlegend=False,
            xaxis_title="Unrealized P&L (INR)",
            yaxis_title="",
        )

        fig.add_vline(x=0, line_color="#64748B")

        st.plotly_chart(fig, use_container_width=True)

    with col4:
        st.markdown("### Client portfolio value")

        if selected_client_name == "All Clients":
            client_values = (
                priced_df.groupby("client_name", as_index=False)
                ["market_value"].sum()
                .sort_values("market_value", ascending=True)
            )

            fig = px.bar(
                client_values,
                x="market_value",
                y="client_name",
                orientation="h",
                color="market_value",
                color_continuous_scale="Blues",
            )

            fig.update_layout(
                height=350,
                margin=dict(t=10, l=5, r=5, b=5),
                coloraxis_showscale=False,
                xaxis_title="Current Value (INR)",
                yaxis_title="",
            )

            st.plotly_chart(fig, use_container_width=True)

        else:
            st.info(
                "Select All Clients to compare portfolio values "
                "across clients."
            )


# =========================================================
# CLIENT PORTFOLIO TABLE
# =========================================================

st.markdown("## Client portfolio summary")

if selected_client_name == "All Clients":
    if portfolio_df.empty:
        st.info("No holdings added yet.")
    else:
        summary = (
            portfolio_df.groupby("client_name", as_index=False)
            .agg(
                Holdings=("ticker", "count"),
                Invested=("invested", "sum"),
                PricedHoldings=("price_available", "sum"),
                CurrentValue=("market_value", "sum"),
                PnL=("pnl", "sum"),
            )
        )

        summary["Return %"] = (
            summary["PnL"]
            / summary["Invested"].replace(0, float("nan"))
            * 100
        )

        summary["Missing Prices"] = (
            summary["Holdings"] - summary["PricedHoldings"]
        )

        summary = summary.rename(
            columns={
                "client_name": "Client",
                "Holdings": "Stocks",
                "Invested": "Invested Capital",
                "CurrentValue": "Priced Market Value",
                "PnL": "Unrealized P&L",
            }
        )

        display_cols = [
            "Client",
            "Stocks",
            "Invested Capital",
            "Priced Market Value",
            "Unrealized P&L",
            "Return %",
            "Missing Prices",
        ]

        st.dataframe(
            summary[display_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "Invested Capital": st.column_config.NumberColumn(
                    format="₹%.2f"
                ),
                "Priced Market Value": st.column_config.NumberColumn(
                    format="₹%.2f"
                ),
                "Unrealized P&L": st.column_config.NumberColumn(
                    format="₹%.2f"
                ),
                "Return %": st.column_config.NumberColumn(
                    format="%.2f%%"
                ),
            },
        )

else:
    st.caption(f"Detailed holdings for {selected_client_name}")

    if selected_df.empty:
        st.info("This client has no holdings yet.")
    else:
        detail = selected_df[
            [
                "ticker",
                "quantity",
                "average_cost",
                "current_price",
                "invested",
                "market_value",
                "pnl",
                "return_pct",
                "price_available",
            ]
        ].copy()

        detail = detail.rename(
            columns={
                "ticker": "Stock",
                "quantity": "Quantity",
                "average_cost": "Average Cost",
                "current_price": "Current Price",
                "invested": "Invested",
                "market_value": "Market Value",
                "pnl": "Unrealized P&L",
                "return_pct": "Return %",
                "price_available": "Price Available",
            }
        )

        st.dataframe(
            detail,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Average Cost": st.column_config.NumberColumn(
                    format="₹%.2f"
                ),
                "Current Price": st.column_config.NumberColumn(
                    format="₹%.2f"
                ),
                "Invested": st.column_config.NumberColumn(
                    format="₹%.2f"
                ),
                "Market Value": st.column_config.NumberColumn(
                    format="₹%.2f"
                ),
                "Unrealized P&L": st.column_config.NumberColumn(
                    format="₹%.2f"
                ),
                "Return %": st.column_config.NumberColumn(
                    format="%.2f%%"
                ),
            },
        )

    if not selected_df.empty:
        export = selected_df.copy()
        csv = export.to_csv(index=False).encode("utf-8")

        st.download_button(
            "Download client holdings CSV",
            data=csv,
            file_name="client_holdings.csv",
            mime="text/csv",
        )


# =========================================================
# CLIENT MANAGEMENT
# =========================================================

st.divider()
st.markdown("## Client management")

with st.expander("+ Add a new client"):
    with st.form("add_client_form", clear_on_submit=True):
        new_name = st.text_input("Client name *")
        new_email = st.text_input("Email (optional)")
        new_phone = st.text_input("Phone (optional)")
        new_notes = st.text_area("Notes (optional)")

        add_client = st.form_submit_button(
            "Create client",
            type="primary",
        )

        if add_client:
            if not new_name.strip():
                st.error("Client name is required.")
            else:
                try:
                    with get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO clients
                                    (name, email, phone, notes, is_archived)
                                VALUES (%s, %s, %s, %s, FALSE)
                                RETURNING id;
                                """,
                                (
                                    new_name.strip(),
                                    new_email.strip() or None,
                                    new_phone.strip() or None,
                                    new_notes.strip() or None,
                                ),
                            )
                            new_id = cur.fetchone()["id"]

                    audit(
                        "CLIENT_CREATED",
                        "client",
                        new_id,
                        {"name": new_name.strip()},
                    )

                    st.success("Client created.")
                    st.rerun()

                except Exception as exc:
                    st.error(
                        "Could not create client. "
                        "Check whether the email already exists or "
                        f"whether the database rejected the operation ({type(exc).__name__})."
                    )


if not active_clients.empty:
    with st.expander("Edit or archive a client"):
        chosen_client_name = st.selectbox(
            "Choose client",
            active_clients["name"].tolist(),
            key="manage_client_select",
        )

        chosen = active_clients[
            active_clients["name"] == chosen_client_name
        ].iloc[0]

        with st.form("edit_client_form"):
            edit_name = st.text_input(
                "Name",
                value=str(chosen["name"] or ""),
            )
            edit_email = st.text_input(
                "Email",
                value=str(chosen["email"] or ""),
            )
            edit_phone = st.text_input(
                "Phone",
                value=str(chosen["phone"] or ""),
            )
            edit_notes = st.text_area(
                "Notes",
                value=str(chosen["notes"] or ""),
            )

            save_client = st.form_submit_button("Save changes")
            archive_client = st.form_submit_button("Archive client")

            if save_client:
                if not edit_name.strip():
                    st.error("Name cannot be empty.")
                else:
                    with get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE clients
                                SET name=%s, email=%s, phone=%s, notes=%s
                                WHERE id=%s;
                                """,
                                (
                                    edit_name.strip(),
                                    edit_email.strip() or None,
                                    edit_phone.strip() or None,
                                    edit_notes.strip() or None,
                                    chosen["id"],
                                ),
                            )

                    audit(
                        "CLIENT_UPDATED",
                        "client",
                        chosen["id"],
                        {"name": edit_name.strip()},
                    )

                    st.success("Client updated.")
                    st.rerun()

            if archive_client:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE clients
                            SET is_archived = TRUE
                            WHERE id = %s;
                            """,
                            (chosen["id"],),
                        )

                audit(
                    "CLIENT_ARCHIVED",
                    "client",
                    chosen["id"],
                    {"name": chosen["name"]},
                )

                st.success("Client archived.")
                st.rerun()


# =========================================================
# HOLDINGS MANAGEMENT
# =========================================================

st.markdown("## Holdings management")

if active_clients.empty:
    st.info("Create a client before adding holdings.")

else:
    with st.expander("+ Add or update a stock holding"):
        with st.form("holding_form", clear_on_submit=True):
            holding_client = st.selectbox(
                "Client",
                active_clients["name"].tolist(),
            )

            ticker_input = st.text_input(
                "Stock symbol",
                placeholder="RELIANCE.NS or RELIANCE",
                help="For NSE, enter RELIANCE.NS or RELIANCE. "
                     "A bare symbol defaults to .NS.",
            )

            quantity_input = st.number_input(
                "Current quantity held",
                min_value=0.000001,
                value=1.0,
                step=1.0,
                format="%.6f",
            )

            average_cost_input = st.number_input(
                "Average purchase price (₹)",
                min_value=0.0,
                value=100.0,
                step=1.0,
                format="%.4f",
            )

            save_holding = st.form_submit_button(
                "Save holding",
                type="primary",
            )

            if save_holding:
                if not ticker_input.strip():
                    st.error("Stock symbol is required.")
                elif quantity_input <= 0:
                    st.error("Quantity must be greater than zero.")
                else:
                    normalized = normalize_ticker(ticker_input)

                    client_row = active_clients[
                        active_clients["name"] == holding_client
                    ].iloc[0]

                    with get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO wealth_holdings
                                    (client_id, ticker, quantity, average_cost)
                                VALUES (%s, %s, %s, %s)
                                ON CONFLICT (client_id, ticker)
                                DO UPDATE SET
                                    quantity = EXCLUDED.quantity,
                                    average_cost = EXCLUDED.average_cost,
                                    updated_at = NOW()
                                RETURNING id;
                                """,
                                (
                                    client_row["id"],
                                    normalized,
                                    quantity_input,
                                    average_cost_input,
                                ),
                            )
                            holding_id = cur.fetchone()["id"]

                    audit(
                        "HOLDING_UPSERTED",
                        "holding",
                        holding_id,
                        {
                            "client": holding_client,
                            "ticker": normalized,
                            "quantity": quantity_input,
                            "average_cost": average_cost_input,
                        },
                    )

                    st.success(
                        f"Saved {normalized} for {holding_client}."
                    )
                    st.rerun()


with st.expander("Edit or remove an existing holding"):
    if holdings_df.empty:
        st.info("No holdings exist yet.")
    else:
        holding_labels = (
            holdings_df["client_name"].astype(str)
            + " · "
            + holdings_df["ticker"].astype(str)
        )

        chosen_label = st.selectbox(
            "Select holding",
            holding_labels.tolist(),
        )

        chosen_index = holding_labels[
            holding_labels == chosen_label
        ].index[0]

        chosen_holding = holdings_df.loc[chosen_index]

        with st.form("edit_holding_form"):
            edit_qty = st.number_input(
                "Quantity",
                min_value=0.000001,
                value=float(chosen_holding["quantity"]),
                step=1.0,
                format="%.6f",
            )

            edit_cost = st.number_input(
                "Average purchase price",
                min_value=0.0,
                value=float(chosen_holding["average_cost"]),
                step=1.0,
                format="%.4f",
            )

            update_holding = st.form_submit_button("Update holding")
            remove_holding = st.form_submit_button("Remove holding")

            if update_holding:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE wealth_holdings
                            SET quantity=%s,
                                average_cost=%s,
                                updated_at=NOW()
                            WHERE id=%s;
                            """,
                            (
                                edit_qty,
                                edit_cost,
                                chosen_holding["id"],
                            ),
                        )

                audit(
                    "HOLDING_UPDATED",
                    "holding",
                    chosen_holding["id"],
                    {
                        "quantity": edit_qty,
                        "average_cost": edit_cost,
                    },
                )

                st.success("Holding updated.")
                st.rerun()

            if remove_holding:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            DELETE FROM wealth_holdings
                            WHERE id=%s;
                            """,
                            (chosen_holding["id"],),
                        )

                audit(
                    "HOLDING_REMOVED",
                    "holding",
                    chosen_holding["id"],
                    {"ticker": chosen_holding["ticker"]},
                )

                st.success("Holding removed.")
                st.rerun()


# =========================================================
# PRESENTATION MODE
# =========================================================

st.divider()

with st.expander("Presentation mode"):
    st.write(
        "Use this view when explaining a portfolio to a prospective "
        "client. It displays calculated values from your current holdings "
        "and cached market prices."
    )

    presentation = st.checkbox(
        "Enable presentation view",
        key="presentation_view",
    )

    if presentation:
        st.markdown("### Portfolio performance summary")
        st.caption(
            "Figures reflect current holdings and the latest available "
            "cached prices. Returns are unrealized, not guaranteed."
        )

        p1, p2, p3 = st.columns(3)

        p1.metric(
            "Portfolio Value",
            money(totals["market_value"]),
        )
        p2.metric(
            "Invested Capital",
            money(totals["invested"]),
        )
        p3.metric(
            "Unrealized P&L",
            signed_money(totals["pnl"]),
            signed_pct(totals["return_pct"]),
        )

        if not priced_df.empty:
            presentation_alloc = (
                priced_df.groupby("ticker", as_index=False)
                ["market_value"].sum()
                .sort_values("market_value", ascending=False)
            )

            fig = px.bar(
                presentation_alloc,
                x="ticker",
                y="market_value",
                color="market_value",
                color_continuous_scale="Blues",
            )

            fig.update_layout(
                title="Current holdings by market value",
                showlegend=False,
                coloraxis_showscale=False,
                xaxis_title="Stock",
                yaxis_title="Market value (INR)",
            )

            st.plotly_chart(fig, use_container_width=True)

st.caption(
    "Wealth Desk · Portfolio values depend on available market prices. "
    "Market data may be delayed. This dashboard does not execute trades."
)
