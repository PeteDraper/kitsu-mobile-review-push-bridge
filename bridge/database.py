import aiosqlite
import logging
from typing import List

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS device_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kitsu_user_id   TEXT NOT NULL,
    expo_push_token TEXT NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(expo_push_token)
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON device_tokens(kitsu_user_id);
"""


class TokenStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        for stmt in SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await self._db.execute(stmt)
        await self._db.commit()
        logger.info("Token store initialised at %s", self.db_path)

    async def upsert(self, kitsu_user_id: str, expo_push_token: str) -> None:
        await self._db.execute(
            """
            INSERT INTO device_tokens (kitsu_user_id, expo_push_token, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(expo_push_token)
            DO UPDATE SET kitsu_user_id = excluded.kitsu_user_id,
                          updated_at    = CURRENT_TIMESTAMP
            """,
            (kitsu_user_id, expo_push_token),
        )
        await self._db.commit()
        logger.debug("Upserted token for user %s", kitsu_user_id)

    async def get_tokens_for_user(self, kitsu_user_id: str) -> List[str]:
        async with self._db.execute(
            "SELECT expo_push_token FROM device_tokens WHERE kitsu_user_id = ?",
            (kitsu_user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["expo_push_token"] for row in rows]

    async def delete_token(self, expo_push_token: str) -> None:
        await self._db.execute(
            "DELETE FROM device_tokens WHERE expo_push_token = ?",
            (expo_push_token,),
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
