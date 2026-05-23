import logging
import re

import aiohttp
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from .config import Config
from .database import TokenStore

logger = logging.getLogger(__name__)

# Raw APNs device token — 64 hex characters
_APNS_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class RegisterTokenRequest(BaseModel):
    kitsu_user_id: str
    device_token: str
    kitsu_token: str

    @field_validator("device_token")
    @classmethod
    def validate_device_token(cls, v: str) -> str:
        if not _APNS_TOKEN_RE.match(v):
            raise ValueError("device_token must be a 64-character hex APNs token")
        return v.lower()


class UnregisterTokenRequest(BaseModel):
    device_token: str
    kitsu_token: str


def create_app(config: Config, store: TokenStore) -> FastAPI:
    app = FastAPI(title="Kitsu Push Bridge", version="1.0.0", docs_url=None, redoc_url=None)

    async def _verify_kitsu_token(kitsu_token: str, expected_user_id: str) -> bool:
        url = f"{config.kitsu_api_url}/auth/authenticated"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers={"Authorization": f"Bearer {kitsu_token}"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
                    # Zou returns either {"user": {"id": ...}} or {"id": ...} directly
                    user = data.get("user") or data
                    actual_id = user.get("id") or user.get("user_id")
                    return actual_id == expected_user_id
        except Exception as e:
            logger.warning("Kitsu token verification error: %s", e)
            return False

    @app.post("/push-tokens", status_code=204)
    async def register_token(body: RegisterTokenRequest) -> None:
        if not await _verify_kitsu_token(body.kitsu_token, body.kitsu_user_id):
            raise HTTPException(status_code=401, detail="Kitsu token verification failed")
        await store.upsert(body.kitsu_user_id, body.device_token)
        logger.info("Registered APNs token for user %s", body.kitsu_user_id)

    @app.delete("/push-tokens", status_code=204)
    async def unregister_token(body: UnregisterTokenRequest) -> None:
        await store.delete_token(body.device_token)
        logger.info("Unregistered token …%s", body.device_token[-8:])

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app
