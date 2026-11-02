"""
StackWealth Dashboard — Client Details & Insights

Features:
- Client portfolio overview
- Add and update clients
- Add, update and remove stock holdings
- Portfolio allocation charts
- Performance and benchmark analysis
- Client archiving and reactivation
- Excel export
- Database audit logging
- Presentation mode

Run using:
    streamlit run app.py
"""

from __future__ import annotations

import html
import io
import json
import logging
import re

import pandas as pd
import streamlit as st

from app import (
    APP_NAME,
    CSS,
    GREEN,
    RED,
    TEXT,
    POS_SQL,
    authed,
    bench_card,
    card,
    chips,
    concentration,
    ctitle,
    db,
    donut_fig,
    enrich,
    fmt_cr,
    fmt_inr,
    load_positions,
    normalize_symbol,
    pct,
    pnl_bar_fig,
    refresh_ui,
    section,
    short,
    show,
    tone,
    tone_css,
    totals,
    treemap_fig,
    value_series,
)


log = logging.getLogger("stackwealth.client")

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE = re.compile(r"^\+?[0-9][0-9 \-()]{6,18}$")


# ============================================================
# DATABASE AUDIT
# ============================================================

def audit(conn, action, entity_id, details=None):
    """Record a client or holding operation in the audit table."""

    payload = dict(details or {})
    payload["actor"] = st.session_state.get("sw_user", "admin")

    entity_type = (
        "holding"
        if action.startswith("holding.")
        else "client"
    )

    conn.execute(
        """
        INSERT INTO audit_logs (
            action,
            entity_type,
            entity_id,
            details
        )
        VALUES (%s, %s, %s, %s::jsonb)
        """,
        (
            action,
            entity_type,
            int(entity_id) if entity_id is not None else None,
            json.dumps(payload, default=str),
        ),
    )


# ============================================================
# VALIDATION
# ============================================================

def clean_contact(name, email, phone, notes):
    """Validate and normalize client contact information."""

    name = (name or "").strip()
    email = (email or "").strip()
    phone = (phone or "").strip()
    notes = (notes or "").strip()

    if not 1 <= len(name) <= 120:
        raise ValueError(
            "Client name is required and must be at most 120 characters."
        )

    if email and not EMAIL.fullmatch(email):
        raise ValueError("Please enter a valid email address.")

    if phone and not PHONE.fullmatch(phone):
        raise ValueError("Please enter a valid phone number.")

    return (
        name,
        email or None,
        phone or None,
        notes or None,
    )


# ============================================================
# CLIENT PORTFOLIO DATA
# ============================================================

def positions_for(client_id) -> pd.DataFrame:
    """Load holdings belonging to one client."""

    query = POS_SQL.replace(
        "WHERE c.status='active'",
        "WHERE c.id=%s",
    )

    with db() as conn:
        cursor = conn.execute(query, (client_id,))
        columns = [column.name for column in cursor.description]
        records = [
            dict(zip(columns, row))
            for row in cursor.fetchall()
        ]

    return enrich(records)


def get_client(client_id):
    """Fetch one client record."""

    with db() as conn:
        return conn.execute(
            """
            SELECT id, name, email, phone, notes, status
            FROM clients
            WHERE id = %s
            """,
            (client_id,),
        ).fetchone()


# ============================================================
# MAIN CLIENT DETAILS PAGE
# ============================================================

def page():
    """Render the complete Client Details page."""

    # Sidebar
    with st.sidebar:
        st.markdown(
            """
            <div class="brand" style="font-size:1.2rem">
                Stack<span>Wealth</span> Dashboard
            </div>
            """,
            unsafe_allow_html=True,
        )

        present = st.toggle(
            "Presentation mode",
            value=True,
            key="client_presentation_mode",
            help="ON: clean presentation view. OFF: show holdings, client editing and management controls."
        )

        home_page = st.session_state.get("_sw_home_page")

        if home_page is not None:
            st.page_link(
                home_page,
                label="← Home dashboard",
                icon="📊",
            )
        else:
            st.page_link(
                "app.py",
                label="← Home dashboard",
                icon="📊",
            )

        st.divider()

        st.caption(
            f"Signed in as {st.session_state.get('sw_user', 'admin')}"
        )

        if st.button("Sign out", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    # Load clients
    with db() as conn:
        clients = conn.execute(
            """
            SELECT id, name, email, phone, notes, status
            FROM clients
            ORDER BY
                CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                name
            """
        ).fetchall()

    # Create page tabs
    tab_names = ["Overview"]

    if not present:
        tab_names.extend(
            [
                "Holdings",
                "Client details",
                "New client",
            ]
        )

    tabs = st.tabs(tab_names)

    # No clients exist yet
    if not clients:
        with tabs[0]:
            st.markdown(
                """
                <div class="brand">Client management</div>
                <div class="tag">
                    Manage clients and their investment portfolios.
                </div>
                """,
                unsafe_allow_html=True,
            )

            st.info(
                "No clients found. Create your first client to get started."
            )

        if not present:
            with tabs[-1]:
                new_client_form()

        return

    # Client selection
    client_ids = [client[0] for client in clients]

    previous_client = st.session_state.get("open_client")

    if previous_client in client_ids:
        selected_index = client_ids.index(previous_client)
    else:
        selected_index = 0

    selected_client = st.selectbox(
        "Select client",
        clients,
        index=selected_index,
        format_func=lambda client: (
            f"{client[1]} "
            f"({'Archived' if client[5] == 'archived' else 'Active'})"
        ),
        key="client_selector",
    )

    client_id = selected_client[0]

    st.session_state["open_client"] = client_id

    # Re-fetch the selected client from the database
    client = get_client(client_id)

    if not client:
        st.error("The selected client could not be found.")
        return

    (
        client_id,
        name,
        email,
        phone,
        notes,
        status,
    ) = client

    # Load portfolio positions
    try:
        df = positions_for(client_id)
    except Exception as exc:
        log.exception("Failed to load client positions.")

        st.error(
            "Unable to load this client's holdings. "
            "Check the database connection and application logs."
        )
        return

    # Overview
    with tabs[0]:
        overview(
            df=df,
            client_id=client_id,
            name=name,
            status=status,
            email=email,
            phone=phone,
            present=present,
        )

    # Editing tabs
    if not present:
        with tabs[1]:
            holdings_tab(
                client_id=client_id,
                name=name,
                status=status,
                df=df,
            )

        with tabs[2]:
            details_tab(
                client_id=client_id,
                name=name,
                email=email,
                phone=phone,
                notes=notes,
                status=status,
            )

        with tabs[3]:
            new_client_form()


# ============================================================
# CLIENT PORTFOLIO OVERVIEW
# ============================================================

def overview(
    df,
    client_id,
    name,
    status,
    email,
    phone,
    present,
):
    """Display client portfolio statistics, charts and holdings."""

    closes = st.session_state.get("closes")

    # Header
    left, right = st.columns([4, 1])

    with left:
        st.markdown(
            f"""
            <div class="brand">
                {html.escape(str(name))}
            </div>
            <div class="tag">
                Individual portfolio dashboard
            </div>
            """,
            unsafe_allow_html=True,
        )

    if not present:
        with right:
            if st.button(
                "Refresh market data",
                type="primary",
                use_container_width=True,
            ):
                refresh_ui()

        refresh_message = st.session_state.get("refresh_msg")

        if refresh_message:
            st.caption(refresh_message)

    # Client contact information
    if not present:
        contact = (
            f"Email: {email or 'Not provided'}"
            f" · Phone: {phone or 'Not provided'}"
        )
    else:
        contact = ""

    st.markdown(
        f"""
        <div class="chip">
            Status: {html.escape(str(status).title())}
        </div>
        """,
        unsafe_allow_html=True,
    )

    if contact:
        st.caption(contact)

    # Empty portfolio
    if df.empty:
        st.markdown(
            f"""
            <div class="hero">
                <div class="app">Client portfolio</div>
                <div class="lbl">
                    {html.escape(str(name))}
                </div>
                <div class="big">₹0</div>
                <div class="sub">
                    No holdings have been added to this portfolio.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.info(
            "Open the Holdings tab to add stocks to this client's portfolio."
        )

        return

    # Price freshness information
    chips(df, closes)

    # Portfolio calculations
    t = totals(df)

    priced = df[df["priced"].astype(bool)].copy()

    if not priced.empty:
        series = value_series(priced, closes)
    else:
        series = pd.Series(dtype=float)

    day = None

    if len(series) >= 2 and series.iloc[-2] > 0:
        day = float(
            (series.iloc[-1] / series.iloc[-2] - 1) * 100
        )

    pnl_color = (
        tone(day if day is not None else t["pnl"])
        if t["n_priced"]
        else TEXT
    )

    portfolio_value = (
        t["value"]
        if t["n_priced"]
        else t["invested"]
    )

    portfolio_label = (
        "Current portfolio value"
        if t["n_priced"]
        else "Total invested capital"
    )

    portfolio_subtitle = (
        f"Unrealized P&L: {fmt_inr(t['pnl'], sign=True)} "
        f"({pct(t['ret'])})"
        if t["n_priced"]
        else "Market prices unavailable. Showing invested capital."
    )

    if day is not None:
        portfolio_subtitle += (
            f" · Latest historical session: {pct(day)}"
        )

    # Main portfolio hero
    st.markdown(
        f"""
        <div class="hero">
            <div class="app">
                StackWealth Dashboard · Client Portfolio
            </div>
            <div class="lbl">
                {html.escape(str(name))}
            </div>
            <div class="lbl">
                {html.escape(portfolio_label)}
            </div>
            <div class="big" style="color:{pnl_color}">
                {html.escape(fmt_inr(portfolio_value))}
            </div>
            <div class="sub">
                {html.escape(portfolio_subtitle)}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Missing prices warning
    if t["n_priced"] < t["n"]:
        st.warning(
            f"{t['n'] - t['n_priced']} holding(s) do not have "
            "a valid market price. They are excluded from market "
            "value and unrealized P&L."
        )

    # Calculate client share of all active-client portfolios
    try:
        all_positions = load_positions()

        total_all_value = (
            totals(all_positions)["value"]
            if not all_positions.empty
            else 0
        )

        share = (
            t["value"] / total_all_value * 100
            if total_all_value > 0 and t["n_priced"]
            else None
        )

    except Exception:
        log.exception("Failed to calculate combined portfolio share.")
        share = None

    basis = "Value" if t["n_priced"] else "Invested"

    top5, hhi = concentration(df, basis)

    best_position = None
    worst_position = None

    if not priced.empty and priced["Ret"].notna().any():
        best_position = priced.loc[priced["Ret"].idxmax()]
        worst_position = priced.loc[priced["Ret"].idxmin()]

    # Primary KPI cards
    st.markdown(
        '<div class="sec">Portfolio summary</div>',
        unsafe_allow_html=True,
    )

    kpis = [
        (
            "Invested capital",
            fmt_inr(t["invested"]),
            "Across all holdings",
            TEXT,
        ),
        (
            "Market value",
            fmt_inr(t["value"]) if t["n_priced"] else "—",
            "Holdings with valid prices",
            TEXT,
        ),
        (
            "Unrealized P&L",
            fmt_inr(t["pnl"], sign=True)
            if t["n_priced"]
            else "—",
            pct(t["ret"]),
            tone(t["pnl"]) if t["n_priced"] else TEXT,
        ),
        (
            "Stock positions",
            str(t["n"]),
            f"{t['n_priced']} priced",
            TEXT,
        ),
    ]

    for col, values in zip(st.columns(4), kpis):
        with col:
            st.markdown(
                card(*values),
                unsafe_allow_html=True,
            )

    st.markdown('<div style="height:12px"></div>', unsafe_allow_html=True)

    # Secondary KPI cards
    secondary_kpis = [
        (
            "Share of combined value",
            f"{share:.2f}%" if share is not None else "—",
            "Of active-client portfolios",
            TEXT,
        ),
        (
            "Top-5 concentration",
            f"{top5:.2f}%" if top5 is not None else "—",
            f"HHI: {hhi:,.0f}" if hhi is not None else "",
            TEXT,
        ),
        (
            "Best position",
            str(best_position["Symbol"])
            if best_position is not None
            else "—",
            pct(best_position["Ret"])
            if best_position is not None
            else "",
            GREEN,
        ),
        (
            "Weakest position",
            str(worst_position["Symbol"])
            if worst_position is not None
            else "—",
            pct(worst_position["Ret"])
            if worst_position is not None
            else "",
            RED,
        ),
    ]

    for col, values in zip(st.columns(4), secondary_kpis):
        with col:
            st.markdown(
                card(*values),
                unsafe_allow_html=True,
            )

    # Allocation charts
    section("Portfolio allocation")

    left, right = st.columns([2, 3])

    with left, st.container(border=True):
        ctitle("Allocation by stock")

        show(
            donut_fig(
                df,
                "Symbol",
                basis,
                title="Stock allocation",
                h=380,
            ),
            "No stock allocation data available.",
        )

    with right, st.container(border=True):
        ctitle("Sector and stock treemap")

        show(
            treemap_fig(df, h=380),
            "No treemap data available.",
        )

    col1, col2, col3 = st.columns(3)

    with col1, st.container(border=True):
        ctitle("Sector allocation")

        show(
            donut_fig(
                df,
                "Sector",
                basis,
                title="Sector allocation",
                h=320,
            )
        )

    with col2, st.container(border=True):
        ctitle("Market-cap allocation")

        show(
            donut_fig(
                df,
                "Cap",
                basis,
                title="Market-cap allocation",
                h=320,
            )
        )

        st.caption(
            "Market-cap categories depend on the metadata available "
            "in your database."
        )

    with col3, st.container(border=True):
        ctitle("Unrealized P&L by stock")

        show(
            pnl_bar_fig(
                priced["Symbol"],
                priced["PnL"],
                320,
            ),
            "Load market prices to see P&L.",
        )

    # Benchmark comparison
    section("Performance analysis")

    bench_card(
        df,
        closes,
        f"client_{client_id}",
        name,
    )

    # Holdings table
    section("Client holdings")

    search = st.text_input(
        "Search stock symbol",
        placeholder="Type a symbol, e.g. RELIANCE",
        key=f"holdings_search_{client_id}",
    )

    table = df.copy()

    if search:
        table = table[
            table["Symbol"].str.contains(
                search.strip(),
                case=False,
                regex=False,
                na=False,
            )
        ]

    # Weight is based on current market value when available;
    # otherwise it uses invested capital.
    denominator = table[basis].sum()

    if denominator and denominator > 0:
        table["Weight"] = table[basis] / denominator * 100
    else:
        table["Weight"] = None

    display_columns = [
        "Symbol",
        "Qty",
        "Avg",
        "Invested",
        "Price",
        "Value",
        "PnL",
        "Ret",
        "Weight",
        "price_date",
        "Sector",
        "Cap",
    ]

    display_table = table[display_columns].rename(
        columns={
            "Avg": "Average price",
            "PnL": "Unrealized P&L",
            "Ret": "Return %",
            "price_date": "Price date",
            "Weight": "Weight %",
            "Qty": "Quantity",
            "Value": "Market value",
            "Price": "Current price",
        }
    )

    formatters = {
        "Quantity": "{:,.4f}",
        "Average price": "₹{:,.2f}",
        "Invested": "₹{:,.2f}",
        "Current price": "₹{:,.2f}",
        "Market value": "₹{:,.2f}",
        "Unrealized P&L": "₹{:+,.2f}",
        "Return %": "{:+.2f}%",
        "Weight %": "{:.2f}%",
    }

    st.dataframe(
        display_table.style.format(
            formatters,
            na_rep="—",
        ).map(
            tone_css,
            subset=["Unrealized P&L", "Return %"],
        ),
        hide_index=True,
        use_container_width=True,
    )

    st.caption(
        "Market prices are provided by Yahoo Finance and may be delayed. "
        "P&L represents unrealized gains or losses only; it does not "
        "include brokerage, taxes, dividends or cash flows."
    )

    # Excel export
    if not present:
        buffer = io.BytesIO()

        with pd.ExcelWriter(
            buffer,
            engine="openpyxl",
        ) as writer:
            export_table = display_table.copy()

            # Prevent exported strings from being interpreted as formulas.
            for column in export_table.select_dtypes(
                include=["object", "string"]
            ).columns:
                export_table[column] = export_table[column].map(
                    lambda value: (
                        "'" + value
                        if isinstance(value, str)
                        and value.startswith(("=", "+", "-", "@"))
                        else value
                    )
                )

            export_table.to_excel(
                writer,
                sheet_name="Client Holdings",
                index=False,
            )

            pd.DataFrame(
                [
                    {
                        "Client": name,
                        "Status": status,
                        "Total invested": t["invested"],
                        "Market value": (
                            t["value"] if t["n_priced"] else None
                        ),
                        "Unrealized P&L": (
                            t["pnl"] if t["n_priced"] else None
                        ),
                        "Return %": t["ret"],
                        "Total positions": t["n"],
                        "Priced positions": t["n_priced"],
                    }
                ]
            ).to_excel(
                writer,
                sheet_name="Summary",
                index=False,
            )

        safe_name = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            str(name),
        ).strip("_") or "Client"

        st.download_button(
            "Download client portfolio (Excel)",
            data=buffer.getvalue(),
            file_name=f"StackWealth_{safe_name}.xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            use_container_width=False,
        )


# ============================================================
# HOLDINGS MANAGEMENT
# ============================================================

def holdings_tab(client_id, name, status, df):
    """Add, update and remove a client's stock holdings."""

    st.subheader("Manage holdings")

    if status != "active":
        st.warning(
            "This client is archived. Reactivate the client before "
            "editing holdings."
        )
        return

    st.caption(
        "Enter the stock symbol, quantity and average purchase price. "
        "Adding an existing symbol updates that holding."
    )

    with st.form(
        f"holding_form_{client_id}",
        clear_on_submit=True,
    ):
        col1, col2, col3 = st.columns(3)

        symbol_input = col1.text_input(
            "Stock symbol",
            placeholder="RELIANCE or NSE:RELIANCE",
        )

        quantity = col2.number_input(
            "Quantity",
            min_value=0.0,
            step=1.0,
            format="%.4f",
        )

        average_price = col3.number_input(
            "Average purchase price (₹)",
            min_value=0.0,
            step=0.05,
            format="%.4f",
        )

        submitted = st.form_submit_button(
            "Add / update holding",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        try:
            symbol = normalize_symbol(symbol_input)

            if quantity <= 0:
                raise ValueError(
                    "Quantity must be greater than zero."
                )

            if average_price <= 0:
                raise ValueError(
                    "Average purchase price must be greater than zero."
                )

            with db() as conn:
                result = conn.execute(
                    """
                    INSERT INTO current_holdings (
                        client_id,
                        symbol,
                        quantity,
                        avg_price
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (client_id, symbol)
                    DO UPDATE SET
                        quantity = EXCLUDED.quantity,
                        avg_price = EXCLUDED.avg_price,
                        updated_at = now()
                    RETURNING (xmax = 0)
                    """,
                    (
                        client_id,
                        symbol,
                        str(quantity),
                        str(average_price),
                    ),
                ).fetchone()

                created = bool(result[0])

                audit(
                    conn,
                    (
                        "holding.created"
                        if created
                        else "holding.updated"
                    ),
                    client_id,
                    {
                        "symbol": symbol,
                        "quantity": quantity,
                        "avg_price": average_price,
                    },
                )

            st.success(
                (
                    "Holding added successfully."
                    if created
                    else "Existing holding updated successfully."
                )
                + " Refresh market data to update its price."
            )

            st.rerun()

        except ValueError as exc:
            st.error(str(exc))

        except Exception:
            log.exception("Failed to save holding.")

            st.error(
                "Unable to save this holding. Check the database "
                "schema, connection and application logs."
            )

    # Existing holdings
    st.divider()
    st.subheader("Current holdings")

    if df.empty:
        st.info("This client has no holdings yet.")
        return

    st.dataframe(
        df[
            [
                "Symbol",
                "Qty",
                "Avg",
                "Invested",
                "Price",
                "Value",
                "PnL",
                "Ret",
            ]
        ].rename(
            columns={
                "Qty": "Quantity",
                "Avg": "Average price",
                "Price": "Current price",
                "Value": "Market value",
                "PnL": "Unrealized P&L",
                "Ret": "Return %",
            }
        ),
        hide_index=True,
        use_container_width=True,
    )

    st.divider()
    st.subheader("Remove holding")

    holding_symbols = list(df["sym"].dropna().unique())

    selected_symbol = st.selectbox(
        "Select a holding to remove",
        [""] + holding_symbols,
        format_func=lambda symbol: (
            short(symbol) if symbol else "Select a stock"
        ),
        key=f"remove_symbol_{client_id}",
    )

    if selected_symbol:
        confirm = st.checkbox(
            f"Confirm removal of {short(selected_symbol)} "
            f"from {name}.",
            key=f"confirm_remove_{client_id}",
        )

        if st.button(
            "Remove selected holding",
            type="secondary",
            disabled=not confirm,
            key=f"remove_button_{client_id}",
        ):
            try:
                with db() as conn:
                    deleted = conn.execute(
                        """
                        DELETE FROM current_holdings
                        WHERE client_id = %s
                          AND symbol = %s
                        """,
                        (
                            client_id,
                            selected_symbol,
                        ),
                    ).rowcount

                    if deleted:
                        audit(
                            conn,
                            "holding.removed",
                            client_id,
                            {"symbol": selected_symbol},
                        )

                if deleted:
                    st.success("Holding removed successfully.")
                else:
                    st.warning(
                        "The holding was not found. Refresh the page."
                    )

                st.rerun()

            except Exception:
                log.exception("Failed to remove holding.")

                st.error(
                    "Unable to remove the holding. Check the "
                    "database and application logs."
                )


# ============================================================
# CLIENT INFORMATION MANAGEMENT
# ============================================================

def details_tab(
    client_id,
    name,
    email,
    phone,
    notes,
    status,
):
    """Edit client details, archive a client or reactivate one."""

    st.subheader("Client information")

    with st.form(f"edit_client_{client_id}"):
        new_name = st.text_input(
            "Client name",
            value=name or "",
        )

        new_email = st.text_input(
            "Email address",
            value=email or "",
        )

        new_phone = st.text_input(
            "Phone number",
            value=phone or "",
        )

        new_notes = st.text_area(
            "Notes",
            value=notes or "",
            height=120,
        )

        save = st.form_submit_button(
            "Save changes",
            type="primary",
            use_container_width=True,
        )

    if save:
        try:
            (
                new_name,
                new_email,
                new_phone,
                new_notes,
            ) = clean_contact(
                new_name,
                new_email,
                new_phone,
                new_notes,
            )

            with db() as conn:
                conn.execute(
                    """
                    UPDATE clients
                    SET
                        name = %s,
                        email = %s,
                        phone = %s,
                        notes = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        new_name,
                        new_email,
                        new_phone,
                        new_notes,
                        client_id,
                    ),
                )

                audit(
                    conn,
                    "client.updated",
                    client_id,
                    {"name": new_name},
                )

            st.success("Client details saved successfully.")

            st.rerun()

        except ValueError as exc:
            st.error(str(exc))

        except Exception:
            log.exception("Failed to update client.")

            st.error(
                "Unable to update the client. Check the database "
                "connection and application logs."
            )

    st.divider()

    # Archive/reactivate
    st.subheader("Client status")

    if status == "active":
        st.warning(
            "Archiving excludes this client from active-client "
            "portfolio totals. The client's holdings are retained."
        )

        confirm_archive = st.checkbox(
            "I confirm that I want to archive this client.",
            key=f"confirm_archive_{client_id}",
        )

        if st.button(
            "Archive client",
            disabled=not confirm_archive,
            key=f"archive_{client_id}",
        ):
            try:
                with db() as conn:
                    conn.execute(
                        """
                        UPDATE clients
                        SET status = 'archived',
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (client_id,),
                    )

                    audit(
                        conn,
                        "client.archived",
                        client_id,
                        {},
                    )

                st.success("Client archived successfully.")

                st.rerun()

            except Exception:
                log.exception("Failed to archive client.")

                st.error(
                    "Unable to archive the client. Check the "
                    "database and application logs."
                )

    else:
        st.info(
            "This client is archived. Reactivating restores the "
            "client to active-client portfolio totals."
        )

        if st.button(
            "Reactivate client",
            type="primary",
            key=f"reactivate_{client_id}",
        ):
            try:
                with db() as conn:
                    conn.execute(
                        """
                        UPDATE clients
                        SET status = 'active',
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (client_id,),
                    )

                    audit(
                        conn,
                        "client.reactivated",
                        client_id,
                        {},
                    )

                st.success("Client reactivated successfully.")

                st.rerun()

            except Exception:
                log.exception("Failed to reactivate client.")

                st.error(
                    "Unable to reactivate the client. Check the "
                    "database and application logs."
                )


# ============================================================
# CREATE NEW CLIENT
# ============================================================

def new_client_form():
    """Create a new client record."""

    st.subheader("Add a new client")

    st.caption(
        "Create a client profile first. You can then open the "
        "client's portfolio and add stock holdings."
    )

    with st.form(
        "new_client_form",
        clear_on_submit=True,
    ):
        name = st.text_input(
            "Client name",
            placeholder="Enter client name",
        )

        email = st.text_input(
            "Email address (optional)",
        )

        phone = st.text_input(
            "Phone number (optional)",
        )

        notes = st.text_area(
            "Notes (optional)",
            height=100,
        )

        submitted = st.form_submit_button(
            "Create client",
            type="primary",
            use_container_width=True,
        )

    if not submitted:
        return

    try:
        name, email, phone, notes = clean_contact(
            name,
            email,
            phone,
            notes,
        )

        with db() as conn:
            client_id = conn.execute(
                """
                INSERT INTO clients (
                    name,
                    email,
                    phone,
                    notes,
                    status
                )
                VALUES (%s, %s, %s, %s, 'active')
                RETURNING id
                """,
                (
                    name,
                    email,
                    phone,
                    notes,
                ),
            ).fetchone()[0]

            audit(
                conn,
                "client.created",
                client_id,
                {"name": name},
            )

        st.session_state["open_client"] = client_id

        st.success(
            f"Client '{name}' created successfully."
        )

        st.rerun()

    except ValueError as exc:
        st.error(str(exc))

    except Exception:
        log.exception("Failed to create client.")

        st.error(
            "Unable to create the client. Check your database "
            "connection, table schema and application logs."
        )


# ============================================================
# STREAMLIT ENTRY POINT
# ============================================================

def main():
    """
    Client Details is registered as a page by app.py.
    The main application handles page configuration and navigation.
    """

    if not authed():
        st.warning(
            "Please sign in through the StackWealth Dashboard first."
        )
        st.stop()

    st.markdown(CSS, unsafe_allow_html=True)

    try:
        page()

    except Exception:
        log.exception("Unexpected Client Details page failure.")

        st.error(
            "Something went wrong while displaying Client Details. "
            "Check the Streamlit application logs for the traceback."
        )


# Execute when Streamlit opens this page.
main()
