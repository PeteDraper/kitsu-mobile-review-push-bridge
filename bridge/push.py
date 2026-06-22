import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

import aiohttp

from .config import Config

logger = logging.getLogger(__name__)


class PushSender:
    def __init__(self, config: Config):
        self._relay_url    = config.relay_url
        self._relay_secret = config.relay_secret
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
                        # Relay forwarded request but APNs rejected it — log detail
                        text = await resp.text()
                        logger.warning(
                            "APNs rejected token …%s: %s",
                            device_token[-8:],
                            text[:300],
                        )
                        # Surface dead tokens: relay returns APNs status in JSON.
                        # The relay wraps the raw APNs response body as a JSON-encoded
                        # string in the "detail" field, so we must parse twice:
                        #   outer: { "apnsStatus": 410, "detail": "{\"reason\":\"Unregistered\",...}" }
                        #   inner: { "reason": "Unregistered", "timestamp": ... }
                        #
                        # APNs dead-token reasons:
                        #   410 + "Unregistered" — device uninstalled the app or revoked
                        #                          push permission; token permanently invalid.
                        #   400 + "BadDeviceToken" — malformed / never-valid token.
                        try:
                            import json
                            outer = json.loads(text)
                            apns_status = outer.get("apnsStatus")
                            inner_str   = outer.get("detail", "")
                            try:
                                inner = json.loads(inner_str) if inner_str else {}
                            except Exception:
                                inner = {}
                            reason = inner.get("reason", "")
                            if apns_status == 410 and reason == "Unregistered":
                                logger.info(
                                    "Dead token (Unregistered) …%s — removing from DB",
                                    device_token[-8:],
                                )
                                if self.on_dead_token:
                                    await self.on_dead_token(device_token)
                            elif apns_status == 400 and reason == "BadDeviceToken":
                                logger.info(
                                    "Dead token (BadDeviceToken) …%s — removing from DB",
                                    device_token[-8:],
                                )
                                if self.on_dead_token:
                                    await self.on_dead_token(device_token)
                        except Exception:
                            pass
                    elif resp.status == 401:
                        logger.error("Relay rejected request: invalid RELAY_SECRET")
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
