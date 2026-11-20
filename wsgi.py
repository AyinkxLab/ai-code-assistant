"""WSGI entry point for production servers (e.g. gunicorn).

Run with::

    gunicorn --bind 0.0.0.0:5000 wsgi:app
"""

from app import create_app

app = create_app()
