from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import pandas as pd
import streamlit as st
import streamlit_authenticator as stauth

from acervo.sheets import read_users


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _resolve_username(credentials: dict[str, Any], login_id: str) -> str | None:
    normalized = login_id.strip().lower()
    if not normalized:
        return None
    for username, details in credentials.get("usernames", {}).items():
        if str(username).strip().lower() == normalized:
            return str(username).strip().lower()
        if str(details.get("email", "")).strip().lower() == normalized:
            return str(username).strip().lower()
    return None


def _cell(row: pd.Series, column: str) -> str:
    value = row.get(column, "")
    return "" if pd.isna(value) else str(value).strip()


def _credentials_from_users(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty or "password" not in frame.columns:
        return {"usernames": {}}

    usernames: dict[str, dict[str, str]] = {}
    for _, row in frame.iterrows():
        first_name = _cell(row, "first_name")
        last_name = _cell(row, "last_name")
        username = _cell(row, "username")
        if not username:
            username = f"{first_name}{last_name}".replace(" ", "")
        password = _cell(row, "password")
        if not username or not password:
            continue
        usernames[username.lower()] = {
            "name": first_name or username,
            "email": _cell(row, "email"),
            "password": password,
        }
    return {"usernames": usernames}


def _auth_config(connection: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        config = _plain(st.secrets.get("auth", {}))
    except Exception:
        config = {}
    credentials = config.get("credentials", {})
    if not credentials.get("usernames"):
        credentials = st.session_state.get("acervo_sheet_credentials", {})
        if not credentials.get("usernames"):
            credentials = _credentials_from_users(read_users(connection))
            st.session_state.acervo_sheet_credentials = credentials
    cookie = config.get("cookie", {})
    if not cookie.get("key") and os.getenv("ACERVO_COOKIE_KEY"):
        cookie["key"] = os.environ["ACERVO_COOKIE_KEY"]
    cookie.setdefault("name", "acervo_auth")
    cookie.setdefault("expiry_days", 30)
    return credentials, cookie


def _validate_config(
    credentials: dict[str, Any], cookie: dict[str, Any]
) -> str | None:
    if not credentials.get("usernames"):
        return (
            "Nenhum usuário foi encontrado. Crie a aba 'users' com as colunas "
            "first_name, last_name, email e password, ou configure "
            "auth.credentials.usernames nos Secrets."
        )
    if not str(cookie.get("key", "")).strip():
        return (
            "A chave do cookie não foi configurada. Adicione auth.cookie.key "
            "aos Secrets ou defina ACERVO_COOKIE_KEY."
        )
    if "SUBSTITUA" in str(cookie.get("key", "")).upper():
        return "A chave do cookie ainda contém o valor de exemplo."
    return None


def require_login(connection: Any):
    credentials, cookie = _auth_config(connection)
    config_error = _validate_config(credentials, cookie)
    if config_error:
        st.error(f"Configuração de login incompleta: {config_error}")
        st.stop()

    authenticator = stauth.Authenticate(
        credentials=credentials,
        cookie_name=str(cookie["name"]),
        cookie_key=str(cookie["key"]),
        cookie_expiry_days=float(cookie.get("expiry_days", 30)),
        auto_hash=True,
    )
    authenticator.login(location="unrendered")

    if st.session_state.get("authentication_status") is not True:
        _, center, _ = st.columns([1, 1.15, 1])
        with center:
            st.markdown(
                '<div class="login-brand"><div class="login-logo">◉</div>'
                '<div class="eyebrow">Música pessoal</div>'
                '<h1>Entrar no Acervo</h1>'
                '<p class="muted">Toda a sua música, em um só lugar.</p></div>',
                unsafe_allow_html=True,
            )
            with st.form("acervo_login", clear_on_submit=False):
                login_id = st.text_input(
                    "Usuário ou e-mail", autocomplete="username"
                )
                password = st.text_input(
                    "Senha", type="password", autocomplete="current-password"
                )
                submitted = st.form_submit_button(
                    "Entrar", type="primary", use_container_width=True
                )
            if submitted:
                username = _resolve_username(credentials, login_id)
                authenticated = bool(
                    username and authenticator.authentication_controller.login(
                        username, password
                    )
                )
                if authenticated:
                    authenticator.cookie_controller.set_cookie()
                    st.rerun()
                st.session_state["authentication_status"] = False
            if st.session_state.get("authentication_status") is False:
                st.error("Usuário, e-mail ou senha incorretos.")
        st.stop()

    username = str(st.session_state.get("username") or "").strip().lower()
    display_name = str(st.session_state.get("name") or username)
    return authenticator, username, display_name

