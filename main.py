#!/usr/bin/env python3

"""Main script: runs the NiB proxy FastAPI application."""

import logging
import pathlib

import environ
import uvicorn

env = environ.Env()
BASE_DIR = pathlib.Path(__file__).parent
environ.Env.read_env(str(BASE_DIR / ".env"))

DEBUG = env.bool("DEBUG", default=False)

logging.basicConfig(level=(logging.DEBUG if DEBUG else logging.INFO))

logger = logging.getLogger(__name__)


def start() -> None:
    """Start the application."""
    host = env.str("HOST", default="0.0.0.0")  # noqa: S104
    port = env.int("PORT", default=8000)
    uvicorn.run(
        "nib_proxy.app:app",
        host=host,
        port=port,
        reload=DEBUG,
        log_level="debug" if DEBUG else "info",
    )


if __name__ == "__main__":
    start()
