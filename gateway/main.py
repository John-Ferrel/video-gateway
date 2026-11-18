"""Entrypoint: `python -m gateway.main` or `video-gateway`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .config import Settings, parse_env_file


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Video Gateway")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--env-file", default=os.getenv("ENV_FILE", ".env"))
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--no-dispatcher", action="store_true", help="API only (for tests/debugging)")
    args = parser.parse_args(argv)

    parse_env_file(Path(args.env_file))
    settings = Settings.from_env()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.log_level:
        settings.log_level = args.log_level.upper()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app(settings, start_dispatcher=not args.no_dispatcher)
    if settings.api_key_file:
        logging.getLogger(__name__).warning(
            "Generated API key is stored in %s (mode 0600). Read it once, then keep it in your password manager.",
            settings.api_key_file,
        )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower(),
                proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main(sys.argv[1:])
