"""F&O Automated Trading — Streamlit UI.

Tabs: Dashboard (live P&L), Open Trades, Leads, Instruments, History, Health, Settings.
Auto-polls Dashboard + Open Trades via st.fragment(run_every).

Enhanced for ease of use:
- Unified top header with status, IST clock, refresh countdown
- Dashboard: P&L sparkline trajectory + day summary stats + per-position mini-cards
- Open Trades: visual position cards with SL-distance bars + LTP delta
- Leads: filter bar + confidence gauges + segment chips
- History: stats panel (win rate, profit factor, drawdown) + P&L curve + period filter
- Instruments: segment chips + bulk enable/disable
- Settings: tabbed sections with reset/confirm
- Health: searchable error log + uptime indicators
- Global: empty-state hints, tooltips, loading spinners
"""

import json
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import api_client as api

st.set_page_config(page_title="TradePilot", page_icon=":material/show_chart:", layout="centered")

# Palette — dark fintech theme
PRIMARY = "#6366f1"    # indigo (primary accent / buttons / links)
SECONDARY = "#22d3ee"  # cyan (secondary accent / info)
PROFIT = "#34d399"     # emerald (positive P&L)
LOSS = "#fb7185"       # rose (negative P&L)
WARN = "#fbbf24"       # amber
MUTED = "#94a3b8"      # slate (secondary text)
BG = "#0b1220"         # app background
CARD = "#131c2e"       # cards
BORDER = "#243049"     # card borders

REFRESH_SECS = 5       # dashboard + open trades auto-refresh cadence


def _css() -> str:
    return f"""
    <style>
    .stApp {{ max-width: 880px; margin: auto; background: {BG}; }}
    [data-testid="stMetric"] {{
        background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px;
        padding: 12px 14px;
    }}
    [data-testid="stMetricLabel"] {{ color: {MUTED}; }}
    [data-testid="stMetricValue"] {{ font-size: 1.3rem; color: #e2e8f0; }}
    [data-testid="stMetric"]:hover {{ border-color: {PRIMARY}66; }}
    [data-testid="stSidebar"] {{
        background: linear-gradient(180deg, #101a2e 0%, #0d1626 100%);
        border-right: 1px solid #202b42;
        padding: 16px 14px;
    }}
    [data-testid="stSidebar"] h3 {{
        color: #e2e8f0; font-size: 0.82rem; text-transform: uppercase;
        letter-spacing: .09em; margin: 16px 4px 8px 4px;
    }}
    [data-testid="stSidebar"] .stSidebarHeader {{
        background: transparent; padding: 0;
    }}
    .sidebar-brand {{
        margin: 0 4px 2px;
        color: #f1f5f9;
        font-size: 1.15rem;
        font-weight: 700;
        letter-spacing: .01em;
    }}
    .sidebar-subtitle {{
        margin: 0 4px 14px 4px;
        color: {MUTED};
        font-size: .76rem;
    }}
    .sidebar-divider {{
        border: 0;
        border-top: 1px solid #202b42;
        margin: 14px 4px;
    }}
    .sidebar-nav {{
        display: grid;
        gap: 4px;
    }}
    [data-testid="stRadio"] label {{
        display: flex;
        align-items: center;
        min-height: 38px;
        padding: 8px 10px;
        border: 1px solid transparent;
        border-radius: 9px;
        font-size: .86rem;
        font-weight: 500;
        color: #b6c1d3;
        transition: background .15s ease, border-color .15s ease, color .15s ease;
    }}
    [data-testid="stRadio"] label:hover {{
        background: #18243a;
        color: #e2e8f0;
    }}
    [data-testid="stRadio"] label:has(input:checked) {{
        background: rgba(99, 102, 241, .16);
        border-color: rgba(99, 102, 241, .55);
        color: #c7d2fe;
        box-shadow: inset 3px 0 0 {PRIMARY};
    }}
    .sidebar-card {{
        background: #111b2e;
        border: 1px solid #202b42;
        border-radius: 10px;
        padding: 10px 11px;
        margin: 6px 0;
    }}
    .sidebar-card-title {{
        color: {MUTED};
        font-size: .72rem;
        font-weight: 700;
        letter-spacing: .07em;
        text-transform: uppercase;
        margin-bottom: 7px;
    }}
    .status-row {{
        display: flex;
        align-items: center;
        gap: 7px;
        min-height: 20px;
        font-size: .78rem;
        color: #cbd5e1;
    }}
    .status-label {{
        flex: 1;
        color: #94a3b8;
    }}
    .status-value {{
        font-weight: 600;
        white-space: nowrap;
    }}
    .status-dot {{
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex-shrink: 0;
        background: {MUTED};
    }}
    .status-dot.ok {{ background: {PROFIT}; box-shadow: 0 0 7px rgba(52, 211, 153, .55); }}
    .status-dot.warn {{ background: {WARN}; box-shadow: 0 0 7px rgba(251, 191, 36, .55); }}
    .status-dot.err {{ background: {LOSS}; box-shadow: 0 0 7px rgba(251, 113, 133, .55); }}
    .ks-banner {{ border-radius: 10px; padding: 10px 14px; margin: 6px 0;
                   border: 1px solid; font-weight: 600; }}
    .badge {{ display:inline-block; border-radius: 999px; padding: 2px 10px;
              font-size: 0.78rem; font-weight: 600; }}
    .chip {{ display:inline-block; border-radius: 6px; padding: 3px 10px;
              font-size: 0.75rem; font-weight: 600; background:{BORDER}; color:{MUTED};
              margin-right: 6px; }}
    .chip-active {{ background:{PRIMARY}33; color:{PRIMARY}; border:1px solid {PRIMARY}66; }}
    .dot {{ width: 12px; height: 12px; border-radius: 50%; display: inline-block;
               flex-shrink: 0; box-shadow: 0 0 6px rgba(0,0,0,0.4); }}
    @keyframes ks-pulse {{
        0% {{ box-shadow: 0 0 0 0 rgba(251,113,133,0.55); }}
        70% {{ box-shadow: 0 0 0 9px rgba(251,113,133,0); }}
        100% {{ box-shadow: 0 0 0 0 rgba(251,113,133,0); }}
    }}
    .ks-dot {{ width: 10px; height: 10px; border-radius: 50%; background: {LOSS};
               display: inline-block; animation: ks-pulse 1.5s infinite; }}
    .row {{ display:flex; gap:8px; align-items:center; }}
    .conf-bar {{ height:6px; border-radius:4px; background:{BORDER}; overflow:hidden; }}
    .conf-fill {{ height:6px; border-radius:4px; }}
    .sec-title {{ color:{MUTED}; font-size:0.85rem; text-transform:uppercase;
                   letter-spacing:.05em; margin: 14px 0 6px 0; }}
    .big-num {{ font-size:1.6rem; font-weight:700; }}
    .muted {{ color:{MUTED}; font-size:0.85rem; }}
    .pos-card {{ background:{CARD}; border:1px solid {BORDER}; border-radius:12px;
                  padding:14px; margin:8px 0; }}
    .pos-card:hover {{ border-color: {PRIMARY}66; }}
    .pos-card.up {{ border-left: 3px solid {PROFIT}; }}
    .pos-card.down {{ border-left: 3px solid {LOSS}; }}
    .pos-card.flat {{ border-left: 3px solid {MUTED}; }}
    .gauge {{ position:relative; width:64px; height:32px; overflow:hidden; }}
    .gauge-bg {{ position:absolute; bottom:0; left:0; right:0; height:32px;
                 border-radius:32px 32px 0 0; background:{BORDER}; }}
    .gauge-fill {{ position:absolute; bottom:0; left:0; right:0; border-radius:32px 32px 0 0; }}
    .gauge-num {{ position:relative; text-align:center; font-weight:700; font-size:0.85rem;
                   padding-top:4px; color:#e2e8f0; }}
    footer, [data-testid="stHeader"] {{ background: transparent; }}
    .stat-tile {{ background:{CARD}; border:1px solid {BORDER}; border-radius:10px;
                  padding:10px 12px; }}
    .stat-label {{ color:{MUTED}; font-size:0.72rem; text-transform:uppercase;
                   letter-spacing:.04em; }}
    .stat-value {{ color:#e2e8f0; font-size:1.15rem; font-weight:700; }}

    /* Primary/secondary button accents */
    [data-testid="stButton"] button[kind="primary"] {{
        background: {PRIMARY}; border-color: {PRIMARY};
    }}
    [data-testid="stButton"] button[kind="primary"]:hover {{
        background: #818cf8; border-color: #818cf8;
    }}

    /* Bigger sidebar controls (navigation + buttons) */
    [data-testid="stSidebar"] [data-testid="stRadio"] label {{
        font-size: 1.05rem; padding: 8px 8px; border-radius: 8px;
    }}
    [data-testid="stSidebar"] [data-testid="stRadio"] div[role="radiogroup"] {{ gap: 2px; }}
    [data-testid="stSidebar"] [data-testid="stButton"] button {{
        height: 44px; font-size: 1.0rem; border-radius: 10px; font-weight: 600;
    }}
    [data-testid="stSidebar"] [data-testid="stExpander"] details {{ border-radius: 10px; }}
    [data-testid="stSidebar"] h3 {{ margin-bottom: 4px; }}

    /* Compact form rows in Settings */
    [data-testid="stNumberInput"] label, [data-testid="stSlider"] label,
    [data-testid="stCheckbox"] label, [data-testid="stTextInput"] label,
    [data-testid="stTimeInput"] label, [data-testid="stSelectbox"] label,
    [data-testid="stMultiSelect"] label {{ font-weight: 500; }}

    /* Mobile-friendly improvements for History page */
    /* Make stat tiles stack vertically on small screens */
    @media (max-width: 768px) {{
        .row {{ 
            flex-wrap: wrap; 
            gap: 12px; 
            justify-content: center; 
        }}
        .stat-tile {{
            min-width: 140px;
            flex: 1 1 140px;
        }}
        .stat-value {{
            font-size: 1rem;
        }}
        .stat-label {{
            font-size: 0.65rem;
        }}
    }}

    /* Improve table rendering on mobile */
    div[data-testid="stDataFrame"] {{
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        margin: 12px 0;
    }}
    div[data-testid="stDataFrame"] table {{
        min-width: 100%;
        font-size: 0.85rem;
    }}
    div[data-testid="stDataFrame"] th,
    div[data-testid="stDataFrame"] td {{
        padding: 8px 6px !important;
        white-space: nowrap;
    }}

    /* Period selector improvements */
    div[data-baseweb="select"] > div {{
        min-height: 36px;
    }}
    div[data-baseweb="select"] [role="option"] {{
        min-height: 32px;
    }}

    /* Touch-friendly buttons */
    button[kind="primary"], button[kind="secondary"] {{
        min-height: 40px;
        padding: 8px 16px;
    }}

    /* Adjust chart container for better mobile display */
    .stPlotlyChart, .stLineChart, .stAreaChart, .stBarChart {{
        margin: 16px 0;
        width: 100% !important;
    }}

    /* Improve spacing in history page */
    .history-section {{
        padding: 0 12px;
    }}

    /* History page specific mobile improvements */
    @media (max-width: 768px) {{
        /* Stat tiles: 2-column grid on mobile for better readability */
        .history-section .row {{
            flex-wrap: wrap;
            gap: 10px;
            justify-content: center;
            padding: 4px 0;
        }}
        .history-section .stat-tile {{
            flex: 1 1 140px;
            min-width: 130px;
            max-width: 200px;
            padding: 10px 12px;
        }}
        .history-section .stat-value {{
            font-size: 1.1rem;
        }}
        .history-section .stat-label {{
            font-size: 0.62rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }}

        /* Period filter: make it prominent and touch-friendly */
        .history-period-row {{
            display: flex;
            gap: 8px;
            align-items: stretch;
            margin-bottom: 12px;
            flex-wrap: wrap;
        }}
        .history-period-row [data-testid="stSelectbox"] {{
            flex: 1;
            min-width: 140px;
        }}
        .history-period-row label {{
            font-weight: 600;
            font-size: 0.85rem;
            color: #e2e8f0;
            margin-bottom: 4px;
        }}

        /* Chart container padding */
        .history-section .stLineChart,
        .history-section .stPlotlyChart {{
            margin: 12px 0;
            padding: 4px;
        }}

        /* Divider between sections */
        .history-divider {{
            border: 0;
            border-top: 1px solid #243049;
            margin: 16px 0;
        }}

        /* Section header for mobile */
        .history-mobile-header {{
            font-size: 0.9rem;
            font-weight: 700;
            color: #e2e8f0;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin: 12px 0 6px 0;
            padding-bottom: 4px;
            border-bottom: 2px solid #6366f1;
        }}

        /* Dataframe row height for touch */
        div[data-testid="stDataFrame"] {{
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            margin: 8px 0;
            border-radius: 8px;
            border: 1px solid #243049;
        }}
        div[data-testid="stDataFrame"] table {{
            min-width: 100%;
            font-size: 0.8rem;
        }}
        div[data-testid="stDataFrame"] th,
        div[data-testid="stDataFrame"] td {{
            padding: 10px 6px !important;
            white-space: nowrap;
            min-height: 36px;
        }}
        div[data-testid="stDataFrame"] thead th {{
            position: sticky;
            top: 0;
            background: #131c2e;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }}
    }}

    /* Always-visible section labels for better navigation */
    .history-section-label {{
        display: none;
    }}
    @media (max-width: 768px) {{
        .history-section-label {{
            display: block;
        }}
    }}
    </style>
    """


def _html(s: str):
    st.markdown(s, unsafe_allow_html=True)


def _money(x) -> str:
    try:
        return f"₹{float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _num(x) -> str:
    try:
        return f"{float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _pct(x) -> str:
    try:
        return f"{float(x):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _ui_time(value) -> time:
    """Parse a "HH:MM" (or "H:MM") setting into a time for st.time_input."""
    try:
        parts = str(value).split(":")
        return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except Exception:
        return time(10, 0)


def _pnl_color(x) -> str:
    try:
        v = float(x)
        return PROFIT if v > 0 else (LOSS if v < 0 else MUTED)
    except (TypeError, ValueError):
        return MUTED


def _badge(text: str, kind: str = "info") -> str:
    colors = {
        "ok": (PROFIT, "rgba(52,211,153,0.15)"),
        "warn": (WARN, "rgba(251,191,36,0.15)"),
        "err": (LOSS, "rgba(251,113,133,0.15)"),
        "up": (PROFIT, "rgba(52,211,153,0.15)"),
        "down": (LOSS, "rgba(251,113,133,0.15)"),
        "info": (SECONDARY, "rgba(34,211,238,0.15)"),
        "muted": (MUTED, "rgba(148,163,184,0.15)"),
    }
    fg, bg = colors.get(kind, colors["info"])
    return f"<span class='badge' style='color:{fg};background:{bg}'>{text}</span>"


def _chip(text: str, active: bool = False) -> str:
    cls = "chip chip-active" if active else "chip"
    return f"<span class='{cls}'>{text}</span>"


def _lead_plan_line(r: dict) -> str:
    """F&O contract a lead maps to, e.g. `RELIANCE 30 SEP 26 2900 CE × 500`."""
    symbol = (r.get("trading_symbol") or "").strip()
    if not symbol:
        return ""
    parts = [symbol]
    qty = r.get("quantity")
    if qty:
        parts.append(f"× {qty}")
    expiry = r.get("expiry")
    if expiry:
        parts.append(f"exp {expiry}")
    margin = r.get("margin_needed")
    if margin is not None:
        parts.append(f"margin ₹{float(margin):,.0f}")
    return f"<div class='muted'>F&O: {' · '.join(parts)}</div>"


def _pnl_html(x) -> str:
    return f"<span style='color:{_pnl_color(x)};font-weight:600'>{_money(x)}</span>"


def _fragment(fn):
    frag = getattr(st, "fragment", None)
    if frag is not None:
        try:
            return frag(run_every=f"{REFRESH_SECS}s")(fn)
        except TypeError:
            return frag(fn)
    return fn


def _utc_to_ist_hm(iso: str | None) -> str:
    try:
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata"))
        return dt.strftime("%d %b %H:%M")
    except Exception:
        return "—"


def _style_pnl_col(s):
    vals = pd.to_numeric(pd.Series(s), errors="coerce")
    return [
        f"color:{_pnl_color(v)};font-weight:600" if pd.notna(v) else ""
        for v in vals
    ]


def _trades_df(rows: list[dict], cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame([{k: r.get(k) for k in cols} for r in rows])


def _ist_now_str() -> str:
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%H:%M:%S")


def _stat_tile(label: str, value: str, color: str = "#e2e8f0") -> str:
    return (
        f"<div class='stat-tile'>"
        f"<div class='stat-label'>{label}</div>"
        f"<div class='stat-value' style='color:{color}'>{value}</div>"
        f"</div>"
    )


# --- auth ----------------------------------------------------------------

SSO_LOGIN_LINK_ID = "__sso_login_link"


def _ensure_top_link(href: str, link_id: str) -> str:
    return (
        "<script>(function(){"
        f"var D=window.top.document;"
        f"var a=D.getElementById({json.dumps(link_id)});"
        "if(!a){"
        "a=D.createElement('a');"
        f"a.id={json.dumps(link_id)};"
        "a.target='_top';"
        "a.rel='noopener noreferrer';"
        "a.style.display='none';"
        "D.body.appendChild(a);"
        "}"
        f"if(a.href!=={json.dumps(href)})a.href={json.dumps(href)};"
        "})();</script>"
    )


def _click_top_link(link_id: str) -> str:
    return (
        "<script>window.top.document.getElementById("
        f"{json.dumps(link_id)}"
        ").click();</script>"
    )


def render_login():
    st.markdown(
        f"""
        <div style="text-align:center; padding-top:40px;">
          <div style="font-size:2rem; font-weight:700; letter-spacing:.02em;">TradePilot</div>
          <div class="muted" style="margin:6px 0 24px 0;">Automated intraday breakout trading · Upstox</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("Upstox **SSO** login")
    auth_error = st.query_params.get("auth_error")
    if auth_error:
        st.error(f"SSO failed: {auth_error}")
        st.query_params.clear()

    st.markdown(_ensure_top_link(api.login_url(), SSO_LOGIN_LINK_ID), unsafe_allow_html=True)
    if st.button(
        "Login with Upstox",
        use_container_width=True,
        type="primary",
        key="sso_login",
    ):
        st.markdown(_click_top_link(SSO_LOGIN_LINK_ID), unsafe_allow_html=True)
        st.stop()
    st.caption("You'll be redirected to Upstox, then back to this dashboard.")


# --- sidebar -------------------------------------------------------------


def render_sidebar() -> str:
    with st.sidebar:
        # Brand
        st.markdown("<div class='sidebar-brand'>TradePilot</div>", unsafe_allow_html=True)
        st.markdown("<div class='sidebar-subtitle'>F&O control center</div>", unsafe_allow_html=True)
        st.markdown("<hr class='sidebar-divider'>", unsafe_allow_html=True)

        # Navigation
        st.markdown("### Navigation")
        page = st.radio(
            "Page",
            [
                "Dashboard",
                "Open Trades",
                "Leads",
                "Instruments",
                "History",
                "Health",
                "Settings",
            ],
            label_visibility="visible",
            help="Navigate between dashboard sections.",
            key="sidebar_page",
        )

        st.markdown("<hr class='sidebar-divider'>", unsafe_allow_html=True)

        # Live status card
        st.markdown("### Status")
        health = api.get_health()
        ks = api.get_killswitch()
        b = health.get("data", {}).get("broker", {}) if health.get("status") == "ok" else {}

        m_open = False
        m_time = "—"
        if health.get("status") == "ok":
            m = health["data"]["market"]
            m_open = bool(m.get("open"))
            m_time = f"{m['time_ist']} IST"
        active = ks.get("data", {}).get("active") if ks.get("status") == "ok" else None
        token_expired = b.get("token_expired", False)
        token_valid = b.get("token_valid_until")

        _html(
            f"""
            <div class='sidebar-card'>
              <div class='sidebar-card-title'>Market</div>
              <div class='status-row'>
                <span class='status-label'>Session</span>
                <span class='status-value'>{'Open' if m_open else 'Closed'}</span>
                <span class='status-dot {'ok' if m_open else 'muted'}'></span>
              </div>
              <div class='status-row' style='margin-top:4px;'>
                <span class='status-label'>Server time</span>
                <span class='status-value'>{m_time}</span>
              </div>
            </div>
            """
        )

        _html(
            f"""
            <div class='sidebar-card'>
              <div class='sidebar-card-title'>Risk</div>
              <div class='status-row'>
                <span class='status-label'>Killswitch</span>
                <span class='status-value'>{'Active' if active else 'Armed'}</span>
                <span class='status-dot {'err' if active else 'ok'}'></span>
              </div>
            </div>
            """
        )

        if token_expired:
            token_dot = "err"
            token_txt = "Expired"
        elif b.get("connected") is False and b.get("message") and b.get("message") != "ok":
            token_dot = "warn"
            token_txt = "Locked" if "Locked" in b.get("message", "") else "Upstox error"
        elif token_valid:
            token_dot = "ok"
            token_txt = f"OK · {_utc_to_ist_hm(token_valid)} IST"
        else:
            token_dot = "muted"
            token_txt = "—"

        _html(
            f"""
            <div class='sidebar-card'>
              <div class='sidebar-card-title'>Broker token</div>
              <div class='status-row'>
                <span class='status-label'>Status</span>
                <span class='status-value'>{token_txt}</span>
                <span class='status-dot {token_dot}'></span>
              </div>
            </div>
            """
        )

        # Killswitch setup expander
        with st.expander("Killswitch", expanded=False):
            st.caption("Square off all managed trades and halt the system for the day.")
            reason = st.text_input("Reason", placeholder="e.g. market crash", label_visibility="visible")
            if st.button("ACTIVATE", type="primary", use_container_width=True):
                resp = api.activate_killswitch(reason or "user requested")
                if resp.get("status") == "ok":
                    st.success(f"Squared off {len(resp['data'].get('squared_off', []))} trade(s).")
                    st.rerun()
                else:
                    st.error(resp.get("error", {}).get("message", "Activation failed."))

        st.markdown("<hr class='sidebar-divider'>", unsafe_allow_html=True)

        # Controls
        st.markdown("### Controls")
        if st.button("Refresh now", use_container_width=True, help="Force-refresh the current view."):
            st.rerun()
        if st.button("Logout", use_container_width=True, help="End the Upstox session and clear tokens."):
            api.logout()

    return page


# --- killswitch ----------------------------------------------------------


def render_killswitch():
    """Full-width alert banner when the killswitch is active (plus Release)."""
    ks = api.get_killswitch()
    data = ks.get("data", {}) if ks.get("status") == "ok" else {}
    active = data.get("active", False)
    if not active:
        return
    reason = data.get("reason") or "no reason given"
    by = data.get("triggered_by") or "user"
    ts = _utc_to_ist_hm(data.get("triggered_at"))
    _html(
        f"""
        <div style="display:flex;align-items:center;gap:14px;
                    background:linear-gradient(90deg,#2c0b18,#1c0a12);
                    border:1px solid {LOSS};border-radius:12px;padding:14px 16px;margin:8px 0;">
          <span class="ks-dot" style="flex-shrink:0;"></span>
          <div>
            <div style="color:{LOSS};font-weight:800;font-size:1.05rem;letter-spacing:.03em;">
              KILLSWITCH ACTIVE — SYSTEM HALTED</div>
            <div class="muted" style="margin-top:2px;">Reason: <b>{reason}</b></div>
            <div class="muted">Triggered by {by} · {ts} IST</div>
          </div>
        </div>
        """
    )
    c1, c2 = st.columns([3, 1])
    with c1:
        st.caption("No new leads or entries; all open trades were squared off. Releasing re-arms the system.")
    with c2:
        _release_button()


def _release_button():
    if st.button("Release", type="primary", use_container_width=True, key="ks_release"):
        _confirm_release()


def _confirm_release():
    dlg = getattr(st, "dialog", None)
    if dlg is None:
        api.release_killswitch()
        st.rerun()
        return

    @dlg("Release killswitch?")
    def _inner():
        st.markdown("Re-arm the system for new leads and entries? Confirm the reason is resolved.")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Yes, release", type="primary", use_container_width=True, key="ks_release_yes"):
                api.release_killswitch()
                st.rerun()
        with c2:
            if st.button("Cancel", use_container_width=True, key="ks_release_no"):
                st.rerun()

    _inner()


def render_token_status():
    """Alert only when the Upstox token is dead (the bot cannot trade)."""
    health = api.get_health()
    if health.get("status") != "ok":
        return
    b = health["data"].get("broker", {})
    if not b.get("token_expired"):
        return
    _html(
        "<div class='ks-banner' style='background:#fee2e2;border-color:#fca5a5;color:#991b1b;'>"
        "Upstox token expired — the bot cannot trade until you re-login.</div>"
    )
    st.markdown(_ensure_top_link(api.login_url(), SSO_LOGIN_LINK_ID), unsafe_allow_html=True)
    if st.button(
        "Login with Upstox",
        use_container_width=True,
        type="primary",
        key="sso_login_token_expired",
    ):
        st.markdown(_click_top_link(SSO_LOGIN_LINK_ID), unsafe_allow_html=True)
        st.stop()


def render_killswitch_setup():
    with st.sidebar.expander("Killswitch", expanded=False):
        st.caption("Square off all managed trades and halt the system for the day.")
        reason = st.text_input("Reason", placeholder="e.g. market crash", label_visibility="visible")
        if st.button("ACTIVATE", type="primary", use_container_width=True):
            resp = api.activate_killswitch(reason or "user requested")
            if resp.get("status") == "ok":
                st.success(f"Squared off {len(resp['data'].get('squared_off', []))} trade(s).")
                st.rerun()
            else:
                st.error(resp.get("error", {}).get("message", "Activation failed."))


# --- tabs ----------------------------------------------------------------


@_fragment
def render_dashboard():
    st.subheader("Dashboard")

    pnl = api.get_pnl()
    if pnl.get("status") != "ok":
        msg = pnl.get("error", {}).get("message", "P&L unavailable")
        err_code = pnl.get("error", {}).get("code", "")
        if "Locked" in msg or err_code == "broker_error":
            st.warning(
                f"Upstox rejected the request: **{msg}** — this is an Upstox-side "
                f"condition (account/app lock). Wait a few minutes and retry, or "
                f"check your Upstox account."
            )
        else:
            st.warning(f"{msg} — live P&L needs an Upstox token.")
        c = st.columns(4)
        c[0].metric("Unrealised", "—")
        c[1].metric("Realised", "—")
        c[2].metric("Day P&L", "—")
        c[3].metric("Margin", "—")
    else:
        d = pnl["data"]
        total = d.get("total", 0)
        c = st.columns(4)
        c[0].metric("Unrealised", _money(d.get("unrealised")))
        c[1].metric("Realised", _money(d.get("realised")))
        c[2].metric("Day P&L", _money(total))
        c[3].metric("Margin", _money(d.get("available_margin")))

        _track_pnl_history(total or 0)

        # Sparkline of the day's P&L trajectory
        history = st.session_state.get("pnl_history", [])
        if len(history) >= 2:
            chart_df = pd.DataFrame({"P&L": history})
            chart_df.index = pd.RangeIndex(len(chart_df), name="tick")
            st.line_chart(chart_df, height=120, use_container_width=True)
            st.caption(f"P&L trajectory · peak {_money(max(history))} · "
                       f"trough {_money(min(history))}")
        else:
            st.caption("P&L trajectory builds up as data refreshes.")

        _html(
            f"<div class='row'><span>Open positions: <b>{d.get('open_positions', 0)}</b></span>"
            f"<span style='margin-left:auto'>{_badge(_money(total), 'up' if (total or 0) > 0 else 'down')}</span></div>"
        )

    # Day-at-a-glance stats (combines open + today's closed)
    _render_day_stats()

    st.markdown("#### Today's queued signals")
    # The leads API no longer accepts status/date params; fetch the current
    # set and filter for queued on the client. The cleanup scheduler keeps
    # this list small (only active queued signals survive).
    leads_resp = api.get_leads()
    if leads_resp.get("status") == "ok":
        rows = [r for r in leads_resp["data"]["leads"] if r.get("status") == "queued"]
        if rows:
            df = pd.DataFrame(
                [
                    {
                        "Time (IST)": (
                            r.get("created_at_ist_label")
                            or _utc_to_ist_hm(r.get("created_at"))
                        ),
                        "Symbol": r.get("symbol") or r["underlying"].split("|")[-1],
                        "Instrument": r.get("trading_symbol") or "—",
                        "Dir": r["direction"],
                        "Pattern": r["signal_type"],
                        "Level": r["signal_level"],
                        "Conf": r["confidence"],
                        "Margin": _money(r.get("margin_needed")) if r.get("margin_needed") is not None else "—",
                        "Status": r["status"],
                    }
                    for r in rows
                ]
            )
            st.dataframe(df, use_container_width=True, hide_index=True, height=min(40 + 35 * len(df), 420))
        else:
            st.info("No queued signals today. Check the **Leads** tab to generate them manually.")


def _track_pnl_history(total: float):
    """Keep a small ring buffer of P&L values for the dashboard sparkline."""
    hist = st.session_state.setdefault("pnl_history", [])
    if not hist or hist[-1] != total:
        hist.append(total)
    if len(hist) > 60:
        del hist[:-60]


def _render_day_stats():
    """Compact stats: # trades today, wins, biggest winner/loser, avg hold (proxy)."""
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    closed = api.get_closed_trades(date=today)
    open_trades = api.get_open_trades(date=today)

    closed_rows = closed.get("data", {}).get("trades", []) if closed.get("status") == "ok" else []
    open_rows = open_trades.get("data", {}).get("trades", []) if open_trades.get("status") == "ok" else []

    pnls = [(r.get("realized_pnl") or 0) for r in closed_rows]
    wins = sum(1 for v in pnls if v > 0)
    losses = sum(1 for v in pnls if v < 0)
    win_rate = (wins / len(pnls) * 100) if pnls else None
    biggest_win = max(pnls) if pnls else None
    biggest_loss = min(pnls) if pnls else None
    avg = (sum(pnls) / len(pnls)) if pnls else None

    tiles = [
        _stat_tile("Trades today", str(len(pnls) + len(open_rows))),
        _stat_tile("Closed", f"{len(pnls)}"),
        _stat_tile("Wins / Losses", f"{wins} / {losses}"),
        _stat_tile("Win rate",
                   f"{win_rate:.0f}%" if win_rate is not None else "—",
                   color=PROFIT if (win_rate or 0) >= 50 else MUTED),
        _stat_tile("Avg P&L",
                   _money(avg) if avg is not None else "—",
                   color=_pnl_color(avg) if avg is not None else "#e2e8f0"),
        _stat_tile("Biggest win",
                   _money(biggest_win) if biggest_win is not None else "—",
                   color=PROFIT if biggest_win else MUTED),
        _stat_tile("Biggest loss",
                   _money(biggest_loss) if biggest_loss is not None else "—",
                   color=LOSS if biggest_loss else MUTED),
    ]
    _html("<div class='row' style='flex-wrap:wrap;gap:8px;'>" + "".join(tiles) + "</div>")


@_fragment
def render_open_trades():
    st.subheader("Open Trades")
    resp = api.get_open_trades()
    if resp.get("status") != "ok":
        st.warning(resp.get("error", {}).get("message", "Could not load open trades."))
        return
    rows = resp["data"].get("trades", [])
    if not rows:
        st.info("No open trades right now. New entries appear here once the lead generator places an order.")
        return

    # Visual position cards
    for r in rows:
        _render_position_card(r)

    total = sum(r.get("unrealised_pnl") or 0 for r in rows)
    _html(
        f"<div class='row' style='margin-top:12px'><span class='muted'>Open positions: <b>{len(rows)}</b></span>"
        f"<span style='margin-left:auto'>Unrealised {_pnl_html(total)}</span></div>"
    )

    # Compact table view as a secondary reference
    with st.expander("Show as table", expanded=False):
        df = _trades_df(
            rows,
            ["symbol", "direction", "entry_price", "ltp", "unrealised_pnl", "current_sl", "trail_state"],
        )
        df["Entered (IST)"] = [_utc_to_ist_hm(r.get("entry_time")) for r in rows]
        df.columns = ["Symbol", "Dir", "Entry", "LTP", "P&L", "SL", "Trail", "Entered (IST)"]
        styled = df.style.apply(_style_pnl_col, subset=["P&L"])
        st.dataframe(styled, use_container_width=True, hide_index=True)


def _render_position_card(r: dict):
    """One visually rich card per open trade."""
    sym = r.get("symbol") or "—"
    direction = r.get("direction") or "?"
    entry = r.get("entry_price")
    ltp = r.get("ltp")
    pnl = r.get("unrealised_pnl") or 0
    sl = r.get("current_sl")
    trail = r.get("trail_state") or "—"
    sl_source = r.get("sl_source")
    lifecycle = r.get("lifecycle_stage")

    pct = None
    if entry and ltp:
        try:
            base = float(entry)
            cur = float(ltp)
            pct = (cur - base) / base * 100.0
        except (TypeError, ValueError):
            pct = None

    # SL is below entry for both CALL and PUT (option-buying bot).
    # sl_pct = total adverse buffer size, expressed as % of entry.
    sl_pct = None
    if entry and sl:
        try:
            sl_pct = abs((float(entry) - float(sl)) / float(entry) * 100.0)
        except (TypeError, ValueError):
            sl_pct = None

    pnl_cls = "up" if pnl > 0 else ("down" if pnl < 0 else "flat")
    dir_kind = "up" if direction == "CALL" else "down"

    sl_bar = ""
    if sl_pct is not None and pct is not None and sl_pct > 0:
        # How much of the SL buffer has been consumed by adverse price movement.
        # pct < 0 means LTP has fallen from entry (toward SL) for both directions.
        consumed_pct = max(0.0, -pct)
        used = max(0.0, min(100.0, (consumed_pct / sl_pct) * 100.0))
        # Higher fill = more danger (closer to SL trigger).
        bar_color = PROFIT if used < 30 else (WARN if used < 70 else LOSS)
        sl_bar = (
            f"<div class='conf-bar' style='margin-top:6px;' title='LTP vs SL'>"
            f"<div class='conf-fill' style='width:{used:.0f}%;background:{bar_color}'></div>"
            f"</div>"
            f"<div class='muted' style='font-size:0.72rem;margin-top:2px;'>"
            f"{used:.0f}% of SL buffer consumed"
            f"</div>"
        )

    sl_chip_label = "bot SL" if sl_source == "bot" else ("user SL" if sl_source == "user" else "—")
    chip_kind = "muted" if sl_source != "user" else "up"
    lifecycle_chip = (
        f" {_badge(lifecycle, 'muted')}" if lifecycle else ""
    )

    _html(
        f"""
        <div class='pos-card {pnl_cls}'>
          <div class='row' style='justify-content:space-between;'>
            <div>
              <div style='font-size:1.05rem;font-weight:700;'>{sym} {_badge(direction, '{dir_kind}')}</div>
              <div class='muted'>{_badge(trail, 'muted')}{lifecycle_chip}</div>
            </div>
            <div style='text-align:right;'>
              <div style='font-size:1.15rem;font-weight:700;color:{_pnl_color(pnl)}'>{_money(pnl)}</div>
              <div class='muted'>{_pct(pct) if pct is not None else '—'}</div>
            </div>
          </div>
          <div class='row' style='margin-top:8px;gap:18px;flex-wrap:wrap;'>
            <div><span class='muted'>Entry</span> <b>{_num(entry)}</b></div>
            <div><span class='muted'>LTP</span> <b>{_num(ltp)}</b></div>
            <div><span class='muted'>SL</span> <b>{_num(sl)}</b> {_badge(sl_chip_label, '{chip_kind}')}</div>
          </div>
          <div class='muted' style='font-size:0.72rem;margin-top:4px;'>
            Entered {_utc_to_ist_hm(r.get('entry_time'))} IST
          </div>
          {sl_bar}
        </div>
        """
    )


def render_leads():
    st.subheader("Leads")

    # The lead_cleanup scheduler keeps this view small and current
    # (processed leads are deleted almost immediately and queued leads
    # age-out at 24h). The API already sorts by composite score, so no
    # filters/status toggles are needed.
    _left, _right = st.columns([4, 1])
    with _right:
        if st.button("Generate now", type="primary", use_container_width=True,
                     help="Run the lead generator manually (works outside trading hours)."):
            # Manual runs now dispatch on the server (background thread) and
            # return 202 immediately, so we just record the job id here and
            # let the status fragment below poll for completion. This keeps
            # the rest of the page responsive while generation is in flight.
            resp = api.generate_leads()
            if resp.get("status") == "ok":
                st.session_state["lead_job"] = {
                    "id": resp["data"]["id"],
                    "submitted_at": resp["data"]["submitted_at"],
                }
                st.toast("Lead generation started.", icon=":material/hourglass_top:")
            elif resp.get("error", {}).get("code") == "lead_generation_in_progress":
                # Attach to the existing job rather than spinning up a second.
                existing = (resp.get("data") or {}).get("id")
                if existing:
                    st.session_state["lead_job"] = {
                        "id": existing,
                        "submitted_at": (resp.get("data") or {}).get("submitted_at"),
                    }
                st.toast("A generation run is already in progress.", icon=":material/hourglass_top:")
            else:
                st.error(resp.get("error", {}).get("message", "Generation failed."))

    # Independently re-running status fragment so polling the server doesn't
    # also re-render the full leads list / spinner.
    _lead_gen_status_fragment()


@st.fragment(run_every="2s")
def _lead_gen_status_fragment():
    job = st.session_state.get("lead_job")
    if not job:
        return

    job_id = job.get("id")
    if not job_id:
        return

    resp = api.get_lead_gen_status(job_id)
    if resp.get("status") != "ok":
        # Network blip / server reload lost the job. Clear so we don't loop.
        st.session_state.pop("lead_job", None)
        return

    data = resp["data"]
    status = data.get("status")
    if status == "running":
        st.info("⏳ Lead generation in progress…", icon=":material/hourglass_top:")
        return

    st.session_state.pop("lead_job", None)
    if status == "done":
        result = data.get("result") or {}
        generated = result.get("generated", 0)
        st.toast(f"Generated {generated} lead(s).", icon=":material/check_circle:")
    elif status == "error":
        st.error(f"Generation failed: {data.get('error') or 'unknown error'}")
    st.rerun()

    resp = api.get_leads()
    if resp.get("status") != "ok":
        st.warning("Could not load leads.")
        return
    rows = resp["data"].get("leads", [])
    if not rows:
        st.info("No leads. Try **Generate now**, or enable more underlyings in **Instruments**.")
        return

    _html("<div class='row' style='margin-bottom:12px;align-items:center;'>")
    _html(f"<span class='muted'>Showing <b>{len(rows)}</b> {'lead' if len(rows) == 1 else 'leads'} · sorted by score</span>")
    _html(f"</div>")

    for r in rows:
        c1, c2, c3 = st.columns([4, 1, 2])
        with c1:
            created_ist = (
                r.get("created_at_ist_label")
                or _utc_to_ist_hm(r.get("created_at"))
            )
            direction_class = "up" if r["direction"] == "CALL" else "down"
            status_class = "ok" if r["status"] in ("placed", "filled") else ("warn" if r["status"] == "queued" else "muted")

            _html(
                f"<div style='display:flex;justify-content:space-between;margin-bottom:6px;'>"
                f"<div style='font-weight:600'>{r.get('symbol') or r['underlying'].split('|')[-1]} "
                f"{_badge(r['direction'], direction_class)} "
                f"{_badge(r['status'], status_class)}</div>"
                f"<div class='muted'>{created_ist} IST</div></div>"
            )

            _html(
                f"<div style='margin:6px 0;'>"
                f"<span class='badge' style='color:{SECONDARY};background:{SECONDARY}33;'>"
                f"{r['signal_type']}</span> "
                f"<span class='badge' style='color:{PRIMARY};background:{PRIMARY}33;'>"
                f"@ {_num(r['signal_level'])}</span></div>"
            )

            # Plan details
            plan_parts = []
            if r.get("expiry"):
                plan_parts.append(f"Exp {r['expiry']}")
            if r.get("strike_price"):
                plan_parts.append(f"Strike {_num(r['strike_price'])}")
            if r.get("option_type"):
                plan_parts.append(f"Opt {r['option_type']}")
            if r.get("quantity"):
                plan_parts.append(f"Qty {r['quantity']}")
            if r.get("lot_size"):
                plan_parts.append(f"Lot {r['lot_size']}")
            if r.get("margin_needed"):
                plan_parts.append(f"Margin ₹{float(r['margin_needed']):,.0f}")
            if r.get("premium"):
                plan_parts.append(f"Prem {_num(r['premium'])}")
            if r.get("spot"):
                plan_parts.append(f"Spot {_num(r['spot'])}")
            if plan_parts:
                _html(f"<div class='muted' style='margin-top:8px;font-size:0.78rem;'>"
                      f"{' · '.join(plan_parts)}</div>")

        with c2:
            pct = int((r.get("confidence") or 0) * 100)
            bar_color = PROFIT if pct >= 80 else (WARN if pct >= 60 else MUTED)
            badge_color = "ok" if pct >= 80 else ("warn" if pct >= 60 else "muted")

            _html(
                f"<div style='text-align:center;margin-top:8px;'>"
                f"<div class='badge {badge_color}' style='margin-bottom:6px;'>"
                f"{pct}% Score</div>"
                f"<div class='conf-bar' style='margin:6px 0;'>"
                f"<div class='conf-fill' style='width:{pct}%;background:{bar_color};'></div>"
                f"</div>"
                f"<div class='muted' style='font-size:0.72rem;'>"
                f"{'High' if pct >= 80 else ('Medium' if pct >= 60 else 'Low')}</div>"
                f"</div>"
            )

        with c3:
            if r.get("note"):
                st.caption(r["note"])


def render_instruments():
    st.subheader("Instruments")
    resp = api.get_instruments()
    if resp.get("status") != "ok":
        st.warning("Could not load instruments.")
        return
    rows = resp["data"].get("instruments", [])
    if not rows:
        st.info("No instruments yet — they are seeded automatically on container start, all inactive by default.")
        return

    enabled = sum(1 for r in rows if r["enabled"])
    segments = sorted({r["segment"] for r in rows})

    # Segment chips + counts
    counts = {seg: sum(1 for r in rows if r["segment"] == seg) for seg in segments}
    counts_on = {seg: sum(1 for r in rows if r["segment"] == seg and r["enabled"]) for seg in segments}

    _html(
        f"<div class='row' style='flex-wrap:wrap;gap:6px;margin:6px 0;'>"
        f"{_chip(f'{len(rows)} total', False)}"
        f"{_chip(f'{enabled} active', enabled > 0)}"
        + "".join(_chip(f'{seg} · {counts_on[seg]}/{counts[seg]}', False) for seg in segments)
        + "</div>"
    )

    top = st.columns([2, 2, 2, 1])
    with top[0]:
        seg = st.selectbox("Segment", ["All"] + segments, key="instr_seg")
    with top[1]:
        q = st.text_input("Search symbol", key="instr_q",
                          placeholder="e.g. RELIANCE").strip().lower()
    with top[2]:
        only_active = st.toggle("Active only", value=False, key="instr_only_active")
    with top[3]:
        if st.button("Enable all", use_container_width=True, help="Enable every visible row."):
            for r in rows:
                if not r["enabled"]:
                    api.set_instrument_enabled(r["id"], True)
            st.rerun()

    filtered = [
        r for r in rows
        if (seg == "All" or r["segment"] == seg)
        and (not q or q in r["symbol"].lower())
        and (not only_active or r["enabled"])
    ]

    if not filtered:
        st.info("No instruments match the filters.")
        return

    df = pd.DataFrame(
        [{"symbol": r["symbol"], "segment": r["segment"], "lot": r["lot_size"], "active": r["enabled"]} for r in filtered]
    )
    orig = {r["symbol"]: r["enabled"] for r in filtered}
    edited = st.data_editor(
        df, hide_index=True, use_container_width=True, num_rows="fixed",
        disabled=["symbol", "segment", "lot"], key="instr_editor",
    )
    if edited is not None:
        for _, row in edited.iterrows():
            sym = row["symbol"]
            if orig.get(sym) != bool(row["active"]):
                meta = next(r for r in filtered if r["symbol"] == sym)
                api.set_instrument_enabled(meta["id"], bool(row["active"]))
                _html(f"<div class='muted'>{sym} → {'enabled' if row['active'] else 'disabled'}</div>")


def render_history():
    st.subheader("History")
    resp = api.get_closed_trades()
    if resp.get("status") != "ok":
        st.warning("Could not load history.")
        return
    rows = resp["data"].get("trades", [])
    if not rows:
        st.info("No closed trades yet. The History tab fills up once the bot exits positions.")
        return

    # Period filter (derived client-side from exit_time; keeps the same /closed endpoint)
    periods = ["All", "Today", "Last 7 days", "Last 30 days"]
    
    # Create mobile-friendly period selector with visible label
    col1, col2 = st.columns([1, 2])
    with col1:
        st.markdown("<div class='history-section-label'>📅 Period</div>", unsafe_allow_html=True)
    with col2:
        period = st.selectbox("Period", periods, label_visibility="collapsed", key="hist_period")
    now = datetime.now(ZoneInfo("Asia/Kolkata"))

    def _in_period(r):
        if period == "All":
            return True
        ts = r.get("exit_time")
        if not ts:
            return False
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(ZoneInfo("Asia/Kolkata"))
        except Exception:
            return False
        if period == "Today":
            return dt.date() == now.date()
        if period == "Last 7 days":
            return (now - dt).days <= 7
        if period == "Last 30 days":
            return (now - dt).days <= 30
        return True

    filtered = [r for r in rows if _in_period(r)]
    if not filtered:
        st.info("No closed trades in this period.")
        return

    pnls = [(r.get("realized_pnl") or 0) for r in filtered]
    wins = sum(1 for v in pnls if v > 0)
    losses = sum(1 for v in pnls if v < 0)
    win_rate = (wins / len(pnls) * 100) if pnls else 0
    gross_profit = sum(v for v in pnls if v > 0)
    gross_loss = abs(sum(v for v in pnls if v < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss else None
    net = sum(pnls)
    avg_win = (gross_profit / wins) if wins else None
    avg_loss = (gross_loss / losses) if losses else None
    # Simple max drawdown over the running P&L series
    cum = []
    running = 0.0
    for v in pnls:
        running += v
        cum.append(running)
    peak = max(cum) if cum else 0
    dd = min(c - peak for c in cum) if cum else 0

    # Wrap content in history-section div for mobile styling
    st.markdown("<div class='history-section'>", unsafe_allow_html=True)
    
    # Mobile section header for stats
    st.markdown("<div class='history-mobile-header'>📊 Performance Summary</div>", unsafe_allow_html=True)
    
    tiles = [
        _stat_tile("Trades", str(len(pnls))),
        _stat_tile("Win rate", f"{win_rate:.0f}%",
                   color=PROFIT if win_rate >= 50 else LOSS),
        _stat_tile("Net", _money(net), color=_pnl_color(net)),
        _stat_tile("Profit factor",
                   f"{profit_factor:.2f}" if profit_factor is not None else "∞",
                   color=PROFIT if (profit_factor or 0) >= 1.5 else MUTED),
        _stat_tile("Avg win", _money(avg_win) if avg_win is not None else "—", color=PROFIT),
        _stat_tile("Avg loss", _money(avg_loss) if avg_loss is not None else "—", color=LOSS),
        _stat_tile("Max drawdown", _money(dd), color=LOSS),
    ]
    _html("<div class='row' style='flex-wrap:wrap;gap:10px;'>" + "".join(tiles) + "</div>")
    
    # Divider
    _html("<div class='history-divider'></div>")

    # Equity curve from running P&L
    if len(cum) >= 2:
        st.markdown("<div class='history-mobile-header'>📈 Equity Curve</div>", unsafe_allow_html=True)
        eq = pd.DataFrame({"Equity": cum})
        eq.index = pd.RangeIndex(1, len(eq) + 1, name="trade #")
        st.line_chart(eq, height=160, use_container_width=True)
        st.caption("Equity curve (running net P&L across the selected period).")
        
        # Divider
    _html("<div class='history-divider'></div>")

    # Mobile section header for table
    st.markdown("<div class='history-mobile-header'>📋 Trade History</div>", unsafe_allow_html=True)
    
    df = _trades_df(
        filtered,
        ["symbol", "direction", "entry_price", "exit_price", "realized_pnl", "exit_reason"],
    )
    df["Closed (IST)"] = [_utc_to_ist_hm(r.get("exit_time")) for r in filtered]
    df.columns = ["Symbol", "Dir", "Entry", "Exit", "P&L", "Reason", "Closed (IST)"]
    styled = df.style.apply(_style_pnl_col, subset=["P&L"])
    st.dataframe(styled, use_container_width=True, hide_index=True)
    
    st.markdown("</div>", unsafe_allow_html=True)


def render_health():
    st.subheader("System Health")
    resp = api.get_health()
    if resp.get("status") != "ok":
        st.warning("Could not load health.")
        return
    d = resp["data"]

    # Compact top-line
    b = d["broker"]
    m = d["market"]
    e = d["errors"]
    top = (
        _stat_tile("Market", "OPEN" if m["open"] else "CLOSED",
                   color=PROFIT if m["open"] else MUTED)
        + _stat_tile("Broker",
                     "CONNECTED" if b.get("connected")
                     else ("NOT CONFIGURED" if not b.get("configured") else "DISCONNECTED"),
                     color=PROFIT if b.get("connected") else (MUTED if not b.get("configured") else LOSS))
        + _stat_tile("Errors", str(e.get("count", 0)),
                     color=LOSS if e.get("count", 0) else PROFIT)
    )
    _html("<div class='row' style='gap:8px;'>" + top + "</div>")

    st.markdown("##### Schedulers")
    for name, hb in d["heartbeats"].items():
        col1, col2, col3 = st.columns([3, 2, 1])
        col1.markdown(f"**{name}**")
        state = hb["status"]
        if hb["stale"]:
            col2.markdown(_badge("STALE", "err"), unsafe_allow_html=True)
        elif state == "ok":
            col2.markdown(_badge("OK", "ok"), unsafe_allow_html=True)
        else:
            col2.markdown(_badge(state.upper(), "warn"), unsafe_allow_html=True)
        last = _utc_to_ist_hm(hb.get("last_run_at"))
        col3.caption(
            f"`{last} IST` · {hb['note']}" if hb["note"] else f"`{last} IST`"
        )

    st.markdown("##### Broker")
    status_kind = "ok" if b.get("connected") else ("muted" if not b.get("configured") else "warn")
    st.markdown(
        f"{_badge('CONNECTED' if b.get('connected') else ('NOT CONFIGURED' if not b.get('configured') else 'DISCONNECTED'), status_kind)}"
        f"<div class='muted'>{b.get('message')}</div>",
        unsafe_allow_html=True,
    )
    if b.get("token_expired"):
        st.markdown(_badge("TOKEN EXPIRED", "err"), unsafe_allow_html=True)
    elif b.get("token_near_expiry"):
        st.markdown(_badge(f"EXPIRES {_utc_to_ist_hm(b.get('token_valid_until'))} IST", "warn"), unsafe_allow_html=True)
    elif b.get("token_valid_until"):
        st.caption(f"Upstox token valid until {_utc_to_ist_hm(b.get('token_valid_until'))} IST")

    st.markdown("##### Errors")
    if e["count"] == 0:
        st.markdown(f"{_badge('NO ERRORS', 'ok')}", unsafe_allow_html=True)
    else:
        st.markdown(_badge(f"{e['count']} error(s)", "err"), unsafe_allow_html=True)
        src_filter = st.text_input("Filter errors", placeholder="Search by source or message…",
                                   label_visibility="collapsed", key="err_q").strip().lower()
        recent = e["recent"][:20]
        if src_filter:
            recent = [x for x in recent
                      if src_filter in (x.get("source", "").lower())
                      or src_filter in (x.get("message", "").lower())]
        if not recent:
            st.info("No errors match the filter.")
        else:
            for er in recent[:10]:
                st.caption(f"`{_utc_to_ist_hm(er['ts'])} IST` · **{er['source']}**: {er['message']}")

    st.markdown("##### Market")
    st.markdown(
        f"{_badge('OPEN' if m['open'] else 'CLOSED', 'ok' if m['open'] else 'muted')} "
        f"<span class='muted'>{m['date']} · {m['time_ist']} IST · session {m['session']['start']}–{m['session']['end']}</span>",
        unsafe_allow_html=True,
    )


def render_settings():
    st.subheader("Settings")
    resp = api.get_config()
    if resp.get("status") != "ok":
        st.warning("Could not load settings.")
        return
    cfg = resp["data"]

    tab_market, tab_sl, tab_entry, tab_strategy, tab_sched = st.tabs(
        ["Market hours", "SL & trailing", "Entry & margin", "Strategy", "Schedulers"]
    )

    new_cfg = {}

    with tab_market:
        st.caption("The bot generates leads and places orders inside this daily window.")
        c1, c2 = st.columns(2)
        with c1:
            start = st.time_input("Trading start (IST)", value=_ui_time(cfg.get("trading_start", "10:00")))
        with c2:
            end = st.time_input("Square-off time (IST)", value=_ui_time(cfg.get("sqoff_time", "14:00")))
        new_cfg["trading_start"] = start.strftime("%H:%M")
        new_cfg["sqoff_time"] = end.strftime("%H:%M")

    with tab_sl:
        st.caption("Risk management per open position.")
        sl = st.number_input("Initial SL %", min_value=1.0, max_value=30.0,
                             value=float(cfg.get("initial_sl_pct", 10.0)),
                             help="Hard stop-loss distance from entry, in %.")
        activate = st.number_input("Trail activate % (past initial SL)", min_value=0.0, max_value=50.0,
                                   value=float(cfg.get("trail_activate_pct", 20.0)),
                                   help="Trailing starts once LTP moves this far PAST the initial SL in the profitable direction. "
                                        "Example: initial SL 90 + 20% → trailing begins at ltp ≥ 108.")
        gap = st.number_input("Trail gap % from current LTP", min_value=1.0, max_value=30.0,
                              value=float(cfg.get("trail_gap_pct", 10.0)),
                              help="After activation, SL = ltp ± this % (ratcheted — never moves against you).")
        new_cfg["initial_sl_pct"] = float(sl)
        new_cfg["trail_activate_pct"] = float(activate)
        new_cfg["trail_gap_pct"] = float(gap)

    with tab_entry:
        st.caption("How leads become orders.")
        divergence = st.number_input("Max lead price divergence %", min_value=0.1, max_value=5.0,
                                     value=float(cfg.get("max_lead_price_divergence_pct", 0.5)),
                                     help="Skip the lead if the contract price has drifted too far from the signal level.")
        min_days = st.number_input("Min days to expiry", min_value=1, max_value=30,
                                   value=int(cfg.get("min_days_to_expiry", 5)))
        lots = st.number_input("Lots per trade", min_value=1, max_value=10,
                               value=int(cfg.get("qty_lots_per_trade", 1)))
        limit_premium = st.number_input(
            "Limit premium % over LTP", min_value=0.0, max_value=5.0, step=0.1,
            value=float(cfg.get("entry_limit_premium_pct", 1.0)),
            help="How much above LTP to bid for the entry LIMIT. Higher = more fills, more slippage.",
        )
        fill_timeout = st.number_input(
            "Fill timeout (seconds)", min_value=5, max_value=300,
            value=int(cfg.get("entry_order_fill_timeout_seconds", 30)),
            help="How long to wait for the LIMIT to fill before cancelling and skipping the lead.",
        )
        st.markdown("**Margin affordability**")
        margin_check = st.checkbox(
            "Skip leads the margin can't afford",
            value=bool(cfg.get("margin_check_enabled", True)),
            help="Prefer ATM; walk toward cheaper OTM up to the depth below if needed.",
        )
        max_depth = st.number_input(
            "Max strikes from ATM for margin", min_value=0, max_value=10,
            value=int(cfg.get("margin_max_depth", cfg.get("margin_strikes_below", 3))),
        )
        new_cfg.update({
            "max_lead_price_divergence_pct": float(divergence),
            "min_days_to_expiry": int(min_days),
            "qty_lots_per_trade": int(lots),
            "entry_limit_premium_pct": float(limit_premium),
            "entry_order_fill_timeout_seconds": int(fill_timeout),
            "margin_check_enabled": bool(margin_check),
            "margin_max_depth": int(max_depth),
        })

    with tab_strategy:
        st.caption("Which patterns qualify as a lead, and how strict the filter is.")
        patterns = st.multiselect(
            "Enabled patterns",
            ["horizontal_range", "trendline", "triangle", "flag_pennant", "head_shoulders", "volume_breakout"],
            default=cfg.get("breakout.patterns_enabled", ["volume_breakout"]),
            help="Only the selected chart patterns can generate leads.",
        )
        min_conf = st.slider("Min confidence", 0.0, 1.0,
                             float(cfg.get("breakout.min_confidence", 0.6)), 0.05,
                             help="Leads below this confidence are dropped.")
        st.markdown("**Volume confirmation**")
        require_spike = st.checkbox(
            "Require volume spike for every signal",
            value=bool(cfg.get("breakout.require_volume_spike", False)),
        )
        vmult = st.number_input("Volume spike multiplier", min_value=1.0, max_value=10.0,
                                value=float(cfg.get("breakout.volume_multiplier", 4.0)), step=0.5)
        vboost = st.slider("Volume confidence boost", 0.0, 0.4,
                           float(cfg.get("breakout.volume_boost", 0.15)), 0.05)
        st.markdown("**Indicator context (Tier-3)**")
        st.caption("Optional context features blended into the composite score. All default OFF — turn on one at a time and watch hit-rate in History before stacking.")
        c1, c2, c3 = st.columns(3)
        with c1:
            tod_on = st.checkbox(
                "Time-of-day",
                value=bool(cfg.get("scoring.enable_time_of_day", False)),
                help="Bias the score by IST session window (10:00-11:30 and 13:00-14:30 score higher).",
            )
        with c2:
            oi_on = st.checkbox(
                "Open Interest",
                value=bool(cfg.get("scoring.enable_oi", False)),
                help="Boost signals that align with dominant OI build-up near the strike.",
            )
        with c3:
            iv_on = st.checkbox(
                "Implied Volatility",
                value=bool(cfg.get("scoring.enable_iv", False)),
                help="Discount signals when IV is high, boost when IV is compressed (breakouts from compression are more meaningful).",
            )
        with st.expander("Pattern detection knobs", expanded=False):
            st.caption("Tune the swing-based detectors. Defaults match the values Bulkowski's pattern-stats tables were derived on.")
            lookback = st.number_input("Lookback (days)", min_value=20, max_value=250,
                                       value=int(cfg.get("breakout.lookback_days", 60)))
            swing_k = st.number_input("Swing K (pivot strength)", min_value=1, max_value=10,
                                      value=int(cfg.get("breakout.swing_k", 3)))
            proximity = st.number_input("Proximity %", min_value=0.05, max_value=5.0,
                                        value=float(cfg.get("breakout.proximity_pct", 0.5)), step=0.05,
                                        help="Max % distance from trigger level for a signal to qualify.")
            min_touches = st.number_input("Min touches (horizontal)", min_value=1, max_value=10,
                                         value=int(cfg.get("breakout.min_touches", 1)))
            min_trendline_points = st.number_input("Min trendline points", min_value=2, max_value=10,
                                                   value=int(cfg.get("breakout.min_trendline_points", 4)))
            pole_pct = st.number_input("Flag pole %", min_value=1.0, max_value=20.0,
                                       value=float(cfg.get("breakout.pole_pct", 3.0)), step=0.5,
                                       help="Minimum prior move (%) before a flag/pennant qualifies.")
            vwindow = st.number_input("Volume window (days)", min_value=5, max_value=60,
                                      value=int(cfg.get("breakout.volume_window", 20)))
            vlookback = st.number_input("Volume lookback (days)", min_value=1, max_value=20,
                                        value=int(cfg.get("breakout.volume_lookback", 5)),
                                        help="Number of recent bars checked for the spike.")
        with st.expander("Market alignment, top-K & decay (Tier-2 / Tier-4)", expanded=False):
            st.caption("Cross-asset filter, signal-per-instrument cap, and post-queue staleness/calibration.")
            alignment = st.selectbox(
                "Market alignment filter",
                ["off", "nifty_sma20"],
                index=["off", "nifty_sma20"].index(cfg.get("breakout.market_alignment", "off"))
                    if cfg.get("breakout.market_alignment", "off") in ["off", "nifty_sma20"] else 0,
                help="'off' = no filter. 'nifty_sma20' = only fire CALL signals in uptrend, PUT in downtrend (else neutral).",
            )
            top_k = st.number_input("Top-K signals per instrument", min_value=1, max_value=5,
                                    value=int(cfg.get("breakout.top_k_per_instrument", 2)),
                                    help="Emit up to this many leads per instrument (each direction can fire independently).")
            staleness_min = st.number_input(
                "Staleness half-life (minutes)", min_value=0, max_value=120,
                value=int(cfg.get("breakout.staleness_half_life_min", 0)),
                help="0 = no decay. >0 = queued lead confidence decays by half every N minutes.",
            )
            cal_alpha = st.slider(
                "Calibration alpha", 0.0, 1.0,
                float(cfg.get("scoring.calibration_alpha", 0.0)), 0.05,
                help="0 = no historical calibration. 1 = lean entirely on past win-rate for this pattern.",
            )
        new_cfg.update({
            "breakout.patterns_enabled": patterns,
            "breakout.min_confidence": float(min_conf),
            "breakout.require_volume_spike": bool(require_spike),
            "breakout.volume_multiplier": float(vmult),
            "breakout.volume_boost": float(vboost),
            "scoring.enable_time_of_day": bool(tod_on),
            "scoring.enable_oi": bool(oi_on),
            "scoring.enable_iv": bool(iv_on),
            "breakout.lookback_days": int(lookback),
            "breakout.swing_k": int(swing_k),
            "breakout.proximity_pct": float(proximity),
            "breakout.min_touches": int(min_touches),
            "breakout.min_trendline_points": int(min_trendline_points),
            "breakout.pole_pct": float(pole_pct),
            "breakout.volume_window": int(vwindow),
            "breakout.volume_lookback": int(vlookback),
            "breakout.market_alignment": alignment,
            "breakout.top_k_per_instrument": int(top_k),
            "breakout.staleness_half_life_min": int(staleness_min),
            "scoring.calibration_alpha": float(cal_alpha),
        })

    with tab_sched:
        st.caption("How often each background task runs.")
        lead_sec = st.number_input("Lead generator (seconds)", min_value=10, max_value=3600,
                                   value=int(cfg.get("scheduler.lead_generator_seconds", 300)))
        track_sec = st.number_input("Trade tracker (seconds)", min_value=5, max_value=600,
                                    value=int(cfg.get("scheduler.trade_tracker_seconds", 30)))
        place_sec = st.number_input("Order placer (seconds)", min_value=5, max_value=600,
                                    value=int(cfg.get("scheduler.order_placer_seconds", 30)))
        new_cfg.update({
            "scheduler.lead_generator_seconds": int(lead_sec),
            "scheduler.trade_tracker_seconds": int(track_sec),
            "scheduler.order_placer_seconds": int(place_sec),
        })

    st.caption("Changes take effect on the next backend restart.")

    b1, b2, _ = st.columns([1, 1, 4])
    with b1:
        if st.button("Save settings", type="primary", use_container_width=True):
            resp = api.update_config(new_cfg)
            if resp.get("status") == "ok":
                st.success("Saved.")
            else:
                st.error(resp.get("error", {}).get("message", "Save failed."))
    with b2:
        if st.button("Reload", use_container_width=True, help="Discard edits and reload from the backend."):
            st.rerun()


# --- main ----------------------------------------------------------------


def main():
    api.bootstrap_from_query()
    if not api.is_authenticated():
        render_login()
        st.stop()

    st.markdown(_css(), unsafe_allow_html=True)
    page = render_sidebar()

    # Page title with status dot
    ks_active = api.get_killswitch().get("data", {}).get("active", False)
    market_open = False
    health = api.get_health()
    if health.get("status") == "ok":
        market_open = health["data"]["market"].get("open", False)
    dot_color = LOSS if ks_active else (PROFIT if market_open else MUTED)
    dot = f"<span class='dot' style='background:{dot_color};'></span>"
    _html(
        f"<div style='display:flex;align-items:center;gap:10px;margin:6px 0 4px 0;'>"
        f"{dot}<span style='font-size:1.5rem;font-weight:700;'>TradePilot</span></div>"
    )

    render_killswitch()
    render_token_status()

    views = {
        "Dashboard": render_dashboard,
        "Open Trades": render_open_trades,
        "Leads": render_leads,
        "Instruments": render_instruments,
        "History": render_history,
        "Health": render_health,
        "Settings": render_settings,
    }
    views[page]()

    _html(
        f"<div class='muted' style='margin-top:24px;text-align:center'>"
        f"Last render: {_ist_now_str()} IST</div>"
    )


main()
