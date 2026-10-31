# app.py
from __future__ import annotations

import io
import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import bcrypt
import pandas as pd
import plotly.express as px
import psycopg
import streamlit as st
import yfinance as yf
from psycopg.rows import dict_row


# ============================================================
# 1. APPLICATION CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="Wealth Desk",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

APP_NAME = "Wealth Desk"
CURRENCY = "₹"
STALE_PRICE_DAYS = 7


# ============================================================
# 2. DATABASE
# ============================================================

def get_connection():
    """Create a PostgreSQL connection using Streamlit secrets."""
    return psycopg.connect(
        st.secrets["DATABASE_URL"],
        connect_timeout=10,
        row_factory=dict_row,
    )


def init_database():
    """Create the application's tables if they do not exist."""
    statements = [
        """
        CREATE TABLE IF NOT EXISTS clients (
            id BIGSERIAL PRIMARY KEY,
            name VARCHAR(150) NOT NULL,
            email VARCHAR(254),
            phone VARCHAR(40),
            notes TEXT,
            is_archived BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id BIGSERIAL PRIMARY KEY,
            client_id BIGINT NOT NULL REFERENCES clients(id),
            ticker VARCHAR(40) NOT NULL,
            transaction_type VARCHAR(4) NOT NULL
                CHECK (transaction_type IN ('BUY', 'SELL')),
            quantity NUMERIC(24, 8) NOT NULL CHECK (quantity > 0),
            price NUMERIC(24, 8) NOT NULL CHECK (price > 0),
            trade_date DATE NOT NULL,
            notes TEXT,
            request_id UUID NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_transactions_client
        ON transactions(client_id, trade_date, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_transactions_ticker
        ON transactions(ticker)
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id BIGSERIAL PRIMARY KEY,
            action VARCHAR(60) NOT NULL,
            entity_type VARCHAR(60) NOT NULL,
            entity_id BIGINT,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    ]

    with get_connection() as conn:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)


def db_fetch(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return its rows as dictionaries."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def db_execute(sql: str, params: tuple = ()):
    """Run a database write and commit it."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def audit(action: str, entity_type: str, entity_id: int | None, details: dict):
    """Record an administrative action without logging credentials."""
    db_execute(
        """
        INSERT INTO audit_logs (action, entity_type, entity_id, details)
        VALUES (%s, %s, %s, %s::jsonb)
        """,
        (action, entity_type, entity_id, json.dumps(details, default=str)),
    )


# ============================================================
# 3. ADMIN AUTHENTICATION
# ============================================================

def login_screen():
    st.title("🔐 Wealth Desk")
    st.caption("Private portfolio management")

    with st.form("login_form"):
        username = st.text_input("Administrator username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)

    if submitted:
        try:
            correct_username = st.secrets["ADMIN_USERNAME"]
            password_hash = st.secrets["ADMIN_PASSWORD_HASH"]

            username_ok = username == correct_username
            password_ok = bcrypt.checkpw(
                password.encode("utf-8"),
                password_hash.encode("utf-8"),
            )

            if username_ok and password_ok:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect username or password.")
        except Exception:
            st.error(
                "Authentication is not configured correctly. "
                "Check the application secrets."
            )


# ============================================================
# 4. CLIENT MANAGEMENT
# ============================================================

def get_clients(include_archived: bool = False) -> pd.DataFrame:
    if include_archived:
        rows = db_fetch("SELECT * FROM clients ORDER BY name")
    else:
        rows = db_fetch(
            "SELECT * FROM clients WHERE is_archived = FALSE ORDER BY name"
        )
    return pd.DataFrame(rows)


def create_client(
    name: str,
    email: str,
    phone: str,
    notes: str,
):
    name = name.strip()
    if not name:
        raise ValueError("Client name is required.")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO clients (name, email, phone, notes)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (name, email.strip() or None, phone.strip() or None,
                 notes.strip() or None),
            )
            client_id = cur.fetchone()["id"]

    audit("CREATE", "client", client_id, {"name": name})


def update_client(
    client_id: int,
    name: str,
    email: str,
    phone: str,
    notes: str,
):
    name = name.strip()
    if not name:
        raise ValueError("Client name is required.")

    db_execute(
        """
        UPDATE clients
        SET name = %s, email = %s, phone = %s, notes = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (
            name,
            email.strip() or None,
            phone.strip() or None,
            notes.strip() or None,
            client_id,
        ),
    )
    audit("UPDATE", "client", client_id, {"name": name})


def set_client_archived(client_id: int, archived: bool):
    db_execute(
        """
        UPDATE clients
        SET is_archived = %s, updated_at = NOW()
        WHERE id = %s
        """,
        (archived, client_id),
    )
    audit(
        "ARCHIVE" if archived else "UNARCHIVE",
        "client",
        client_id,
        {"is_archived": archived},
    )


# ============================================================
# 5. TRANSACTION LEDGER
# ============================================================

def get_transactions(client_id: int | None = None) -> pd.DataFrame:
    if client_id is None:
        rows = db_fetch(
            """
            SELECT t.*, c.name AS client_name
            FROM transactions t
            JOIN clients c ON c.id = t.client_id
            ORDER BY t.trade_date, t.id
            """
        )
    else:
        rows = db_fetch(
            """
            SELECT t.*, c.name AS client_name
            FROM transactions t
            JOIN clients c ON c.id = t.client_id
            WHERE t.client_id = %s
            ORDER BY t.trade_date, t.id
            """,
            (client_id,),
        )

    return pd.DataFrame(rows)


def decimal_value(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Quantity and price must be valid numbers.")

    if not result.is_finite():
        raise ValueError("Quantity and price must be finite numbers.")

    return result


def calculate_fifo(transactions: pd.DataFrame) -> dict:
    """
    Calculate open lots and realized P&L using FIFO.

    Each purchase creates a lot. Each sale consumes the oldest
    available lot first. Fees, taxes, dividends, and corporate
    actions are intentionally excluded in this version.
    """
    lots_by_key: dict[tuple[int, str], list[dict]] = {}
    realized_by_key: dict[tuple[int, str], Decimal] = {}
    realized_events = []

    if transactions.empty:
        return {
            "lots": lots_by_key,
            "realized": realized_by_key,
            "realized_events": realized_events,
        }

    tx = transactions.copy()
    tx["trade_date"] = pd.to_datetime(tx["trade_date"]).dt.date
    tx = tx.sort_values(["trade_date", "id"])

    for row in tx.to_dict("records"):
        client_id = int(row["client_id"])
        ticker = str(row["ticker"]).strip().upper()
        tx_type = str(row["transaction_type"]).upper()
        quantity = decimal_value(row["quantity"])
        price = decimal_value(row["price"])
        key = (client_id, ticker)

        lots = lots_by_key.setdefault(key, [])
        realized_by_key.setdefault(key, Decimal("0"))

        if quantity <= 0 or price <= 0:
            raise ValueError(f"Invalid transaction #{row['id']}.")

        if tx_type == "BUY":
            lots.append({
                "quantity": quantity,
                "unit_cost": price,
                "buy_date": row["trade_date"],
                "buy_transaction_id": int(row["id"]),
            })

        elif tx_type == "SELL":
            remaining_to_sell = quantity
            sale_realized = Decimal("0")

            while remaining_to_sell > 0:
                # Remove lots that have been completely consumed.
                while lots and lots[0]["quantity"] <= 0:
                    lots.pop(0)

                if not lots:
                    raise ValueError(
                        f"Transaction #{row['id']} sells more "
                        f"{ticker} than the available FIFO quantity. "
                        "Check the transaction history and dates."
                    )

                lot = lots[0]
                matched = min(remaining_to_sell, lot["quantity"])

                sale_realized += matched * (price - lot["unit_cost"])
                lot["quantity"] -= matched
                remaining_to_sell -= matched

            realized_by_key[key] += sale_realized
            realized_events.append({
                "transaction_id": int(row["id"]),
                "client_id": client_id,
                "ticker": ticker,
                "trade_date": row["trade_date"],
                "quantity_sold": float(quantity),
                "sale_price": float(price),
                "realized_pnl": float(sale_realized),
            })

        else:
            raise ValueError(
                f"Unsupported transaction type: {tx_type}"
            )

    return {
        "lots": lots_by_key,
        "realized": realized_by_key,
        "realized_events": realized_events,
    }


def add_transaction(
    client_id: int,
    ticker: str,
    transaction_type: str,
    quantity: Decimal,
    price: Decimal,
    trade_date: date,
    notes: str,
):
    ticker = ticker.strip().upper()

    if not ticker:
        raise ValueError("A Yahoo Finance ticker is required.")
    if len(ticker) > 40:
        raise ValueError("Ticker is too long.")
    if quantity <= 0 or price <= 0:
        raise ValueError("Quantity and price must be greater than zero.")
    if trade_date > date.today():
        raise ValueError("Future-dated transactions are not supported.")

    transaction_type = transaction_type.upper()
    if transaction_type not in {"BUY", "SELL"}:
        raise ValueError("Transaction type must be BUY or SELL.")

    # Validate the entire resulting ledger before writing anything.
    existing = get_transactions(client_id)
    candidate_id = (
        int(existing["id"].max()) + 1 if not existing.empty else 1
    )

    candidate = pd.DataFrame([{
        "id": candidate_id,
        "client_id": client_id,
        "ticker": ticker,
        "transaction_type": transaction_type,
        "quantity": quantity,
        "price": price,
        "trade_date": trade_date,
    }])

    if not existing.empty:
        validation_df = existing[
            [
                "id", "client_id", "ticker", "transaction_type",
                "quantity", "price", "trade_date",
            ]
        ].copy()
        validation_df = pd.concat(
            [validation_df, candidate], ignore_index=True
        )
    else:
        validation_df = candidate

    calculate_fifo(validation_df)

    request_id = str(uuid.uuid4())

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO transactions
                    (client_id, ticker, transaction_type,
                     quantity, price, trade_date, notes, request_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    client_id,
                    ticker,
                    transaction_type,
                    quantity,
                    price,
                    trade_date,
                    notes.strip() or None,
                    request_id,
                ),
            )
            transaction_id = cur.fetchone()["id"]

    audit(
        "CREATE",
        "transaction",
        transaction_id,
        {
            "client_id": client_id,
            "ticker": ticker,
            "type": transaction_type,
            "quantity": str(quantity),
            "price": str(price),
            "trade_date": str(trade_date),
        },
    )


def delete_transaction(transaction_id: int):
    rows = db_fetch(
        """
        SELECT id, client_id, ticker, transaction_type,
               quantity, price, trade_date
        FROM transactions WHERE id = %s
        """,
        (transaction_id,),
    )

    if not rows:
        raise ValueError("Transaction not found.")

    old = rows[0]

    # Keep a record of the deletion before removing the transaction.
    audit(
        "DELETE",
        "transaction",
        transaction_id,
        {key: str(value) for key, value in old.items()},
    )

    db_execute("DELETE FROM transactions WHERE id = %s", (transaction_id,))

    # A deletion can affect the FIFO validity of later transactions.
    remaining = get_transactions(int(old["client_id"]))
    calculate_fifo(remaining)


# ============================================================
# 6. MARKET DATA — IN-MEMORY CACHE ONLY
# ============================================================

@st.cache_data(ttl=300, show_spinner=False, max_entries=500)
def get_latest_close(ticker: str) -> dict:
    """
    Fetch the latest available daily closing price.

    Prices are cached in application memory for five minutes.
    No market-price history is written to PostgreSQL.
    """
    try:
        history = yf.Ticker(ticker).history(
            period="1mo",
            interval="1d",
            auto_adjust=False,
            actions=False,
        )

        if history.empty or "Close" not in history.columns:
            return {
                "price": None,
                "price_date": None,
                "status": "MISSING",
                "message": "No daily price data returned.",
            }

        history = history.dropna(subset=["Close"])
        if history.empty:
            return {
                "price": None,
                "price_date": None,
                "status": "MISSING",
                "message": "No valid closing price returned.",
            }

        last_index = pd.Timestamp(history.index[-1])
        price_date = last_index.date()
        price = float(history["Close"].iloc[-1])

        if price <= 0:
            return {
                "price": None,
                "price_date": price_date.isoformat(),
                "status": "MISSING",
                "message": "Returned price is not valid.",
            }

        if (date.today() - price_date).days > STALE_PRICE_DAYS:
            return {
                "price": price,
                "price_date": price_date.isoformat(),
                "status": "STALE",
                "message": "Last available close is older than expected.",
            }

        return {
            "price": price,
            "price_date": price_date.isoformat(),
            "status": "OK",
            "message": "Latest available daily close.",
        }

    except Exception as exc:
        # Avoid exposing technical details or credentials in the UI.
        return {
            "price": None,
            "price_date": None,
            "status": "MISSING",
            "message": f"Price retrieval failed ({type(exc).__name__}).",
        }


# ============================================================
# 7. PORTFOLIO VALUATION
# ============================================================

def build_holdings(client_id: int | None = None) -> pd.DataFrame:
    transactions = get_transactions(client_id)

    columns = [
        "client_id", "client_name", "ticker", "quantity",
        "cost_basis", "average_cost", "realized_pnl",
        "market_price", "market_value", "unrealized_pnl",
        "total_pnl", "price_date", "price_status", "price_message",
    ]

    if transactions.empty:
        return pd.DataFrame(columns=columns)

    fifo = calculate_fifo(transactions)
    client_names = {
        int(row["id"]): row["name"]
        for row in db_fetch("SELECT id, name FROM clients")
    }

    rows = []

    for (cid, ticker), lots in fifo["lots"].items():
        open_lots = [lot for lot in lots if lot["quantity"] > 0]
        quantity = sum(
            (lot["quantity"] for lot in open_lots), Decimal("0")
        )

        if quantity <= 0:
            continue

        cost_basis = sum(
            (
                lot["quantity"] * lot["unit_cost"]
                for lot in open_lots
            ),
            Decimal("0"),
        )
        average_cost = cost_basis / quantity
        realized_pnl = fifo["realized"].get((cid, ticker), Decimal("0"))

        price_data = get_latest_close(ticker)

        # Do not include missing or stale prices in market-value totals.
        usable_price = (
            price_data["price"]
            if price_data["status"] == "OK"
            else None
        )

        market_value = (
            quantity * Decimal(str(usable_price))
            if usable_price is not None
            else None
        )

        unrealized_pnl = (
            market_value - cost_basis
            if market_value is not None
            else None
        )

        total_pnl = (
            realized_pnl + unrealized_pnl
            if unrealized_pnl is not None
            else None
        )

        rows.append({
            "client_id": cid,
            "client_name": client_names.get(cid, f"Client {cid}"),
            "ticker": ticker,
            "quantity": float(quantity),
            "cost_basis": float(cost_basis),
            "average_cost": float(average_cost),
            "realized_pnl": float(realized_pnl),
            "market_price": usable_price,
            "market_value": (
                float(market_value) if market_value is not None else None
            ),
            "unrealized_pnl": (
                float(unrealized_pnl)
                if unrealized_pnl is not None else None
            ),
            "total_pnl": (
                float(total_pnl) if total_pnl is not None else None
            ),
            "price_date": price_data["price_date"],
            "price_status": price_data["status"],
            "price_message": price_data["message"],
        })

    return pd.DataFrame(rows, columns=columns)


def money(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{CURRENCY}{value:,.2f}"


def render_metrics(holdings: pd.DataFrame):
    if holdings.empty:
        st.info("No open holdings yet. Add a purchase to get started.")
        return

    total_cost = holdings["cost_basis"].sum()
    valid_prices = holdings["price_status"].eq("OK")
    priced = holdings[valid_prices]

    market_value = priced["market_value"].sum()
    unrealized = priced["unrealized_pnl"].sum()
    realized = holdings["realized_pnl"].sum()
    missing_count = int((~valid_prices).sum())

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Remaining cost basis", money(total_cost))
    col2.metric(
        "Market value",
        money(market_value),
        help="Only holdings with usable, non-stale prices are included.",
    )
    col3.metric("Realized P&L", money(realized))
    col4.metric(
        "Unrealized P&L",
        money(unrealized),
        help="Only holdings with usable, non-stale prices are included.",
    )

    if missing_count:
        st.warning(
            f"{missing_count} holding(s) have missing or stale prices. "
            "Market value and unrealized P&L are partial; those holdings "
            "are excluded from these totals."
        )


def render_holdings_table(holdings: pd.DataFrame):
    if holdings.empty:
        st.info("There are no open holdings to display.")
        return

    display = holdings.copy()
    display = display.rename(columns={
        "client_name": "Client",
        "ticker": "Ticker",
        "quantity": "Quantity",
        "average_cost": "Average cost",
        "cost_basis": "Remaining cost basis",
        "market_price": "Latest close",
        "market_value": "Market value",
        "realized_pnl": "Realized P&L",
        "unrealized_pnl": "Unrealized P&L",
        "total_pnl": "Total P&L",
        "price_date": "Price date",
        "price_status": "Price status",
    })

    wanted = [
        "Client", "Ticker", "Quantity", "Average cost",
        "Remaining cost basis", "Latest close", "Market value",
        "Realized P&L", "Unrealized P&L", "Total P&L",
        "Price date", "Price status",
    ]
    st.dataframe(
        display[wanted],
        use_container_width=True,
        hide_index=True,
    )


# ============================================================
# 8. DASHBOARDS AND CHARTS
# ============================================================

def render_portfolio_dashboard(
    holdings: pd.DataFrame,
    transactions: pd.DataFrame,
    title: str,
):
    st.title(title)
    st.caption(
        "INR reporting • FIFO cost basis • Latest available daily close"
    )

    render_metrics(holdings)

    if not holdings.empty:
        st.subheader("Open holdings")
        render_holdings_table(holdings)

        priced = holdings[
            holdings["price_status"].eq("OK")
            & holdings["market_value"].notna()
        ]

        if not priced.empty:
            left, right = st.columns(2)

            with left:
                st.subheader("Allocation by ticker")
                allocation = (
                    priced.groupby("ticker", as_index=False)["market_value"]
                    .sum()
                )
                fig = px.pie(
                    allocation,
                    names="ticker",
                    values="market_value",
                    hole=0.45,
                )
                st.plotly_chart(fig, use_container_width=True)

            with right:
                st.subheader("Cost basis vs. market value")
                chart_df = priced.melt(
                    id_vars=["ticker"],
                    value_vars=["cost_basis", "market_value"],
                    var_name="Measure",
                    value_name="Amount",
                )
                fig = px.bar(
                    chart_df,
                    x="ticker",
                    y="Amount",
                    color="Measure",
                    barmode="group",
                )
                st.plotly_chart(fig, use_container_width=True)

    if not transactions.empty:
        st.subheader("Recent transactions")
        recent = transactions.sort_values(
            ["trade_date", "id"], ascending=False
        ).head(15).copy()

        for col in ["quantity", "price"]:
            recent[col] = pd.to_numeric(recent[col], errors="coerce")

        st.dataframe(
            recent[
                [
                    "trade_date", "ticker", "transaction_type",
                    "quantity", "price", "notes",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )


# ============================================================
# 9. EXPORTS
# ============================================================

def csv_download(df: pd.DataFrame, filename: str, label: str):
    payload = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label,
        data=payload,
        file_name=filename,
        mime="text/csv",
        use_container_width=True,
    )


def excel_download(
    holdings: pd.DataFrame,
    transactions: pd.DataFrame,
    filename: str,
    label: str,
):
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        holdings.to_excel(
            writer, sheet_name="Holdings", index=False
        )
        transactions.to_excel(
            writer, sheet_name="Transactions", index=False
        )

    st.download_button(
        label,
        data=output.getvalue(),
        file_name=filename,
        mime=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        use_container_width=True,
    )


# ============================================================
# 10. STREAMLIT PAGES
# ============================================================

def page_overview():
    clients = get_clients()
    holdings = build_holdings()
    transactions = get_transactions()

    st.title("📊 Wealth Desk")
    st.caption("Consolidated view of all active client portfolios")

    if clients.empty:
        st.info("Create your first client in Client Management.")
    else:
        st.metric("Active clients", len(clients))

    render_metrics(holdings)

    if not holdings.empty:
        st.subheader("Consolidated holdings")
        render_holdings_table(holdings)

        priced = holdings[
            holdings["price_status"].eq("OK")
            & holdings["market_value"].notna()
        ]
        if not priced.empty:
            st.subheader("Portfolio value by client")
            by_client = (
                priced.groupby("client_name", as_index=False)["market_value"]
                .sum()
                .sort_values("market_value", ascending=False)
            )
            fig = px.bar(
                by_client,
                x="client_name",
                y="market_value",
                labels={
                    "client_name": "Client",
                    "market_value": "Market value (INR)",
                },
            )
            st.plotly_chart(fig, use_container_width=True)

    if not clients.empty:
        st.subheader("Client directory")
        st.dataframe(
            clients[["id", "name", "email", "phone", "created_at"]],
            use_container_width=True,
            hide_index=True,
        )

    with st.expander("Export consolidated reports"):
        col1, col2 = st.columns(2)
        with col1:
            csv_download(
                holdings,
                "wealth_desk_consolidated_holdings.csv",
                "Download holdings CSV",
            )
        with col2:
            excel_download(
                holdings,
                transactions,
                "wealth_desk_consolidated_report.xlsx",
                "Download Excel report",
            )


def page_clients():
    st.title("👥 Client Management")

    create_tab, edit_tab = st.tabs(["Add client", "Edit / archive client"])

    with create_tab:
        with st.form("create_client_form", clear_on_submit=True):
            name = st.text_input("Client name *")
            email = st.text_input("Email")
            phone = st.text_input("Phone")
            notes = st.text_area("Notes")
            submitted = st.form_submit_button(
                "Create client", use_container_width=True
            )

        if submitted:
            try:
                create_client(name, email, phone, notes)
                st.success("Client created.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not create client: {exc}")

    with edit_tab:
        all_clients = get_clients(include_archived=True)

        if all_clients.empty:
            st.info("No clients have been created yet.")
            return

        options = {
            f"{row['name']} (ID {row['id']})": row
            for row in all_clients.to_dict("records")
        }
        selected_label = st.selectbox(
            "Select client",
            list(options.keys()),
        )
        selected = options[selected_label]

        with st.form("edit_client_form"):
            name = st.text_input("Client name", value=selected["name"])
            email = st.text_input(
                "Email", value=selected.get("email") or ""
            )
            phone = st.text_input(
                "Phone", value=selected.get("phone") or ""
            )
            notes = st.text_area(
                "Notes", value=selected.get("notes") or ""
            )
            save = st.form_submit_button(
                "Save changes", use_container_width=True
            )

        if save:
            try:
                update_client(
                    int(selected["id"]), name, email, phone, notes
                )
                st.success("Client updated.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not update client: {exc}")

        st.divider()
        is_archived = bool(selected["is_archived"])

        if not is_archived:
            st.warning(
                "Archiving hides this client from active client selectors. "
                "It does not delete the client's transactions."
            )
            if st.button("Archive selected client"):
                set_client_archived(int(selected["id"]), True)
                st.success("Client archived.")
                st.rerun()
        else:
            st.info("This client is archived.")
            if st.button("Restore selected client"):
                set_client_archived(int(selected["id"]), False)
                st.success("Client restored.")
                st.rerun()


def page_transactions():
    st.title("🧾 Transaction Management")

    clients = get_clients()
    if clients.empty:
        st.warning("Create an active client before adding transactions.")
        return

    client_map = {
        f"{row['name']} (ID {row['id']})": int(row["id"])
        for row in clients.to_dict("records")
    }

    with st.form("add_transaction_form", clear_on_submit=True):
        client_label = st.selectbox("Client", list(client_map.keys()))
        ticker = st.text_input(
            "Yahoo Finance ticker *",
            placeholder="RELIANCE.NS",
            help="Use .NS for NSE or .BO for BSE when supported.",
        )
        transaction_type = st.selectbox(
            "Transaction type", ["BUY", "SELL"]
        )
        col1, col2 = st.columns(2)
        with col1:
            quantity_input = st.text_input(
                "Quantity *", placeholder="10 or 0.5"
            )
        with col2:
            price_input = st.text_input(
                "Price per share (INR) *", placeholder="1250.00"
            )

        trade_date = st.date_input(
            "Trade date",
            value=date.today(),
            max_value=date.today(),
        )
        notes = st.text_input("Notes (optional)")
        submitted = st.form_submit_button(
            "Save transaction", use_container_width=True
        )

    if submitted:
        try:
            quantity = decimal_value(quantity_input)
            price = decimal_value(price_input)

            add_transaction(
                client_id=client_map[client_label],
                ticker=ticker,
                transaction_type=transaction_type,
                quantity=quantity,
                price=price,
                trade_date=trade_date,
                notes=notes,
            )
            st.success("Transaction saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Transaction was not saved: {exc}")

    st.divider()
    st.subheader("Transaction history")

    selected_client_label = st.selectbox(
        "Filter history by client",
        ["All clients"] + list(client_map.keys()),
        key="transaction_history_filter",
    )

    selected_client_id = (
        None if selected_client_label == "All clients"
        else client_map[selected_client_label]
    )
    history = get_transactions(selected_client_id)

    if history.empty:
        st.info("No transactions found.")
        return

    st.dataframe(
        history[
            [
                "id", "client_name", "trade_date", "ticker",
                "transaction_type", "quantity", "price", "notes",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

    with st.expander("Correct an incorrect transaction"):
        st.warning(
            "Deleting a transaction changes FIFO cost basis and P&L. "
            "Export a report first if you need a record of the original."
        )

        tx_options = {
            (
                f"ID {row['id']} · {row['client_name']} · "
                f"{row['ticker']} · {row['transaction_type']} · "
                f"{row['trade_date']}"
            ): int(row["id"])
            for row in history.to_dict("records")
        }

        chosen_tx = st.selectbox(
            "Transaction to delete",
            list(tx_options.keys()),
        )
        confirm_delete = st.checkbox(
            "I confirm I want to permanently delete this transaction."
        )

        if st.button("Delete transaction", type="secondary"):
            if not confirm_delete:
                st.error("Confirm the deletion first.")
            else:
                try:
                    delete_transaction(tx_options[chosen_tx])
                    st.success("Transaction deleted.")
                    st.rerun()
                except Exception as exc:
                    st.error(
                        "Deletion failed or made the remaining ledger "
                        f"invalid. Check transaction dates and quantities. {exc}"
                    )


def page_client_portfolio():
    st.title("📁 Client Portfolio")

    clients = get_clients()
    if clients.empty:
        st.info("Create an active client first.")
        return

    client_map = {
        f"{row['name']} (ID {row['id']})": int(row["id"])
        for row in clients.to_dict("records")
    }

    label = st.selectbox("Select client", list(client_map.keys()))
    client_id = client_map[label]

    holdings = build_holdings(client_id)
    transactions = get_transactions(client_id)

    render_portfolio_dashboard(
        holdings,
        transactions,
        title=label,
    )

    st.subheader("Client reports")
    col1, col2 = st.columns(2)
    with col1:
        csv_download(
            holdings,
            f"client_{client_id}_holdings.csv",
            "Download holdings CSV",
        )
    with col2:
        excel_download(
            holdings,
            transactions,
            f"client_{client_id}_report.xlsx",
            "Download Excel report",
        )


def page_reports():
    st.title("📑 Reports and Data Quality")

    clients = get_clients(include_archived=True)
    holdings = build_holdings()
    transactions = get_transactions()

    st.subheader("Price status")
    if holdings.empty:
        st.info("No open holdings to report.")
    else:
        price_status = holdings[
            [
                "client_name", "ticker", "quantity",
                "price_status", "price_date", "price_message",
            ]
        ]
        st.dataframe(
            price_status,
            use_container_width=True,
            hide_index=True,
        )

        missing = holdings[holdings["price_status"] != "OK"]
        if not missing.empty:
            st.warning(
                f"{len(missing)} open holding(s) have missing or stale "
                "prices and are excluded from current market-value totals."
            )

    st.subheader("Export")
    col1, col2 = st.columns(2)
    with col1:
        csv_download(
            transactions,
            "wealth_desk_transactions.csv",
            "Download transaction ledger CSV",
        )
    with col2:
        excel_download(
            holdings,
            transactions,
            "wealth_desk_full_report.xlsx",
            "Download full Excel report",
        )


# ============================================================
# 11. MAIN
# ============================================================

def main():
    if not st.session_state.get("authenticated", False):
        login_screen()
        return

    # Initialize tables after authentication so an unauthenticated visitor
    # cannot trigger database initialization through the UI.
    try:
        init_database()
    except Exception:
        st.error(
            "Cannot connect to PostgreSQL. Verify DATABASE_URL and "
            "confirm the database is reachable."
        )
        st.stop()

    with st.sidebar:
        st.title("📊 Wealth Desk")
        st.caption("Portfolio management")
        page = st.radio(
            "Navigation",
            [
                "Overview",
                "Client Management",
                "Transactions",
                "Client Portfolio",
                "Reports & Data Quality",
            ],
        )

        st.divider()
        st.caption("All figures in INR unless stated otherwise.")
        if st.button("Log out", use_container_width=True):
            st.session_state["authenticated"] = False
            st.rerun()

    try:
        if page == "Overview":
            page_overview()
        elif page == "Client Management":
            page_clients()
        elif page == "Transactions":
            page_transactions()
        elif page == "Client Portfolio":
            page_client_portfolio()
        elif page == "Reports & Data Quality":
            page_reports()
    except Exception:
        st.error(
            "The page could not be loaded. Check the database connection "
            "and transaction history. If the problem persists, inspect "
            "the Streamlit application logs."
        )


if __name__ == "__main__":
    main()
