from app import create_app
from app.scheduler.manager import init_scheduler, shutdown_scheduler

app = create_app()

# Start the three trading schedulers (single process). Gunicorn runs 1 worker,
# so this runs exactly once per container.
init_scheduler()

if __name__ == "__main__":
    import atexit

    atexit.register(shutdown_scheduler)
    app.run(host="0.0.0.0", port=8000, debug=False)