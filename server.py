"""Entry point for the Flask backend.

Run:  python server.py
Then open http://127.0.0.1:5050 in your browser.
"""
import logging
import logging.handlers
import os

from oi_dashboard.app import create_app

HOST, PORT = "127.0.0.1", 5050


def configure_logging():
    os.makedirs("logs", exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        "logs/dashboard.log", maxBytes=10_000_000, backupCount=5
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


app = create_app()

if __name__ == "__main__":
    configure_logging()
    print("=" * 50)
    print("  Nifty OI Dashboard -- Flask Server")
    print("=" * 50)
    print(f"  Dashboard: http://{HOST}:{PORT}/")
    print(f"  API:       http://{HOST}:{PORT}/api/data")
    print("  Press Ctrl+C to stop")
    print("=" * 50)
    app.run(host=HOST, port=PORT, debug=False)
