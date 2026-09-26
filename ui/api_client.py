"""Streamlit API client.

Auth: prefers the browser cookies (single-origin prod, via st.context.cookies),
falls back to the bootstrap->session_state header path (dev, different ports).
Sends Authorization: Bearer <jwt>; silent-refreshes on 401.
"""

import json
import os

import requests
import streamlit as st

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
PUBLIC_API = os.getenv("PUBLIC_API_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = 10
# /api/trades/leads/generate dispatches to a background thread server-side and
# returns 202 immediately, so a 5s ceiling here is enough: a failed POST just
# means the request itself couldn't reach the server, not that the run is slow.
LEAD_GEN_TIMEOUT = 5


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


def logout_url() -> str:
    """Absolute backend URL for the SSO logout endpoint.

    MUST be rendered as a real `<a target="_top">` (not a programmatic JS .click()
    inside the iframe): Streamlit's sandboxed iframe does not allow top-frame
    navigation triggered from JS without `allow-top-navigation-by-user-activation`,
    so a hidden anchor + `.click()` is silently dropped by modern browsers. A real
    user click on the link is what actually navigates the top frame.
    """
    return f"{PUBLIC_API}/api/auth/logout"


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
    timeout = kw.pop("timeout", TIMEOUT)
    try:
        r = requests.request(method, f"{BACKEND}{path}", headers=h, timeout=timeout, **kw)
    except requests.RequestException:
        return None
    if r.status_code == 401:
        new_access = _refresh()
        if new_access:
            h["Authorization"] = f"Bearer {new_access}"
            try:
                r = requests.request(method, f"{BACKEND}{path}", headers=h, timeout=timeout, **kw)
            except requests.RequestException:
                return None
    return r


def api(method: str, path: str, **kw) -> dict:
    """Single entry point for every backend call.

    Error envelope (returned when something went wrong):
        {
          "status": "error",
          "error": {
            "code": "<machine-readable tag>",
            "message": "<human-readable detail>",
            "http_status": <int>,        # present on transport errors
            "endpoint": "<METHOD /path>", # present on transport errors
            "body": "<truncated server response>",
          },
        }

    The HTTP status / endpoint / body fields let the UI surface *why* a
    call failed (e.g. "HTTP 500 from GET /api/drifts: <html error>")
    instead of the previous opaque "HTTP 500".
    """
    r = _raw(method, path, **kw)
    if r is None:
        return {
            "status": "error",
            "error": {
                "code": "network",
                "message": "API unreachable (timeout or connection refused)",
                "endpoint": f"{method} {path}",
            },
        }
    if r.ok:
        # 2xx — server response. Try JSON; if the body is malformed,
        # surface it instead of pretending success.
        try:
            return r.json()
        except Exception as e:  # noqa: BLE001
            return {
                "status": "error",
                "error": {
                    "code": "bad_json",
                    "message": f"Response was not valid JSON: {e}",
                    "http_status": r.status_code,
                    "endpoint": f"{method} {path}",
                    "body": (r.text or "")[:300],
                },
            }
    # Non-2xx. Prefer JSON `{error: ...}` from the server; fall back to
    # the raw body so the operator can see the actual failure.
    try:
        parsed = r.json()
    except Exception:
        return {
            "status": "error",
            "error": {
                "code": f"http_{r.status_code}",
                "message": f"HTTP {r.status_code} from {method} {path}: "
                           f"{(r.text or '<empty body>')[:300]}",
                "http_status": r.status_code,
                "endpoint": f"{method} {path}",
                "body": (r.text or "")[:300],
            },
        }
    # Server returned JSON. Pass it through but normalise the shape so
    # the UI can rely on `error.code` / `error.message` consistently.
    if isinstance(parsed, dict) and parsed.get("status") == "ok":
        # Unusual — server said "ok" but used a non-2xx status. Surface.
        return {
            "status": "error",
            "error": {
                "code": f"http_{r.status_code}",
                "message": f"HTTP {r.status_code} (server reported success?)",
                "http_status": r.status_code,
                "endpoint": f"{method} {path}",
                "body": json.dumps(parsed)[:300],
            },
        }
    if isinstance(parsed, dict) and "error" in parsed and isinstance(parsed["error"], dict):
        err = dict(parsed["error"])
        err.setdefault("http_status", r.status_code)
        err.setdefault("endpoint", f"{method} {path}")
        return {"status": "error", "error": err}
    return {
        "status": "error",
        "error": {
            "code": f"http_{r.status_code}",
            "message": f"HTTP {r.status_code} from {method} {path}",
            "http_status": r.status_code,
            "endpoint": f"{method} {path}",
            "body": json.dumps(parsed)[:300] if parsed is not None else "",
        },
    }


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


def get_leads(status: str | None = None, date: str | None = None, sort: str | None = None):
    params = []
    if date:
        params.append(f"date={date}")
    if status:
        params.append(f"status={status}")
    if sort:
        params.append(f"sort={sort}")
    path = "/api/trades/leads"
    if params:
        path += "?" + "&".join(params)
    return api("GET", path)


def get_lead_detail(lead_id: int):
    """Per-lead detail for the "Why this lead?" expander.

    Returns the same shape as a single item in `get_leads` plus a `trade`
    block if the lead was placed.
    """
    return api("GET", f"/api/trades/leads/{lead_id}")


def get_closed_trade_detail(trade_id: int):
    """Single closed-trade view for post-trade monitoring.

    Returns the same shape as a single item in `get_closed_trades` plus a
    `drifts` array of audit events recorded while the trade was open.
    """
    return api("GET", f"/api/trades/closed/{trade_id}")


def get_drifts(trade_id: int | None = None, since: str | None = None, limit: int = 200):
    """Drift audit events (SL mismatch, position missing, qty mismatch, …).

    Used by the post-trade monitoring tile in the History tab. Pass
    `trade_id` to scope to a single trade; `since` as an ISO datetime.
    """
    params = []
    if trade_id is not None:
        params.append(f"trade_id={trade_id}")
    if since:
        params.append(f"since={since}")
    if limit:
        params.append(f"limit={limit}")
    path = "/api/drifts"
    if params:
        path += "?" + "&".join(params)
    return api("GET", path)


def get_drift_summary():
    """Counts by drift_type + severity for the last 24h (header tile)."""
    return api("GET", "/api/drifts/summary")


def generate_leads():
    """Kick off a manual lead-generation run.

    The endpoint now dispatches the work on the server and returns 202
    immediately, so we use a short timeout — a failed POST only means the
    server is unreachable, not that the run itself is slow. The actual wait
    happens via :func:`get_lead_gen_status` polling.
    """
    return api("POST", "/api/trades/leads/generate", timeout=LEAD_GEN_TIMEOUT)


def get_lead_gen_status(job_id: str):
    return api("GET", f"/api/trades/leads/generate/{job_id}", timeout=LEAD_GEN_TIMEOUT)


def purge_leads():
    """Delete every row in the `leads` table regardless of status.

    The backend nulls out `Trade.lead_id` first so audit data survives. The
    caller is expected to gate this through a typed-confirmation dialog so
    a stray click can't wipe queued signals.
    """
    return api("DELETE", "/api/trades/leads", timeout=15)


def get_active_lead_gen_job():
    """Look up the in-flight manual lead-generation job, if any.

    Used on page load to recover the running-job handle after a tab reload
    — without this, a Streamlit page refresh clears session_state and the
    "Generate now" button silently re-enables even though a run is mid-flight.
    Returns ``{"status": "error", "error": {"code": "lead_generation_no_active_job"}}``
    when nothing is running; callers treat that as "no job to attach to".
    """
    return api("GET", "/api/trades/leads/generate/active", timeout=LEAD_GEN_TIMEOUT)


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