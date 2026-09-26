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

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
import json

import pandas as pd
import streamlit as st

import api_client as api

st.set_page_config(
    page_title="TradePilot",
    page_icon=":material/show_chart:",
    layout="centered",
    initial_sidebar_state="expanded",
    menu_items=None,
)

# Palette — Groww-inspired dark trading-terminal theme.
# Pure-black canvas (Groww dark mode), single teal accent for primary actions
# (Groww brand #04B488), subtle elevated cards, semantic green/red for P&L.
# Generous 6-8px radii (Groww uses rounded surfaces), tight typography, and
# crisp tabular numerals for a fintech look.
PRIMARY = "#04b488"        # Groww brand teal — single accent for actions / focus
PRIMARY_HOVER = "#1ed09a"  # brighter teal on hover (Groww "Grow" state)
PRIMARY_DIM = "#0a3d2f"    # subtle teal tint for active backgrounds
SECONDARY = "#a0a0a0"      # informational, not a decoration
ACCENT_PURPLE = "#617bff"  # Groww "prime" purple — used sparingly
PROFIT = "#00b887"         # Groww green for positive P&L
LOSS = "#ea5455"           # Groww red for negative P&L
WARN = "#f5a623"           # amber for warnings / killswitch
MUTED = "#8b8b8b"          # secondary text
TEXT = "#ffffff"           # primary text (Groww white-on-black)
TEXT_DIM = "#6a6a6a"       # tertiary text / dividers
BG = "#000000"             # pure black — Groww dark mode canvas
BG_RAISED = "#0a0a0a"      # panels that float above BG slightly
CARD = "#121212"           # raised surfaces (Groww surface primary)
CARD_HOVER = "#1a1a1a"     # hover state
CARD_ACTIVE = "#202020"    # pressed/active state
BORDER = "#222222"         # default 1px border
BORDER_STRONG = "#333333"  # hover/active border
SIDEBAR_BG = "#0a0a0a"     # sidebar background (Groww-style layered)

REFRESH_SECS = 5       # dashboard + open trades auto-refresh cadence


def _css() -> str:
    """Groww-inspired dark trading-terminal design.

    Pure black canvas, Groww teal as the single accent, generously rounded
    cards (6-8px), subtle elevated surfaces, semantic green/red for P&L.
    Clean tabular typography for fintech feel.
    """
    return f"""
    <style>
    /* ---------- Typography ---------- */
    html, body, [class*="css"], .stApp, .stMarkdown {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter',
                   system-ui, 'Helvetica Neue', Arial, sans-serif;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      letter-spacing: -0.005em;
    }}
    /* Tabular nums everywhere — fintech-aligned numeric columns */
    .stApp, [data-testid="stMetricValue"], [data-testid="stMetricDelta"],
    .stat-value, .big-num, [data-testid="stDataFrame"] {{
      font-variant-numeric: tabular-nums;
    }}
    code, .mono, [data-testid="stDataFrame"], .stMarkdown pre {{
      font-family: 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace;
      font-variant-numeric: tabular-nums;
    }}

    /* ---------- App shell ---------- */
    .stApp {{
      max-width: 1080px; margin: auto;
      background: {BG}; color: {TEXT};
    }}
    [data-testid="stHeader"] {{ background: transparent; height: 0; }}
    footer {{ visibility: hidden; }}
    #MainMenu {{ visibility: hidden; }}

    /* ---------- Global: kill any default anchor link styling ----------
       Belt-and-braces: even though the login/logout buttons are real
       Streamlit `<button>` widgets (no anchor tag in the DOM), any stray
       link embedded elsewhere in the page must never look like the
       browser-default underlined hyperlink. */
    a, a:link, a:visited, a:hover, a:active, a:focus {{
      color: inherit !important;
      text-decoration: none !important;
      outline: none !important;
    }}
    a, a * {{ cursor: pointer; }}

    /* ---------- Sidebar — Groww-style layered nav ---------- */
    [data-testid="stSidebar"] {{
      background: {SIDEBAR_BG};
      border-right: 1px solid {BORDER};
      padding: 16px 12px;
      min-width: 220px;
    }}
    [data-testid="stSidebar"] h3 {{
      color: {MUTED}; font-size: 0.66rem; text-transform: uppercase;
      letter-spacing: .14em; margin: 18px 10px 8px 10px; font-weight: 700;
    }}
    .sidebar-brand {{
      margin: 4px 10px 4px; color: {TEXT};
      font-size: 1.05rem; font-weight: 700; letter-spacing: -.01em;
      display: flex; align-items: center; gap: 10px;
    }}
    .sidebar-brand .brand-mark {{
      width: 24px; height: 24px; border-radius: 6px;
      background: linear-gradient(135deg, {PRIMARY}, {PRIMARY_HOVER});
      display: inline-flex; align-items: center; justify-content: center;
      color: #000; font-weight: 800; font-size: 12px;
      box-shadow: 0 1px 6px rgba(4,180,136,.35);
    }}
    .sidebar-subtitle {{
      margin: 0 10px 16px; color: {MUTED}; font-size: .74rem;
    }}
    .sidebar-divider {{
      border: 0; border-top: 1px solid {BORDER}; margin: 14px 8px;
    }}
    /* Compact radio nav (Groww uses pill-shaped nav rows) */
    [data-testid="stSidebar"] [data-testid="stRadio"] label {{
      font-size: 0.88rem; padding: 8px 12px;
      border-radius: 6px; min-height: 34px;
      font-weight: 500;
    }}
    [data-testid="stSidebar"] [data-testid="stRadio"] div[role="radiogroup"] {{ gap: 2px; }}
    [data-testid="stRadio"] label {{
      transition: background .12s ease, color .12s ease;
    }}
    [data-testid="stRadio"] label:hover {{
      background: {CARD_HOVER}; color: {TEXT};
    }}
    [data-testid="stRadio"] label:has(input:checked) {{
      background: {PRIMARY_DIM}; color: {PRIMARY_HOVER};
      font-weight: 600;
      box-shadow: inset 2px 0 0 {PRIMARY};
    }}
    .sidebar-card {{
      background: {CARD}; border: 1px solid {BORDER};
      border-radius: 8px; padding: 10px 12px; margin: 6px 4px;
    }}
    .sidebar-card-title {{
      color: {MUTED}; font-size: .66rem; font-weight: 700;
      letter-spacing: .12em; text-transform: uppercase; margin-bottom: 8px;
    }}
    .status-row {{
      display: flex; align-items: center; gap: 8px;
      min-height: 20px; font-size: .78rem; color: {TEXT};
    }}
    .status-label {{ flex: 1; color: {MUTED}; font-size: .76rem; }}
    .status-value {{ font-weight: 600; white-space: nowrap; font-variant-numeric: tabular-nums; font-size: .78rem; }}
    .status-dot {{
      width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: {MUTED};
    }}
    .status-dot.ok {{ background: {PROFIT}; }}
    .status-dot.warn {{ background: {WARN}; }}
    .status-dot.err {{ background: {LOSS}; }}

    /* ---------- Stat tiles — denser, smoother ---------- */
    .stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }}
    .stat-tile {{
      background: {CARD}; border: 1px solid {BORDER};
      border-radius: 8px; padding: 10px 12px;
      transition: border-color .12s ease, background .12s ease, transform .12s ease;
    }}
    .stat-tile:hover {{
      border-color: {BORDER_STRONG}; background: {CARD_HOVER};
      transform: translateY(-1px);
    }}
    .stat-label {{
      color: {MUTED}; font-size: .66rem; text-transform: uppercase;
      letter-spacing: .1em; font-weight: 700;
    }}
    .stat-value {{
      color: {TEXT}; font-size: 1.15rem; font-weight: 700;
      margin-top: 4px; font-variant-numeric: tabular-nums;
    }}

    /* ---------- Badges + chips ---------- */
    .badge {{
      display: inline-block; border-radius: 4px; padding: 3px 8px;
      font-size: 0.7rem; font-weight: 600; letter-spacing: .02em;
      font-variant-numeric: tabular-nums;
    }}
    .chip {{
      display: inline-block; border-radius: 6px; padding: 3px 10px;
      font-size: 0.7rem; font-weight: 600;
      background: {CARD}; color: {MUTED};
      margin-right: 4px; border: 1px solid {BORDER};
      font-variant-numeric: tabular-nums;
      transition: background .12s ease, color .12s ease, border-color .12s ease;
    }}
    .chip-active {{
      background: {PRIMARY_DIM}; color: {PRIMARY_HOVER};
      border-color: rgba(4,180,136,.4);
    }}
    .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }}
    @keyframes ks-pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.35; }} }}
    .ks-dot {{
      width: 8px; height: 8px; border-radius: 50%; background: {LOSS};
      display: inline-block; animation: ks-pulse 1.5s infinite;
    }}
    .ks-banner {{
      border-radius: 8px; padding: 12px 16px; margin: 8px 0;
      border: 1px solid {BORDER}; border-left: 3px solid {LOSS};
      font-weight: 600; background: {CARD};
    }}

    /* ---------- Position cards (open trades) ---------- */
    .row {{ display: flex; gap: 8px; align-items: center; }}
    .conf-bar {{ height: 4px; background: {BORDER}; overflow: hidden; border-radius: 4px; }}
    .conf-fill {{ height: 4px; transition: width .3s ease; border-radius: 4px; }}
    .sec-title {{
      color: {MUTED}; font-size: 0.74rem; text-transform: uppercase;
      letter-spacing: .14em; margin: 18px 0 8px 0; font-weight: 700;
    }}
    .big-num {{ font-size: 1.5rem; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: -.01em; }}
    .muted {{ color: {MUTED}; font-size: 0.84rem; }}
    .pos-card {{
      background: {CARD}; border: 1px solid {BORDER};
      border-radius: 8px; padding: 14px 16px; margin: 8px 0;
      transition: border-color .12s ease, background .12s ease;
    }}
    .pos-card:hover {{ border-color: {BORDER_STRONG}; background: {CARD_HOVER}; }}
    .pos-card.up {{ border-left: 3px solid {PROFIT}; }}
    .pos-card.down {{ border-left: 3px solid {LOSS}; }}
    .pos-card.flat {{ border-left: 3px solid {TEXT_DIM}; }}

    /* ---------- Buttons — rounded, smooth ---------- */
    [data-testid="stButton"] button {{
      border-radius: 6px; font-weight: 600;
      transition: background .12s ease, border-color .12s ease, transform .06s ease;
    }}
    [data-testid="stButton"] button[kind="primary"] {{
      background: {PRIMARY}; border-color: {PRIMARY}; color: #000;
    }}
    [data-testid="stButton"] button[kind="primary"]:hover {{
      background: {PRIMARY_HOVER}; border-color: {PRIMARY_HOVER}; color: #000;
      box-shadow: 0 2px 12px rgba(4,180,136,.30);
    }}
    [data-testid="stButton"] button[kind="primary"]:active {{
      background: {PRIMARY}; transform: translateY(1px);
    }}
    [data-testid="stButton"] button[kind="secondary"] {{
      background: {CARD}; border: 1px solid {BORDER}; color: {TEXT};
    }}
    [data-testid="stButton"] button[kind="secondary"]:hover {{
      background: {CARD_HOVER}; border-color: {BORDER_STRONG};
    }}
    /* Generate button — same primary teal, slightly taller */
    .lead-toolbar .stButton > button[kind="primary"] {{
      min-height: 44px; font-size: 0.96rem; border-radius: 8px;
    }}

    /* SSO login button — full-width teal CTA (anchor under the hood) */
    .sso-login-btn {{
      display: inline-flex !important; align-items: center; justify-content: center;
      gap: 10px; cursor: pointer; user-select: none;
      background: {PRIMARY}; color: #000 !important;
      padding: 0.85rem 1.1rem !important; border-radius: 8px;
      font-weight: 700; font-size: 1.0rem; line-height: 1.2;
      text-decoration: none !important; margin: 8px 0; width: 100%;
      border: 1px solid {PRIMARY};
      transition: background .12s ease, border-color .12s ease, box-shadow .12s ease;
      box-shadow: 0 2px 12px rgba(4,180,136,.18);
    }}
    .sso-login-btn:hover {{
      background: {PRIMARY_HOVER}; border-color: {PRIMARY_HOVER}; color: #000 !important;
      box-shadow: 0 4px 18px rgba(4,180,136,.35);
    }}
    .sso-login-btn:active {{ background: {PRIMARY}; }}
    .sso-login-btn:focus-visible {{
      outline: 2px solid {PRIMARY}; outline-offset: 2px;
      color: #000 !important;
    }}

    /* Sidebar logout — neutral, hover reveals danger */
    .sidebar-logout-btn {{
      display: flex !important; align-items: center; justify-content: center;
      gap: 8px; cursor: pointer; user-select: none;
      background: {CARD}; color: {TEXT} !important;
      border: 1px solid {BORDER}; border-radius: 6px;
      padding: 0 1rem; margin: 4px 0 6px 0; height: 38px;
      font-size: 0.88rem; font-weight: 600; line-height: 1.2;
      text-decoration: none !important; box-sizing: border-box;
      transition: background .12s ease, border-color .12s ease, color .12s ease;
    }}
    .sidebar-logout-btn:hover {{
      background: {CARD_HOVER}; border-color: rgba(234,84,85,.4);
      color: {LOSS} !important;
    }}
    .sidebar-logout-btn:focus-visible {{
      outline: 2px solid {PRIMARY}; outline-offset: 2px; color: {TEXT} !important;
    }}

    /* Sidebar controls — Groww-compact */
    [data-testid="stSidebar"] [data-testid="stButton"] button {{
      height: 38px; font-size: 0.88rem; border-radius: 6px; font-weight: 600;
    }}
    [data-testid="stSidebar"] [data-testid="stExpander"] details {{ border-radius: 8px; }}

    /* Form labels — small caps for density */
    [data-testid="stNumberInput"] label, [data-testid="stSlider"] label,
    [data-testid="stCheckbox"] label, [data-testid="stTextInput"] label,
    [data-testid="stTimeInput"] label, [data-testid="stSelectbox"] label,
    [data-testid="stMultiSelect"] label {{
      font-weight: 500; font-size: 0.8rem; color: {MUTED};
      text-transform: uppercase; letter-spacing: .06em;
    }}

    /* ---------- Tables — clean Groww look ---------- */
    div[data-testid="stDataFrame"] {{
      background: {CARD}; border: 1px solid {BORDER};
      border-radius: 8px; overflow: hidden;
      font-variant-numeric: tabular-nums;
    }}
    div[data-testid="stDataFrame"] table {{ font-size: 0.84rem; }}
    div[data-testid="stDataFrame"] th {{
      background: {BG_RAISED} !important;
      color: {MUTED} !important; font-weight: 700;
      text-transform: uppercase; font-size: 0.68rem; letter-spacing: .1em;
      padding: 10px 12px !important;
      border-bottom: 1px solid {BORDER} !important;
    }}
    div[data-testid="stDataFrame"] td {{
      padding: 9px 12px !important;
      border-bottom: 1px solid rgba(255,255,255,0.04) !important;
      color: {TEXT};
    }}

    /* Touch-friendly select + buttons + charts */
    div[data-baseweb="select"] > div {{ min-height: 34px; border-radius: 6px; }}
    div[data-baseweb="select"] [role="option"] {{ min-height: 32px; }}
    button[kind="primary"], button[kind="secondary"] {{ min-height: 38px; padding: 6px 14px; }}
    .stPlotlyChart, .stLineChart, .stAreaChart, .stBarChart {{
      margin: 14px 0; width: 100% !important;
    }}

    /* Streamlit's built-in metric (Dashboard P&L row) */
    [data-testid="stMetric"] {{
      background: {CARD}; border: 1px solid {BORDER};
      border-radius: 8px; padding: 12px 14px;
    }}
    [data-testid="stMetricLabel"] {{
      color: {MUTED}; font-size: 0.68rem;
      text-transform: uppercase; letter-spacing: 0.1em; font-weight: 700;
    }}
    [data-testid="stMetricValue"] {{
      color: {TEXT}; font-size: 1.3rem; font-weight: 700;
      font-variant-numeric: tabular-nums; padding-top: 2px; letter-spacing: -.01em;
    }}
    [data-testid="stMetricDelta"] {{
      font-size: 0.78rem; font-variant-numeric: tabular-nums;
    }}

    /* ---------- History page ---------- */
    .history-section {{ padding: 0 4px; }}
    .history-divider {{ border: 0; border-top: 1px solid {BORDER}; margin: 16px 0; }}
    .history-mobile-header {{
      font-size: 0.8rem; font-weight: 700; color: {TEXT};
      text-transform: uppercase; letter-spacing: 0.12em;
      margin: 14px 0 8px 0; padding-bottom: 6px;
      border-bottom: 1px solid {PRIMARY};
    }}

    /* ---------- Leads page ---------- */
    .lead-toolbar {{
      display: flex; flex-direction: column; gap: 6px; margin: 4px 0 12px 0;
    }}

    /* Top-level summary strip: 4 cells (Queued / Placed / Skipped / Expired).
       Always visible, so the user has the overall state without
       scrolling. Cells collapse to a 2x2 grid on narrow screens. */
    .lead-summary {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
      margin: 8px 0 14px 0;
    }}
    .lead-summary-cell {{
      background: {CARD}; border: 1px solid {BORDER};
      border-radius: 8px; padding: 12px 10px;
      text-align: center;
      transition: border-color .12s ease;
    }}
    .lead-summary-cell:hover {{ border-color: {BORDER_STRONG}; }}
    .lead-summary-num {{
      font-size: 1.5rem; font-weight: 800;
      color: {TEXT}; line-height: 1.1;
      font-variant-numeric: tabular-nums; letter-spacing: -.01em;
    }}
    .lead-summary-lbl {{
      font-size: 0.66rem; text-transform: uppercase;
      letter-spacing: .1em; color: {MUTED};
      font-weight: 700; margin-top: 4px;
    }}
    @media (max-width: 540px) {{
      .lead-summary {{ grid-template-columns: repeat(2, 1fr); }}
    }}

    .lead-section-header {{
      display: flex; align-items: center; justify-content: space-between;
      gap: 10px; margin: 16px 0 8px 0;
      padding-bottom: 6px; border-bottom: 1px solid {BORDER};
    }}
    .lead-section-title {{
      font-size: 0.94rem; font-weight: 700; color: {TEXT}; letter-spacing: -.01em;
    }}
    .lead-section-count {{
      background: {CARD}; color: {TEXT};
      border: 1px solid {BORDER}; border-radius: 4px;
      padding: 2px 8px; font-size: 0.7rem; font-weight: 600;
      font-variant-numeric: tabular-nums;
    }}
    .lead-section-count.warn {{
      background: rgba(245,166,35,.10); color: {WARN};
      border-color: rgba(245,166,35,.4);
    }}
    .lead-card {{
      background: {CARD}; border: 1px solid {BORDER};
      border-radius: 8px; padding: 14px 16px; margin: 8px 0;
      transition: border-color .12s ease, background .12s ease;
    }}
    .lead-card:hover {{ border-color: {BORDER_STRONG}; background: {CARD_HOVER}; }}
    .lead-card.queued {{ border-left: 3px solid {PRIMARY}; }}
    .lead-card.skipped {{ border-left: 3px solid {MUTED}; }}

    /* ---------- Leads table (the main Leads view) ----------
       Four columns: Symbol | Status | Reason (full, no truncation) |
       Details button. Reason column wraps freely; on phones it
       collapses to a stacked layout below the symbol. */
    .lead-table-head {{
      display: grid;
      grid-template-columns: 1.4fr 1.0fr 6.0fr 1.2fr;
      gap: 12px; align-items: center;
      padding: 8px 12px;
      background: {BG_RAISED};
      border: 1px solid {BORDER}; border-radius: 6px;
      margin: 12px 0 6px 0;
    }}
    .lead-table-h {{
      color: {MUTED}; font-size: 0.68rem;
      text-transform: uppercase; letter-spacing: .1em;
      font-weight: 700;
    }}
    .lead-table-row {{ /* reserved for future per-row styling */ }}
    .lead-table-cell {{
      color: {TEXT}; font-size: 0.88rem; line-height: 1.5;
      min-width: 0;
    }}
    .lead-table-sym b {{
      font-size: 0.96rem; font-weight: 700;
      letter-spacing: -.005em;
    }}
    .lead-table-reason {{
      color: {TEXT}; font-size: 0.88rem; line-height: 1.55;
      overflow-wrap: anywhere; white-space: pre-wrap;
    }}
    .lead-table-row-sep {{
      border: 0; border-top: 1px solid rgba(255,255,255,0.05);
      margin: 6px 0;
    }}
    /* On phones, drop the grid layout and let columns stack so the
       reason isn't squeezed into a thin right-aligned column. */
    @media (max-width: 760px) {{
      .lead-table-head {{ display: none; }}
    }}
    .lead-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; min-width: 0; }}
    .lead-head-left {{ flex: 1; min-width: 0; }}
    .lead-head-right {{
      display: flex; flex-direction: column; align-items: flex-end;
      gap: 4px; flex-shrink: 0; max-width: 45%;
    }}
    .lead-symbol {{ font-size: 1.0rem; font-weight: 700; color: {TEXT}; word-break: break-word; letter-spacing: -.005em; }}
    .lead-time {{ color: {MUTED}; font-size: 0.76rem; white-space: nowrap; flex-shrink: 0; font-variant-numeric: tabular-nums; }}
    .lead-chips {{ margin-top: 2px; display: flex; flex-wrap: wrap; gap: 4px; justify-content: flex-end; }}
    .lead-instrument {{
      margin-top: 6px; background: {BG_RAISED};
      border: 1px solid {BORDER}; border-radius: 6px;
      padding: 6px 10px; font-size: 0.8rem; color: {TEXT};
      word-break: break-word; overflow-wrap: anywhere;
    }}
    .lead-instrument .lbl {{ color: {MUTED}; font-weight: 600; }}

    /* Inline reason strip — the most important piece of info on the
       card. Thin accent left-border with a label and the explanation
       in plain English so users don't need to open a modal to see
       WHY this lead was generated/skipped. */
    .lead-reason {{
      margin-top: 12px; padding: 10px 12px;
      background: {BG_RAISED}; border: 1px solid {BORDER};
      border-left: 3px solid {PRIMARY};
      border-radius: 6px;
      display: flex; align-items: flex-start; gap: 8px;
      flex-wrap: wrap; min-width: 0;
    }}
    .lead-reason-label {{
      font-size: 0.72rem; text-transform: uppercase;
      letter-spacing: .08em; font-weight: 700;
      flex-shrink: 0; padding-top: 1px;
    }}
    .lead-reason-body {{
      color: {TEXT}; font-size: 0.88rem; line-height: 1.5;
      flex: 1; min-width: 0;
      overflow-wrap: anywhere;
    }}

    /* Compact meta hint under the reason ("3 tool calls · 78% conf").
       Tells the user there's a richer story without showing it. */
    .lead-meta-hint {{
      margin-top: 8px; font-size: 0.74rem;
      color: {MUTED}; line-height: 1.4;
      overflow-wrap: anywhere;
    }}

    /* ---- Legacy classes kept only so any leftover selectors
           don't 404 — the card no longer renders score bars, plan
           details or score chips inline. */
    .lead-plan {{ display: none; }}
    .lead-score-row {{ display: none; }}
    .lead-score-bar {{ display: none; }}
    .lead-score-meta {{ display: none; }}
    .lead-sb-row {{ display: none; }}
    .lead-sb-chip {{ display: none; }}
    .lead-tc-strip {{ display: none; }}
    .lead-note {{ display: none; }}

    /* Lead-gen live progress panel */
    .lg-panel {{
      background: {CARD};
      border: 1px solid {BORDER}; border-radius: 8px;
      padding: 14px 16px; margin: 6px 0 14px 0;
    }}
    .lg-head {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }}
    .lg-phase {{ display: flex; align-items: center; gap: 8px; color: {TEXT}; font-size: 0.88rem; }}
    .lg-elapsed {{ font-variant-numeric: tabular-nums; font-size: 0.84rem; color: {MUTED}; }}
    .lg-spinner {{
      display: inline-block; width: 14px; height: 14px; border-radius: 50%;
      border: 2px solid {BORDER}; border-top-color: {PRIMARY};
      animation: lg-spin 0.9s linear infinite;
    }}
    @keyframes lg-spin {{ to {{ transform: rotate(360deg); }} }}
    .lg-bar {{ height: 4px; background: {BG_RAISED}; overflow: hidden; margin-bottom: 10px; border-radius: 4px; }}
    .lg-fill {{ height: 4px; transition: width 0.4s ease; border-radius: 4px; }}
    .lg-meta {{ display: flex; gap: 14px; flex-wrap: wrap; font-size: 0.78rem; margin-bottom: 10px; color: {MUTED}; font-variant-numeric: tabular-nums; }}
    .lg-current {{ font-size: 0.84rem; margin: 6px 0 10px 0; color: {TEXT}; font-variant-numeric: tabular-nums; }}
    .lg-recent {{ display: flex; flex-direction: column; gap: 2px; border-top: 1px solid {BORDER}; padding-top: 8px; }}
    .lg-recent-row {{ display: flex; align-items: center; gap: 8px; font-size: 0.82rem; padding: 2px 0; font-variant-numeric: tabular-nums; }}

    /* LLM activity feed (live tool-call stream during agent loop) */
    .lg-tool-wrap {{
      margin: 10px 0 6px 0; padding-top: 8px;
      border-top: 1px dashed {BORDER};
    }}
    .lg-tool-head {{
      display: flex; align-items: center; justify-content: space-between;
      font-size: 0.68rem; text-transform: uppercase; letter-spacing: .12em;
      margin-bottom: 6px;
    }}
    .lg-tool-list {{ display: flex; flex-direction: column; gap: 4px; }}
    .lg-tool-row {{
      display: flex; align-items: center; gap: 8px;
      padding: 6px 10px; border: 1px solid; border-radius: 6px;
      font-size: 0.78rem; line-height: 1.3;
      font-variant-numeric: tabular-nums;
    }}
    .lg-tool-pill {{
      background: {BG_RAISED}; color: {TEXT};
      border: 1px solid {BORDER}; border-radius: 4px;
      padding: 1px 7px; font-size: 0.72rem; font-weight: 600;
      white-space: nowrap; flex-shrink: 0;
    }}
    .lg-tool-args {{
      flex: 1; min-width: 0;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      color: {MUTED}; font-family: 'JetBrains Mono', 'SF Mono', monospace;
      font-size: 0.74rem;
    }}
    .lg-tool-sym {{
      background: {BG_RAISED}; color: {TEXT};
      border: 1px solid {BORDER}; border-radius: 4px;
      padding: 1px 7px; font-size: 0.72rem; font-weight: 600;
      flex-shrink: 0;
    }}
    .lg-tool-keys {{
      display: inline-flex; gap: 2px; flex-shrink: 0;
    }}
    .lg-tool-key {{
      background: rgba(0,184,135,.10); color: {PROFIT};
      border: 1px solid rgba(0,184,135,.30); border-radius: 4px;
      padding: 1px 6px; font-size: 0.68rem; font-weight: 600;
      font-family: 'JetBrains Mono', 'SF Mono', monospace;
    }}
    .lg-tool-key.muted {{
      background: transparent; color: {MUTED}; border-color: {BORDER};
    }}
    .lg-tool-ts {{
      font-family: 'JetBrains Mono', 'SF Mono', monospace;
      font-size: 0.68rem; flex-shrink: 0;
      font-variant-numeric: tabular-nums;
    }}

    /* Args display: collapsed preview that expands to full JSON on click.
       Tools like `breakout_calc` carry large payloads (ATR series of 200
       values) that don't fit in a 60-char truncate — let the user
       click to see the full arg block. */
    .lg-tool-meta {{
      display: inline-flex; gap: 6px; align-items: center;
      margin-left: auto; flex-shrink: 0;
    }}
    .lg-tool-args-wrap {{
      flex: 1; min-width: 0;
      margin: 0;
    }}
    .lg-tool-args-wrap > summary {{
      list-style: none;
      cursor: pointer;
      color: {MUTED}; font-family: 'JetBrains Mono', 'SF Mono', monospace;
      font-size: 0.74rem;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      padding: 1px 0;
      user-select: none;
    }}
    .lg-tool-args-wrap > summary::-webkit-details-marker {{ display: none; }}
    .lg-tool-args-wrap > summary:hover {{ color: {TEXT}; }}
    .lg-tool-args-wrap[open] > summary {{ color: {PRIMARY}; }}
    .lg-tool-args-full {{
      background: {BG_RAISED};
      border: 1px solid {BORDER}; border-radius: 6px;
      padding: 8px 10px; margin: 4px 0 0 14px;
      font-family: 'JetBrains Mono', 'SF Mono', monospace;
      font-size: 0.72rem; color: {TEXT};
      max-height: 240px; overflow: auto;
      white-space: pre-wrap; word-break: break-all;
    }}
    .lg-tool-args-empty {{
      color: {MUTED}; font-size: 0.74rem;
    }}

    /* Per-symbol timeline (groups tool calls by underlying) */
    .lg-tl-wrap {{
      margin: 10px 0 4px 0; padding-top: 8px;
      border-top: 1px dashed {BORDER};
    }}
    .lg-tl-head {{
      display: flex; align-items: center; justify-content: space-between;
      font-size: 0.68rem; text-transform: uppercase; letter-spacing: .12em;
      margin-bottom: 6px;
    }}
    .lg-tl-list {{ display: flex; flex-direction: column; gap: 8px; }}
    .lg-tl-sym {{
      display: flex; align-items: center; gap: 8px;
      padding: 6px 10px;
      background: {BG_RAISED}; border: 1px solid {BORDER};
      border-radius: 6px;
    }}
    .lg-tl-name {{
      color: {TEXT}; font-weight: 700; font-size: 0.8rem;
      flex-shrink: 0;
    }}
    .lg-tl-count {{
      color: {MUTED}; font-size: 0.7rem; margin-left: auto;
      font-variant-numeric: tabular-nums;
    }}
    .lg-tl-steps {{
      display: flex; flex-wrap: wrap; gap: 6px 10px;
      padding: 6px 10px 0 16px;
    }}
    .lg-tl-step {{
      font-size: 0.76rem;
    }}

    /* Direction pill in the recent-results list (CALL / PUT) */
    .lg-recent-dir {{
      background: rgba(0,184,135,.05);
      border: 1px solid; border-radius: 4px;
      padding: 0 7px; font-size: 0.68rem; font-weight: 700;
      letter-spacing: .04em;
    }}

    /* Gauges + small bits */
    .gauge {{ position: relative; width: 64px; height: 32px; overflow: hidden; }}
    .gauge-bg {{ position: absolute; bottom: 0; left: 0; right: 0; height: 32px; border-radius: 32px 32px 0 0; background: {BORDER}; }}
    .gauge-fill {{ position: absolute; bottom: 0; left: 0; right: 0; border-radius: 32px 32px 0 0; }}
    .gauge-num {{ position: relative; text-align: center; font-weight: 700; font-size: 0.85rem; padding-top: 4px; color: {TEXT}; }}

    /* ---------- Custom scrollbar ---------- */
    ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    ::-webkit-scrollbar-thumb {{
      background: #2a2a2a; border-radius: 4px;
    }}
    ::-webkit-scrollbar-thumb:hover {{ background: {PRIMARY}; }}

    /* ---------- Responsive ---------- */
    @media (max-width: 768px) {{
      .stApp {{ max-width: 100%; padding: 0 4px; }}
      .stat-tile {{ padding: 10px 12px; }}
      .stat-value {{ font-size: 1.05rem; }}

      /* Lead cards: tighter padding, smaller symbols, full-width
         chips so they wrap cleanly on narrow screens. */
      .lead-card {{ padding: 12px 14px; }}
      .lead-symbol {{ font-size: 0.94rem; }}
      .lead-chips {{ gap: 4px; }}
      .lead-chips .badge {{ font-size: 0.7rem; padding: 2px 7px; }}
      .lead-time {{ font-size: 0.72rem; }}

      /* Reason strip: keep readable but allow wrap */
      .lead-reason {{ padding: 8px 10px; }}
      .lead-reason-body {{ font-size: 0.84rem; }}

      /* Inline generate button — already use_container_width, but
         ensure min 44px touch target */
      .lead-toolbar .stButton > button[kind="primary"] {{
        min-height: 46px; font-size: 0.94rem;
      }}

      /* "Why this lead?" detail button on mobile — same touch-target
         rule. */
      .stButton > button[kind="primary"] {{
        min-height: 44px;
      }}

      /* Summary strip on mobile already collapses to 2-col above */
      .lead-summary-num {{ font-size: 1.25rem; }}

      .row {{ flex-wrap: wrap; gap: 10px; justify-content: flex-start; }}

      /* "Look up by ID" expander: stack the input + button vertically */
      [data-testid="stExpander"] {{
        border-radius: 8px;
      }}
    }}

    /* Extra-tight mobile breakpoint for small phones (≤480px). The
       lead card stacks its header so the instrument name + reason
       get full width and never push below the fold. */
    @media (max-width: 480px) {{
      /* Summary becomes a single tight row */
      .lead-summary {{ grid-template-columns: repeat(2, 1fr); gap: 8px; }}
      .lead-summary-cell {{ padding: 8px 10px; }}
      .lead-summary-num {{ font-size: 1.15rem; }}
      .lead-summary-lbl {{ font-size: 0.62rem; }}

      /* Lead card header: symbol + instrument stacked, then a single
         row of status chips + time below. Prevents long F&O contract
         strings from forcing horizontal scroll on narrow phones. */
      .lead-head {{ flex-direction: column; align-items: flex-start; gap: 8px; }}
      .lead-head-right {{
        flex-direction: row; align-items: center;
        justify-content: space-between;
        width: 100%; max-width: 100%;
      }}
      .lead-time {{ align-self: auto; }}
    }}

    /* Lead-detail dialog: full-width on phones, larger viewport on
       desktop. Streamlit renders the dialog inside a portal; the
       modal panel is the section > div with the title row + content. */
    div[role="dialog"] section,
    [data-testid="stDialog"] section {{
      max-width: 720px;
    }}
    @media (max-width: 768px) {{
      div[role="dialog"], [data-testid="stDialog"] {{
        max-width: 100vw !important;
        width: 100vw !important;
        margin: 0 !important;
        border-radius: 0 !important;
      }}
      div[role="dialog"] section,
      [data-testid="stDialog"] section {{
        max-width: 100vw;
        width: 100vw;
        padding: 14px 16px !important;
      }}
    }}
    /* Tighter content padding inside the dialog on mobile so the
       scrollable content area isn't dominated by gaps */
    @media (max-width: 768px) {{
      [data-testid="stDialog"] .stMarkdown {{ margin-bottom: 6px; }}
      [data-testid="stDialog"] [data-testid="stExpander"] summary {{
        font-size: 0.86rem;
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


def _llm_status_label(llm: dict) -> str:
    if not isinstance(llm, dict) or not llm:
        return "UNKNOWN"
    if not llm.get("configured"):
        return "NOT CONFIGURED"
    s = (llm.get("status") or "unknown").upper()
    return s


def _llm_status_color(llm: dict) -> str:
    if not isinstance(llm, dict) or not llm:
        return MUTED
    if not llm.get("configured"):
        return MUTED
    s = llm.get("status")
    if s == "ok":
        return PROFIT
    if s == "error":
        return LOSS
    return MUTED


# --- auth ----------------------------------------------------------------

def _top_nav_link(label: str, url: str, *, css_class: str,
                  help: str | None = None) -> None:
    """Render a real ``<a target="_top">`` link styled as a button.

    Why a real anchor and not a JS-driven button:

    Streamlit serves the app from inside a sandboxed iframe. Programmatic
    ``window.top.location.href = url`` from a script injected after a button
    click is silently dropped by modern browsers (the iframe sandbox only
    allows ``allow-top-navigation`` on a *real* user gesture, and a script
    injected during a re-render is not considered one). A genuine ``<a>``
    anchor with ``target="_top"`` and a native ``href`` works because the
    browser itself handles the navigation when the user clicks the link —
    no JavaScript required, no CSP gymnastics, no race between the script
    injection and the next Streamlit re-render. The link is styled with a
    pre-existing class (``.sso-login-btn`` / ``.sidebar-logout-btn``) so it
    looks and behaves like the Streamlit button it replaces.
    """
    title_attr = f' title="{_html_escape(help)}"' if help else ""
    st.markdown(
        f"<a class='{css_class}' href='{_html_escape(url)}' "
        f"target='_top' rel='noopener noreferrer'{title_attr}>"
        f"{_html_escape(label)}</a>",
        unsafe_allow_html=True,
    )


def render_login():
    """Modern fintech landing page shown when no SSO session is present.

    Streamlit's stApp container is reused; the login surface is a centred
    card with Groww-style rounded corners and a teal CTA. The CTA is a real
    ``<a target="_top">`` anchor styled to look like a Streamlit button — see
    `_top_nav_link` for why a real anchor (and not a JS-driven button) is the
    only reliable way to break out of Streamlit's sandboxed iframe.

    The card chrome and the Streamlit-rendered button are siblings in the
    page flow; both are constrained to the same width via Streamlit
    columns so they read as a single unified surface.
    """

    auth_error = st.query_params.get("auth_error")
    error_html = ""
    if auth_error:
        error_html = (
            f"<div class='login-error'>"
            f"<span class='login-error-dot'></span>"
            f"<span>SSO failed: {auth_error}</span>"
            f"</div>"
        )
        st.query_params.clear()

    # Constrain everything to a centred column matching the card's width.
    _spacer_l, mid, _spacer_r = st.columns([1, 4, 1])
    with mid:
        # Card chrome (brand, tagline, error, footer). The CTA slot is
        # empty — the Streamlit button below fills that visual space.
        st.html(_login_css() + _login_card_chrome_html(error_html))
        _top_nav_link(
            "Continue with Upstox SSO",
            api.login_url(),
            css_class="sso-login-btn",
            help="Redirects to Upstox to authorise this dashboard. You'll be returned here after sign-in.",
        )
        # Tiny spacer that mimics the original card's bottom padding.
        _html("<div style='height:8px'></div>")


def _login_css() -> str:
    """Page-specific CSS for the login surface.

    Kept separate from the main `_css()` so the dashboard styling doesn't
    bleed in. The login card uses the same design tokens (PRIMARY, SECONDARY,
    CARD, BORDER) so the two surfaces feel like the same product — pure-black
    canvas, rounded Groww-style card, single teal CTA.
    """
    return f"""
    <style>
    .stApp {{
      background: {BG};
      min-height: 100vh;
    }}
    [data-testid="stHeader"], footer, #MainMenu {{ display: none !important; }}

    /* Subtle radial teal glow behind the card, like a fintech login screen */
    .stApp::before {{
      content: '';
      position: fixed;
      top: -10%; left: 50%;
      width: 80vw; height: 80vh;
      transform: translateX(-50%);
      background: radial-gradient(circle, rgba(4,180,136,0.06) 0%, rgba(4,180,136,0) 60%);
      pointer-events: none;
      z-index: 0;
    }}

    .login-shell {{
      display: flex; align-items: center; justify-content: center;
      padding: 28px 16px 16px 16px;
      position: relative; z-index: 1;
    }}
    .login-card {{
      width: 100%; max-width: 440px;
      background: {CARD};
      border: 1px solid {BORDER};
      border-radius: 12px;
      padding: 32px 30px;
      box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    }}
    /* Slot that visually holds the Streamlit-rendered CTA below.
       Hides its own border (Streamlit widgets carry their own) and
       gives the CTA a matching surface feel. */
    .login-cta-slot {{
      margin: 8px 0 4px 0;
      min-height: 1px;
    }}
    /* Push the Streamlit CTA into the card's vertical slot. The login
       page is restricted to the card width by the column layout — we
       just need to ensure primary CTA styling is consistent. */
    .stButton > button[kind="primary"] {{
      width: 100%;
      min-height: 48px;
      font-size: 1.0rem;
      border-radius: 8px;
    }}
    .login-brand {{
      display: flex; align-items: center; gap: 12px; margin-bottom: 18px;
    }}
    .login-mark {{
      width: 32px; height: 32px; border-radius: 8px;
      background: linear-gradient(135deg, {PRIMARY}, {PRIMARY_HOVER});
      box-shadow: 0 2px 12px rgba(4,180,136,.35);
      display: inline-flex; align-items: center; justify-content: center;
    }}
    .login-mark::after {{
      content: 'T';
      color: #000; font-weight: 800; font-size: 16px;
      font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
      line-height: 1;
    }}
    .login-name {{
      font-size: 1.5rem; font-weight: 700; letter-spacing: -.015em;
      color: {TEXT};
    }}
    .login-tagline {{
      color: {MUTED}; font-size: 1.0rem; line-height: 1.55;
      margin-bottom: 24px; font-weight: 400;
    }}
    .login-grad {{
      color: {PRIMARY_HOVER}; font-weight: 600;
    }}
    .login-cta {{
      margin: 8px 0 0 0 !important;
      padding: 0.9rem 1.15rem !important;
      font-size: 1.04rem !important;
      display: inline-flex !important;
      align-items: center; justify-content: center;
      gap: 10px;
    }}
    .login-error {{
      display: flex; align-items: center; gap: 10px;
      padding: 12px 14px; margin: 4px 0 16px 0;
      background: rgba(234,84,85,.10);
      border: 1px solid rgba(234,84,85,.4); border-radius: 10px;
      color: #fda4af; font-size: 0.88rem;
    }}
    .login-error-dot {{
      width: 8px; height: 8px; border-radius: 50%;
      background: {LOSS}; flex-shrink: 0;
    }}
    .login-foot {{
      margin-top: 22px; text-align: center;
      color: {MUTED}; font-size: 0.8rem; line-height: 1.55;
    }}
    @media (max-width: 540px) {{
      .login-card {{ padding: 24px 20px; border-radius: 10px; }}
      .login-name {{ font-size: 1.4rem; }}
      .login-tagline {{ font-size: 0.96rem; }}
    }}
    </style>
    """


def _login_card_chrome_html(error_html: str = "") -> str:
    """HTML body of the login card WITH the login button (no longer an anchor).

    The CTA sits inside the card chrome — it's a real Streamlit `<button>`
    rendered directly below this HTML, but the chrome reserves visual
    padding + a matching surface so the two read as one unified card.

    Concatenated with `_login_css()` so the whole surface lands in a single
    `st.html()` call (avoids the two-`unsafe_allow_html` bug where the
    second block renders as escaped text).
    """
    return f"""
    <div class='login-shell'>
      <div class='login-card'>
        <div class='login-brand'>
          <div class='login-mark'></div>
          <div class='login-name'>TradePilot</div>
        </div>
        <div class='login-tagline'>
          Automated intraday breakout trading,
          <span class='login-grad'>powered by LLMs.</span>
        </div>
        {error_html}
        <div class='login-cta-slot'></div>
        <div class='login-foot'>
          You'll be redirected to Upstox, then back here.
          Your tokens never touch this dashboard's storage.
        </div>
      </div>
    </div>
    """


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
                "Drifts",
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
        # Logout: a real ``<a target="_top">`` anchor styled to look like a
        # secondary button. The browser handles the top-frame navigation
        # natively on click, so no JS injection is needed — the previous
        # `st.button` + JS approach was unreliable because scripts injected
        # via `st.markdown(unsafe_allow_html=True)` are not always executed
        # by the browser when the iframe is sandboxed.
        _top_nav_link(
            "Logout",
            api.logout_url(),
            css_class="sidebar-logout-btn",
            help="End the Upstox session and clear tokens.",
        )

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
                    background:{CARD};border-left:2px solid {LOSS};
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
        f"<div class='ks-banner' style='background:rgba(234,84,85,0.10);"
        f"border-color:rgba(234,84,85,0.4);color:#fda4af;'>"
        f"Upstox token expired — the bot cannot trade until you re-login.</div>"
    )
    # Real ``<a target="_top">`` anchor styled to look like a primary button
    # — same approach as the SSO login button; the browser handles the
    # top-frame navigation natively on click.
    _top_nav_link(
        "Login with Upstox",
        api.login_url(),
        css_class="sso-login-btn",
        help="Redirect to Upstox to refresh your session.",
    )


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

    # Note: the Leads section carries the only "scanned stocks" / queued signals
    # preview — the Dashboard stays focused on P&L.


def _track_pnl_history(total: float):
    """Keep a small ring buffer of P&L values for the dashboard sparkline."""
    hist = st.session_state.setdefault("pnl_history", [])
    if not hist or hist[-1] != total:
        hist.append(total)
    if len(hist) > 60:
        del hist[:-60]


def _render_day_stats():
    """Compact at-a-glance stats for today. Trimmed to 5 tiles: drop
    redundant "Closed" + "Wins/Losses" (both are encoded in the others) and
    "Avg P&L" (rarely more useful than biggest win/loss)."""
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    closed = api.get_closed_trades(date=today)
    open_trades = api.get_open_trades(date=today)

    closed_rows = closed.get("data", {}).get("trades", []) if closed.get("status") == "ok" else []
    open_rows = open_trades.get("data", {}).get("trades", []) if open_trades.get("status") == "ok" else []

    pnls = [(r.get("realized_pnl") or 0) for r in closed_rows]
    wins = sum(1 for v in pnls if v > 0)
    win_rate = (wins / len(pnls) * 100) if pnls else None
    biggest_win = max(pnls) if pnls else None
    biggest_loss = min(pnls) if pnls else None

    tiles = [
        _stat_tile("Trades today", str(len(pnls) + len(open_rows))),
        _stat_tile("Win rate",
                   f"{win_rate:.0f}%" if win_rate is not None else "—",
                   color=PROFIT if (win_rate or 0) >= 50 else MUTED),
        _stat_tile("Closed", f"{len(pnls)}"),
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


def _latest_outcomes() -> list[dict]:
    """Return the most-recent lead-gen run's scan-outcome list.

    Source priority:
      1. **Live ring buffer** — `st.session_state["lead_job"]` carries the
         in-flight job handle; on each fragment poll we copy the latest
         `progress["scan_outcomes"]` into a session_state cache so the
         outer render (which doesn't poll) can still render the table.
      2. **DB fallback** — `GET /leads/scan-outcomes?job_id=<last>` for the
         last completed job, so the table survives a page reload after a
         run finished.
      3. **Most-recent scan outcomes** — if no specific job is attached,
         fall back to the very latest run's outcomes (operator-friendly:
         "show me the most recent thing that happened").
    """
    job = st.session_state.get("lead_job") or {}
    job_id = job.get("id") or st.session_state.get("last_lead_job_id")
    cached = st.session_state.get("_latest_outcomes_cache") or []

    # Live ring buffer wins — it's the freshest.
    if cached:
        return list(cached)

    if job_id:
        try:
            resp = api.get_scan_outcomes(job_id=job_id, limit=200)
            if resp.get("status") == "ok":
                outcomes = list(resp.get("data") or [])
                if outcomes:
                    return outcomes
        except Exception:
            pass

    # Final fallback: latest scan outcomes by scanned_at, no job filter.
    try:
        resp = api.get_scan_outcomes(limit=200)
        if resp.get("status") == "ok":
            return list(resp.get("data") or [])
    except Exception:
        pass
    return []


def _count_decisions(outcomes: list[dict]) -> dict[str, int]:
    """Tally how many scan outcomes landed in each decision bucket."""
    counts = {"generated": 0, "no_signal": 0, "error": 0}
    for o in outcomes:
        d = (o.get("decision") or "").lower()
        if d in counts:
            counts[d] += 1
    return counts


def _count_total_leads() -> int:
    """Best-effort count of `Lead` rows so the delete-all button can be
    disabled when there's nothing to delete. Used only for the button's
    enabled/disabled state and the typed-confirmation dialog copy — we
    surface the real count from the API right before the delete, so a
    stale count here just disables the button a few seconds early.
    """
    try:
        resp = api.get_leads()
        if resp.get("status") == "ok":
            return int((resp.get("data") or {}).get("count") or 0)
    except Exception:
        pass
    return 0


def _render_outcome_table(outcomes: list[dict]) -> None:
    """Render the main Leads table.

    One row per underlying the lead generator analysed. Layout:

        [ Symbol ]  [ Status pill ]  [ Full reason (wraps freely) ]  [ Details ]

    Every column is sized for readability: the symbol column is fixed
    width, the status pill is compact, and the reason column gets the
    rest of the row — the full LLM rejection_reason or generated-lead
    rationale, NO truncation. The "Details" button opens the
    scan-outcome modal which carries every tool call, the full LLM
    rationale, indicators and error context.
    """
    if not outcomes:
        return

    _html(
        "<div class='lead-table-head'>"
        "<div class='lead-table-h lead-table-h-sym'>Symbol</div>"
        "<div class='lead-table-h lead-table-h-status'>Status</div>"
        "<div class='lead-table-h lead-table-h-reason'>Reason</div>"
        "</div>"
    )

    for o in outcomes:
        _render_outcome_row(o)


def _render_outcome_row(o: dict) -> None:
    """Single row of the Leads table — symbol + status + short reason.

    The row preview shows ONLY the LLM-supplied single-sentence
    ``short_reason`` (≤200 chars) so the operator can scan the run at a
    glance without opening the detail modal. The WHOLE row is clickable
    once the row is persisted (DB id present) — clicking opens the
    scan-outcome modal which renders the FULL LLM rationale, every
    tool call (args + result), indicators and error context with NO
    truncation.

    Streaming rows (no DB id yet — the scan is still being processed
    in the background worker thread) render a spinner-style placeholder
    instead of a muted truncated caption.
    """
    symbol = o.get("symbol") or "?"
    decision = (o.get("decision") or "no_signal").lower()
    status_label, status_color = _outcome_status_label(decision)

    # Row preview = short_reason only (≤200 chars, LLM-supplied). For
    # an error row that didn't get a short_reason, fall back to the
    # error string so the row never looks empty.
    short_reason = o.get("short_reason")
    if not short_reason:
        # No short_reason — derive one so the row stays readable.
        if decision == "error":
            short_reason = o.get("error") or "Error — see details"
        else:
            short_reason = "(no detail recorded)"
    short_reason_str = str(short_reason).strip()[:200] or "(no detail recorded)"

    # Streaming rows (no DB id yet) can't open the modal — the row
    # endpoint doesn't exist server-side. We render an inline spinner
    # so it's clear the scan is still in flight.
    is_streaming = o.get("id") is None

    # Row id for the Streamlit button key — for streaming rows the
    # server hasn't yet returned a stable id, so we synthesise one.
    row_key = (
        f"scan_view_{o.get('id')}"
        if not is_streaming
        else f"scan_view_stream::{o.get('symbol')}::{o.get('scanned_at')}"
    )

    cols = st.columns([1.4, 1.0, 7.2], gap="small")
    with cols[0]:
        _html(
            f"<div class='lead-table-cell lead-table-sym'>"
            f"<b>{_html_escape(symbol)}</b></div>"
        )
    with cols[1]:
        _html(
            f"<div class='lead-table-cell lead-table-status'>"
            f"<span style='color:{status_color};font-weight:600;'>"
            f"{_html_escape(status_label)}</span></div>"
        )
    with cols[2]:
        if is_streaming:
            # Inline spinner + status label — no truncated caption.
            _html(
                "<div class='lead-table-cell lead-table-reason' "
                "style='color:#94a3b8;font-style:italic;display:flex;"
                "align-items:center;gap:8px;'>"
                "<span class='lg-spinner' style='width:12px;height:12px;'>"
                "</span>Analyzing…</div>"
            )
        else:
            # The whole cell is the click target — opens the detail
            # modal with full LLM thinking, tool calls, indicators and
            # any error context, no character truncation.
            if st.button(
                short_reason_str,
                key=row_key,
                type="tertiary",
                use_container_width=True,
                help="Click to open the full LLM thinking, tool calls, "
                     "indicators and error context for this scan.",
            ):
                st.session_state["open_scan_outcome"] = int(o["id"])
                st.rerun()

    _html("<hr class='lead-table-row-sep'/>")


_OUTCOME_DECISION_LABEL = {
    "generated": ("Lead generated", "profit"),
    "no_signal": ("No signal",      "muted"),
    "error":     ("Error",          "loss"),
}


def _outcome_status_label(decision: str) -> tuple[str, str]:
    """Return ``(label, css_class_or_color)`` for a scan-outcome decision."""
    if decision == "generated":
        return "Lead generated", PROFIT
    if decision == "error":
        return "Error", LOSS
    return "No signal", MUTED


def render_leads():
    """The Leads section — a single, simple table.

    Layout:

      * **Toolbar**: Generate-now + manual lead-detail lookup.
      * **Live status fragment**: while a run is in flight, shows the
        progress bar + the same scanned-stocks table streaming in.
      * **Latest-run table** (the main view): one row per underlying
        the lead generator analysed — just the instrument and the
        full LLM-supplied reason for generating or not generating a
        lead. Nothing else.
      * **Click a row to open the full detail modal** with the complete LLM
        rationale, every tool call (with args + result), the indicator
        snapshot and any error — no character truncation anywhere.
      * **Delete-all leads** at the bottom (typed-confirmation gated).

    The previous version stacked multiple summary cards, per-lead
    detail cards, a queued-signals table and a separate "scanned
    stocks" expander — which made the page busy and forced
    truncation of the reasons just to keep the cards readable. The
    new layout uses one table for everything: queued, placed,
    skipped and expired leads all surface here, along with the
    underlyings the LLM decided not to trade.
    """
    st.subheader("Leads")

    # Toolbar — the Generate button is the only thing on the top row.
    # Manual lookup sits in its own expander below so it doesn't
    # dominate the page.
    _html("<div class='lead-toolbar'>")
    _render_generate_lead_button("leads_tab")
    _html("</div>")

    # Manual lead-detail lookup: useful when the leads list endpoint is
    # failing (e.g. mid-deploy) but you still want to drill into a
    # specific lead. Just type the lead ID and click Open.
    with st.expander("Look up a lead by ID", expanded=False):
        _lookup_l, _lookup_r = st.columns([3, 1], vertical_alignment="bottom")
        with _lookup_l:
            lookup_id = st.number_input(
                "Lead ID",
                min_value=1,
                step=1,
                key="lead_lookup_id",
                label_visibility="visible",
            )
        with _lookup_r:
            if st.button(
                "Open",
                type="primary",
                use_container_width=True,
                key="lead_lookup_open",
            ):
                st.session_state["open_lead_dialog"] = int(lookup_id)

    # Live status fragment: while a run is in flight it streams the
    # scanned-stocks table below this line; on terminal status it
    # clears session_state["lead_job"] and triggers a final rerun.
    _lead_gen_status_fragment()

    # Resolve the latest job's outcomes — live ring buffer first,
    # then the DB-backed endpoint as a fallback. We use scan outcomes
    # (not the leads table) because they cover EVERY underlying the
    # generator analysed, including the ones it decided not to trade —
    # which is exactly the view the user asked for ("just the
    # instrument and the reason it was or wasn't generated").
    outcomes = _latest_outcomes()

    # Empty-state copy — surfaced early so the user always has
    # something to read while the Generate button is at the top.
    if not outcomes:
        st.info(
            "No scanned stocks yet. Tap **Generate now**, or enable more "
            "underlyings in **Instruments**."
        )
        return

    # Summary strip — tiny, sits above the table.
    counts = _count_decisions(outcomes)
    _html(
        f"<div class='lead-summary'>"
        f"<div class='lead-summary-cell'>"
        f"<div class='lead-summary-num' style='color:{PROFIT};'>{counts['generated']}</div>"
        f"<div class='lead-summary-lbl'>Lead generated</div>"
        f"</div>"
        f"<div class='lead-summary-cell'>"
        f"<div class='lead-summary-num' style='color:{MUTED};'>{counts['no_signal']}</div>"
        f"<div class='lead-summary-lbl'>No signal</div>"
        f"</div>"
        f"<div class='lead-summary-cell'>"
        f"<div class='lead-summary-num' style='color:{LOSS};'>{counts['error']}</div>"
        f"<div class='lead-summary-lbl'>Error</div>"
        f"</div>"
        f"</div>"
    )

    # The main table — one row per underlying. Just symbol + status +
    # full reason (NO truncation). Each row carries a "Details"
    # button that opens the scan-outcome modal with the full LLM
    # thinking, every tool call, etc.
    st.caption(
        f"{len(outcomes)} underlying{'s' if len(outcomes) != 1 else ''} scanned. "
        "Click a row's **Details** for the full LLM thinking, tool calls, and indicators."
    )
    _render_outcome_table(outcomes)

    # Delete-all — gated by a typed-confirmation dialog so a stray
    # click can't wipe queued signals.
    lead_count = _count_total_leads()
    if st.button(
        "Delete all leads",
        type="secondary",
        use_container_width=True,
        disabled=lead_count == 0,
        help=(
            "Remove every lead row (queued, placed, skipped, expired). "
            "Trade audit data is preserved."
            if lead_count > 0 else
            "No leads to delete."
        ),
    ):
        st.session_state["purge_leads_count"] = lead_count
        st.session_state["purge_dialog_open"] = True
        st.rerun()

    if st.session_state.pop("purge_dialog_open", False):
        _render_purge_leads_dialog(lead_count)

    result = st.session_state.pop("purge_leads_last_result", None)
    if result:
        kind, payload = result
        if kind == "success":
            st.success(f"Deleted {payload} lead row(s).")
        else:
            st.error(payload)

    # Lead-detail dialog is invoked from `main()` at script top level
    # — Streamlit's @st.dialog decorator requires top-level calls.
    # `render_leads` only sets `session_state["open_lead_dialog"]`.


@st.dialog("Delete all leads")
def _render_purge_leads_dialog(lead_count: int):
    """Typed-confirmation gate for the destructive ``DELETE /api/trades/leads``
    call. The user must type ``DELETE`` to enable the confirm button, which
    makes the action impossible to trigger with a stray click."""
    st.warning(
        f"This will permanently remove all **{lead_count}** lead row(s) from the "
        f"database, regardless of status (queued, placed, skipped, expired). "
        f"Trade audit data is preserved."
    )
    typed = st.text_input(
        f'Type DELETE to confirm',
        key="purge_leads_typed",
        placeholder="DELETE",
    )
    c1, c2, _ = st.columns([1, 1, 4])
    with c1:
        confirm_clicked = st.button(
            "Delete forever",
            type="primary",
            use_container_width=True,
            disabled=(typed.strip() != "DELETE"),
        )
    with c2:
        if st.button("Cancel", use_container_width=True):
            st.session_state.pop("purge_leads_count", None)
            st.rerun()

    if confirm_clicked:
        with st.spinner("Deleting leads…"):
            resp = api.purge_leads()
        st.session_state.pop("purge_leads_typed", None)
        st.session_state.pop("purge_leads_count", None)
        if resp.get("status") == "ok":
            st.session_state["purge_leads_last_result"] = (
                "success",
                int(resp.get("data", {}).get("deleted", 0)),
            )
            st.rerun()
        else:
            err_msg = resp.get("error", {}).get("message", "Delete failed.")
            st.session_state["purge_leads_last_result"] = ("error", err_msg)
            st.rerun()


def _open_lead_detail_modal(lead_id: int) -> None:
    """Top-level entry point that opens the lead-detail modal.

    `render_leads` sets `session_state["open_lead_dialog"]` when the user
    clicks the inline button (or the manual lookup expander). `main()`
    sees the flag and invokes THIS function at script top level —
    Streamlit's @st.dialog decorator requires the function call to be at
    top level (not nested at the end of another function) for the modal
    to actually render.

    The function builds a "best-effort" fallback row from the latest
    leads list response (if present in session_state) so the dialog has
    something to render even if the per-id endpoint is down.
    """
    fallback_row: dict = {"id": lead_id}
    cached_rows: list[dict] = st.session_state.get("_last_leads_rows") or []
    for r in cached_rows:
        if int(r.get("id") or 0) == lead_id:
            fallback_row = r
            break
    _render_lead_detail_dialog(lead_id, fallback_row)


@st.dialog("Lead detail", width="large")
def _render_lead_detail_dialog(lead_id: int, fallback_row: dict) -> None:
    """Full explanation of why a lead was generated / skipped / placed.

    Opened by the small button under every lead card. Layout (top-down):
      1. Header — symbol, direction pill, status pill, signal price, confidence
      2. "Reason" — explicit one-line answer (Generated / Skipped / Placed because …)
      3. LLM rationale — the full reasoning text
      4. Tool calls — full arg payload + result keys, expandable per row
      5. Indicators — slim snapshot dict
      6. Score breakdown — per-dimension contribution bars
      7. Trade — entry / SL / exit / P&L (if placed)
      8. Raw meta JSON — collapsed by default (for power users)
    """
    # Lazy-fetch the full detail. Falls back to the list-row meta if the
    # per-id endpoint is unavailable (e.g. 500 on the leads endpoint).
    with st.spinner("Loading lead detail…"):
        resp = api.get_lead_detail(lead_id)
    if resp.get("status") != "ok":
        err = resp.get("error") or {}
        st.warning(
            f"Could not load full detail (`{err.get('code', 'error')}`). "
            f"Showing slim summary from the list payload."
        )
        meta = fallback_row.get("meta") or {}
        _render_lead_detail_body(meta, fallback_row, fallback_meta=True)
    else:
        data = resp.get("data") or {}
        meta = data.get("meta") or {}
        _render_lead_detail_body(meta, data, fallback_meta=False)
    col1, col2 = st.columns([1, 4])
    if col1.button("Close", type="secondary", key=f"close_lead_dialog_{lead_id}"):
        st.rerun()


def _render_lead_detail_body(meta: dict, row: dict, *, fallback_meta: bool) -> None:
    """Render the full dialog body for a single lead."""
    symbol = row.get("symbol") or (row.get("underlying") or "?").split("|")[-1]
    direction = row.get("direction") or ""
    status = row.get("status") or "?"
    signal_type = row.get("signal_type") or ""
    signal_level = row.get("signal_level")
    confidence = row.get("confidence")
    note = row.get("note")
    components = row.get("components") or {}
    score_breakdown = row.get("score_breakdown") or []
    trade = row.get("trade") if not fallback_meta else None

    # Direction pill colour
    dir_color = PROFIT if direction == "CALL" else (LOSS if direction == "PUT" else MUTED)

    _html(
        f"""
        <div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px;'>
          <span style='font-size:1.25rem;font-weight:700;color:{TEXT};'>{_html_escape(symbol)}</span>
          <span class='badge' style='color:{dir_color};background:{dir_color}22;border:1px solid {dir_color}66;'>
            {_html_escape(direction or '—')}
          </span>
          <span class='badge' style='color:{MUTED};background:rgba(255,255,255,.05);border:1px solid {BORDER};'>
            {_html_escape(status)}
          </span>
          <span class='badge' style='color:{SECONDARY};background:rgba(34,211,238,.10);border:1px solid rgba(34,211,238,.35);'>
            {_html_escape(signal_type or '—')}
          </span>
        </div>
        """
    )

    # ---- 2. The reason (one-line answer to "why was this lead generated/not generated?") ----
    pct = int((confidence or 0) * 100)
    reason_title, reason_body = _format_lead_reason(status, note, meta, pct)
    _html(
        f"""
        <div style='background:{CARD};border:1px solid {BORDER};border-left:3px solid {PRIMARY};
                    border-radius:4px;padding:10px 14px;margin-bottom:10px;'>
          <div style='font-size:0.7rem;text-transform:uppercase;letter-spacing:.08em;
                      color:{MUTED};font-weight:700;margin-bottom:4px;'>
            {_html_escape(reason_title)}
          </div>
          <div style='color:{TEXT};font-size:0.92rem;line-height:1.5;'>{reason_body}</div>
        </div>
        """
    )

    if fallback_meta:
        st.caption("(Detail endpoint unavailable — showing slim summary from the list payload.)")

    # ---- 3. LLM rationale ----
    rationale = meta.get("llm_rationale")
    if rationale:
        st.markdown("**LLM rationale**")
        st.info(str(rationale))

    # ---- 4. Tool calls (full agent-loop log) ----
    tool_calls = meta.get("llm_tool_calls") or []
    if tool_calls:
        st.markdown(f"**Tool calls** ({len(tool_calls)})")
        _render_tool_calls_full(tool_calls)

    # ---- 5. Indicators ----
    indicators = meta.get("indicators") or {}
    if indicators:
        st.markdown("**Indicators**")
        ind_rows = [{"Key": str(k), "Value": _short_value(v)} for k, v in indicators.items()]
        st.dataframe(ind_rows, use_container_width=True, hide_index=True)

    # ---- 6. Score breakdown ----
    if score_breakdown:
        st.markdown("**Score breakdown**")
        _render_score_breakdown_bars(score_breakdown, float(confidence or 0))

    # ---- 7. Trade info (only when placed) ----
    if trade:
        st.markdown("**Trade**")
        _render_trade_for_lead(trade)

    # ---- 8. Raw meta (collapsed) ----
    with st.expander("Raw meta (JSON)", expanded=False):
        st.code(json.dumps({k: v for k, v in meta.items() if v}, indent=2, default=str),
                language="json")


def _format_lead_reason(status: str, note: str | None, meta: dict, confidence_pct: int) -> tuple[str, str]:
    """Return (title, body_html) for the 'reason' box at the top of the dialog.

    The title is one of:
      - "Generated because"      (status=queued|placed)
      - "Skipped because"        (status=skipped)
      - "Expired because"        (status=expired)
      - "Status: <status>"       (anything else)
    The body is the most informative human-readable string we have:
      the explicit `note` if present, else the LLM's first sentence of
      rationale, else a confidence-based explanation.
    """
    if status == "skipped":
        title = "Skipped because"
        body = note or meta.get("skip_reason") or _format_reason_fallback(meta, confidence_pct)
        body_html = f"<b>{_html_escape(body)}</b>"
    elif status == "queued":
        title = "Generated because"
        body = (
            note
            or _first_sentence(meta.get("llm_rationale"))
            or _format_reason_fallback(meta, confidence_pct)
        )
        body_html = f"<b>{_html_escape(body)}</b>"
    elif status == "placed":
        title = "Traded because"
        body = (
            note
            or _first_sentence(meta.get("llm_rationale"))
            or _format_reason_fallback(meta, confidence_pct)
        )
        body_html = f"<b>{_html_escape(body)}</b>"
    elif status == "expired":
        title = "Expired because"
        body = note or "24-hour queue TTL elapsed before the order could be placed."
        body_html = f"<b>{_html_escape(body)}</b>"
    else:
        title = f"Status: {status}"
        body = note or "(no reason recorded)"
        body_html = f"<b>{_html_escape(body)}</b>"

    if meta.get("indicator_sample"):
        body_html += (
            f"<div style='color:{MUTED};font-size:0.78rem;margin-top:4px;'>"
            f"Indicator snapshot: {_html_escape(_short_value(meta['indicator_sample']))}"
            f"</div>"
        )
    return title, body_html


def _format_reason_fallback(meta: dict, confidence_pct: int) -> str:
    """Last-resort reason text when neither `note` nor rationale is set."""
    pattern_fit = (meta.get("components") or {}).get("pattern_fit")
    bits = []
    if pattern_fit is not None:
        try:
            bits.append(f"pattern fit {float(pattern_fit):.2f}")
        except (TypeError, ValueError):
            pass
    bits.append(f"confidence {confidence_pct}%")
    return "Model signal met the threshold (" + ", ".join(bits) + ")."


def _first_sentence(text: str | None) -> str | None:
    """First sentence of the LLM rationale (up to first `.`, `;` or newline).

    No truncation — the user explicitly asked for full visibility, and
    LLM rationales routinely run to several hundred words. The function
    only shortens at a natural sentence boundary so the preview still
    reads as a complete thought; if the rationale never closes with a
    sentence boundary, the entire text is returned verbatim.
    """
    if not text:
        return None
    for sep in (".", ";", "\n"):
        i = text.find(sep)
        if 0 < i:
            return text[: i + 1].strip()
    return text.strip()


def _short_value(v) -> str:
    """Compact repr for the lead-detail indicator table.

    Truncates long strings/JSON to 80 chars so a verbose indicator value
    (e.g. a multi-KB EMA series) doesn't push the table off-screen. The
    full value is still available inside the scan-outcome modal — this
    helper is only used by the lead-detail body's indicator table.
    """
    if isinstance(v, str):
        return v if len(v) <= 80 else v[:77] + "…"
    if isinstance(v, (int, float, bool)):
        return str(v)
    s = repr(v)
    return s if len(s) <= 80 else s[:77] + "…"


def _render_tool_calls_full(tool_calls: list[dict]) -> None:
    """Render every tool call as a small expandable card with full args + result keys."""
    tool_labels = {
        "compute_indicators": "compute indicators",
        "breakout_calc":      "breakout calc",
        "fetch_news":         "fetch news",
        "option_chain_summary": "option chain summary",
    }
    for i, c in enumerate(tool_calls, 1):
        name = c.get("name") or "?"
        label = tool_labels.get(name, name)
        args = c.get("args")
        result = c.get("result") or {}
        result_keys = list(result.keys())
        with st.expander(
            f"`{i}. {label}`"
            + (f": {', '.join(result_keys[:4])}" if result_keys else ""),
            expanded=False,
        ):
            ca, cb = st.columns(2)
            with ca:
                st.markdown("**Args**")
                st.code(_format_args_for_display(args), language="json")
            with cb:
                st.markdown("**Result keys**")
                if result_keys:
                    st.code(", ".join(result_keys), language=None)
                else:
                    st.caption("(no result captured)")


def _format_args_for_display(args) -> str:
    """Pretty-print args payload — accept either a dict or a JSON string."""
    if isinstance(args, str):
        try:
            return json.dumps(json.loads(args), indent=2)
        except (json.JSONDecodeError, ValueError):
            return args
    if isinstance(args, dict):
        return json.dumps(args, indent=2, default=str)
    if args is None:
        return "(no args)"
    return repr(args)


def _render_score_breakdown_bars(rows: list[dict], composite: float) -> None:
    """Render each score component as a labelled bar + numeric contribution."""
    for r in rows:
        key = r.get("label") or r.get("key") or "?"
        value = float(r.get("value") or 0)
        weight = float(r.get("weight") or 0)
        contrib = float(r.get("contribution") or 0)
        max_abs = max(abs(value) for r in rows) or 1.0
        pct = max(8, min(100, int(abs(value) / max_abs * 100)))
        bar_color = (
            PROFIT if value > 0.6
            else (WARN if value >= 0.4 else MUTED)
        )
        _html(
            f"""
            <div style='margin-bottom:6px;'>
              <div style='display:flex;justify-content:space-between;
                          font-size:0.78rem;color:{MUTED};margin-bottom:2px;'>
                <span style='color:{TEXT};font-weight:600;'>{_html_escape(str(key))}</span>
                <span>value {value:.2f} · weight {weight:.2f} ·
                      <b style='color:{PROFIT};'>+{contrib:.3f}</b></span>
              </div>
              <div style='height:6px;background:{BORDER};border-radius:999px;overflow:hidden;'>
                <div style='height:6px;width:{pct}%;background:{bar_color};
                            border-radius:999px;'></div>
              </div>
            </div>
            """
        )
    _html(
        f"<div style='margin-top:8px;font-size:0.78rem;color:{MUTED};'>"
        f"Composite confidence: <b style='color:{TEXT};font-variant-numeric:tabular-nums;'>"
        f"{int(composite * 100)}%</b></div>"
    )


def _render_trade_for_lead(trade: dict) -> None:
    """Compact trade block shown inside the lead-detail expander."""
    status = trade.get("status") or "?"
    color = PROFIT if trade.get("realized_pnl", 0) >= 0 else LOSS
    rows = [
        {"key": "Status", "value": status},
        {"key": "Entry", "value": _num(trade.get("entry_price")) if trade.get("entry_price") is not None else "—"},
        {"key": "Exit", "value": _num(trade.get("exit_price")) if trade.get("exit_price") is not None else "—"},
        {"key": "Initial SL", "value": _num(trade.get("initial_sl")) if trade.get("initial_sl") is not None else "—"},
        {"key": "Current SL", "value": _num(trade.get("current_sl")) if trade.get("current_sl") is not None else "—"},
        {"key": "Exit reason", "value": trade.get("exit_reason") or "—"},
        {"key": "Closure cause", "value": trade.get("closure_cause") or "—"},
        {"key": "Realized P&L", "value": f"₹{(trade.get('realized_pnl') or 0):,.2f}"},
    ]
    st.markdown("**Trade**")
    st.dataframe(rows, use_container_width=True, hide_index=True)
    if trade.get("sl_order_id"):
        st.caption(f"SL order id: `{trade['sl_order_id']}` · type: `{trade.get('sl_order_type', '—')}`")


def _is_job_running(job: dict | None) -> bool:
    """Cheap client-side check: a job is "running" iff its server-side status
    is still ``running``. Called on every render so the disabled state on the
    Generate button stays in sync with the background poll."""
    if not job or not job.get("id"):
        return False
    # We don't poll here — that's what the status fragment is for. A stale
    # "running" state just means the button stays disabled for one more
    # render after the job actually finishes, which is harmless.
    return job.get("status", "running") == "running"


def _recover_in_flight_lead_job() -> None:
    """Re-attach session_state to any lead-gen run the server already has.

    Streamlit wipes session_state on every browser refresh, so without this
    the Generate button silently re-enables mid-run. We poll the server
    lazily — once per render, only when session_state is empty — so a normal
    click path doesn't double-call the recovery endpoint.
    """
    if "lead_job" in st.session_state:
        return
    active = api.get_active_lead_gen_job()
    active_data = active.get("data") if active.get("status") == "ok" else None
    if active_data and active_data.get("id"):
        st.session_state["lead_job"] = {
            "id": active_data["id"],
            "submitted_at": active_data.get("submitted_at"),
        }


def _render_generate_lead_button(key_suffix: str) -> bool:
    """The "Generate now" primary action.

    Used by both the Dashboard's queued-signals header and the Leads tab so a
    run started on one tab is visible on the other. Returns True if a fresh
    run was just dispatched (so callers can show follow-up toasts).

    While a run is in flight, a sibling "Stop" button is rendered in a
    second column. Clicking it calls `api.cancel_lead_gen(job_id)`, which
    flips the server-side cancel flag — the generator thread picks it up
    between instruments and the job transitions to its "cancelled" terminal
    status with the partial `scan_outcomes` still on screen.
    """
    _recover_in_flight_lead_job()
    job = st.session_state.get("lead_job")
    is_running = _is_job_running(job)
    button_label = "Generating…" if is_running else "Generate now"
    if is_running:
        gen_col, stop_col = st.columns([3, 1])
        with gen_col:
            clicked = st.button(
                button_label,
                key=f"gen_lead_btn_{key_suffix}",
                type="primary",
                use_container_width=True,
                disabled=True,
                help="A lead-generation run is already in progress.",
            )
        with stop_col:
            stop_clicked = st.button(
                "Stop",
                key=f"stop_lead_btn_{key_suffix}",
                type="secondary",
                use_container_width=True,
                help="Stop the in-flight lead-generation run after the current underlying finishes.",
            )
            if stop_clicked:
                _stop_lead_gen()
        if not clicked:
            return False
    else:
        clicked = st.button(
            button_label,
            key=f"gen_lead_btn_{key_suffix}",
            type="primary",
            use_container_width=True,
            help="Run the lead generator manually (works outside trading hours).",
        )
        if not clicked:
            return False
    gen_resp = api.generate_leads()
    if gen_resp.get("status") == "ok":
        st.session_state["lead_job"] = {
            "id": gen_resp["data"]["id"],
            "submitted_at": gen_resp["data"]["submitted_at"],
        }
        st.toast("Lead generation started.", icon=":material/hourglass_top:")
        return True
    err_code = gen_resp.get("error", {}).get("code")
    if err_code == "lead_generation_in_progress":
        existing = (gen_resp.get("data") or {}).get("id")
        if existing:
            st.session_state["lead_job"] = {
                "id": existing,
                "submitted_at": (gen_resp.get("data") or {}).get("submitted_at"),
            }
        st.toast("A generation run is already in progress.", icon=":material/hourglass_top:")
        return False
    st.error(gen_resp.get("error", {}).get("message", "Generation failed."))
    return False


def _stop_lead_gen() -> None:
    """Send a cancel request to the server for the in-flight lead-gen run.

    Best-effort: the server returns 409 once the run has already terminated,
    which we swallow silently — by the time the user clicks "Stop" the
    polling fragment may have already drained the job. The next render will
    clean up `session_state["lead_job"]` either way.
    """
    job = st.session_state.get("lead_job") or {}
    job_id = job.get("id")
    if not job_id:
        return
    resp = api.cancel_lead_gen(job_id)
    if resp.get("status") == "ok":
        # Mark cancel_requested locally so the button can grey out before
        # the next 2s poll cycle flips the job to "cancelled".
        st.session_state["lead_job"] = {
            **job,
            "cancel_requested": True,
            "status": job.get("status", "running"),
        }
        st.toast("Stop requested — finishing current underlying…",
                 icon=":material/stop_circle:")
        return
    err_code = (resp.get("error") or {}).get("code") or ""
    if err_code in ("lead_generation_not_running", "lead_generation_job_not_found"):
        # Already done — the polling fragment will drop the handle on the
        # next tick. Nothing to surface.
        return
    st.error(f"Could not stop the run: {resp.get('error', {}).get('message', 'unknown error')}")


def _format_elapsed(started_at: str | None, submitted_at: str | None) -> str:
    """Best-effort elapsed-time formatter ("00:14"). Falls back to
    `submitted_at` if `started_at` isn't available yet (the generator thread
    hasn't reached the `started_at` stamp)."""
    from datetime import datetime, timezone

    raw = started_at or submitted_at
    if not raw:
        return "00:00"
    try:
        start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "00:00"
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - start).total_seconds())
    secs = max(0, secs)
    return f"{secs // 60:02d}:{secs % 60:02d}"


def _render_lead_progress_panel(data: dict) -> None:
    """Live progress panel rendered inside the polling fragment.

    `data` is the JobState.to_dict() payload from GET /leads/generate/<id>:
    contains `status`, `progress` (with scanned/total/created/errors/recent),
    and timestamps. The layout must be stable across 2-second re-renders so
    the user sees smooth motion rather than a flicker.
    """
    progress = data.get("progress") or {}
    scanned = int(progress.get("scanned") or 0)
    total = int(progress.get("total") or 0)
    created = int(progress.get("created") or 0)
    errors = int(progress.get("errors") or 0)
    current = progress.get("current")
    phase = progress.get("phase") or "starting"
    recent = progress.get("recent") or []

    pct = 0 if total <= 0 else min(100, int(round(scanned * 100 / total)))
    bar_color = PRIMARY if pct < 100 else PROFIT

    # Phase label — short, human-friendly mapping for the four phases the
    # generator emits. `analyzing` is the long, visible phase; `starting`
    # and `finalizing` flash by quickly.
    phase_label = {
        "starting": "Preparing…",
        "analyzing": "Analyzing underlyings",
        "finalizing": "Finalizing…",
    }.get(phase, "Running")

    elapsed = _format_elapsed(data.get("started_at"), data.get("submitted_at"))

    # Recent-instrument list — fixed-height container so the bar above doesn't
    # jump as items appear. Render newest-first; cap to whatever the server
    # already trimmed to (typically 5).
    status_glyph = {
        "leads": ("", PROFIT),
        "empty": ("", MUTED),
        "cap":   ("", WARN),
        "error": ("", LOSS),
    }

    def _short_status(item: dict) -> str:
        """Format the 'status' field the way we stored it (a string when the
        recent dict came from `lead_generator._emit_progress`, never None
        in practice but we defend anyway)."""
        s = item.get("status")
        return s if isinstance(s, str) else "empty"

    def _recent_row(item: dict) -> str:
        sym = item.get("symbol") or "?"
        st_name = _short_status(item)
        glyph, color = status_glyph.get(st_name, ("?", MUTED))
        n = int(item.get("leads") or 0)
        conf = item.get("confidence")
        direction = item.get("direction") or ""
        signal_type = item.get("signal_type") or ""
        if st_name == "leads":
            # The recent dict from `lead_generator._emit_progress` carries
            # just `{symbol, status, leads, error}` — direction/confidence
            # show up when the lead was actually persisted, which we get
            # from `created` but not from this row. Show count + direction
            # pill if present.
            pill = ""
            if direction in ("CALL", "PUT"):
                d_color = PROFIT if direction == "CALL" else LOSS
                pill = (
                    f"<span class='lg-recent-dir' style='color:{d_color};border-color:{d_color}66;'>"
                    f"{_html_escape(direction)}</span>"
                )
            tail = (
                f" {pill}"
                f"<span style='color:{PROFIT};font-weight:600;'>{n} lead{'s' if n != 1 else ''}</span>"
            )
        elif st_name == "error":
            err_msg = item.get("error") or ""
            tail = f" <span style='color:{LOSS};font-size:0.78rem;'>{_html_escape(err_msg[:60])}</span>"
        else:
            tail = ""
        return (
            f"<div class='lg-recent-row'>"
            f"<span style='color:{color};font-weight:700;width:14px;flex-shrink:0;'>{glyph}</span>"
            f"<span style='flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'>{_html_escape(sym)}</span>"
            f"<span style='flex-shrink:0;display:flex;gap:6px;align-items:center;'>{tail}</span>"
            f"</div>"
        )

    rows_html = "".join(_recent_row(r) for r in recent[:5])
    if not rows_html:
        rows_html = "<div class='muted' style='font-size:0.8rem;'>Waiting for first result…</div>"

    current_html = (
        f"<div class='lg-current'><span class='muted'>Current:</span> "
        f"<b>{_html_escape(current)}</b></div>"
        if current else ""
    )

    # NOTE: the live LLM agent-loop tool-call activity (per-symbol
    # timeline + 8-row preview feed) was removed — the operator only
    # needs the symbol + status + short reason to monitor a run. Full
    # tool-call detail is available inside the scan-outcome detail
    # modal (open by clicking a row in the table below).

    _html(
        f"""
        <div class='lg-panel'>
          <div class='lg-head'>
            <div class='lg-phase'>
              <span class='lg-spinner'></span>
              <b>{_html_escape(phase_label)}</b>
            </div>
            <div class='lg-elapsed muted'>{elapsed}</div>
          </div>
          <div class='lg-bar'>
            <div class='lg-fill' style='width:{pct}%;background:{bar_color};'></div>
          </div>
          <div class='lg-meta muted'>
            <span><b style='color:#e2e8f0;'>{scanned}</b> / {total} underlyings scanned</span>
            <span><b style='color:{PROFIT};'>{created}</b> leads created</span>
            {f"<span style='color:{LOSS};'><b>{errors}</b> errors</span>" if errors else ""}
          </div>
          {current_html}
          <div class='lg-recent'>{rows_html}</div>
        </div>
        """
    )


def _render_llm_tool_calls_timeline(tool_calls: list[dict]) -> str:
    """Removed: live LLM tool-call activity was dropped from the live
    progress panel (operator only needs symbol + status + short reason
    to monitor a run). Function kept as a stub so any leftover import
    keeps working; returns an empty string.
    """
    return ""


def _render_llm_tool_calls(tool_calls: list[dict]) -> str:
    """Removed: live LLM tool-call activity feed was dropped from the
    live progress panel. Function kept as a stub so any leftover import
    keeps working; returns an empty string.
    """
    return ""


def _html_escape(s: str) -> str:
    """Minimal HTML escape for values embedded into raw HTML in the progress
    panel. Avoids pulling in `markupsafe` as a UI dependency."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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
        err = resp.get("error") or {}
        code = err.get("code") or ""
        http_status = err.get("http_status")
        # 404 = the server trimmed the registry; nothing to recover.
        if code == "http_404":
            st.session_state.pop("lead_job", None)
            return
        # Otherwise (500, network, etc.) keep the local handle so the
        # next 2s poll can recover once the backend is back. Show the
        # actual failure inline so the user knows whether to wait or
        # contact ops.
        msg = err.get("message") or "Status polling failed."
        endpoint = err.get("endpoint") or f"GET /leads/generate/{job_id}"
        st.error(
            f"**Cannot reach `{endpoint}`**  \n"
            f"Status: `{http_status or code or 'unknown'}`  \n"
            f"{msg}  \n\n"
            f"Auto-retrying every 2s. The run is still running on the "
            f"server; this UI just can't see its progress until the API "
            f"responds again."
        )
        with st.expander("Response body"):
            st.code(err.get("body") or "(empty)", language=None)
        col_a, col_b = st.columns(2)
        if col_a.button("Retry now", type="secondary", key="retry_poll_now"):
            st.rerun()
        if col_b.button("Drop job handle", type="secondary", key="drop_job_handle"):
            st.session_state.pop("lead_job", None)
            st.rerun()
        return

    data = resp["data"]
    status = data.get("status")
    # Cache the server-side status on the session-state entry so the
    # `_is_job_running()` check on the main render loop stays in sync
    # without re-polling.
    st.session_state["lead_job"]["status"] = status
    # Mirror the live ring buffer of scan_outcomes into session_state so
    # the outer render (which doesn't poll) can render the latest-run
    # table without an extra round-trip.
    #
    # The ring buffer items are emitted BEFORE each per-instrument
    # ``LeadScanOutcome`` row is persisted (they're the live progress
    # payload, not the DB row), so they DON'T have an ``id`` field. While
    # the run is in flight that's correct — they render the "Analyzing…"
    # spinner. Once the run hits a terminal status, those entries are
    # stale: the same outcomes now exist in the DB with an ``id`` and the
    # ``_render_outcome_row`` spinner check would otherwise keep them
    # pegged at "Analyzing…" forever. Drop the cache on terminal so the
    # next render falls through to the DB endpoint.
    progress = data.get("progress") or {}
    live_outcomes = list(progress.get("scan_outcomes") or [])
    if status == "running" and live_outcomes:
        st.session_state["_latest_outcomes_cache"] = live_outcomes
    # Remember the most-recent job id so the leads table can keep
    # rendering the just-finished run even after the job transitions
    # out of "running" (the polling fragment pops `lead_job` on terminal
    # status, but the operator should still be able to inspect what the
    # last run did). Stored on a dedicated key so a NEW run doesn't lose
    # the previous one until the new one produces its own outcomes.
    if status != "running":
        # Drop any pre-terminal streaming cache so the next outer render
        # hits the DB-backed endpoint and sees rows with a stable ``id``.
        st.session_state.pop("_latest_outcomes_cache", None)
        st.session_state["last_lead_job_id"] = job_id
        st.session_state["last_lead_job_status"] = status
        st.session_state["last_lead_job_result"] = data.get("result") or {}

    if status == "running":
        _render_lead_progress_panel(data)
        _render_scan_outcomes_panel(job_id, data)
        return

    st.session_state.pop("lead_job", None)
    if status == "done":
        result = data.get("result") or {}
        generated = result.get("generated", 0)
        checked = result.get("checked", 0)
        elapsed = _format_elapsed(data.get("started_at"), data.get("submitted_at"))
        st.toast(
            f"Generated {generated} lead{'s' if generated != 1 else ''} "
            f"from {checked} underlying{'s' if checked != 1 else ''} in {elapsed}.",
            icon=":material/check_circle:",
        )
        _render_scan_outcomes_panel(job_id, data)
    elif status == "cancelled":
        result = data.get("result") or {}
        elapsed = _format_elapsed(data.get("started_at"), data.get("submitted_at"))
        st.toast(
            f"Run stopped after {elapsed}. Partial results kept on screen.",
            icon=":material/stop_circle:",
        )
        _render_scan_outcomes_panel(job_id, data)
    elif status == "error":
        st.error(f"Generation failed: {data.get('error') or 'unknown error'}")
    st.rerun()


# ---------------------------------------------------------------------------
# Scanned stocks panel
# ---------------------------------------------------------------------------


def _render_scan_outcomes_panel(job_id: str | None, job_data: dict | None) -> None:
    """Render the per-instrument scan-outcome list for a lead-gen job.

    While the job is running we use the live `progress["scan_outcomes"]`
    ring buffer the server emits with each progress patch — no extra
    round-trip needed. For terminal statuses (or for a refresh after the
    job dropped out of session_state) we fall back to the REST endpoint
    so the panel survives a page reload.

    The live fragment uses the same table layout as the main Leads tab
    so the operator sees one consistent view across a run — the table
    streams in during the run, settles when the run finishes, and the
    outer render then re-uses the same component to display the
    finalised list.
    """
    # Try the in-memory ring buffer first (live runs).
    outcomes = []
    if isinstance(job_data, dict):
        progress = job_data.get("progress") or {}
        outcomes = list(progress.get("scan_outcomes") or [])

    if not outcomes and job_id:
        # Fall back to the DB-backed endpoint. We do this when the polling
        # fragment already popped the job (so `job_data` is empty) but the
        # operator still wants to inspect the just-finished run.
        try:
            resp = api.get_scan_outcomes(job_id=job_id, limit=200)
            if resp.get("status") == "ok":
                outcomes = list(resp.get("data") or [])
        except Exception:
            outcomes = []

    if not outcomes:
        return

    # Live-streaming rows (no DB id yet) plus persisted rows render
    # through the same table the Leads tab uses — see `_render_outcome_table`.
    _render_outcome_table(outcomes)


@st.dialog("Scan outcome detail", width="large")
def _scan_outcome_modal(outcome_id: int) -> None:
    """Modal showing one scan outcome's full LLM thinking.

    Rendered top-level by `main` whenever `session_state["open_scan_outcome"]`
    is set. Pulls the detail endpoint (which carries the full `tool_calls`
    JSON) so the operator can see every LLM tool call (indicators / news /
    option chain / calc) the agent loop made for this underlying — no
    truncation anywhere; the entire LLM rationale and every tool-call
    result payload are rendered verbatim so the user can copy them
    out and reason about the decision end-to-end.
    """
    resp = api.get_scan_outcome_detail(outcome_id)
    if resp.get("status") != "ok":
        st.error(f"Could not load scan outcome: "
                 f"{resp.get('error', {}).get('message', 'unknown error')}")
        if st.button("Close"):
            st.session_state.pop("open_scan_outcome", None)
            st.rerun()
        return

    o = resp["data"]
    sym = o.get("symbol") or "?"
    decision = (o.get("decision") or "no_signal").lower()
    label, _ = _outcome_status_label(decision)
    scanned_at = o.get("scanned_at_ist_label") or "—"

    # The headline summary the operator saw in the row. When the LLM
    # supplied one, we render it as the prominent takeaway so the modal
    # opens with the same one-liner they clicked on, then expand into
    # the full reasoning below.
    short_reason = (o.get("short_reason") or "").strip()

    st.markdown(
        f"### {sym}  \n"
        f"<span style='color:#94a3b8;font-size:0.9rem;'>"
        f"Decision: <b>{_html_escape(label)}</b> · "
        f"Scanned at: {_html_escape(scanned_at)} · "
        f"Strategy: <code>{_html_escape(o.get('strategy') or '?')}</code> · "
        f"Agent iters: {int(o.get('agent_iters') or 0)} · "
        f"Duration: {int(o.get('duration_ms') or 0)} ms"
        f"</span>",
        unsafe_allow_html=True,
    )

    if short_reason:
        # Render the LLM-supplied single-sentence summary as the
        # headline. This is the same text the row preview showed, but
        # here it's the entry point into the full detail below.
        decision_color = {
            "generated": PROFIT,
            "error":     LOSS,
            "no_signal": MUTED,
        }.get(decision, MUTED)
        st.markdown(
            f"<div style='margin:10px 0 6px 0;padding:10px 12px;"
            f"border-left:3px solid {decision_color};"
            f"background:rgba(148,163,184,0.08);border-radius:6px;'>"
            f"<div style='font-size:0.72rem;text-transform:uppercase;"
            f"letter-spacing:0.06em;color:{decision_color};font-weight:600;'>"
            f"Summary</div>"
            f"<div style='font-size:0.95rem;color:#e2e8f0;margin-top:2px;'>"
            f"{_html_escape(short_reason)}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    if o.get("rejection_reason"):
        st.markdown("**Rejection reason (LLM)**")
        # Full text — no truncation. Wrap in a `st.code` block so very long
        # strings get a horizontal scrollbar instead of being clipped.
        st.code(str(o["rejection_reason"]), language=None)
    if o.get("rationale"):
        st.markdown("**Full LLM rationale**")
        st.code(str(o["rationale"]), language=None)
    if o.get("error"):
        st.markdown("**Error**")
        st.code(str(o["error"]), language=None)
    if o.get("lead_id"):
        st.markdown(f"**Generated lead**: `#{int(o['lead_id'])}` "
                    f"({int(o.get('leads_created') or 0)} lead(s) created)")

    tool_calls = o.get("tool_calls") or []
    st.markdown(f"**Agent-loop tool calls** · {len(tool_calls)}")
    if not tool_calls:
        st.caption("(no tool calls were made — the LLM declined without looking up any tool data)")
    else:
        for i, tc in enumerate(tool_calls, start=1):
            iter_n = tc.get("iter")
            name = tc.get("name") or "?"
            args = tc.get("args")
            result = tc.get("result") or {}
            with st.expander(
                f"`{i}. {name}`"
                + (f"  ·  iter {iter_n}" if iter_n else "")
                + (f": {', '.join(list(result.keys())[:4])}" if result else ""),
                expanded=False,
            ):
                ca, cb = st.columns(2)
                with ca:
                    st.markdown("**Args**")
                    st.code(_format_args_for_display(args), language="json")
                with cb:
                    st.markdown("**Result**")
                    if not result:
                        st.caption("(no result captured)")
                    else:
                        # Full result — no `[:8000]` slice. A single tool
                        # call's payload can be tens of KB; Streamlit's
                        # `st.code` block handles that natively.
                        st.code(
                            json.dumps(result, indent=2, default=str),
                            language="json",
                        )

    if st.button("Close", key=f"scan_close_{outcome_id}"):
        st.session_state.pop("open_scan_outcome", None)
        st.rerun()


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
                _html(f"<div class='muted'>{sym}: {'enabled' if row['active'] else 'disabled'}</div>")


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
        st.markdown("<div class='history-section-label'>Period</div>", unsafe_allow_html=True)
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
    st.markdown("<div class='history-mobile-header'>Performance Summary</div>", unsafe_allow_html=True)
    
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
        st.markdown("<div class='history-mobile-header'>Equity Curve</div>", unsafe_allow_html=True)
        eq = pd.DataFrame({"Equity": cum})
        eq.index = pd.RangeIndex(1, len(eq) + 1, name="trade #")
        st.line_chart(eq, height=160, use_container_width=True)
        st.caption("Equity curve (running net P&L across the selected period).")
        
        # Divider
    _html("<div class='history-divider'></div>")

    # Mobile section header for table
    st.markdown("<div class='history-mobile-header'>Trade History</div>", unsafe_allow_html=True)
    
    df = _trades_df(
        filtered,
        ["symbol", "direction", "entry_price", "exit_price", "realized_pnl", "exit_reason"],
    )
    df["Closed (IST)"] = [_utc_to_ist_hm(r.get("exit_time")) for r in filtered]
    df.columns = ["Symbol", "Dir", "Entry", "Exit", "P&L", "Reason", "Closed (IST)"]
    styled = df.style.apply(_style_pnl_col, subset=["P&L"])
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # --- Post-Trade Monitoring -------------------------------------------
    # Drift audit + per-trade detail. The two admin-priority questions are:
    #  (1) Why did a trade close the way it did?  (drift events during life)
    #  (2) What drift is the system seeing in aggregate right now?
    _render_post_trade_monitoring(filtered)

    st.markdown("</div>", unsafe_allow_html=True)


def _render_post_trade_monitoring(filtered_rows: list[dict]) -> None:
    """Aggregate drift tiles + per-trade drift expander for the visible rows.

    Two pieces:
      - Summary tiles (last 24h) from GET /api/drifts/summary — handy header.
      - Per-trade expander inside a selectbox — opens /closed/<id> and shows
        any drift events recorded for that trade.
    """
    st.markdown("<div class='history-mobile-header'>Post-trade monitoring</div>",
                unsafe_allow_html=True)
    _html("<div class='history-divider'></div>")

    # Aggregate summary
    summary = api.get_drift_summary()
    s_data = summary.get("data") or {} if summary.get("status") == "ok" else {}
    by_type = s_data.get("by_type") or []
    by_sev = s_data.get("by_severity") or []
    total_24h = sum(int(x.get("count") or 0) for x in by_sev)
    critical = next((int(x.get("count") or 0) for x in by_sev
                     if x.get("severity") == "critical"), 0)
    warn = next((int(x.get("count") or 0) for x in by_sev
                 if x.get("severity") == "warn"), 0)

    tiles = [
        _stat_tile("Drifts (24h)", str(total_24h),
                   color=LOSS if critical > 0 else (WARN if warn > 0 else MUTED)),
        _stat_tile("Critical", str(critical), color=LOSS if critical else MUTED),
        _stat_tile("Warn", str(warn), color=WARN if warn else MUTED),
    ]
    _html("<div class='row' style='flex-wrap:wrap;gap:10px;'>" + "".join(tiles) + "</div>")

    if by_type:
        rows_html = "".join(
            f"<div class='row' style='justify-content:space-between;'>"
            f"<span class='muted'>{_html_escape(str(x.get('drift_type') or '?'))}</span>"
            f"<b>{int(x.get('count') or 0)}</b></div>"
            for x in by_type[:8]
        )
        _html(
            f"<div class='card' style='margin-top:10px;'>"
            f"<div class='card-title'>By drift type (last 24h)</div>"
            f"{rows_html}</div>"
        )

    # Per-trade picker
    options = [
        (r.get("id"), f"#{r.get('id')} · {r.get('symbol', '?')} · "
                      f"{_money(r.get('realized_pnl') or 0)} · "
                      f"{r.get('exit_reason') or '—'}")
        for r in filtered_rows if r.get("id") is not None
    ]
    if not options:
        return
    with st.expander("Inspect a single trade", expanded=False):
        labels = [o[1] for o in options]
        pick = st.selectbox(
            "Trade", labels, key="post_trade_pick",
            label_visibility="collapsed",
        )
        sel_id = next((o[0] for o in options if o[1] == pick), None)
        if sel_id is None:
            return
        with st.spinner("Loading trade + drift events…"):
            resp = api.get_closed_trade_detail(int(sel_id))
        if resp.get("status") != "ok":
            st.warning("Could not load trade detail.")
            return
        td = resp.get("data") or {}
        trade_rows = [
            {"key": "Status", "value": str(td.get("status") or "—")},
            {"key": "Entry", "value": _num(td.get("entry_price"))},
            {"key": "Exit", "value": _num(td.get("exit_price"))},
            {"key": "Initial SL", "value": _num(td.get("initial_sl"))},
            {"key": "Current SL", "value": _num(td.get("current_sl"))},
            {"key": "Exit reason", "value": str(td.get("exit_reason") or "—")},
            {"key": "Closure cause", "value": str(td.get("closure_cause") or "—")},
            {"key": "Realized P&L", "value": _money(td.get("realized_pnl") or 0)},
        ]
        st.markdown("**Trade**")
        st.dataframe(trade_rows, use_container_width=True, hide_index=True)
        drifts = td.get("drifts") or []
        if not drifts:
            st.caption("No drift events recorded for this trade — "
                       "broker view matched our DB throughout.")
        else:
            st.markdown("**Drift events**")
            sev_color = {"critical": LOSS, "warn": WARN, "info": MUTED}
            for d in drifts:
                sev = (d.get("severity") or "info").lower()
                color = sev_color.get(sev, MUTED)
                st.markdown(
                    f"<div class='card' style='border-left:3px solid {color};margin:6px 0;'>"
                    f"<div style='display:flex;justify-content:space-between;'>"
                    f"<b>{_html_escape(str(d.get('drift_type') or '?'))}</b>"
                    f"<span class='muted' style='font-size:0.78rem;'>"
                    f"{_html_escape(str(d.get('ts') or ''))}</span></div>"
                    f"<div style='font-size:0.85rem;color:#cbd5e1;'>"
                    f"{_html_escape(str(d.get('detail') or ''))}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )


def render_drifts():
    """Admin-only drift audit page.

    Shows the last 24h of broker-vs-DB discrepancies the trade tracker and
    reconciler recorded. This is the central place for post-trade monitoring:
    SL mismatches, position-missing events, qty drift, exit-price mismatches.
    """
    st.subheader("Drift audit")
    resp = api.get_drifts()
    if resp.get("status") != "ok":
        err = resp.get("error") or {}
        msg = err.get("message") or "Could not load drift events."
        code = err.get("code") or ""
        if code in ("unauthorized", "jwt_expired") or "auth" in code.lower():
            st.error(f"{msg} — your session has expired. Use the sidebar **Logout** and re-login.")
        else:
            st.error(
                f"**{code or 'error'}**: {msg}  \n"
                f"Endpoint: `{err.get('endpoint', 'GET /api/drifts')}`  \n"
                f"*(Drift events are an audit log; the bot keeps trading even when this view is offline.)*"
            )
            with st.expander("Response body"):
                st.code(err.get("body") or "(empty)", language=None)
        if st.button("Retry", type="secondary", key="retry_drift_load"):
            st.rerun()
        return
    rows = (resp.get("data") or {}).get("drifts") or []
    summary = api.get_drift_summary()
    s = (summary.get("data") or {}) if summary.get("status") == "ok" else {}
    by_type = s.get("by_type") or []
    by_sev = s.get("by_severity") or []
    crit = next((int(x.get("count") or 0) for x in by_sev
                 if x.get("severity") == "critical"), 0)
    warn = next((int(x.get("count") or 0) for x in by_sev
                 if x.get("severity") == "warn"), 0)
    info = next((int(x.get("count") or 0) for x in by_sev
                 if x.get("severity") == "info"), 0)

    # Header tiles
    tiles = (
        _stat_tile("Critical", str(crit), color=LOSS if crit else MUTED)
        + _stat_tile("Warn", str(warn), color=WARN if warn else MUTED)
        + _stat_tile("Info", str(info), color=MUTED)
        + _stat_tile("Total", str(len(rows)))
    )
    _html("<div class='row' style='flex-wrap:wrap;gap:10px;'>" + tiles + "</div>")

    if by_type:
        rows_html = "".join(
            f"<div class='row' style='justify-content:space-between;'>"
            f"<span class='muted'>{_html_escape(str(x.get('drift_type') or '?'))}</span>"
            f"<b>{int(x.get('count') or 0)}</b></div>"
            for x in by_type[:8]
        )
        _html(
            f"<div class='card' style='margin-top:10px;'>"
            f"<div class='card-title'>By drift type (last 24h)</div>"
            f"{rows_html}</div>"
        )

    # Filter chips
    drift_types = sorted({r.get("drift_type") for r in rows if r.get("drift_type")})
    severity_opts = sorted({r.get("severity") for r in rows if r.get("severity")})
    fc1, fc2, fc3 = st.columns([2, 2, 3])
    with fc1:
        type_filter = st.selectbox("Drift type", ["All"] + drift_types,
                                  key="drift_type_filter",
                                  label_visibility="collapsed")
    with fc2:
        sev_filter = st.selectbox("Severity", ["All"] + severity_opts,
                                 key="drift_sev_filter",
                                 label_visibility="collapsed")
    with fc3:
        trade_filter = st.text_input("Trade id", key="drift_trade_filter",
                                     placeholder="(any)", label_visibility="collapsed")

    filtered = rows
    if type_filter != "All":
        filtered = [r for r in filtered if r.get("drift_type") == type_filter]
    if sev_filter != "All":
        filtered = [r for r in filtered if r.get("severity") == sev_filter]
    if trade_filter.strip():
        try:
            tid = int(trade_filter.strip())
            filtered = [r for r in filtered if r.get("trade_id") == tid]
        except ValueError:
            pass

    if not filtered:
        st.info("No drift events match the current filters.")
        return

    sev_color = {"critical": LOSS, "warn": WARN, "info": MUTED}
    items = []
    for d in filtered[:200]:
        sev = (d.get("severity") or "info").lower()
        items.append({
            "ts": d.get("ts") or "",
            "trade": f"#{d.get('trade_id')}" if d.get("trade_id") else "—",
            "type": d.get("drift_type") or "?",
            "severity": sev,
            "source": d.get("source") or "",
            "detail": (d.get("detail") or "")[:160],
            "_sev_color": sev_color.get(sev, MUTED),
        })
    for it in items:
        _html(
            f"<div class='card' style='border-left:3px solid {it['_sev_color']};"
            f"margin:6px 0;'>"
            f"<div style='display:flex;justify-content:space-between;'>"
            f"<b>{_html_escape(it['type'])}</b>"
            f"<span class='muted' style='font-size:0.78rem;'>"
            f"{_html_escape(it['ts'])} · {it['trade']} · {it['source']}</span></div>"
            f"<div style='font-size:0.85rem;color:#cbd5e1;'>"
            f"{_html_escape(it['detail'])}</div>"
            f"</div>"
        )


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
    llm = d.get("llm") or {}
    top = (
        _stat_tile("Market", "OPEN" if m["open"] else "CLOSED",
                   color=PROFIT if m["open"] else MUTED)
        + _stat_tile("Broker",
                     "CONNECTED" if b.get("connected")
                     else ("NOT CONFIGURED" if not b.get("configured") else "DISCONNECTED"),
                     color=PROFIT if b.get("connected") else (MUTED if not b.get("configured") else LOSS))
        + _stat_tile("LLM",
                     _llm_status_label(llm),
                     color=_llm_status_color(llm))
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

    st.markdown("##### LLM (MiniMax M3)")
    llm_status = (llm.get("status") or "unknown") if isinstance(llm, dict) else "unknown"
    if not llm.get("configured"):
        status_kind = "muted"
        badge_text = "NOT CONFIGURED"
    elif llm_status == "ok":
        status_kind = "ok"
        badge_text = "OK"
    elif llm_status == "error":
        status_kind = "warn"
        badge_text = "ERROR"
    else:
        status_kind = "warn" if llm_status == "stale" else "muted"
        badge_text = llm_status.upper()
    model = llm.get("model") or "(unset)"
    st.markdown(
        f"{_badge(badge_text, status_kind)} "
        f"<span class='muted'>model: <code>{model}</code></span>",
        unsafe_allow_html=True,
    )
    llm_stats = llm.get("stats") or {}
    calls_total = llm_stats.get("calls_total", 0)
    errors_total = llm_stats.get("errors_total", 0)
    last_success = llm_stats.get("last_success_at")
    last_error = llm_stats.get("last_error_at")
    last_error_msg = llm_stats.get("last_error") or ""
    col1, col2, col3 = st.columns([2, 2, 3])
    col1.metric("LLM calls (cumulative)", calls_total)
    col2.metric("LLM errors (cumulative)", errors_total)
    if last_success:
        col3.caption(f"last success: `{_utc_to_ist_hm(last_success)} IST`")
    elif last_error:
        col3.caption(f"last error: `{_utc_to_ist_hm(last_error)} IST`")
    if last_error_msg:
        st.caption(f"last error: {last_error_msg[:200]}")

    st.markdown("##### Errors")
    if e["count"] == 0:
        st.markdown(f"{_badge('NO ERRORS', 'ok')}", unsafe_allow_html=True)
    else:
        st.markdown(_badge(f"{e['count']} error(s)", "err"), unsafe_allow_html=True)

    # Drift audit summary — surfaces broker-vs-DB discrepancies in the
    # last 24h. Reuses the API client; cheap (single SQL aggregate query).
    st.markdown("##### Drift audit (24h)")
    summary = api.get_drift_summary()
    if summary.get("status") != "ok":
        st.caption("Drift summary unavailable.")
    else:
        s = summary.get("data") or {}
        by_type = s.get("by_type") or []
        by_sev = s.get("by_severity") or []
        crit = next((int(x.get("count") or 0) for x in by_sev
                     if x.get("severity") == "critical"), 0)
        warn = next((int(x.get("count") or 0) for x in by_sev
                     if x.get("severity") == "warn"), 0)
        info = next((int(x.get("count") or 0) for x in by_sev
                     if x.get("severity") == "info"), 0)
        c1, c2, c3 = st.columns(3)
        c1.metric("Critical", crit)
        c2.metric("Warn", warn)
        c3.metric("Info", info)
        if by_type:
            rows = [{"drift_type": x.get("drift_type"),
                     "count": int(x.get("count") or 0)} for x in by_type[:8]]
            st.dataframe(rows, use_container_width=True, hide_index=True)
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
    trade_session_label = (
        f"session {m['session']['start']}"
        f"–{m['session']['trade_end']} (new trades)"
        f" · {m['session']['end']} (sqoff)"
    )
    st.markdown(
        f"{_badge('OPEN' if m['open'] else 'CLOSED', 'ok' if m['open'] else 'muted')} "
        f"<span class='muted'>{m['date']} · {m['time_ist']} IST · {trade_session_label}</span>",
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
        c1, c2, c3 = st.columns(3)
        with c1:
            start = st.time_input("Trading start (IST)", value=_ui_time(cfg.get("trading_start", "10:00")))
        with c2:
            trade_end = st.time_input(
                "Trade end time (IST)",
                value=_ui_time(cfg.get("trade_end_time", cfg.get("sqoff_time", "11:00"))),
                help="No new trades are opened after this time. Existing positions are still tracked and squared off at Market end time. Must be ≤ Market end time.",
            )
        with c3:
            end = st.time_input(
                "Market end time (IST)",
                value=_ui_time(cfg.get("sqoff_time", "14:00")),
                help="Existing positions are squared off at this time (was previously called \"Square-off time\").",
            )
        new_cfg["trading_start"] = start.strftime("%H:%M")
        new_cfg["trade_end_time"] = trade_end.strftime("%H:%M")
        new_cfg["sqoff_time"] = end.strftime("%H:%M")
        if trade_end > end:
            st.error("Trade end time must be ≤ Market end time.")

    with tab_sl:
        st.caption("Risk management per open position.")
        sl = st.number_input("Initial SL %", min_value=1.0, max_value=30.0,
                             value=float(cfg.get("initial_sl_pct", 10.0)),
                             help="Hard stop-loss distance from entry, in %.")
        activate = st.number_input("Trail activate % (past initial SL)", min_value=0.0, max_value=50.0,
                                   value=float(cfg.get("trail_activate_pct", 20.0)),
                                   help="Trailing starts once LTP moves this far PAST the initial SL in the profitable direction. "
                                        "Example: initial SL 90 + 20% then trailing begins at ltp ≥ 108.")
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
        max_leads = st.number_input(
            "Max leads per generator run", min_value=0, max_value=50,
            value=int(cfg.get("lead_generator.max_leads_per_run", 5)),
            help="Stop the lead-generator scheduler tick once this many leads have been queued. "
                 "Set to 0 for unlimited. Tune lower if you have limited deployment capital.",
        )
        max_workers = st.number_input(
            "Concurrent worker threads", min_value=1, max_value=16,
            value=int(cfg.get("lead_generator.max_workers", 4)),
            help="How many threads to run in parallel when fetching candles and calling the LLM. "
                 "Each worker holds its own strategy instance. Higher = faster ticks but more "
                 "concurrent Upstox/LLM calls. Keep ≤8 to respect free-tier rate limits.",
        )
        shuffle = st.checkbox(
            "Shuffle instrument order each run",
            value=bool(cfg.get("lead_generator.shuffle_instruments", True)),
            help="Pick enabled instruments in a random order each tick so the same stocks don't "
                 "always get first crack at the LLM call budget.",
        )
        new_cfg["lead_generator.max_leads_per_run"] = int(max_leads)
        new_cfg["lead_generator.max_workers"] = int(max_workers)
        new_cfg["lead_generator.shuffle_instruments"] = bool(shuffle)
        st.markdown("---")
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
        with st.expander("Decay & calibration (Tier-4)", expanded=False):
            st.caption("Post-queue staleness decay and historical win-rate calibration.")
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
            "scoring.enable_time_of_day": bool(tod_on),
            "scoring.enable_oi": bool(oi_on),
            "scoring.enable_iv": bool(iv_on),
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

    # Lead-detail dialog: invoked at the TOP LEVEL of the script (not
    # nested at the bottom of `render_leads`) so Streamlit always
    # renders it as a modal overlay. `render_leads` only sets the
    # `open_lead_dialog` session_state flag; the actual call happens
    # here.
    open_id = st.session_state.get("open_lead_dialog")
    if open_id:
        st.session_state.pop("open_lead_dialog", None)
        _open_lead_detail_modal(int(open_id))

    # Scan-outcome modal: same pattern as the lead-detail modal above.
    # The "Scanned stocks" panel sets `open_scan_outcome` to the row id
    # the user clicked; we hand it off to `_scan_outcome_modal` here so
    # Streamlit renders it as a modal overlay on top of whatever tab is
    # currently active.
    open_scan = st.session_state.get("open_scan_outcome")
    if open_scan:
        st.session_state.pop("open_scan_outcome", None)
        _scan_outcome_modal(int(open_scan))

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
        "Drifts": render_drifts,
        "Health": render_health,
        "Settings": render_settings,
    }
    views[page]()

    _html(
        f"<div class='muted' style='margin-top:24px;text-align:center'>"
        f"Last render: {_ist_now_str()} IST</div>"
    )


main()
