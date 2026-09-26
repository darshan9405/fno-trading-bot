"""API blueprint registration."""

from flask import Flask

from app.api.auth_api import bp as auth_bp
from app.api.config_api import bp as config_bp
from app.api.drift_api import bp as drift_bp
from app.api.health_api import bp as health_bp
from app.api.instrument_api import bp as instrument_bp
from app.api.killswitch_api import bp as killswitch_bp
from app.api.trade_api import bp as trade_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(auth_bp)
    app.register_blueprint(trade_bp)
    app.register_blueprint(killswitch_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(instrument_bp)
    app.register_blueprint(drift_bp)