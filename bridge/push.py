import logging
import uuid
from typing import Any, Callable, Coroutine, Dict, List, Optional

from aioapns import APNs, NotificationRequest
from aioapns.common import APNS_RESPONSE_CODE

from .config import Config

logger = logging.getLogger(__name__)

# APNs HTTP status codes that mean the token is permanently invalid — remove from DB
# GONE (410)       → Unregistered: device reset or app uninstalled
# BAD_REQUEST (400) → BadDeviceToken: malformed / wrong-environment token
_DEAD_TOKEN_ERRORS = {
    APNS_RESPONSE_CODE.GONE,
    APNS_RESPONSE_CODE.BAD_REQUEST,
}


class PushSender:
    def __init__(self, config: Config):
        self._apns = APNs(
            key=config.apns_key_path,
            key_id=config.apns_key_id,
            team_id=config.apns_team_id,
            topic=config.apns_bundle_id,
            use_sandbox=config.apns_sandbox,
        )
        # Optional callback set by main.py so dead tokens can be purged
        self.on_dead_token: Optional[Callable[[str], Coroutine]] = None

    async def send(
        self,
        tokens: List[str],
        title: str,
        body: str,
        data: Dict[str, Any] | None = None,
        sound: str = "default",
    ) -> None:
        if not tokens:
            return

        for token in tokens:
            await self._send_one(token, title, body, data or {}, sound)

    async def _send_one(
        self,
        device_token: str,
        title: str,
        body: str,
        data: Dict[str, Any],
        sound: str,
    ) -> None:
        payload: Dict[str, Any] = {
            "aps": {
                "alert": {"title": title, "body": body},
                "sound": sound,
            },
        }
        payload.update(data)

        request = NotificationRequest(
            device_token=device_token,
            message=payload,
            notification_id=str(uuid.uuid4()),
        )

        try:
            result = await self._apns.send_notification(request)
            if result.is_successful:
                logger.debug("APNs delivery OK for token …%s", device_token[-8:])
            else:
                logger.warning(
                    "APNs error for token …%s: %s",
                    device_token[-8:],
                    result.description,
                )
                if result.status in _DEAD_TOKEN_ERRORS and self.on_dead_token:
                    logger.info("Removing dead token …%s", device_token[-8:])
                    await self.on_dead_token(device_token)
        except Exception as e:
            logger.error("APNs send exception for token …%s: %s", device_token[-8:], e)
