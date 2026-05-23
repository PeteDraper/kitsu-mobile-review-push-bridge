#!/usr/bin/env python3
"""
Kitsu Push Bridge
-----------------
Connects to a Kitsu/Zou server via Socket.IO, listens for production events,
and forwards push notifications to the Kitsu Mobile Review relay for delivery
to iOS devices.

Setup:
    pip install -r requirements.txt
    cp .env.example .env && nano .env
    python3 main.py
"""

import asyncio
import logging
import signal
import sys

import uvicorn
from dotenv import load_dotenv

load_dotenv()

from bridge.api import create_app
from bridge.config import Config
from bridge.database import TokenStore
from bridge.kitsu import KitsuClient
from bridge.push import PushSender


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("socketio").setLevel(logging.WARNING)
    logging.getLogger("engineio").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


async def main() -> None:
    try:
        config = Config.from_env()
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.log_level)
    logger = logging.getLogger("bridge.main")

    store = TokenStore(config.db_path)
    await store.init()

    pusher = PushSender(config)

    async def _remove_dead_token(token: str) -> None:
        await store.delete_token(token)

    pusher.on_dead_token = _remove_dead_token

    kitsu = KitsuClient(config, store, pusher)
    app = create_app(config, store)

    uv_config = uvicorn.Config(
        app,
        host=config.bridge_host,
        port=config.bridge_port,
        log_level="warning",
    )
    server = uvicorn.Server(uv_config)

    logger.info(
        "Kitsu Push Bridge starting  kitsu=%s  sandbox=%s  api=%s:%d",
        config.kitsu_url,
        config.apns_sandbox,
        config.bridge_host,
        config.bridge_port,
    )

    loop = asyncio.get_running_loop()

    def _shutdown(signum, frame):
        logger.info("Shutdown signal received")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        await asyncio.gather(kitsu.run(), server.serve())
    except asyncio.CancelledError:
        pass
    finally:
        await store.close()
        logger.info("Bridge stopped")


if __name__ == "__main__":
    asyncio.run(main())
