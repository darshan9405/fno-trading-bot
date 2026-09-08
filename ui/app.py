"""F&O Automated Trading — Streamlit UI.

Tabs: Dashboard (live P&L), Open Trades, Leads, History, Health, Settings.
Auto-polls Dashboard + Open Trades via st.fragment(run_every).
"""

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


def _css() -> str:
    return f"""
    <style>
    .stApp {{ max-width: 760px; margin: auto; background: {BG}; }}
    [data-testid="stMetric"] {{
        background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px;
        padding: 10px 14px;
    }}
    [data-testid="stMetricLabel"] {{ color: {MUTED}; }}
    [data-testid="stMetricValue"] {{ font-size: 1.25rem; color: #e2e8f0; }}
    [data-testid="stMetric"]:hover {{ border-color: {PRIMARY}66; }}
    [data-testid="stSidebar"] {{ background: {CARD}; }}
    .ks-banner {{ border-radius: 10px; padding: 10px 14px; margin: 6px 0;
                  border: 1px solid; font-weight: 600; }}
    .badge {{ display:inline-block; border-radius: 999px; padding: 2px 10px;
              font-size: 0.78rem; font-weight: 600; }}
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
    footer, [data-testid="stHeader"] {{ background: transparent; }}

    /* Primary/secondary button accents */
    [data-testid="stButton"] button[kind="primary"] {{
        background: {PRIMARY}; border-color: {PRIMARY};
    }}
    [data-testid="stButton"] button[kind="primary"]:hover {{
        background: #818cf8; border-color: #818cf8;
    }}

    /* Bigger sidebar controls (navigation + buttons) */
    [data-testid="stSidebar"] [data-testid="stRadio"] label {{
        font-size: 1.08rem; padding: 10px 8px; border-radius: 8px;
    }}
    [data-testid="stSidebar"] [data-testid="stRadio"] div[role="radiogroup"] {{ gap: 2px; }}
    [data-testid="stSidebar"] [data-testid="stButton"] button {{
        height: 48px; font-size: 1.05rem; border-radius: 10px; font-weight: 600;
    }}
    [data-testid="stSidebar"] [data-testid="stExpander"] details {{ border-radius: 10px; }}
    [data-testid="stSidebar"] h3 {{ margin-bottom: 4px; }}
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
            return frag(run_every="5s")(fn)
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
    df = pd.DataFrame([{k: r.get(k) for k in cols} for r in rows])
    return df


# --- auth ----------------------------------------------------------------


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
    st.markdown(
        f"<a href='{api.login_url()}' style='display:block;text-align:center;"
        f"background:{PRIMARY};color:#fff;padding:12px;border-radius:10px;"
        f"text-decoration:none;font-weight:600;'>Login with Upstox</a>",
        unsafe_allow_html=True,
    )
    st.caption("You'll be redirected to Upstox, then back to this dashboard.")


# --- sidebar -------------------------------------------------------------


def render_sidebar() -> str:
    with st.sidebar:
        st.markdown("### Navigation")
        page = st.radio(
            "Page",
            ["Dashboard", "Open Trades", "Leads", "Instruments", "History", "Health", "Settings"],
            label_visibility="collapsed",
        )

        st.markdown("---")
        st.markdown("### Market")
        health = api.get_health()
        if health.get("status") == "ok":
            m = health["data"]["market"]
            _html(f"**{_badge('OPEN' if m['open'] else 'CLOSED', 'ok' if m['open'] else 'muted')}**")
            st.caption(f"Session {m['session']['start']}–{m['session']['end']} · {m['time_ist']} IST")
        else:
            _html(f"**{_badge('—', 'muted')}**")

        ks = api.get_killswitch()
        active = ks.get("data", {}).get("active") if ks.get("status") == "ok" else None
        _html(f"**Killswitch:** {_badge('ACTIVE' if active else 'ARMED', 'err' if active else 'ok')}")

        b = health.get("data", {}).get("broker", {}) if health.get("status") == "ok" else {}
        if b.get("token_expired"):
            _html(f"**Token:** {_badge('EXPIRED', 'err')}")
        elif b.get("token_valid_until"):
            _html(f"**Token:** {_badge(f'OK · {_utc_to_ist_hm(b.get("token_valid_until"))} IST', 'ok')}")

        render_killswitch_setup()

        st.markdown("---")
        st.markdown("### Controls")
        if st.button("Refresh", use_container_width=True):
            st.rerun()
        if st.button("Logout", use_container_width=True):
            api.logout()
            st.rerun()

    return page


# --- killswitch ----------------------------------------------------------


def render_killswitch():
    """Full-width alert banner when the killswitch is active (plus Release)."""
    ks = api.get_killswitch()
    data = ks.get("data", {}) if ks.get("status") == "ok" else {}
    active = data.get("active", False)
    if not active:
        return  # the green status dot in the header indicates "system active"
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
    """Alert only when the Upstox token is dead (the bot cannot trade).

    SSO login refreshes the token each time, so a healthy/near-expiry token is
    informational only (sidebar badge + Health tab), not a banner.
    """
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
    st.markdown(
        f"<a href='{api.login_url()}' style='display:block;text-align:center;"
        f"background:{PRIMARY};color:#fff;padding:10px;border-radius:8px;"
        f"text-decoration:none;font-weight:600;'>Login with Upstox</a>",
        unsafe_allow_html=True,
    )


def render_killswitch_setup():
    with st.sidebar.expander("Killswitch", expanded=False):
        st.caption("Square off all managed trades and halt the system for the day.")
        reason = st.text_input("Reason", placeholder="e.g. market crash")
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
        st.warning(f"{pnl.get('error', {}).get('message', 'P&L unavailable')} — live P&L needs an Upstox token.")
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
        _html(
            f"<div class='row'><span>Open positions: <b>{d.get('open_positions', 0)}</b></span>"
            f"<span style='margin-left:auto'>{_badge(_money(total), 'up' if (total or 0) > 0 else 'down')}</span></div>"
        )

    health = api.get_health()
    if health.get("status") == "ok":
        m = health["data"]["market"]
        st.caption(
            f"Market {'OPEN' if m['open'] else 'closed'} · {m['time_ist']} IST · "
            f"session {m['session']['start']}–{m['session']['end']}"
        )

    st.markdown("#### Today's signals")
    leads = api.get_leads()
    if leads.get("status") == "ok":
        rows = leads["data"]["leads"]
        if rows:
            df = pd.DataFrame(
                [
                    {
                        "Symbol": r["underlying"].split("|")[-1],
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
            st.info("No signals yet today.")


@_fragment
def render_open_trades():
    st.subheader("Open Trades")
    resp = api.get_open_trades()
    if resp.get("status") != "ok":
        st.warning(resp.get("error", {}).get("message", "Could not load open trades."))
        return
    rows = resp["data"].get("trades", [])
    if not rows:
        st.info("No open trades.")
        return

    df = _trades_df(
        rows,
        ["symbol", "direction", "entry_price", "ltp", "unrealised_pnl", "current_sl", "trail_state"],
    )
    df.columns = ["Symbol", "Dir", "Entry", "LTP", "P&L", "SL", "Trail"]
    styled = df.style.map(_style_pnl_col, subset=["P&L"])
    st.dataframe(styled, use_container_width=True, hide_index=True)

    total = sum(r.get("unrealised_pnl") or 0 for r in rows)
    _html(
        f"<div class='row'><span class='muted'>Open positions: <b>{len(rows)}</b></span>"
        f"<span style='margin-left:auto'>Unrealised {_pnl_html(total)}</span></div>"
    )


def render_leads():
    st.subheader("Leads")

    c1, c2 = st.columns([3, 1])
    with c2:
        if st.button("Generate now", type="primary", use_container_width=True,
                     help="Run the lead generator manually (works outside trading hours)."):
            resp = api.generate_leads()
            if resp.get("status") == "ok":
                generated = resp["data"].get("generated", 0)
                st.success(f"Generated {generated} lead(s).")
            else:
                st.error(resp.get("error", {}).get("message", "Generation failed."))
            st.rerun()

    resp = api.get_leads()
    if resp.get("status") != "ok":
        st.warning("Could not load leads.")
        return
    rows = resp["data"].get("leads", [])
    if not rows:
        st.info("No leads.")
        return

    for r in rows:
        c1, c2, c3 = st.columns([4, 1, 2])
        with c1:
            _html(
                f"<div style='font-weight:600'>{r['underlying'].split('|')[-1]} "
                f"{_badge(r['direction'], 'up' if r['direction'] == 'CALL' else 'down')} "
                f"{_badge(r['status'], 'ok' if r['status'] == 'placed' else 'muted')}</div>"
                f"<div class='muted'>{r['signal_type']} @ {_num(r['signal_level'])}</div>"
                + _lead_plan_line(r)
            )
        with c2:
            pct = int((r.get("confidence") or 0) * 100)
            _html(
                f"<div style='margin-top:8px'>{pct}%</div>"
                f"<div class='conf-bar'><div class='conf-fill' style='width:{pct}%;"
                f"background:{PROFIT if pct >= 60 else WARN}'></div></div>"
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
    _html(f"<div class='muted'>{len(rows)} F&O underlyings · <b>{enabled} active</b> · "
          f"only active ones generate leads</div>")

    segments = ["All"] + sorted({r["segment"] for r in rows})
    seg = st.selectbox("Segment", segments, key="instr_seg")
    q = st.text_input("Search symbol", key="instr_q").strip().lower()
    filtered = [
        r for r in rows
        if (seg == "All" or r["segment"] == seg) and (not q or q in r["symbol"].lower())
    ]

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
        st.info("No closed trades.")
        return

    df = _trades_df(
        rows,
        ["symbol", "direction", "entry_price", "exit_price", "realized_pnl", "exit_reason", "exit_time"],
    )
    df.columns = ["Symbol", "Dir", "Entry", "Exit", "P&L", "Reason", "Closed"]
    styled = df.style.map(_style_pnl_col, subset=["P&L"])
    st.dataframe(styled, use_container_width=True, hide_index=True)

    total = sum(r.get("realized_pnl") or 0 for r in rows)
    wins = sum(1 for r in rows if (r.get("realized_pnl") or 0) > 0)
    _html(
        f"<div class='row'><span class='muted'>Trades: <b>{len(rows)}</b> · Wins: <b>{wins}</b></span>"
        f"<span style='margin-left:auto'>Net {_pnl_html(total)}</span></div>"
    )


def render_health():
    st.subheader("System Health")
    resp = api.get_health()
    if resp.get("status") != "ok":
        st.warning("Could not load health.")
        return
    d = resp["data"]

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
        col3.caption(hb["note"] or "")

    st.markdown("##### Broker")
    b = d["broker"]
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
    if d["errors"]["count"] == 0:
        st.markdown(f"{_badge('NO ERRORS', 'ok')}", unsafe_allow_html=True)
    else:
        st.markdown(_badge(f"{d['errors']['count']} error(s)", "err"), unsafe_allow_html=True)
        for e in d["errors"]["recent"][:5]:
            st.caption(f"`{e['ts']}` · {e['source']}: {e['message']}")

    st.markdown("##### Market")
    m = d["market"]
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

    st.markdown("##### Market hours")
    start = st.time_input("Trading start (IST)", value=_ui_time(cfg.get("trading_start", "10:00")))
    end = st.time_input("Square-off time (IST)", value=_ui_time(cfg.get("sqoff_time", "14:00")))
    st.caption("The bot generates leads and places orders inside this daily window.")

    st.markdown("##### Stop-loss & trailing")
    sl = st.number_input("Initial SL %", min_value=1.0, max_value=30.0, value=float(cfg.get("initial_sl_pct", 10.0)))
    activate = st.number_input("Trail activate %", min_value=0.0, max_value=20.0, value=float(cfg.get("trail_activate_pct", 5.0)))
    gap = st.number_input("Trail gap %", min_value=1.0, max_value=20.0, value=float(cfg.get("trail_gap_pct", 5.0)))

    st.markdown("##### Entry rules")
    divergence = st.number_input("Max lead price divergence %", min_value=0.1, max_value=5.0, value=float(cfg.get("max_lead_price_divergence_pct", 0.5)))
    min_days = st.number_input("Min days to expiry", min_value=1, max_value=30, value=int(cfg.get("min_days_to_expiry", 5)))
    lots = st.number_input("Lots per trade", min_value=1, max_value=10, value=int(cfg.get("qty_lots_per_trade", 1)))
    mp = st.number_input(
        "Market protection %", min_value=1, max_value=10, value=int(cfg.get("market_protection_pct", 2)),
        help="Max % the MARKET/SL-M fill may deviate from the option's LTP (Upstox market protection).",
    )

    st.markdown("##### Margin affordability")
    margin_check = st.checkbox(
        "Skip leads the margin can't afford",
        value=bool(cfg.get("margin_check_enabled", True)),
        help="Prefer the ATM strike; walk toward cheaper OTM (PUT down, CALL up) up to the depth below and pick the first contract the margin covers. Skip the lead if none fits.",
    )
    max_depth = st.number_input(
        "Max strikes from ATM for margin", min_value=0, max_value=10,
        value=int(cfg.get("margin_max_depth", cfg.get("margin_strikes_below", 3))),
        help="How many strikes away from ATM (toward cheaper OTM) the margin check may go. ATM is tried first.",
    )

    st.markdown("##### Strategy")
    patterns = st.multiselect(
        "Enabled patterns",
        ["horizontal_range", "trendline", "triangle", "flag_pennant", "head_shoulders", "volume_breakout"],
        default=cfg.get("breakout.patterns_enabled", ["volume_breakout"]),
    )
    min_conf = st.slider("Min confidence", 0.0, 1.0, float(cfg.get("breakout.min_confidence", 0.6)), 0.05)

    st.markdown("##### Volume confirmation (Durgia 2025)")
    require_spike = st.checkbox(
        "Require volume spike for every signal",
        value=bool(cfg.get("breakout.require_volume_spike", False)),
        help="Only emit breakouts that coincide with a volume spike (>= multiplier x rolling average).",
    )
    vmult = st.number_input("Volume spike multiplier", min_value=1.0, max_value=10.0, value=float(cfg.get("breakout.volume_multiplier", 4.0)), step=0.5)
    vboost = st.slider("Volume confidence boost", 0.0, 0.4, float(cfg.get("breakout.volume_boost", 0.15)), 0.05)

    if st.button("Save settings", type="primary", use_container_width=True):
        resp = api.update_config(
            {
                "trading_start": start.strftime("%H:%M"),
                "sqoff_time": end.strftime("%H:%M"),
                "initial_sl_pct": float(sl),
                "trail_activate_pct": float(activate),
                "trail_gap_pct": float(gap),
                "max_lead_price_divergence_pct": float(divergence),
                "min_days_to_expiry": int(min_days),
                "qty_lots_per_trade": int(lots),
                "market_protection_pct": int(mp),
                "margin_check_enabled": bool(margin_check),
                "margin_max_depth": int(max_depth),
                "breakout.patterns_enabled": patterns,
                "breakout.min_confidence": float(min_conf),
                "breakout.require_volume_spike": bool(require_spike),
                "breakout.volume_multiplier": float(vmult),
                "breakout.volume_boost": float(vboost),
            }
        )
        if resp.get("status") == "ok":
            st.success("Saved.")
        else:
            st.error(resp.get("error", {}).get("message", "Save failed."))


# --- main ----------------------------------------------------------------


def main():
    api.bootstrap_from_query()
    if not api.is_authenticated():
        render_login()
        st.stop()

    st.markdown(_css(), unsafe_allow_html=True)
    page = render_sidebar()

    ks_active = api.get_killswitch().get("data", {}).get("active", False)
    dot = f"<span class='dot' style='background:{LOSS if ks_active else PROFIT};'></span>"
    _html(
        f"<div style='display:flex;align-items:center;gap:10px;margin:4px 0 2px 0;'>"
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

    _html(f"<div class='muted' style='margin-top:24px;text-align:center'>Last render: {datetime.now():%H:%M:%S}</div>")


main()