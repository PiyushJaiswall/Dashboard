"""Reusable UI pieces and the (small) stylesheet."""
import streamlit as st
from portfolio import fmt_inr, fmt_pct

NAVY, PANEL, EMERALD, RED, AMBER = "#0b1426", "#111d36", "#10b981", "#ef4444", "#f59e0b"

CSS = f"""
<style>
.stApp {{ background:{NAVY}; }}
[data-testid="stSidebar"] {{ background:{PANEL}; }}
html, body, .stApp, label, p, span, h1, h2, h3, h4 {{ color:#e6edf7; }}
[data-testid="stMetric"] {{ background:{PANEL}; border:1px solid #1f2f52; border-radius:8px; padding:14px 16px; }}
[data-testid="stMetricLabel"] p {{ color:#9fb0cc; font-size:.85rem; }}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {{ background:{EMERALD}; border-color:{EMERALD}; color:#04130d; font-weight:600; }}
.sw-brand {{ font-size:2rem; font-weight:700; text-align:center; color:#fff; }}
.sw-brand span, .sw-side-brand span {{ color:{EMERALD}; }}
.sw-side-brand {{ font-size:1.25rem; font-weight:700; margin-bottom:.5rem; color:#fff; }}
.sw-tag {{ text-align:center; color:#9fb0cc; margin:.25rem 0 1.25rem; }}
.sw-login-spacer {{ height:8vh; }}
.sw-note {{ border-left:3px solid {AMBER}; background:#2a2310; padding:.6rem .9rem; border-radius:4px; color:#fde68a; }}
</style>"""


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


def pnl_color(v):
    return "#9fb0cc" if v is None else (EMERALD if v > 0 else RED if v < 0 else "#9fb0cc")


def kpis(s: dict, extra: dict = None):
    c = st.columns(4)
    c[0].metric("Invested capital (all holdings)", fmt_inr(s["invested_total"]))
    c[1].metric("Current market value (priced)", fmt_inr(s["market_value"]))
    c[2].metric("Unrealized P&L (priced)", fmt_inr(s["pnl"], sign=True),
                delta=fmt_pct(s["return_pct"]) if s["return_pct"] is not None else None)
    c[3].metric("Return on priced capital", fmt_pct(s["return_pct"]))
    d = st.columns(4)
    for i, (k, v) in enumerate((extra or {}).items()):
        d[i % 4].metric(k, v)
    d[2].metric("Invested capital (priced)", fmt_inr(s["invested_priced"]))
    d[3].metric("Price coverage", f"{s['priced_positions']}/{s['positions']}")
    if s["missing_positions"]:
        st.markdown(f"<div class='sw-note'>{s['missing_positions']} position(s) have no price and are excluded from market value, "
                    f"P&L and return. Totals cover {fmt_pct(s['capital_coverage_pct'], sign=False)} of invested capital.</div>",
                    unsafe_allow_html=True)
