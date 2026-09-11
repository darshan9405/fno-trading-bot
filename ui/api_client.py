"""Streamlit API client.

Auth: prefers the browser cookies (single-origin prod, via st.context.cookies),
falls back to the bootstrap->session_state header path (dev, different ports).
Sends Authorization: Bearer <jwt>; silent-refreshes on 401.
"""

import os

import requests
import streamlit as st

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
PUBLIC_API = os.getenv("PUBLIC_API_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = 10


# --- token helpers -------------------------------------------------------


def _cookies() -> dict:
    try:
        return {k: v for k, v in st.context.cookies.items()}
    except Exception:
        return {}


def _tokens() -> dict:
    tokens = {}
    if st.session_state.get("access_token"):
        tokens["access_token"] = st.session_state["access_token"]
    if st.session_state.get("refresh_token"):
        tokens["refresh_token"] = st.session_state["refresh_token"]
    cookies = _cookies()
    tokens.setdefault("access_token", cookies.get("upstox_at"))
    tokens.setdefault("refresh_token", cookies.get("upstox_rt"))
    return tokens


def set_tokens(access: str, refresh: str | None = None) -> None:
    st.session_state["access_token"] = access
    if refresh:
        st.session_state["refresh_token"] = refresh


def clear_tokens() -> None:
    st.session_state.pop("access_token", None)
    st.session_state.pop("refresh_token", None)


def login_url() -> str:
    return f"{PUBLIC_API}/api/auth/upstox/login"


def logout() -> None:
    """Log out by redirecting to the backend logout endpoint.
    
    This ensures HttpOnly cookies are cleared by the browser.
    """
    clear_tokens()
    # Use JS to redirect to backend logout, which clears cookies via response headers
    st.markdown(
        f"""
        <script>
        window.location.href = "{BACKEND}/api/auth/logout";
        </script>
        """,
        unsafe_allow_html=True,
    )
    st.stop()


def bootstrap_from_query() -> None:
    """Exchange a one-time bootstrap code left by the SSO callback (dev path)."""
    code = st.query_params.get("bootstrap")
    if not code:
        return
    try:
        r = requests.post(f"{BACKEND}/api/auth/bootstrap", json={"code": code}, timeout=TIMEOUT)
        if r.ok:
            data = r.json()["data"]
            set_tokens(data["access_token"], data["refresh_token"])
            st.query_params.clear()
    except Exception:
        pass


def is_authenticated() -> bool:
    if not _tokens().get("access_token"):
        return False
    r = _raw("GET", "/api/auth/status")
    return bool(r and r.ok and r.json().get("data", {}).get("authenticated"))


# --- requests ------------------------------------------------------------


def _refresh() -> str | None:
    refresh = _tokens().get("refresh_token")
    if not refresh:
        return None
    try:
        r = requests.post(f"{BACKEND}/api/auth/refresh", headers={"X-Refresh-Token": refresh}, timeout=TIMEOUT)
        if r.ok:
            data = r.json()["data"]
            set_tokens(data["access_token"], data["refresh_token"])
            return data["access_token"]
    except Exception:
        pass
    return None


def _raw(method: str, path: str, headers: dict | None = None, **kw) -> requests.Response | None:
    h = {"Content-Type": "application/json"}
    access = _tokens().get("access_token")
    if access:
        h["Authorization"] = f"Bearer {access}"
    if headers:
        h.update(headers)
    try:
        r = requests.request(method, f"{BACKEND}{path}", headers=h, timeout=TIMEOUT, **kw)
    except requests.RequestException:
        return None
    if r.status_code == 401:
        new_access = _refresh()
        if new_access:
            h["Authorization"] = f"Bearer {new_access}"
            try:
                r = requests.request(method, f"{BACKEND}{path}", headers=h, timeout=TIMEOUT, **kw)
            except requests.RequestException:
                return None
    return r


def api(method: str, path: str, **kw) -> dict:
    r = _raw(method, path, **kw)
    if r is None:
        return {"status": "error", "error": {"code": "network", "message": "API unreachable"}}
    try:
        return r.json()
    except Exception:
        return {"status": "error", "error": {"code": "bad_json", "message": f"HTTP {r.status_code}"}}


# --- endpoints -----------------------------------------------------------


def get_pnl():
    return api("GET", "/api/trades/pnl")


def get_open_trades(date: str | None = None):
    path = "/api/trades/open"
    if date:
        path += f"?date={date}"
    return api("GET", path)


def get_closed_trades(date: str | None = None):
    path = "/api/trades/closed"
    if date:
        path += f"?date={date}"
    return api("GET", path)


def get_leads(status: str | None = None, date: str | None = None):
    params = []
    if date:
        params.append(f"date={date}")
    if status:
        params.append(f"status={status}")
    path = "/api/trades/leads"
    if params:
        path += "?" + "&".join(params)
    return api("GET", path)


def generate_leads():
    return api("POST", "/api/trades/leads/generate")


def get_instruments():
    return api("GET", "/api/instruments")


def set_instrument_enabled(instrument_id, enabled: bool):
    return api("PUT", f"/api/instruments/{instrument_id}", json={"enabled": enabled})


def get_health():
    return api("GET", "/api/health")


def get_killswitch():
    return api("GET", "/api/killswitch/status")


def activate_killswitch(reason: str):
    return api("POST", "/api/killswitch/activate", json={"reason": reason})


def release_killswitch():
    return api("POST", "/api/killswitch/release")


def get_config():
    return api("GET", "/api/config")


def update_config(payload: dict):
    return api("PUT", "/api/config", json=payload)