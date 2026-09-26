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

import pandas as pd
import streamlit as st

import api_client as api

st.set_page_config(page_title="TradePilot", page_icon=":material/show_chart:", layout="centered")

# Palette — modern fintech dark theme
PRIMARY = "#7c3aed"    # violet (primary accent / buttons / links)
PRIMARY_SOFT = "#a78bfa"
SECONDARY = "#22d3ee"  # cyan (secondary accent / info)
PROFIT = "#10b981"     # emerald (positive P&L)
LOSS = "#f43f5e"       # rose (negative P&L)
WARN = "#fbbf24"       # amber
MUTED = "#94a3b8"      # slate (secondary text)
TEXT = "#e2e8f0"       # primary text
BG = "#0a0e1a"         # app background
BG_GRAD_TOP = "#0e1322"  # subtle top gradient
CARD = "#131826"       # cards
CARD_HOVER = "#1a2138"  # card hover state
BORDER = "#1f2937"     # card borders

REFRESH_SECS = 5       # dashboard + open trades auto-refresh cadence


def _css() -> str:
    """Modern dark fintech design system.

    Visual language inspired by Robinhood / Coinbase / Delta / Binance —
    glassy surfaces, vivid accents, generous spacing, animated micro-states.
    """
    return f"""
    <style>
    /* ---------- Global tokens + typography ---------- */
    :root {{
      --bg: {BG};
      --bg-grad-top: {BG_GRAD_TOP};
      --card: {CARD};
      --card-hover: {CARD_HOVER};
      --border: {BORDER};
      --primary: {PRIMARY};
      --primary-soft: {PRIMARY_SOFT};
      --secondary: {SECONDARY};
      --profit: {PROFIT};
      --loss: {LOSS};
      --warn: {WARN};
      --muted: {MUTED};
      --text: {TEXT};
    }}
    html, body, [class*="css"], .stApp {{
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI',
                   Roboto, 'Helvetica Neue', Arial, sans-serif;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }}
    code, .mono, [data-testid="stDataFrame"], .stMarkdown pre {{
      font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', Menlo, Consolas, monospace;
      font-variant-numeric: tabular-nums;
    }}

    /* ---------- App shell ---------- */
    .stApp {{
      max-width: 920px;
      margin: auto;
      background:
        radial-gradient(1200px 600px at 80% -100px, rgba(124,58,237,.10), transparent 60%),
        radial-gradient(800px 500px at 0% -50px, rgba(34,211,238,.06), transparent 60%),
        linear-gradient(180deg, {BG_GRAD_TOP} 0%, {BG} 240px);
      color: {TEXT};
    }}
    /* Strip Streamlit's default top header so we can render our own */
    [data-testid="stHeader"] {{ background: transparent; height: 0; }}
    footer {{ visibility: hidden; }}
    #MainMenu {{ visibility: hidden; }}

    /* ---------- Sidebar ---------- */
    [data-testid="stSidebar"] {{
      background: linear-gradient(180deg, #0d1220 0%, #0a0e1a 100%);
      border-right: 1px solid {BORDER};
      padding: 18px 14px;
    }}
    [data-testid="stSidebar"] h3 {{
      color: {MUTED}; font-size: 0.7rem; text-transform: uppercase;
      letter-spacing: .12em; margin: 18px 6px 8px 6px; font-weight: 700;
    }}
    .sidebar-brand {{
      margin: 4px 6px 2px; color: #f8fafc;
      font-size: 1.2rem; font-weight: 800; letter-spacing: -.01em;
      display: flex; align-items: center; gap: 8px;
    }}
    .sidebar-brand .brand-mark {{
      width: 28px; height: 28px; border-radius: 8px;
      background: linear-gradient(135deg, {PRIMARY} 0%, {SECONDARY} 100%);
      display: inline-flex; align-items: center; justify-content: center;
      box-shadow: 0 4px 12px rgba(124,58,237,.35);
    }}
    .sidebar-subtitle {{
      margin: 0 6px 16px; color: {MUTED}; font-size: .74rem;
    }}
    .sidebar-divider {{
      border: 0; border-top: 1px solid {BORDER}; margin: 14px 6px;
    }}
    [data-testid="stRadio"] label {{
      display: flex; align-items: center; min-height: 40px;
      padding: 8px 12px; border: 1px solid transparent; border-radius: 10px;
      font-size: .9rem; font-weight: 500; color: #b6c1d3;
      transition: background .15s ease, border-color .15s ease, color .15s ease, transform .1s ease;
    }}
    [data-testid="stRadio"] label:hover {{
      background: rgba(124,58,237,.06); color: {TEXT};
    }}
    [data-testid="stRadio"] label:has(input:checked) {{
      background: linear-gradient(90deg, rgba(124,58,237,.18) 0%, rgba(124,58,237,.04) 100%);
      border-color: rgba(124,58,237,.5);
      color: #ede9fe;
      box-shadow: inset 3px 0 0 {PRIMARY};
    }}
    .sidebar-card {{
      background: rgba(255,255,255,.02);
      border: 1px solid {BORDER}; border-radius: 12px;
      padding: 11px 13px; margin: 6px 4px;
      backdrop-filter: blur(6px);
    }}
    .sidebar-card-title {{
      color: {MUTED}; font-size: .68rem; font-weight: 700;
      letter-spacing: .09em; text-transform: uppercase; margin-bottom: 9px;
    }}
    .status-row {{
      display: flex; align-items: center; gap: 8px;
      min-height: 22px; font-size: .8rem; color: #cbd5e1;
    }}
    .status-label {{ flex: 1; color: {MUTED}; }}
    .status-value {{ font-weight: 600; white-space: nowrap; font-variant-numeric: tabular-nums; }}
    .status-dot {{
      width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: {MUTED};
    }}
    .status-dot.ok {{ background: {PROFIT}; box-shadow: 0 0 8px rgba(16,185,129,.7); }}
    .status-dot.warn {{ background: {WARN}; box-shadow: 0 0 8px rgba(251,191,36,.7); }}
    .status-dot.err {{ background: {LOSS}; box-shadow: 0 0 8px rgba(244,63,94,.7); }}

    /* ---------- Top app bar ---------- */
    .appbar {{
      position: sticky; top: 0; z-index: 50;
      display: flex; align-items: center; gap: 12px;
      padding: 12px 16px; margin: -8px -16px 16px -16px;
      background: rgba(10,14,26,.7);
      backdrop-filter: blur(14px) saturate(140%);
      -webkit-backdrop-filter: blur(14px) saturate(140%);
      border-bottom: 1px solid {BORDER};
    }}
    .appbar-title {{
      font-size: 1.05rem; font-weight: 700; color: #f8fafc;
      letter-spacing: -.01em; display: flex; align-items: center; gap: 9px;
    }}
    .appbar-title .brand-mark {{
      width: 26px; height: 26px; border-radius: 8px;
      background: linear-gradient(135deg, {PRIMARY} 0%, {SECONDARY} 100%);
      display: inline-flex; align-items: center; justify-content: center;
      box-shadow: 0 4px 12px rgba(124,58,237,.4);
    }}
    .pill {{
      display: inline-flex; align-items: center; gap: 6px;
      padding: 4px 10px; border-radius: 999px; font-size: .74rem; font-weight: 600;
      background: rgba(255,255,255,.04); border: 1px solid {BORDER};
      color: #cbd5e1; white-space: nowrap;
    }}
    .pill.profit {{ background: rgba(16,185,129,.12); border-color: rgba(16,185,129,.4); color: #6ee7b7; }}
    .pill.loss {{ background: rgba(244,63,94,.12); border-color: rgba(244,63,94,.4); color: #fda4af; }}
    .pill.warn {{ background: rgba(251,191,36,.12); border-color: rgba(251,191,36,.4); color: #fcd34d; }}
    .pill.muted {{ color: {MUTED}; }}
    .pill-dot {{
      width: 7px; height: 7px; border-radius: 50%;
    }}
    .appbar-spacer {{ flex: 1; }}
    .appbar-meta {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}

    /* ---------- Hero P&L card ---------- */
    .hero {{
      position: relative; overflow: hidden;
      background:
        linear-gradient(135deg, rgba(124,58,237,.18) 0%, rgba(34,211,238,.08) 50%, transparent 100%),
        rgba(255,255,255,.02);
      border: 1px solid {BORDER}; border-radius: 18px;
      padding: 22px 24px; margin: 8px 0 16px 0;
      backdrop-filter: blur(8px);
    }}
    .hero::before {{
      content: ''; position: absolute; inset: 0; pointer-events: none;
      background: radial-gradient(600px 200px at 100% 0%, rgba(124,58,237,.18), transparent 60%);
    }}
    .hero-label {{
      font-size: .75rem; color: {MUTED}; text-transform: uppercase;
      letter-spacing: .12em; font-weight: 700;
    }}
    .hero-amount {{
      font-size: 2.6rem; font-weight: 800; line-height: 1.05;
      letter-spacing: -.02em; margin: 6px 0 4px 0;
      font-variant-numeric: tabular-nums;
    }}
    .hero-amount.profit {{ color: #34d399; text-shadow: 0 0 30px rgba(16,185,129,.25); }}
    .hero-amount.loss {{ color: #fb7185; text-shadow: 0 0 30px rgba(244,63,94,.25); }}
    .hero-amount.flat {{ color: {TEXT}; }}
    .hero-meta {{
      display: flex; gap: 14px; flex-wrap: wrap;
      font-size: .82rem; color: {MUTED}; margin-top: 4px;
    }}
    .hero-meta b {{ color: {TEXT}; font-weight: 600; }}

    /* ---------- Stat tiles (modernised) ---------- */
    .stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }}
    .stat-tile {{
      background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px;
      padding: 12px 14px; transition: border-color .15s ease, transform .12s ease, background .15s ease;
    }}
    .stat-tile:hover {{
      border-color: rgba(124,58,237,.4); background: {CARD_HOVER};
      transform: translateY(-1px);
    }}
    .stat-label {{
      color: {MUTED}; font-size: .7rem; text-transform: uppercase;
      letter-spacing: .08em; font-weight: 700;
    }}
    .stat-value {{
      color: {TEXT}; font-size: 1.2rem; font-weight: 700;
      margin-top: 4px; font-variant-numeric: tabular-nums;
    }}

    /* ---------- Badges + chips ---------- */
    .badge {{
      display: inline-block; border-radius: 999px; padding: 3px 10px;
      font-size: 0.75rem; font-weight: 600; letter-spacing: .01em;
    }}
    .chip {{
      display: inline-block; border-radius: 8px; padding: 3px 10px;
      font-size: 0.75rem; font-weight: 600; background: rgba(255,255,255,.04);
      color: {MUTED}; margin-right: 6px; border: 1px solid {BORDER};
    }}
    .chip-active {{
      background: rgba(124,58,237,.15); color: {PRIMARY_SOFT};
      border-color: rgba(124,58,237,.5);
    }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex-shrink: 0; }}
    @keyframes ks-pulse {{
      0% {{ box-shadow: 0 0 0 0 rgba(244,63,94,.55); }}
      70% {{ box-shadow: 0 0 0 10px rgba(244,63,94,0); }}
      100% {{ box-shadow: 0 0 0 0 rgba(244,63,94,0); }}
    }}
    .ks-dot {{
      width: 10px; height: 10px; border-radius: 50%; background: {LOSS};
      display: inline-block; animation: ks-pulse 1.5s infinite;
    }}
    .ks-banner {{
      border-radius: 12px; padding: 12px 16px; margin: 6px 0;
      border: 1px solid; font-weight: 600;
      background: linear-gradient(90deg, rgba(244,63,94,.15) 0%, rgba(244,63,94,.04) 100%);
    }}

    /* ---------- Position cards (open trades) ---------- */
    .row {{ display: flex; gap: 8px; align-items: center; }}
    .conf-bar {{ height: 6px; border-radius: 999px; background: {BORDER}; overflow: hidden; }}
    .conf-fill {{ height: 6px; border-radius: 999px; transition: width .3s ease; }}
    .sec-title {{
      color: {MUTED}; font-size: 0.78rem; text-transform: uppercase;
      letter-spacing: .1em; margin: 18px 0 8px 0; font-weight: 700;
      display: flex; align-items: center; gap: 8px;
    }}
    .sec-title::before {{
      content: ''; width: 3px; height: 14px; border-radius: 2px;
      background: linear-gradient(180deg, {PRIMARY}, {SECONDARY});
    }}
    .big-num {{ font-size: 1.6rem; font-weight: 700; font-variant-numeric: tabular-nums; }}
    .muted {{ color: {MUTED}; font-size: 0.85rem; }}
    .pos-card {{
      background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px;
      padding: 16px; margin: 10px 0;
      transition: border-color .15s ease, transform .12s ease;
    }}
    .pos-card:hover {{ border-color: rgba(124,58,237,.4); transform: translateY(-1px); }}
    .pos-card.up {{ border-left: 3px solid {PROFIT}; }}
    .pos-card.down {{ border-left: 3px solid {LOSS}; }}
    .pos-card.flat {{ border-left: 3px solid {MUTED}; }}

    /* ---------- Buttons ---------- */
    [data-testid="stButton"] button {{
      border-radius: 10px; font-weight: 600;
      transition: background .15s ease, border-color .15s ease,
                  box-shadow .15s ease, transform .05s ease;
    }}
    [data-testid="stButton"] button[kind="primary"] {{
      background: linear-gradient(135deg, {PRIMARY} 0%, #6d28d9 100%);
      border-color: transparent;
      box-shadow: 0 4px 14px rgba(124,58,237,.35);
    }}
    [data-testid="stButton"] button[kind="primary"]:hover {{
      box-shadow: 0 6px 20px rgba(124,58,237,.5);
      transform: translateY(-1px);
    }}
    [data-testid="stButton"] button[kind="primary"]:active {{ transform: translateY(0); }}
    [data-testid="stButton"] button[kind="secondary"] {{
      background: rgba(255,255,255,.04); border: 1px solid {BORDER};
    }}
    [data-testid="stButton"] button[kind="secondary"]:hover {{
      background: rgba(255,255,255,.06); border-color: rgba(124,58,237,.4);
    }}
    /* Lead Generate button: extra emphasis — bigger, glowing */
    .lead-toolbar .stButton > button[kind="primary"] {{
      min-height: 52px; font-size: 1.0rem;
      box-shadow: 0 6px 22px rgba(124,58,237,.45);
    }}

    /* SSO login + sidebar logout — same visual language as primary buttons */
    .sso-login-btn {{
      display: inline-flex; align-items: center; justify-content: center;
      gap: 8px; cursor: pointer; user-select: none;
      background: linear-gradient(135deg, {PRIMARY} 0%, #6d28d9 100%);
      color: #fff; padding: 0.65rem 1.1rem; border-radius: 12px;
      font-weight: 700; font-size: 1rem; line-height: 1.2;
      text-decoration: none; margin: 8px 0; width: 100%;
      border: none; box-shadow: 0 6px 20px rgba(124,58,237,.4);
      transition: box-shadow .15s ease, transform .1s ease;
    }}
    .sso-login-btn:hover {{
      box-shadow: 0 8px 26px rgba(124,58,237,.55);
      transform: translateY(-1px); color: #fff;
    }}
    .sso-login-btn:active {{ transform: translateY(0); }}
    .sso-login-btn:focus-visible {{ outline: 2px solid {PRIMARY_SOFT}; outline-offset: 2px; }}
    .sidebar-logout-btn {{
      display: flex; align-items: center; justify-content: center;
      gap: 8px; cursor: pointer; user-select: none;
      background: rgba(255,255,255,.04); color: {TEXT};
      border: 1px solid {BORDER}; border-radius: 12px;
      padding: 0 1rem; margin: 4px 0 6px 0; height: 44px;
      font-size: 1.0rem; font-weight: 600; line-height: 1.2;
      text-decoration: none; box-sizing: border-box;
      transition: background .15s ease, border-color .15s ease, color .15s ease;
    }}
    .sidebar-logout-btn:hover {{
      background: rgba(244,63,94,.08); border-color: rgba(244,63,94,.5); color: #fda4af;
    }}
    .sidebar-logout-btn:focus-visible {{ outline: 2px solid rgba(244,63,94,.5); outline-offset: 2px; }}

    /* Bigger sidebar controls */
    [data-testid="stSidebar"] [data-testid="stRadio"] label {{
      font-size: 0.92rem; padding: 9px 12px; border-radius: 10px;
    }}
    [data-testid="stSidebar"] [data-testid="stRadio"] div[role="radiogroup"] {{ gap: 3px; }}
    [data-testid="stSidebar"] [data-testid="stButton"] button {{
      height: 44px; font-size: 1.0rem; border-radius: 10px; font-weight: 600;
    }}
    [data-testid="stSidebar"] [data-testid="stExpander"] details {{ border-radius: 10px; }}

    /* Settings form labels — slightly heavier */
    [data-testid="stNumberInput"] label, [data-testid="stSlider"] label,
    [data-testid="stCheckbox"] label, [data-testid="stTextInput"] label,
    [data-testid="stTimeInput"] label, [data-testid="stSelectbox"] label,
    [data-testid="stMultiSelect"] label {{ font-weight: 500; }}

    /* ---------- Tables ---------- */
    div[data-testid="stDataFrame"] {{
      background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px;
      overflow: hidden;
    }}
    div[data-testid="stDataFrame"] table {{ font-size: 0.86rem; }}
    div[data-testid="stDataFrame"] th {{
      background: rgba(255,255,255,.02) !important;
      color: {MUTED} !important; font-weight: 700;
      text-transform: uppercase; font-size: 0.72rem; letter-spacing: .06em;
    }}

    /* Touch-friendly select + buttons + charts */
    div[data-baseweb="select"] > div {{ min-height: 38px; }}
    div[data-baseweb="select"] [role="option"] {{ min-height: 34px; }}
    button[kind="primary"], button[kind="secondary"] {{ min-height: 42px; padding: 8px 16px; }}
    .stPlotlyChart, .stLineChart, .stAreaChart, .stBarChart {{
      margin: 16px 0; width: 100% !important;
    }}

    /* ---------- History page ---------- */
    .history-section {{ padding: 0 4px; }}
    .history-divider {{ border: 0; border-top: 1px solid {BORDER}; margin: 18px 0; }}
    .history-mobile-header {{
      font-size: 0.85rem; font-weight: 700; color: {TEXT};
      text-transform: uppercase; letter-spacing: 0.08em;
      margin: 14px 0 8px 0; padding-bottom: 6px;
      border-bottom: 2px solid {PRIMARY};
    }}

    /* ---------- Leads page ---------- */
    .lead-toolbar {{
      display: flex; flex-direction: column; gap: 8px; margin: 4px 0 14px 0;
    }}
    .lead-section-header {{
      display: flex; align-items: center; justify-content: space-between;
      gap: 10px; margin: 18px 0 10px 0;
      padding-bottom: 8px; border-bottom: 1px solid {BORDER};
    }}
    .lead-section-title {{
      font-size: 1.0rem; font-weight: 700; color: {TEXT}; letter-spacing: -.01em;
    }}
    .lead-section-count {{
      background: rgba(124,58,237,.15); color: {PRIMARY_SOFT};
      border: 1px solid rgba(124,58,237,.5); border-radius: 999px;
      padding: 3px 12px; font-size: 0.78rem; font-weight: 700;
      font-variant-numeric: tabular-nums;
    }}
    .lead-section-count.warn {{
      background: rgba(251,191,36,.12); color: {WARN}; border-color: rgba(251,191,36,.5);
    }}
    .lead-card {{
      background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px;
      padding: 16px; margin: 10px 0;
      transition: border-color .15s ease, transform .12s ease;
    }}
    .lead-card:hover {{ border-color: rgba(124,58,237,.5); transform: translateY(-1px); }}
    .lead-card.queued {{ border-left: 3px solid {PRIMARY}; }}
    .lead-card.skipped {{ border-left: 3px solid {MUTED}; }}
    .lead-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }}
    .lead-head-left {{ flex: 1; min-width: 0; }}
    .lead-symbol {{ font-size: 1.05rem; font-weight: 700; color: #f8fafc; word-break: break-word; }}
    .lead-time {{ color: {MUTED}; font-size: 0.78rem; white-space: nowrap; flex-shrink: 0; }}
    .lead-chips {{ margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }}
    .lead-instrument {{
      margin-top: 10px; background: rgba(255,255,255,.02);
      border: 1px solid {BORDER}; border-radius: 10px;
      padding: 9px 11px; font-size: 0.84rem; color: #cbd5e1; word-break: break-word;
    }}
    .lead-instrument .lbl {{ color: {MUTED}; font-weight: 600; }}
    .lead-plan {{
      display: flex; flex-wrap: wrap; gap: 10px 16px;
      margin-top: 10px; padding-top: 10px;
      border-top: 1px dashed {BORDER};
    }}
    .lead-plan > div {{ font-size: 0.84rem; }}
    .lead-plan .lbl {{ color: {MUTED}; font-weight: 600; margin-right: 3px; }}
    .lead-plan .val {{ color: {TEXT}; font-weight: 600; }}
    .lead-score-row {{
      display: flex; align-items: center; gap: 10px;
      margin-top: 12px; padding-top: 10px;
      border-top: 1px dashed {BORDER};
    }}
    .lead-score-bar {{ flex: 1; }}
    .lead-score-meta {{ color: {MUTED}; font-size: 0.72rem; margin-top: 3px; display: flex; justify-content: space-between; }}
    .lead-note {{
      margin-top: 10px; padding: 9px 11px;
      background: rgba(244,63,94,.10); border: 1px solid rgba(244,63,94,.35);
      border-radius: 10px; color: #fecdd3; font-size: 0.82rem; word-break: break-word;
    }}

    /* Lead-gen live progress panel */
    .lg-panel {{
      background: linear-gradient(135deg, rgba(124,58,237,.10) 0%, rgba(34,211,238,.04) 100%);
      border: 1px solid rgba(124,58,237,.4); border-radius: 14px;
      padding: 14px 16px; margin: 6px 0 14px 0;
    }}
    .lg-head {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }}
    .lg-phase {{ display: flex; align-items: center; gap: 8px; color: {TEXT}; font-size: 0.92rem; }}
    .lg-elapsed {{ font-variant-numeric: tabular-nums; font-size: 0.85rem; color: {PRIMARY_SOFT}; }}
    .lg-spinner {{
      display: inline-block; width: 14px; height: 14px; border-radius: 50%;
      border: 2px solid {BORDER}; border-top-color: {PRIMARY};
      animation: lg-spin 0.9s linear infinite;
    }}
    @keyframes lg-spin {{ to {{ transform: rotate(360deg); }} }}
    .lg-bar {{ height: 6px; border-radius: 999px; background: rgba(255,255,255,.05); overflow: hidden; margin-bottom: 8px; }}
    .lg-fill {{ height: 6px; border-radius: 999px; transition: width 0.4s ease; }}
    .lg-meta {{ display: flex; gap: 14px; flex-wrap: wrap; font-size: 0.78rem; margin-bottom: 8px; color: {MUTED}; }}
    .lg-current {{ font-size: 0.84rem; margin: 4px 0 8px 0; color: {SECONDARY}; }}
    .lg-recent {{ display: flex; flex-direction: column; gap: 3px; border-top: 1px solid rgba(255,255,255,.05); padding-top: 8px; }}
    .lg-recent-row {{ display: flex; align-items: center; gap: 8px; font-size: 0.82rem; padding: 2px 0; }}

    /* LLM activity feed (live tool-call stream during agent loop) */
    .lg-tool-wrap {{
      margin: 10px 0 4px 0; padding-top: 8px;
      border-top: 1px dashed rgba(255,255,255,.08);
    }}
    .lg-tool-head {{
      display: flex; align-items: center; justify-content: space-between;
      font-size: 0.72rem; text-transform: uppercase; letter-spacing: .08em;
      margin-bottom: 6px;
    }}
    .lg-tool-list {{
      display: flex; flex-direction: column; gap: 4px;
    }}
    .lg-tool-row {{
      display: flex; align-items: center; gap: 8px;
      padding: 6px 9px; border: 1px solid; border-radius: 8px;
      font-size: 0.78rem; line-height: 1.3;
      transition: background .2s ease, border-color .2s ease;
    }}
    .lg-tool-pill {{
      background: rgba(124,58,237,.18); color: {PRIMARY_SOFT};
      border: 1px solid rgba(124,58,237,.4); border-radius: 999px;
      padding: 1px 9px; font-size: 0.72rem; font-weight: 700;
      letter-spacing: .02em; white-space: nowrap; flex-shrink: 0;
    }}
    .lg-tool-args {{
      flex: 1; min-width: 0;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      color: #cbd5e1; font-family: 'JetBrains Mono', 'SF Mono', monospace;
      font-size: 0.74rem;
    }}
    .lg-tool-sym {{
      background: rgba(34,211,238,.12); color: {SECONDARY};
      border: 1px solid rgba(34,211,238,.3); border-radius: 6px;
      padding: 1px 7px; font-size: 0.72rem; font-weight: 600;
      flex-shrink: 0;
    }}

    /* Gauges + small bits */
    .gauge {{ position: relative; width: 64px; height: 32px; overflow: hidden; }}
    .gauge-bg {{ position: absolute; bottom: 0; left: 0; right: 0; height: 32px; border-radius: 32px 32px 0 0; background: {BORDER}; }}
    .gauge-fill {{ position: absolute; bottom: 0; left: 0; right: 0; border-radius: 32px 32px 0 0; }}
    .gauge-num {{ position: relative; text-align: center; font-weight: 700; font-size: 0.85rem; padding-top: 4px; color: {TEXT}; }}

    /* ---------- Custom scrollbar ---------- */
    ::-webkit-scrollbar {{ width: 10px; height: 10px; }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    ::-webkit-scrollbar-thumb {{
      background: rgba(255,255,255,.08); border-radius: 999px;
      border: 2px solid transparent; background-clip: padding-box;
    }}
    ::-webkit-scrollbar-thumb:hover {{ background: rgba(124,58,237,.4); background-clip: padding-box; border: 2px solid transparent; }}

    /* ---------- Responsive ---------- */
    @media (max-width: 768px) {{
      .stApp {{ max-width: 100%; }}
      .hero {{ padding: 18px; }}
      .hero-amount {{ font-size: 2.1rem; }}
      .stat-tile {{ padding: 10px 12px; }}
      .stat-value {{ font-size: 1.05rem; }}
      .lead-card {{ padding: 13px; }}
      .lead-symbol {{ font-size: 1rem; }}
      .row {{ flex-wrap: wrap; gap: 12px; justify-content: flex-start; }}
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

def render_login():
    """Modern fintech landing page shown when no SSO session is present.

    Streamlit's stApp container is reused, but we override the page background
    (full-bleed radial gradient) and centre a glassmorphism hero card.
    """
    st.markdown(_login_css(), unsafe_allow_html=True)

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

    # Real <a target="_top"> — a user click on a real link is the only
    # top-frame navigation pattern that works inside Streamlit's sandboxed
    # iframe. JS-clicking a hidden top-doc anchor is silently dropped by
    # modern browsers (no allow-top-navigation).
    features_html = "".join(
        f"<li><span class='login-feat-dot'></span>"
        f"<div><b>{_html_escape(title)}</b>"
        f"<div class='login-feat-sub'>{_html_escape(sub)}</div></div></li>"
        for title, sub in [
            ("LLM in the loop", "Breakout detector calls real-time tools for indicators, news, and option chain."),
            ("Risk first", "Every position has a bot SL with a tight ratchet trail; a one-tap killswitch squares off."),
            ("Full audit trail", "Every SL change, fill, and broker mismatch is logged for post-trade review."),
        ]
    )

    st.markdown(
        f"""
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
            <a href='{api.login_url()}' target='_top' class='sso-login-btn login-cta'>
              <svg width='18' height='18' viewBox='0 0 24 24' fill='none'
                   xmlns='http://www.w3.org/2000/svg' aria-hidden='true'>
                <path d='M12 2L3 7v6c0 5 3.8 9.4 9 11 5.2-1.6 9-6 9-11V7l-9-5z'
                      stroke='currentColor' stroke-width='2' stroke-linejoin='round'/>
                <path d='M9 12l2 2 4-4' stroke='currentColor' stroke-width='2'
                      stroke-linecap='round' stroke-linejoin='round'/>
              </svg>
              Continue with Upstox SSO
            </a>
            <div class='login-divider'>
              <span>What you get</span>
            </div>
            <ul class='login-features'>{features_html}</ul>
            <div class='login-foot'>
              You'll be redirected to Upstox, then back here.
              Your tokens never touch this dashboard's storage.
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _login_css() -> str:
    """Page-specific CSS for the login surface.

    Kept separate from the main `_css()` so the dashboard styling doesn't
    bleed in. The login card uses the same design tokens (PRIMARY, SECONDARY,
    CARD, BORDER) so the two surfaces feel like the same product.
    """
    return f"""
    <style>
    .stApp {{
      background:
        radial-gradient(900px 600px at 18% 12%, rgba(124,58,237,.35), transparent 55%),
        radial-gradient(700px 500px at 88% 88%, rgba(34,211,238,.22), transparent 55%),
        radial-gradient(500px 400px at 50% 0%, rgba(244,63,94,.10), transparent 60%),
        linear-gradient(180deg, {BG_GRAD_TOP} 0%, {BG} 60%);
      min-height: 100vh;
    }}
    [data-testid="stHeader"], footer, #MainMenu {{ display: none !important; }}

    .login-shell {{
      display: flex; align-items: center; justify-content: center;
      min-height: calc(100vh - 80px); padding: 24px 16px;
    }}
    .login-card {{
      width: 100%; max-width: 460px;
      background: rgba(255,255,255,.03);
      backdrop-filter: blur(18px) saturate(150%);
      -webkit-backdrop-filter: blur(18px) saturate(150%);
      border: 1px solid {BORDER};
      border-radius: 22px;
      padding: 32px 30px;
      box-shadow:
        0 24px 60px rgba(0,0,0,.45),
        inset 0 1px 0 rgba(255,255,255,.04);
    }}
    .login-brand {{
      display: flex; align-items: center; gap: 12px; margin-bottom: 16px;
    }}
    .login-mark {{
      width: 42px; height: 42px; border-radius: 12px;
      background: linear-gradient(135deg, {PRIMARY} 0%, {SECONDARY} 100%);
      box-shadow: 0 8px 24px rgba(124,58,237,.45);
      position: relative;
    }}
    .login-mark::after {{
      content: ''; position: absolute; inset: 8px;
      background: rgba(255,255,255,.18);
      clip-path: polygon(50% 8%, 92% 50%, 50% 92%, 8% 50%);
      border-radius: 4px;
    }}
    .login-name {{
      font-size: 1.6rem; font-weight: 800; letter-spacing: -.02em;
      color: #f8fafc;
    }}
    .login-tagline {{
      color: #cbd5e1; font-size: 1.05rem; line-height: 1.45;
      margin-bottom: 22px; font-weight: 500;
    }}
    .login-grad {{
      background: linear-gradient(90deg, {PRIMARY_SOFT} 0%, {SECONDARY} 100%);
      -webkit-background-clip: text; background-clip: text; color: transparent;
      font-weight: 600;
    }}
    .login-cta {{
      margin: 6px 0 0 0 !important;
      padding: 0.85rem 1.1rem !important;
      font-size: 1.02rem !important;
      display: inline-flex !important;
      align-items: center; justify-content: center;
      gap: 10px;
    }}
    .login-error {{
      display: flex; align-items: center; gap: 8px;
      padding: 10px 12px; margin: 4px 0 14px 0;
      background: rgba(244,63,94,.10);
      border: 1px solid rgba(244,63,94,.4); border-radius: 10px;
      color: #fecdd3; font-size: 0.86rem;
    }}
    .login-error-dot {{
      width: 8px; height: 8px; border-radius: 50%;
      background: {LOSS};
      box-shadow: 0 0 8px rgba(244,63,94,.6);
      flex-shrink: 0;
    }}
    .login-divider {{
      display: flex; align-items: center; gap: 12px;
      margin: 22px 0 14px 0; color: {MUTED}; font-size: 0.74rem;
      text-transform: uppercase; letter-spacing: .12em; font-weight: 700;
    }}
    .login-divider::before, .login-divider::after {{
      content: ''; flex: 1; height: 1px; background: {BORDER};
    }}
    .login-features {{
      list-style: none; padding: 0; margin: 0;
      display: flex; flex-direction: column; gap: 10px;
    }}
    .login-features li {{
      display: flex; align-items: flex-start; gap: 11px;
      padding: 11px 13px;
      background: rgba(255,255,255,.025);
      border: 1px solid {BORDER}; border-radius: 12px;
    }}
    .login-feat-dot {{
      width: 8px; height: 8px; border-radius: 50%;
      background: linear-gradient(135deg, {PRIMARY} 0%, {SECONDARY} 100%);
      flex-shrink: 0; margin-top: 7px;
      box-shadow: 0 0 8px rgba(124,58,237,.4);
    }}
    .login-features b {{
      color: {TEXT}; font-weight: 600; font-size: 0.93rem;
    }}
    .login-feat-sub {{
      color: {MUTED}; font-size: 0.82rem; margin-top: 2px; line-height: 1.4;
    }}
    .login-foot {{
      margin-top: 18px; text-align: center;
      color: {MUTED}; font-size: 0.78rem; line-height: 1.5;
    }}
    @media (max-width: 540px) {{
      .login-card {{ padding: 26px 22px; border-radius: 18px; }}
      .login-name {{ font-size: 1.4rem; }}
      .login-tagline {{ font-size: 0.98rem; }}
    }}
    </style>
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
        # Real <a target="_top"> — a user click on a real link is the only
        # top-frame navigation pattern that works inside Streamlit's sandboxed
        # iframe (JS-clicking a hidden top-doc anchor is silently dropped).
        st.markdown(
            f'<a href="{api.logout_url()}" target="_top" class="sidebar-logout-btn" '
            f'title="End the Upstox session and clear tokens">Logout</a>',
            unsafe_allow_html=True,
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
    st.markdown(
        f'<a href="{api.login_url()}" target="_top" class="sso-login-btn">'
        f'Login with Upstox</a>',
        unsafe_allow_html=True,
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
                        "Signal Price": r["signal_level"],
                        "Conf": r["confidence"],
                        "Margin": _money(r.get("margin_needed")) if r.get("margin_needed") is not None else "—",
                        "Status": r["status"],
                    }
                    for r in rows
                ]
            )
            st.dataframe(df, use_container_width=True, hide_index=True, height=min(40 + 35 * len(df), 420))
        else:
            st.info("No queued signals today. Head to the **Leads** tab and tap **Generate now** to scan underlyings.")


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


def render_leads():
    st.subheader("Leads")

    # The lead_cleanup scheduler keeps this view current:
    # - Queued leads age out at 24h
    # - Processed leads (skipped/placed/expired) retained for 7 days

    # Fetch the lead list once and reuse it for both the toolbar (to size
    # the destructive-action confirmation) and the section rendering.
    resp = api.get_leads()
    if resp.get("status") != "ok":
        st.warning("Could not load leads.")
        return
    rows = resp["data"].get("leads", [])
    lead_count = len(rows)

    # Action buttons in their own column so each stays full-width on mobile
    # (a side column would shrink to ~20% of the screen width).
    _html("<div class='lead-toolbar'>")
    _render_generate_lead_button("leads_tab")
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
    _html("</div>")

    if st.session_state.pop("purge_dialog_open", False):
        _render_purge_leads_dialog(lead_count)

    result = st.session_state.pop("purge_leads_last_result", None)
    if result:
        kind, payload = result
        if kind == "success":
            st.success(f"Deleted {payload} lead row(s).")
        else:
            st.error(payload)

    _lead_gen_status_fragment()

    if not rows:
        st.info("No leads. Tap **Generate now**, or enable more underlyings in **Instruments**.")
        return

    # Split into active (queued) and skipped
    active_rows = [r for r in rows if r.get("status") == "queued"]
    skipped_rows = [r for r in rows if r.get("status") == "skipped"]

    # --- Active Leads ---
    _html(
        f"<div class='lead-section-header'>"
        f"<div class='lead-section-title'>Active leads (queued)</div>"
        f"<div class='lead-section-count'>{len(active_rows)} waiting</div>"
        f"</div>"
    )
    if active_rows:
        for r in active_rows:
            _render_lead_card(r, show_note=False)
            _render_lead_detail_expander(r)
    else:
        _html("<div class='muted' style='padding:6px 2px;'>No active queued leads right now.</div>")

    # --- Skipped Leads (with reasons) ---
    _html(
        f"<div class='lead-section-header'>"
        f"<div class='lead-section-title'>Skipped leads</div>"
        f"<div class='lead-section-count warn'>{len(skipped_rows)}</div>"
        f"</div>"
    )
    if skipped_rows:
        for r in skipped_rows:
            _render_lead_card(r, show_note=True)
            _render_lead_detail_expander(r)
    else:
        _html("<div class='muted' style='padding:6px 2px;'>No skipped leads in retention window.</div>")


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


def _render_lead_card(r: dict, show_note: bool = False):
    """Render a single lead card. Single-column mobile-first layout."""
    created_ist = (
        r.get("created_at_ist_label")
        or _utc_to_ist_hm(r.get("created_at"))
    )
    direction_class = "up" if r["direction"] == "CALL" else "down"
    status_class = "warn" if r["status"] == "queued" else "muted"
    card_class = "queued" if r["status"] == "queued" else "skipped"

    symbol = r.get("symbol") or r["underlying"].split("|")[-1]

    # Confidence score
    pct = int((r.get("confidence") or 0) * 100)
    bar_color = PROFIT if pct >= 80 else (WARN if pct >= 60 else MUTED)
    badge_color = "ok" if pct >= 80 else ("warn" if pct >= 60 else "muted")
    score_label = "High" if pct >= 80 else ("Medium" if pct >= 60 else "Low")

    # Build plan details as key/value pairs for clean wrap-friendly layout
    plan_pairs = []
    if r.get("expiry"):
        plan_pairs.append(("Expiry", r["expiry"]))
    if r.get("strike_price"):
        plan_pairs.append(("Strike", _num(r["strike_price"])))
    if r.get("option_type"):
        plan_pairs.append(("Opt", r["option_type"]))
    if r.get("quantity"):
        plan_pairs.append(("Qty", str(r["quantity"])))
    if r.get("lot_size"):
        plan_pairs.append(("Lot", str(r["lot_size"])))
    if r.get("premium") is not None:
        plan_pairs.append(("Premium", _num(r["premium"])))
    if r.get("spot") is not None:
        plan_pairs.append(("Spot", _num(r["spot"])))
    if r.get("margin_needed") is not None:
        plan_pairs.append(("Margin", f"₹{float(r['margin_needed']):,.0f}"))

    plan_html = "".join(
        f"<div><span class='lbl'>{lbl}</span><span class='val'>{val}</span></div>"
        for lbl, val in plan_pairs
    )

    instrument_html = _lead_plan_line(r)

    note_html = ""
    if show_note and r.get("note"):
        note_html = (
            f"<div class='lead-note'>⚠ <b>Skipped:</b> {r['note']}</div>"
        )
    elif r.get("note") and not show_note:
        note_html = (
            f"<div class='lead-note' style='background:rgba(34,211,238,.08);"
            f"border-color:rgba(34,211,238,.35);color:#a5f3fc;'>"
            f"ℹ {r['note']}</div>"
        )

    _html(
        f"""
        <div class='lead-card {card_class}'>
          <div class='lead-head'>
            <div class='lead-head-left'>
              <div class='lead-symbol'>{symbol}</div>
              <div class='lead-chips'>
                {_badge(r['direction'], direction_class)}
                {_badge(r['status'], status_class)}
                <span class='badge' style='color:{SECONDARY};background:rgba(34,211,238,.15);'>{r['signal_type']}</span>
                <span class='badge' style='color:{PRIMARY};background:rgba(99,102,241,.15);'>Signal: {_num(r['signal_level'])}</span>
              </div>
            </div>
            <div class='lead-time'>{created_ist} IST</div>
          </div>
          {instrument_html}
          {f"<div class='lead-plan'>{plan_html}</div>" if plan_pairs else ""}
          <div class='lead-score-row'>
            <span class='badge {badge_color}' style='flex-shrink:0;'>{pct}%</span>
            <div class='lead-score-bar'>
              <div class='conf-bar'>
                <div class='conf-fill' style='width:{pct}%;background:{bar_color};'></div>
              </div>
              <div class='lead-score-meta'><span>Confidence</span><span>{score_label}</span></div>
            </div>
          </div>
          {note_html}
        </div>
        """
    )


def _render_lead_detail_expander(r: dict) -> None:
    """"Why this lead?" expander — shows the LLM rationale + tool-call log.

    Pulls the full lead detail (meta + trade linkage) lazily on first click
    so the dashboard stays snappy. Falls back to the slim meta returned by
    /api/trades/leads if the per-id fetch fails.
    """
    lead_id = r.get("id")
    if not lead_id:
        return
    with st.expander(
        f"Why this lead? (LLM rationale + tool calls)",
        expanded=False,
    ):
        with st.spinner("Loading lead detail…"):
            resp = api.get_lead_detail(int(lead_id))
        if resp.get("status") != "ok":
            # Fall back to the slim meta from the list payload.
            meta = r.get("meta") or {}
            _render_lead_detail_meta(meta, r)
            st.caption("(Detail endpoint unavailable — showing slim summary.)")
            return
        data = resp.get("data") or {}
        meta = data.get("meta") or {}
        _render_lead_detail_meta(meta, data)
        # If the lead was placed, also show the trade row.
        trade = data.get("trade")
        if trade:
            _render_trade_for_lead(trade)


def _render_lead_detail_meta(meta: dict, fallback: dict) -> None:
    """Render the slim meta block (rationale + tool calls + indicators)."""
    rationale = meta.get("llm_rationale")
    if rationale:
        st.markdown("**LLM rationale**")
        st.info(str(rationale))
    tool_calls = meta.get("llm_tool_calls") or []
    if tool_calls:
        st.markdown("**Tool calls (agent loop)**")
        rows_html = []
        for c in tool_calls[-10:]:
            name = c.get("name") or "?"
            args = c.get("args")
            res_keys = list((c.get("result") or {}).keys())[:4]
            rows_html.append({
                "tool": name,
                "args": str(args)[:120] if args else "",
                "result_keys": ", ".join(res_keys) if res_keys else "",
            })
        st.dataframe(rows_html, use_container_width=True, hide_index=True)
    indicators = meta.get("indicators") or {}
    if indicators:
        st.markdown("**Indicators (slim)**")
        ind_rows = [{"key": k, "value": v} for k, v in indicators.items()]
        st.dataframe(ind_rows, use_container_width=True, hide_index=True)
    if not (rationale or tool_calls or indicators):
        # Slim fallback: show whatever signal/confidence info we DO have.
        sig_level = fallback.get("signal_level")
        conf = fallback.get("confidence")
        bits = []
        if sig_level is not None:
            bits.append(f"Signal level: **{_num(sig_level)}**")
        if conf is not None:
            bits.append(f"Confidence: **{int(conf * 100)}%**")
        st.caption(" · ".join(bits) or "No rationale captured for this lead.")


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
    """
    _recover_in_flight_lead_job()
    job = st.session_state.get("lead_job")
    is_running = _is_job_running(job)
    button_label = "Generating…" if is_running else "Generate now"
    clicked = st.button(
        button_label,
        key=f"gen_lead_btn_{key_suffix}",
        type="primary",
        use_container_width=True,
        disabled=is_running,
        help=(
            "A lead-generation run is already in progress."
            if is_running else
            "Run the lead generator manually (works outside trading hours)."
        ),
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
        "leads": ("✓", PROFIT),
        "empty": ("·", MUTED),
        "cap":   ("·", WARN),
        "error": ("✗", LOSS),
    }

    def _recent_row(item: dict) -> str:
        sym = item.get("symbol") or "?"
        st_name = item.get("status") or "empty"
        glyph, color = status_glyph.get(st_name, ("?", MUTED))
        n = int(item.get("leads") or 0)
        if st_name == "leads":
            tail = f" <span style='color:{PROFIT};font-weight:600;'>{n} lead{'s' if n != 1 else ''}</span>"
        elif st_name == "error":
            err_msg = item.get("error") or ""
            tail = f" <span style='color:{LOSS};font-size:0.78rem;'>{_html_escape(err_msg[:60])}</span>"
        else:
            tail = ""
        return (
            f"<div class='lg-recent-row'>"
            f"<span style='color:{color};font-weight:700;width:14px;flex-shrink:0;'>{glyph}</span>"
            f"<span style='flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'>{_html_escape(sym)}</span>"
            f"<span style='flex-shrink:0;'>{tail}</span>"
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

    # Live LLM tool-call activity. `current_tool_calls` is appended by
    # `lead_jobs._on_tool_call` every time the agent loop finishes a tool
    # execution. Render the last 6, newest-first, with a tiny status pill
    # so the user can see what the model is reasoning about in real time.
    tool_calls = progress.get("current_tool_calls") or []
    tool_html = _render_llm_tool_calls(tool_calls)

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
          {tool_html}
          <div class='lg-recent'>{rows_html}</div>
        </div>
        """
    )


def _render_llm_tool_calls(tool_calls: list[dict]) -> str:
    """Render the live LLM agent-loop activity feed.

    Each entry is the compact payload emitted by
    `agent.run_agent_loop` via `on_tool_call`. We show the tool name as a
    pill, the (truncated) args, and which underlying the LLM was reasoning
    about — newest event at the top.
    """
    if not tool_calls:
        return ""
    # Friendlier labels for the registered tools. Anything not in the map
    # falls back to its raw name so future tools don't go missing silently.
    tool_labels = {
        "compute_indicators": "indicators",
        "breakout_calc": "breakout calc",
        "fetch_news": "news",
        "option_chain_summary": "option chain",
    }

    def _row(tc: dict, idx: int) -> str:
        name = tc.get("name") or "?"
        label = tool_labels.get(name, name)
        sym = tc.get("symbol") or ""
        args = tc.get("args") or ""
        iter_n = tc.get("iter")
        # First row gets a slightly stronger tint to mark "most recent".
        is_latest = idx == 0
        bg = "rgba(124,58,237,.14)" if is_latest else "rgba(255,255,255,.03)"
        border = "rgba(124,58,237,.5)" if is_latest else "rgba(255,255,255,.05)"
        sym_html = (
            f"<span class='lg-tool-sym'>{_html_escape(sym)}</span>" if sym else ""
        )
        iter_html = (
            f"<span class='muted'>iter {iter_n}</span>" if iter_n is not None else ""
        )
        return (
            f"<div class='lg-tool-row' style='background:{bg};border-color:{border};'>"
            f"<span class='lg-tool-pill'>{_html_escape(label)}</span>"
            f"<span class='lg-tool-args'>{_html_escape(str(args)[:80])}</span>"
            f"{sym_html}{iter_html}"
            f"</div>"
        )

    rows = "".join(_row(tc, i) for i, tc in enumerate(tool_calls[:6]))
    return (
        f"<div class='lg-tool-wrap'>"
        f"<div class='lg-tool-head'>"
        f"<span class='muted'>LLM activity</span>"
        f"<span class='muted' style='font-weight:600;'>{len(tool_calls)} call{'s' if len(tool_calls) != 1 else ''}</span>"
        f"</div>"
        f"<div class='lg-tool-list'>{rows}</div>"
        f"</div>"
    )


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
        # 404 — the server trimmed the registry. Drop the local handle so
        # the button re-enables on the next render.
        st.session_state.pop("lead_job", None)
        return

    data = resp["data"]
    status = data.get("status")
    # Cache the server-side status on the session-state entry so the
    # `_is_job_running()` check on the main render loop stays in sync
    # without re-polling.
    st.session_state["lead_job"]["status"] = status

    if status == "running":
        _render_lead_progress_panel(data)
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
    elif status == "error":
        st.error(f"Generation failed: {data.get('error') or 'unknown error'}")
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
    st.markdown("<div class='history-mobile-header'>🛰️ Post-trade monitoring</div>",
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
        st.warning("Could not load drift events.")
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
