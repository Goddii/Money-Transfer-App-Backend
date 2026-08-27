import os

from app import create_app

app = create_app()


def _debug_enabled():
    """Enable the Flask debug server only when explicitly requested.

    Production deployments run under Gunicorn with debug disabled; the debug
    reloader must never be active in production.
    """

    return os.environ.get("FLASK_DEBUG", "0") == "1" or os.environ.get(
        "FLASK_ENV"
    ) == "development"


if __name__ == '__main__':
    app.run(debug=_debug_enabled())
