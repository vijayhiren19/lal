"""Flask application factory for the Stock Scrip Scoring API.

Usage:
    python -m src.api.app
    python -m src.api.app --host 0.0.0.0 --port 5000
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml
from flask import Flask, redirect, url_for

from config import ROOT_DIR
from db.connection import init_schema

logger = logging.getLogger("runner")


def load_columns_config():
    """Load column registry from config/columns.yaml."""
    config_path = ROOT_DIR / "config" / "columns.yaml"
    if not config_path.exists():
        logger.warning("columns.yaml not found at %s", config_path)
        return {"columns": []}
    with open(config_path, "r") as f:
        return yaml.safe_load(f) or {"columns": []}


def create_app():
    """Flask application factory.

    Loads column registry, initializes DB schema + stock_universe view,
    registers blueprints.
    """
    app = Flask(__name__)

    app.config.from_mapping(
        DEBUG=True,
        COLUMNS_CONFIG=load_columns_config(),
    )

    # Initialize database schema (creates tables + stock_universe view)
    init_schema()

    # Register blueprints under /api/v1
    from src.api.routes_stocks import stocks_bp
    from src.api.routes_pipeline import pipeline_bp
    from src.api.routes_meta import meta_bp

    app.register_blueprint(stocks_bp, url_prefix="/api/v1")
    app.register_blueprint(pipeline_bp, url_prefix="/api/v1")
    app.register_blueprint(meta_bp, url_prefix="/api/v1")

    # Root redirect for discoverability
    @app.route("/")
    def index():
        return redirect("/api/v1/health")

    @app.route("/health")
    def health_redirect():
        return redirect("/api/v1/health")

    return app


def _parse_args(argv=None):
    """Parse CLI args for the API server."""
    parser = argparse.ArgumentParser(description="Stock Scrip Scoring API")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5000, help="Port to bind to")
    return parser.parse_args(argv)


def main():
    """CLI entry point for running the API server."""
    args = _parse_args()
    app = create_app()
    logger.info("Starting API server at http://%s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=True)


if __name__ == "__main__":
    main()
