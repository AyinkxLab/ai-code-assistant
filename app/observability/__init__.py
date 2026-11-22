"""Prometheus metrics: registry, request counters and the /metrics route."""

from __future__ import annotations

from flask import Flask, request

from .enterprise import record_from_request
from .metrics import record_request, record_usage
from .routes import bp

__all__ = ["init_observability", "record_usage"]


def init_observability(app: Flask) -> None:
    app.register_blueprint(bp)

    @app.after_request
    def _count_request(response):
        rule = request.url_rule.rule if request.url_rule else "unmatched"
        record_request(request.method, rule, response.status_code)
        record_from_request(request, response)
        return response
