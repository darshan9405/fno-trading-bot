import pytest
from datetime import date

from sqlalchemy import select
from app.db import Base, _run_sqlite_migrations, create_engine, init_db, sessionmaker
from app.models import (
    AuthToken,
    ErrorLog,
    FundsSnapshot,
    Instrument,
    KillSwitch,
    Lead,
    OptionContract,
    Order,
    OrderFill,
    SchedulerHeartbeat,
    Setting,
    Trade,
)

pytestmark = pytest.mark.usefixtures("db")


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def seed(db):
    nifty = Instrument(
        symbol="NIFTY",
        exchange="NSE",
        segment="NSE_INDEX",
        spot_instrument_key="NSE_INDEX|Nifty 50",
        instrument_token="26000",
        trading_symbol="NIFTY",
        lot_size=50,
        tick_size=0.05,
        chart_interval="day",
        enabled=True,
    )
    reliance = Instrument(
        symbol="RELIANCE",
        exchange="NSE",
        segment="NSE_EQ",
        spot_instrument_key="NSE_EQ|INE002A01018",
        instrument_token="2885",
        trading_symbol="RELIANCE",
        lot_size=1250,
        tick_size=0.05,
        chart_interval="day",
        enabled=True,
    )
    db.add_all([nifty, reliance])
    db.flush()

    lead_ce = Lead(
        instrument_id=nifty.id,
        underlying_key="NSE_INDEX|Nifty 50",
        direction="CALL",
        strategy="breakout",
        signal_type="horizontal_range",
        signal_level=26800.0,
        confidence=0.82,
        status="placed",
    )
    lead_pe = Lead(
        instrument_id=reliance.id,
        underlying_key="NSE_EQ|INE002A01018",
        direction="PUT",
        strategy="breakout",
        signal_type="head_shoulders",
        signal_level=2985.0,
        confidence=0.74,
        status="queued",
    )
    lead_skip = Lead(
        instrument_id=reliance.id,
        underlying_key="NSE_EQ|INE002A01018",
        direction="CALL",
        strategy="breakout",
        signal_type="flag_pennant",
        signal_level=3050.0,
        confidence=0.51,
        status="expired",
    )
    db.add_all([lead_ce, lead_pe, lead_skip])
    db.flush()

    trade_open = Trade(
        lead_id=lead_ce.id,
        underlying_key="NSE_INDEX|Nifty 50",
        option_instrument_key="NSE_FO|84123",
        option_instrument_token="84123",
        tradingsymbol="NIFTY 10 SEP 26 26800 CE",
        lot_size=50,
        product="D",
        direction="CALL",
        entry_price=245.00,
        quantity=50,
        initial_sl=220.50,
        current_sl=273.60,
        trail_state="trailing",
        best_price=288.00,
        status="open",
    )
    trade_closed = Trade(
        lead_id=lead_pe.id,
        underlying_key="NSE_EQ|INE002A01018",
        option_instrument_key="NSE_FO|90111",
        option_instrument_token="90111",
        tradingsymbol="RELIANCE 10 SEP 26 3000 PE",
        lot_size=1250,
        product="D",
        direction="PUT",
        entry_price=120.00,
        quantity=1250,
        initial_sl=132.00,
        current_sl=132.00,
        trail_state="at_initial",
        status="closed",
        exit_price=108.00,
        exit_reason="sl_hit",
        realized_pnl=-15000.00,
    )
    db.add_all([trade_open, trade_closed])
    db.flush()

    order_entry = Order(
        order_id="o-22014",
        trade_id=trade_open.id,
        order_request_id="1",
        exchange_order_id="1100000012345",
        status="complete",
        status_message="OK",
        order_type="MARKET",
        variety="regular",
        transaction_type="BUY",
        product="D",
        price=0.0,
        trigger_price=0.0,
        average_price=245.00,
        quantity=50,
        filled_quantity=50,
        instrument_token="NSE_FO|84123",
        tradingsymbol="NIFTY 10 SEP 26 26800 CE",
        exchange="NSE",
        validity="DAY",
        is_amo=False,
        tag="trade-201",
    )
    db.add(order_entry)
    db.flush()

    db.add(
        OrderFill(
            upstox_trade_id="t-5001",
            order_id=order_entry.order_id,
            exchange_order_id="1100000012345",
            instrument_token="NSE_FO|84123",
            tradingsymbol="NIFTY 10 SEP 26 26800 CE",
            transaction_type="BUY",
            quantity=50,
            average_price=245.00,
            order_type="MARKET",
            product="D",
            exchange="NSE",
        )
    )

    db.add_all(
        [
            FundsSnapshot(available_margin=184500.00, used_margin=12800.00, span_margin=12000.00, exposure_margin=800.00, notional_cash=5000.00),
            OptionContract(
                underlying_key="NSE_INDEX|Nifty 50",
                expiry=date(2026, 9, 10),
                strike_price=26800.00,
                instrument_key="NSE_FO|84123",
                lot_size=50,
                call_or_put="CE",
            ),
            AuthToken(token_type="refresh", token_hash="sha256:9f2cab1d", user_id="upstox-usr-8841", expires_at=date(2026, 9, 11)),
            KillSwitch(reason="manual - market crash", triggered_by="user", active=True),
            ErrorLog(source="scheduler.order_placer", message="Order rejected — insufficient margin"),
            Setting(key="initial_sl_pct", value="10.0"),
            Setting(key="patterns_enabled", value='["horizontal_range","trendline"]'),
            SchedulerHeartbeat(scheduler="lead_generator", status="ok", note="ran 3 instruments"),
        ]
    )
    db.commit()
    return {
        "nifty": nifty,
        "reliance": reliance,
        "lead_ce": lead_ce,
        "lead_pe": lead_pe,
        "trade_open": trade_open,
        "trade_closed": trade_closed,
        "order_entry": order_entry,
    }


def test_whitelist_instruments(seed, db):
    instruments = db.execute(select(Instrument)).scalars().all()
    assert {i.symbol for i in instruments} == {"NIFTY", "RELIANCE"}
    nifty = next(i for i in instruments if i.symbol == "NIFTY")
    assert nifty.spot_instrument_key == "NSE_INDEX|Nifty 50"
    assert nifty.segment == "NSE_INDEX"
    assert nifty.lot_size == 50


def test_leads_and_trade_link(seed, db):
    leads = db.execute(select(Lead)).scalars().all()
    assert len(leads) == 3
    placed = next(l for l in leads if l.signal_type == "horizontal_range")
    assert placed.direction == "CALL"
    assert placed.signal_level == 26800.0
    assert placed.strategy == "breakout"
    assert placed.trade is not None and placed.trade.status == "open"
    assert placed.instrument.symbol == "NIFTY"


def test_trade_pnl_and_sl(seed, db):
    trades = db.execute(select(Trade)).scalars().all()
    assert len(trades) == 2
    closed = next(t for t in trades if t.status == "closed")
    assert closed.exit_reason == "sl_hit"
    assert closed.realized_pnl == -15000.00
    assert closed.quantity == 1250
    assert closed.initial_sl == 132.00  # legacy fixture value, retained for audit


def test_orders_and_fills_audit(seed, db):
    orders = db.execute(select(Order)).scalars().all()
    assert len(orders) == 1
    o = orders[0]
    assert o.order_id == "o-22014"
    assert o.tag == "trade-201"
    assert o.trade is not None
    assert len(o.fills) == 1
    assert o.fills[0].upstox_trade_id == "t-5001"
    assert o.fills[0].average_price == 245.00


def test_sqlite_migration_adds_plan_column():
    from sqlalchemy import text

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE leads (id INTEGER PRIMARY KEY, "
                "instrument_id INTEGER, underlying_key VARCHAR(64), direction VARCHAR(8), "
                "strategy VARCHAR(32), signal_type VARCHAR(32), signal_level FLOAT, "
                "confidence FLOAT, chart_interval VARCHAR(16), status VARCHAR(16), "
                "note TEXT, created_at DATETIME, processed_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO leads (id, instrument_id, underlying_key, direction, signal_level) "
                "VALUES (1, 1, 'NSE_INDEX|Nifty 50', 'CALL', 26800.0)"
            )
        )

    _run_sqlite_migrations(engine)

    with engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(leads)")}
        assert "plan" in cols
        row = conn.execute(text("SELECT plan FROM leads WHERE id = 1")).fetchone()
        assert row[0] is None  # pre-existing rows get NULL

    engine.dispose()


def test_option_cache_unique(seed, db):
    db.add(
        OptionContract(
            underlying_key="NSE_INDEX|Nifty 50",
            expiry=date(2026, 9, 10),
            strike_price=26800.00,
            instrument_key="NSE_FO|99999",
            lot_size=50,
            call_or_put="CE",
        )
    )
    with pytest.raises(Exception):
        db.commit()


def test_settings_and_heartbeats(seed, db):
    settings = db.execute(select(Setting)).scalars().all()
    assert len(settings) == 2
    assert any(s.key == "initial_sl_pct" and s.value == "10.0" for s in settings)
    beats = db.execute(select(SchedulerHeartbeat)).scalars().all()
    assert beats[0].scheduler == "lead_generator"
    assert beats[0].status == "ok"


def test_killswitch_and_funds(seed, db):
    ks = db.execute(select(KillSwitch)).scalar_one()
    assert ks.active is True
    fs = db.execute(select(FundsSnapshot)).scalar_one()
    assert fs.available_margin == 184500.00


def test_error_log_surfaces_message(seed, db):
    err = db.execute(select(ErrorLog)).scalar_one()
    assert err.source == "scheduler.order_placer"
    assert "insufficient margin" in err.message