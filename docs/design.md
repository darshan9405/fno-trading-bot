# F&O Automated Trading System — Low-Level Design (Stage 1)

**Version:** 1.0 · **Date:** 2026-09-05

---

## 1. Overview

Automated **intraday F&O trading system** for the Indian market. Detects
**breakout patterns on the daily chart of the underlying (spot)**, places
option trades on whitelisted F&O symbols, manages a trailing stop-loss, and
auto-squares-off at 14:00 IST. Single user, deployed on AWS EC2.

- **Backend:** Flask + APScheduler + SQLite (SQLAlchemy)
- **UI:** Streamlit (mobile-first, polls the backend)
- **Broker:** Upstox (official `upstox_client` SDK)
- **Scope:** intraday only; **no overnight carry**; max **1 trade per instrument per day**
- **Trading window:** 10:00–14:00 IST

---

## 2. Architecture

```
                     +-----------------------------------------------+
                     |                 AWS EC2                        |
                     |                                               |
   Mobile/Desktop    |   +----------------+       +--------------+   |
   (Streamlit UI) ---+-->|  Flask Backend |<----->|   SQLite     |   |
        (HTTPS)      |   |  (REST API)    |       |  (persistent) |  |
                     |   +-------+--------+       +--------------+   |
                     |           |                                  |
                     |   +-------v---------+                        |
                     |   |  APScheduler    |                        |
                     |   |  - lead_generator                       |
                     |   |  - trade_tracker                        |
                     |   |  - order_placer                          |
                     |   +-------+---------+                        |
                     |           |                                  |
                     |   +-------v---------+      +--------------+   |
                     |   |  Strategy Engine |---->| Lead queue    |  |
                     |   |  (pluggable,    |      | (DB-backed)   |  |
                     |   |   e.g. breakout)|      +--------------+   |
                     |   +-----------------+                        |
                     |           |                                  |
                     |   +-------v---------+                        |
                     |   |  Broker layer   |                        |
                     |   |  UpstoxBroker   |----> Upstox REST API   |
                     |   |                |----> Upstox WebSocket  |
                     |   +-----------------+                        |
                     +-----------------------------------------------+
```

Three independently scheduled jobs coordinate via the SQLite `leads` /
`trades` tables (no shared-memory coupling), so the system survives restarts.

---

## 3. Actors

| Actor | Description |
|---|---|
| **User** | Single trader. Interacts via Streamlit UI: login (Upstox SSO), killswitch, view open trades / leads / live P&L / system health. |
| **Lead Generator (S1)** | Runs 10:00–14:00, fetches daily candles of whitelisted underlyings, runs the configured **strategy** (via `StrategyRegistry`), writes `leads`. |
| **Order Placer (S3)** | Polls queued leads, validates constraints, resolves option contract, places entry + initial SL order, creates `trade`. |
| **Trade Tracker (S2)** | Polls open trades: updates P&L, applies trailing-SL rule (via `modify_order`), auto square-off at 14:00, marks closed. |
| **Killswitch (manual)** | User-triggered. Squares off all open trades, blocks new orders, persists state for the day. |
| **Upstox** | Broker. Source of truth for orders, positions, funds, candles, option chain. |

---

## 4. Module breakdown

```
fno-trading-bot/
├── app/
│   ├── __init__.py            # Flask app factory
│   ├── config.py              # env-driven config
│   ├── db.py                  # SQLAlchemy engine/session
│   ├── models.py              # Stage 1: ORM schema (12 tables)
│   ├── settings.py            # runtime settings table + get/set/seed
│   ├── auth.py                # Stage 3: JWT issue/verify, daily key rotation
│   ├── broker/
│   │   ├── base.py            # Stage 2: BrokerBase ABC + neutral types
│   │   └── upstox_broker.py   # Stage 2: Upstox SDK implementation (no mock)
│   ├── strategy/              # pluggable lead-generation strategies
│   │   ├── base.py            # Strategy ABC, LeadCandidate, StrategyRegistry
│   │   ├── breakout/          # Stage 9 — the breakout engine
│   │   │   ├── __init__.py    # BreakoutStrategy (empty generate until Stage 9)
│   │   │   ├── swing.py  horizontal.py  trendline.py
│   │   │   ├── triangle.py  flag_pennant.py  head_shoulders.py
│   │   │   └── detector.py    # orchestrates patterns -> candidates
│   │   └── (future) momentum.py, gap_fade.py, ...
│   ├── scheduler/
│   │   ├── manager.py         # APScheduler wiring
│   │   ├── lead_generator.py  # Scheduler 1 (strategy-agnostic, calendar sync)
│   │   ├── trade_tracker.py   # Scheduler 2 (trailing + session-end sq-off)
│   │   └── order_placer.py    # Scheduler 3
│   ├── services/
│   │   ├── market_calendar.py  killswitch_service.py  trade_service.py
│   │   ├── recon_service.py    lead_service.py        health_service.py
│   └── api/
│       ├── auth_api.py  trade_api.py  killswitch_api.py
│       ├── health_api.py  config_api.py  common.py
│   ├── strategy/
│   │   ├── base.py            # Strategy ABC + registry
│   │   └── breakout/          # swing + horizontal/trendline/triangle/flag/H&S detectors
│   ├── broker/                # base.py + upstox_broker.py
├── ui/
│   ├── app.py                 # Streamlit (SSO, tabs, polling)
│   └── api_client.py          # cookie-first auth + endpoints
├── backtests/validate_patterns.py     # Stage 9 validation
├── docs/design.md
├── tests/
├── requirements.txt  .env.example  run_backend.py  run_ui.py
```

---

## 5. Data model

12 tables. Column source = the verified Upstox SDK model each field maps to
(see the *Schema → Upstox mapping* section below).

### 5.1 `instruments` — whitelisted underlyings (pattern detection only)

| column | type | notes |
|---|---|---|
| id | int PK | |
| symbol | str | `NIFTY`, `RELIANCE`, … |
| exchange | str | `NSE` |
| segment | str | `NSE_INDEX` \| `NSE_EQ` (spot) |
| spot_instrument_key | str UQ | `NSE_INDEX\|Nifty 50` / `NSE_EQ\|INE…` — used for candles & option chain |
| instrument_token | str | v2 numeric token |
| trading_symbol | str | |
| lot_size | int | F&O lot (from `InstrumentData.lot_size`) |
| tick_size | float | |
| chart_interval | str | `day` (default) |
| enabled | bool | Seeded **false**; operator enables from the UI (`/api/instruments`). Only enabled rows are scanned. |
| created_at / updated_at | datetime | |

**Auto-seeding (no manual step):** the backend Docker entrypoint
(`deploy/entrypoint.sh`) runs `scripts/seed_instruments.py` before gunicorn
starts. It downloads the Upstox instrument master
(`assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz`), builds the
universe of every F&O optionable underlying (FO lot-size joined to the
EQ/INDEX spot row), and **upserts** — new rows are inserted `enabled=false`,
existing rows get metadata refreshed while **preserving the operator's enabled
flag** (a re-seed never re-disables a choice). The seed is idempotent and
non-fatal: if the download fails at boot, S1 retries via
`seed_instruments_if_empty()` (gated by `AUTO_SEED_INSTRUMENTS=1`). The
whitelist is managed entirely from the **Instruments** page in the UI.

**Rule:** breakouts are detected on the **spot** series (`NSE_INDEX` index or
`NSE_EQ` equity), **never** on the `NSE_FO` contract series.

### 5.2 `leads` — strategy signals

| column | type | notes |
|---|---|---|
| id | int PK | |
| instrument_id | FK→instruments | |
| underlying_key | str | |
| direction | str | `CALL` \| `PUT` |
| strategy | str | strategy that produced it (`breakout`, …) |
| signal_type | str | strategy-specific label (`horizontal_range`, `head_shoulders`, `gap_fade`, …) |
| signal_level | float | the trigger price |
| confidence | float | signal-fit score 0–1 |
| chart_interval | str | |
| status | str | `queued` → `picked` → `placed` \| `skipped` \| `expired` |
| note | str null | skip/expire reason |
| created_at / processed_at | datetime | |

One lead per instrument per day (dedup by `instrument_id` + date).

### 5.3 `trades` — managed intraday option trades

| column | type | notes |
|---|---|---|
| id | int PK | |
| lead_id | FK→leads, UQ | 1 trade ↔ 1 lead |
| underlying_key | str | |
| option_instrument_key | str | `NSE_FO\|…` (order leg) |
| option_instrument_token | str | v2 token for position reconcile |
| tradingsymbol | str | |
| lot_size | int | |
| product | str | `I` (intraday) |
| direction | str | `CALL` \| `PUT` |
| entry_price / quantity | float/int | |
| initial_sl / current_sl | float | |
| trail_state | str | `at_initial` → `breakeven` → `trailing` |
| best_price | float null | extreme favorable price (high for CALL, low for PUT) |
| status | str | `open` → `closed` \| `sqoff` \| `killed` |
| entry_time / exit_time | datetime | |
| exit_price / exit_reason | float/str | `sl_hit` \| `trailing_sl` \| `sqoff` \| `killswitch` \| `manual` |
| realized_pnl | float null | per-trade realized P&L (audit) |
| created_at / updated_at | datetime | |

### 5.4 `orders` — order audit (mirrors `OrderData`)

`order_id` PK; `trade_id` FK; `order_request_id`, `exchange_order_id`,
`status`, `status_message`, `order_type` (MARKET/LIMIT/SL/SL-M), `variety`,
`transaction_type` (BUY/SELL), `product`, `price`, `trigger_price`,
`average_price`, `quantity`, `filled_quantity`, `instrument_token`,
`tradingsymbol`, `exchange`, `validity`, `is_amo`, `tag` (=`trade-<id>`),
`order_timestamp`, `exchange_timestamp`, `created_at`.

### 5.5 `order_fills` — fill audit (mirrors `TradeData`)

`id` PK, `upstox_trade_id`, `order_id` FK, `exchange_order_id`,
`instrument_token`, `tradingsymbol`, `transaction_type`, `quantity`,
`average_price`, `order_type`, `product`, `exchange`, `exchange_timestamp`.

### 5.6 `option_cache` — cached option contracts per underlying (no premium)

| column | type | notes |
|---|---|---|
| id | int PK | |
| underlying_key / expiry | str/date | |
| strike_price | float | |
| instrument_key | str | the `NSE_FO` contract to order |
| lot_size | int | |
| call_or_put | str | `CE` \| `PE` |
| cached_at | datetime | |
| UQ | | (underlying_key, expiry, strike_price, call_or_put) |

Minimal by design — **no premium/greeks stored**; populated via
`OptionsApi.get_option_contracts` (InstrumentData). Entry uses market orders;
strike selection by ATM from `underlying_spot_price`.

### 5.7 `funds_snapshots`

`id`, `ts`, `available_margin` (**live balance**), `used_margin`,
`span_margin`, `exposure_margin`, `notional_cash`.

### 5.8 `killswitches`

`id`, `triggered_at`, `reason`, `triggered_by` (`user`\|`system`), `released_at`,
`active`.

### 5.9 `auth_tokens`

`id`, `token_type`, `token_hash` (UQ), `user_id`, `created_at`, `expires_at`,
`revoked`. `token_type` is one of:
- `refresh` — opaque refresh tokens, **sha256-hashed**
- `bootstrap` — one-time codes (60s TTL) handed to the UI after SSO
- `upstox` — the raw Upstox access token, stored **Fernet-encrypted**; `expires_at` = the next **3:30 AM IST** (Upstox standard access tokens expire then, regardless of issue time)

### 5.10 `settings`

`key` PK, `value` (JSON-encoded). See §9.

### 5.11 `scheduler_heartbeats`

`scheduler` PK (`lead_generator`\|`trade_tracker`\|`order_placer`),
`last_run_at`, `status`, `note`.

### 5.12 `errors`

`id`, `ts`, `source`, `message`, `stack` — consumed by the health API.

### 5.13 `market_holidays` / `special_sessions`

Trading calendar (weekends are handled in code):

- `market_holidays` — `date` PK, `note`. Non-trading days. Seeded with fixed
  civil holidays (Republic Day, Maharashtra Day, Independence Day, Gandhi
  Jayanti, Christmas); **synced daily from Upstox** `get_holidays()` (NSE
  `closed_exchanges`), which also covers lunar/misc holidays.
- `special_sessions` — `date` PK, `start`/`end` ("HH:MM" IST), `note`.
  Per-day trading-window override for special/half-day sessions (manual;
  Upstox `open_exchanges` days fall back to the configured default window).

`app/services/market_calendar.py` is the single source of truth:
`trading_hours(day)`, `is_market_open(now)`, `session_start/end(day)`,
`sync_from_broker(broker)` (once per day via S1), `seed_defaults()`.

### 5.14 Schema → Upstox mapping (verified from SDK)

| our table | Upstox SDK source |
|---|---|
| `instruments` | `InstrumentsApi.search_instrument` → `InstrumentData` |
| `option_cache` | `ExpiredInstrumentApi.get_expiries` + `OptionsApi.get_option_contracts` → `InstrumentData` |
| `orders` | `OrderApiV3.place_order`/`modify_order` + `OrderApi.get_order_book` → `OrderData` |
| `order_fills` | `OrderApi.get_trades_by_order` → `TradeData` |
| `trades.pnl` (live) | `PortfolioApi.get_positions` → `PositionData` (`unrealised`, `realised`, `pnl`, `multiplier`) |
| `funds_snapshots` | `UserApi.get_user_fund_margin` → `UserFundMarginData` |
| `market_holidays` | `MarketHolidaysAndTimingsApi.get_holidays` → `HolidayData.closed_exchanges` (NSE) |
| candles (transient) | `HistoryApi.get_historical_candle_data1(..., interval='day')` → `HistoricalCandleData.candles` (raw arrays, parsed to pandas) |

---

## 6. API contracts (Flask, JSON; all JWT-protected except auth)

| Method | Path | Body / Query | Response |
|---|---|---|---|
| GET | `/api/auth/status` | — | `{authenticated, user_id}` |
| GET | `/api/auth/upstox/login` | — | 302 → Upstox OAuth dialog |
| GET | `/api/auth/upstox/callback` | `?code=` | sets JWT+refresh cookies; 302 → `{FRONTEND_URL}?bootstrap=` |
| POST | `/api/auth/bootstrap` | `{code}` (one-time) | `{access_token, refresh_token, expires_in}` |
| POST | `/api/auth/refresh` | cookie or `X-Refresh-Token` | rotated `{access_token, refresh_token}` |
| POST | `/api/auth/logout` | cookie or `X-Refresh-Token` | revokes refresh, clears cookies |
| POST | `/api/killswitch/activate` | `{reason?}` | `{active: true}` |
| POST | `/api/killswitch/release` | — | `{active: false}` |
| GET | `/api/killswitch/status` | — | `{active, triggered_at, reason}` |
| GET | `/api/trades/open` | — | `[{trade,…}]` |
| GET | `/api/trades/closed` | `?limit&from&to` | `[{trade,…}]` |
| GET | `/api/trades/pnl` | — | `{unrealised, realised, total, available_margin, ts}` |
| GET | `/api/leads` | `?date` | `[{lead,…}]` |
| GET | `/api/health` | — | `{heartbeats, error_count, broker (incl. token_valid_until/token_expired/token_near_expiry), market}` |
| GET | `/api/config` | — | current settings |
| PUT | `/api/config` | settings diff | updated settings |

Unified response envelope: `{status: "ok"|"error", data: …, error: {code, message}}`.

**Rate limiting (Flask-Limiter).** All APIs are rate-limited per client IP
(real IP via nginx `X-Forwarded-For` + `ProxyFix`):
- default `60/min`; UI auto-polled endpoints (`/api/health`,
  `/api/trades/open`, `/api/trades/pnl`) get `120/min`; auth endpoints are
  tightened (`/api/auth/bootstrap` 5/min, `/refresh`/`/logout` 10/min,
  `/upstox/login` 20/min). Over limit → `429` with the error envelope
  (`code: "rate_limited"`). Config: `RATE_LIMIT_ENABLED`, `RATE_LIMIT_DEFAULT`,
  `RATE_LIMIT_STORAGE_URI` (default in-memory).

---

## 7. Schedulers

Three APScheduler jobs in the Flask process; each writes a heartbeat row.

**Market calendar awareness.** "Market open" is decided by
`app/services/market_calendar.py`, not just the clock: weekdays only,
no NSE holidays (`market_holidays`, synced once per day from Upstox
`get_holidays()`), and per-day window overrides (`special_sessions`). The
trade tracker squares off at that day's `session_end` (so half-days are
respected).

| Job | Cadence | Window | Work |
|---|---|---|---|
| **S1 lead_generator** | every 15 min | market open | sync calendar (once/day) → fetch daily candles (spot) → run configured **strategy** → insert `leads` (status `queued`) |
| **S2 trade_tracker** | every 30 s | market open | refresh LTP of open trades; apply trailing rule; **modify_order** when SL moves; square-off at the day's session end; update P&L; close trades |
| **S3 order_placer** | every 30 s | market open | pop queued leads → validate → resolve option → place entry + SL → create `trade` |

**Strategy interface (S1 is strategy-agnostic).** `app/strategy/base.py` defines
`Strategy.generate(instrument, candles, now) -> list[LeadCandidate]` plus a
`StrategyRegistry` (name → class). S1 reads the global `strategy` setting,
instantiates `StrategyRegistry.get(name)`, fetches candles at the strategy's
`required_interval`, and persists returned candidates as `leads`. New
strategies are just registered subclasses — schedulers, schema, and UI don't
change. A contract test (`tests/test_strategy_contract.py`) enforces the
interface for every registered strategy.

### 7.1 Order-placer validation (S3)

1. killswitch inactive AND market open
2. no open trade for that underlying today
3. current price within `max_lead_price_divergence_pct` of `signal_level`
4. option expiry ≥ `min_days_to_expiry` days away (`get_expiries`)
5. strike selection: `ATM` (default) or `ITM_0.5` config; contract from `option_cache`
6. place entry (`MARKET`) first; only if it fills does it place the protective SL (`SL-M`, trigger = `entry ± initial_sl_pct`), `product='D'`, `tag='trade-<id>'`
7. on failure: mark lead `skipped` + log `errors`

### 7.2 Trailing SL rule (S2) — configurable

```
entry_price  → initial_sl = entry_price * (1 -+ initial_sl_pct)   # - for CALL, + for PUT
favorable move >= trail_activate_pct  →  move SL to breakeven (trail_state=breakeven)
after breakeven:
    for CALL:  new_sl = max(current_sl, best_price * (1 - trail_gap_pct))
    for PUT :  new_sl = min(current_sl, best_price * (1 + trail_gap_pct))
if new_sl moved by >= order_tick → broker.modify_order(ModifyOrderParams(order_id, quantity, trigger_price=new_sl, order_type="SL-M"))
```

Trailing is executed by **S2 via `OrderApiV3.modify_order`** (v3 `ModifyOrderRequest`
requires the full SL-order spec — quantity/validity/price/order_type/trigger_price),
not GTT — Upstox GTT `trailing_gap` has a minimum of 10% of (LTP − SL), too coarse
for a 5% trail. GTT remains an optional redundant exit.

---

## 8. Authentication (Stage 3)

Cookie-based SSO mechanism (per requirement):

1. UI loads → calls `GET /api/auth/status` → **no valid cookie set** → UI
   redirects to `GET /api/auth/upstox/login` → 302 to the Upstox OAuth dialog.
2. Upstox redirects to the callback with `?code=`; backend exchanges via
   `LoginApi.token(...)` (client_id/secret/redirect_uri/grant_type) → Upstox
   **access token** (stored Fernet-encrypted in `auth_tokens`, injected into the
   broker).
3. Backend sets two **HttpOnly cookies**:
   - `upstox_at` — short-lived **access JWT** (15 min) with claim
     `day = YYYY-MM-DD`. Signing key = `HMAC-SHA256(JWT_MASTER_SECRET, day)` →
     **key rotates daily**; yesterday's tokens are rejected.
   - `upstox_rt` — opaque **refresh token** (7-day TTL, hashed in `auth_tokens`,
     path-scoped to `/api/auth`).
   Then 302 → `{FRONTEND_URL}?bootstrap=<one-time code>`.
4. **UI hand-off** (Streamlit calls the API server-side, where browser cookies
   aren't visible): the UI exchanges the one-time bootstrap code via
   `POST /api/auth/bootstrap` → stores `{access_token, refresh_token}` in
   `st.session_state`, clears the query param, and sends
   `Authorization: Bearer <jwt>` on every API call (the decorator accepts header
   **or** cookie).
5. On 401 (`token_expired`) → `POST /api/auth/refresh` (header or cookie) →
   **refresh token rotation** (old revoked, new issued). If refresh fails →
   re-run SSO.

`@jwt_required` reads `Authorization: Bearer <jwt>` first, then the
`upstox_at` cookie; both paths share the same daily-rotating verification.

**Upstox token lifetime.** Upstox standard access tokens expire at **3:30 AM IST
the following day** — there is no long-lived trading token (extended/analytics
tokens are read-only). Every Upstox SSO login refreshes the stored token
(expiry recorded in `auth_tokens.expires_at`), so the operational loop is simply
to log in each trading morning. The health API surfaces `token_valid_until`,
`token_expired`, `token_near_expiry`; the UI raises a banner **only when the
token is expired** (the bot cannot trade) with a one-click re-login link —
expiry info is otherwise shown in the sidebar badge and Health tab.

---

## 9. Runtime settings (`settings` table)

| key | default | purpose |
|---|---|---|
| `strategy` | `breakout` | global lead-generation strategy name |
| `trading_start` | `10:00` | S1/S3 window start |
| `sqoff_time` | `14:00` | S2 auto square-off |
| `initial_sl_pct` | `10.0` | initial SL % from entry |
| `trail_activate_pct` | `5.0` | favorable move % to trigger breakeven |
| `trail_gap_pct` | `5.0` | trailing distance from `best_price` |
| `max_lead_price_divergence_pct` | `0.5` | max price drift at placement |
| `min_days_to_expiry` | `5` | option expiry filter |
| `strike_selection` | `ATM` | `ATM` \| `ITM_0.5` |
| `qty_lots_per_trade` | `1` | lot multiples |
| `breakout.patterns_enabled` | all 6 | breakout-strategy pattern toggles |
| `breakout.min_confidence` | `0.7` | min signal confidence to emit a lead |
| `breakout.volume_multiplier` | `4.0` | volume-spike threshold (× rolling avg) |
| `breakout.volume_window` | `20` | rolling-average volume window |
| `breakout.volume_lookback` | `5` | bars to check for a spike |
| `breakout.require_volume_spike` | `true` | hard-require a volume spike |
| `breakout.volume_boost` | `0.15` | confidence boost when a spike is present |
| `market_calendar_last_sync_date` | — | last day holidays were synced from Upstox |

---

## 10. Killswitch (Stage 5) — two layers

Our killswitch is a **system-level** halt, distinct from Upstox's broker-level
kill switch (which is **not** used).

1. **App flag** (`killswitches` active row): set **first and unconditionally**,
   so the system halts even if square-off fails. Persists across restarts.
2. **Square off (best-effort):** each open trade is exited in its **own
   transaction** (per-trade isolation) via `broker.exit_all(tag="trade-<id>")`
   / explicit opposite order; failures are caught, logged, and reported —
   one bad trade never rolls back the others or blocks the flag.

`activate()` returns `{active: true, squared_off: [...], failed: [...]}` even
on partial failure. Scope: **managed trades only** — manual / carry-forward
positions are never touched. Release clears the app flag.

## 10a. Position reconciliation

`app/services/recon_service.py` syncs DB trade status to the broker's real
positions: if a DB-`open` trade has **no live position at the broker** (SL hit,
manual exit, killswitch, or an order that executed without our DB update), it is
closed in the DB with the best-available exit price (SL fill → LTP → current
SL). Runs automatically each `trade_tracker` cycle and manually via
`POST /api/trades/recon`. Guards:
- per-trade transaction isolation (failures don't roll back others),
- **min-age 5 min** before a trade can be reconciled (avoids closing a freshly
  opened trade whose position hasn't propagated yet).

---

## 11. Breakout detection engine (Stage 9 — core problem)

Implemented as the **`breakout` strategy** (`app/strategy/breakout/`), conforming
to the `Strategy` interface. Built in-house on pandas/numpy (informed by
PatternPy / chart_patterns / numta approaches), fully parameterised:

1. **Swing points** — fractal: a bar is a swing high/low if it is the extreme
   within `k` bars each side (`k` from settings).
2. **Detectors** (each toggleable):
   - **horizontal_range** — N-day high/low band; signal when price within
     `proximity_pct` of the band edge. Above high → CALL; below low → PUT.
   - **trendline** — regression through last M swing lows (support) / highs
     (resistance); close crossing the line = breakout.
   - **triangle** — converging best-fit lines through swing highs & lows
     (symmetric/ascending/descending); close exits the envelope.
   - **flag_pennant** — steep pole then tight consolidation; breakout in pole
     direction (continuation).
   - **head_shoulders** — 3 swing highs, middle highest, shoulders within
     tolerance; close below neckline → PUT; inverse → CALL.
   - **volume_breakout** *(from Durgia 2025, "Algorithmic Breakout Detection
     Via Volume Spike Analysis")* — a **volume spike** (≥ `volume_multiplier` ×
     the `volume_window`-bar rolling average, within the last `volume_lookback`
     bars) coinciding with a price break of recent highs → CALL / lows → PUT.
3. **Filters** — "exactly at level" enforced via `proximity_pct` band (no
   chasing); confidence = pattern-fit quality.
4. **Volume confirmation** — the paper's core: volume spikes filter false
   breakouts. `breakout.require_volume_spike` makes a spike **mandatory** for
   any signal; otherwise a spike adds `breakout.volume_boost` to confidence.
5. **Output** → `LeadCandidate(direction, signal_type, signal_level, confidence)`.
6. **Validation** — `backtests/validate_patterns.py`: run detectors over
   historical daily data, report matches + forward returns, tune false-positive
   rate before live.

---

## 12. Deployment — single EC2, Docker Compose + Cloudflare Tunnel

One EC2, nothing publicly exposed. Cloudflare Tunnel terminates TLS at the
edge; `cloudflared` is the only outward path.

```
 browser ─ HTTPS ─► Cloudflare edge ─ tunnel ─► cloudflared ─► nginx (127.0.0.1:80)
                                                       /api/* → backend:8000
                                                       /      → ui:8501 (websocket)
```

**Stack (`docker-compose.yml`):** `backend` (Flask/gunicorn, DB on `fno-data`
volume), `ui` (Streamlit), `nginx` (localhost-only ingress, passes through
`X-Forwarded-Proto` for Flask `ProxyFix`), `cloudflared`.

**One-time setup (host):**
```bash
cloudflared tunnel login
cloudflared tunnel create fno            # -> ~/.cloudflared/<TUNNEL_ID>.json
cloudflared tunnel route dns fno trade.example.com
cp deploy/cloudflared-config.yml.example ~/.cloudflared/config.yml   # fill TUNNEL_ID/HOSTNAME
```

**Config:** `FRONTEND_URL` / `UPSTOX_REDIRECT_URI` = `https://trade.example.com`
(must match the Upstox app registration), `COOKIE_SECURE=true`.

**Notes:**
- A **named tunnel** is required — the Upstox redirect URI must be a stable hostname.
- Backend runs **1 gunicorn worker** (`--threads 4`): SQLite single-writer plus
  the in-process schedulers and the in-memory Upstox-token store must live in
  one process.
- Secrets via `.env` (gitignored); optionally wrap the tunnel with Cloudflare Access.
- SQLite WAL mode; single-user so single-writer is fine.

---

## 13. Testing strategy

| level | approach |
|---|---|
| Unit (models) | `tests/test_models.py` — schema, relationships, constraints (in-memory SQLite) |
| Broker | `tests/test_broker.py` — SDK-call mapping + normalization with monkeypatched API instances (no network) |
| Auth | `tests/test_auth.py` — JWT daily rotation, SSO→cookies→bootstrap→refresh/logout |
| Patterns | `tests/test_patterns.py` — synthetic OHLC with embedded patterns |
| Schedulers | `tests/test_schedulers.py` — lead gen, order placement, trailing/SL/square-off, holidays/special sessions |
| Calendar | `tests/test_market_calendar.py` — weekends/holidays/special sessions/sync |
| API | `tests/test_api.py` — killswitch, trades, P&L, health, config (JWT-guarded) |
| Strategy contract | `tests/test_strategy_contract.py` — every registered strategy conforms |
| Integration | `scripts/upstox_integration.py` + `tests/integration` — live production API matrix (needs `UPSTOX_INTEGRATION_TOKEN`); order lifecycle opt-in via `UPSTOX_LIVE_ORDER=1` |
| Real-data signals | `backtests/validate_patterns.py --upstox` — run detectors on real Upstox candles pre-market |
| Pipeline dry-run | `scripts/dry_run.py --scenario {profit,sl_hit}` — full S1→S3→S2 simulation offline (SimBroker, simulated clock) |
| Validation | `backtests/validate_patterns.py` — synthetic demo + user CSV |
| Pre-live | small-qty live validation for ≥1 trading week before sizing up |

---

## 14. Stage roadmap (from plan.md)

1. ✅ **Stage 1** — LLD + schema + API contracts; `Strategy` interface + registry
2. ✅ **Stage 2** — Broker interface + Upstox SDK implementation (no mock)
3. ✅ **Stage 3** — SSO auth + daily-rotating JWT + cookies + refresh
4. ✅ **Stage 4** — Schedulers (S1/S2/S3) + strategy wiring + market calendar
5. ✅ **Stage 5** — Killswitch API (squares managed trades only)
6. ✅ **Stage 6** — P&L / open / closed trades / leads APIs
7. ✅ **Stage 7** — Health + config APIs
8. ✅ **Stage 8** — Streamlit UI (SSO, cookie-first auth, live tabs)
9. ✅ **Stage 9** — Breakout strategy engine (5 detectors) + validation script
10. 🔄 **Stage 10** — Integration + Docker/Cloudflare stack built; **remaining**: live-account validation, trailing tuning, then go-live