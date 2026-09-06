"""API rate limiting (Flask-Limiter).

The shared Limiter is bound to the app via `init_app` inside create_app, which
configures it from the `RATELIMIT_*` app-config keys (set from our env config).
"""

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)