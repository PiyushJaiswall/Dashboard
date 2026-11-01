"""StackWealth Dashboard - entry point and routing."""
import logging
import streamlit as st
from auth import AuthResult, is_authenticated, load_auth_config, login, logout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
st.set_page_config(page_title="StackWealth Dashboard", page_icon="📈", layout="wide")

import components, views  # noqa: E402  (after set_page_config)

components.inject_css()


def login_page():
    st.markdown("<div class='sw-login-spacer'></div>", unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown("<div class='sw-brand'>Stack<span>Wealth</span> Dashboard</div>"
                    "<div class='sw-tag'>Multi-client Indian equity portfolio monitoring</div>", unsafe_allow_html=True)
        with st.form("login", clear_on_submit=False):
            u = st.text_input("Username", autocomplete="username")
            p = st.text_input("Password", type="password", autocomplete="current-password")
            ok = st.form_submit_button("Sign in", type="primary", use_container_width=True)
        if ok:
            try:
                secrets = dict(st.secrets)
            except Exception:
                secrets = {}
            r = login(st.session_state, u, p, load_auth_config(secrets))
            if r is AuthResult.OK:
                st.rerun()
            elif r is AuthResult.INVALID:
                st.error("Invalid username or password.")
            else:
                st.error("Sign-in is not configured correctly. The administrator should check the "
                         "[auth] section in Streamlit secrets (see README, 'Generate the password hash').")


def main():
    if not is_authenticated(st.session_state):
        st.session_state.pop("sw_auth", None)
        return login_page()
    with st.sidebar:
        st.markdown("<div class='sw-side-brand'>Stack<span>Wealth</span> Dashboard</div>", unsafe_allow_html=True)
        pres = st.toggle("Presentation mode", key="presentation", help="Hides editing controls and contact details.")
        pages = ["Dashboard", "Client Portfolio"] if pres else ["Dashboard", "Client Portfolio", "Clients", "Import / Export", "Audit & Diagnostics"]
        page = st.radio("Navigation", pages, key="nav")
        st.caption(f"Signed in as {st.session_state.get('sw_user')}")
        if st.button("Sign out", use_container_width=True):
            logout(st.session_state); st.rerun()
    views.route(page, presentation=pres)


main()
