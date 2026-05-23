import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

import aiohttp

from .config import Config

logger = logging.getLogger(__name__)


class PushSender:
    def __init__(self, config: Config):
        self._relay_url    = Config.RELAY_URL
        self._relay_secret = Config.RELAY_SECRET
        self._sandbox      = config.apns_sandbox
        self.on_dead_token: Optional[Callable[[str], Coroutine]] = None

    async def send(
        self,
        tokens: List[str],
        title: str,
        body: str,
        subtitle: str = "",
        data: Dict[str, Any] | None = None,
        sound: str = "default",
    ) -> None:
        if not tokens:
            return
        for token in tokens:
            await self._send_one(token, title, body, subtitle, data or {})

    async def _send_one(
        self,
        device_token: str,
        title: str,
        body: str,
        subtitle: str,
        data: Dict[str, Any],
    ) -> None:
        payload: Dict[str, Any] = {
            "device_token": device_token,
            "title":        title,
            "body":         body,
            "data":         data,
            "sandbox":      self._sandbox,
        }
        if subtitle:
            payload["subtitle"] = subtitle

        headers = {
            "Content-Type":   "application/json",
            "X-Relay-Secret": self._relay_secret,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._relay_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    if resp.status == 200:
                        logger.debug("Relay delivery OK for token …%s", device_token[-8:])
                    elif resp.status == 502:
                        text = await resp.text()
                        logger.warning(
                            "APNs rejected token …%s: %s",
                            device_token[-8:],
                            text[:300],
                        )
                        try:
                            import json
                            detail = json.loads(text)
                            apns_status = detail.get("apnsStatus")
                            apns_reason = detail.get("detail", "")
                            if apns_status in (410, 400) and "DeviceToken" in apns_reason:
                                logger.info("Dead token …%s — removing", device_token[-8:])
                                if self.on_dead_token:
                                    await self.on_dead_token(device_token)
                        except Exception:
                            pass
                    elif resp.status == 401:
                        logger.error("Relay rejected request: authentication failed")
                    else:
                        text = await resp.text()
                        logger.warning(
                            "Relay HTTP %s for token …%s: %s",
                            resp.status,
                            device_token[-8:],
                            text[:200],
                        )
        except Exception as e:
            logger.error("Relay send exception for token …%s: %s", device_token[-8:], e)
