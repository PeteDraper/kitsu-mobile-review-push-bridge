import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp
import socketio

from .config import Config
from .database import TokenStore
from .push import PushSender

logger = logging.getLogger(__name__)

# How long to wait before reconnecting after a disconnect
RECONNECT_DELAY = 10
# JWT is valid for 12 h in Kitsu; refresh 30 min before expiry
TOKEN_REFRESH_INTERVAL = 11 * 60 * 60


class KitsuClient:
    def __init__(self, config: Config, store: TokenStore, pusher: PushSender):
        self.config = config
        self.store = store
        self.pusher = pusher
        self._token: Optional[str] = None
        self._sio = socketio.AsyncClient(reconnection=False, logger=False)
        self._register_handlers()

    # ── Authentication ─────────────────────────────────────────────────────────

    async def _login(self) -> str:
        url = f"{self.config.kitsu_api_url}/auth/login"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"email": self.config.kitsu_email, "password": self.config.kitsu_password},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Kitsu login failed ({resp.status}): {text}")
                data = await resp.json()
                token = data.get("access_token") or data.get("token")
                if not token:
                    raise RuntimeError(f"No token in Kitsu login response: {data}")
                logger.info("Logged into Kitsu as %s", self.config.kitsu_email)
                return token

    async def _token_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(TOKEN_REFRESH_INTERVAL)
            try:
                self._token = await self._login()
                logger.info("Kitsu token refreshed")
            except Exception as e:
                logger.error("Token refresh failed: %s", e)

    # ── Kitsu REST helpers ─────────────────────────────────────────────────────

    async def _api_get(self, path: str) -> Optional[Dict]:
        if not self._token:
            return None
        url = f"{self.config.kitsu_api_url}{path}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    logger.warning("Kitsu API %s → %d", path, resp.status)
        except aiohttp.ClientError as e:
            logger.error("Kitsu API GET %s error: %s", path, e)
        return None

    async def _get_task(self, task_id: str) -> Optional[Dict]:
        return await self._api_get(f"/data/tasks/{task_id}?relations=true")

    async def _get_task_status(self, status_id: str) -> Optional[Dict]:
        return await self._api_get(f"/data/task-status/{status_id}")

    async def _get_person(self, person_id: str) -> Optional[Dict]:
        return await self._api_get(f"/data/persons/{person_id}")

    # ── Notification dispatch ──────────────────────────────────────────────────

    async def _notify_users(
        self,
        user_ids: List[str],
        title: str,
        body: str,
        data: Dict[str, Any],
        exclude_user_id: Optional[str] = None,
    ) -> None:
        for user_id in user_ids:
            if user_id == exclude_user_id:
                continue
            tokens = await self.store.get_tokens_for_user(user_id)
            if tokens:
                logger.info(
                    "Notifying user %s (%d token(s)): %s", user_id, len(tokens), title
                )
                await self.pusher.send(tokens=tokens, title=title, body=body, data=data)

    # ── Socket.IO event handlers ───────────────────────────────────────────────

    def _register_handlers(self) -> None:
        sio = self._sio

        NS = "/events"

        @sio.event(namespace=NS)
        async def connect():
            logger.info("Socket.IO connected to Kitsu (%s)", NS)

        @sio.event(namespace=NS)
        async def disconnect():
            logger.warning("Socket.IO disconnected from Kitsu (%s)", NS)

        @sio.event(namespace=NS)
        async def connect_error(data):
            logger.error("Socket.IO connect error (%s): %s", NS, data)

        @sio.on("comment:new", namespace=NS)
        async def on_comment_new(data):
            asyncio.create_task(self._handle_comment_new(data))

        @sio.on("task:status-changed", namespace=NS)
        async def on_status_changed(data):
            asyncio.create_task(self._handle_status_changed(data))

        @sio.on("task:to-review", namespace=NS)
        async def on_to_review(data):
            asyncio.create_task(self._handle_to_review(data))

        @sio.on("task:assign", namespace=NS)
        async def on_assign(data):
            asyncio.create_task(self._handle_assign(data))

        @sio.on("preview-file:new", namespace=NS)
        async def on_preview_new(data):
            asyncio.create_task(self._handle_preview_new(data))

    async def _handle_comment_new(self, data: Dict) -> None:
        task_id = data.get("task_id") or (data.get("comment") or {}).get("task_id")
        if not task_id:
            return

        comment = data.get("comment") or {}
        commenter_id = comment.get("person_id")
        text = (comment.get("text") or "").strip()

        task = await self._get_task(task_id)
        if not task:
            return

        assignees: List[str] = task.get("assignees", [])
        task_name = task.get("name") or "a task"
        entity_name = task.get("entity_name") or ""
        display_name = f"{entity_name} / {task_name}" if entity_name else task_name

        commenter_name = ""
        if commenter_id:
            person = await self._get_person(commenter_id)
            if person:
                commenter_name = person.get("full_name") or person.get("name") or ""

        title = f"💬 {commenter_name}: {display_name}" if commenter_name else f"New comment on {display_name}"
        body = text[:200] if text else "A new comment was posted."

        await self._notify_users(
            assignees,
            title=title,
            body=body,
            data={"type": "comment:new", "task_id": task_id, "project_id": task.get("project_id", "")},
            exclude_user_id=commenter_id,
        )

    async def _handle_status_changed(self, data: Dict) -> None:
        task_id = data.get("task_id")
        if not task_id:
            return

        new_status_id = data.get("new_task_status_id")
        changed_by = data.get("person_id")

        task = await self._get_task(task_id)
        if not task:
            return

        assignees: List[str] = task.get("assignees", [])
        task_name = task.get("name") or "a task"
        entity_name = task.get("entity_name") or ""
        display_name = f"{entity_name} / {task_name}" if entity_name else task_name

        status_name = data.get("task_status_name", "")
        if not status_name and new_status_id:
            status = await self._get_task_status(new_status_id)
            if status:
                status_name = status.get("short_name") or status.get("name") or ""

        body = f"Status → {status_name}" if status_name else "Task status changed."

        await self._notify_users(
            assignees,
            title=f"🔄 {display_name}",
            body=body,
            data={"type": "task:status-changed", "task_id": task_id, "project_id": task.get("project_id", "")},
            exclude_user_id=changed_by,
        )

    async def _handle_to_review(self, data: Dict) -> None:
        task_id = data.get("task_id")
        if not task_id:
            return

        task = await self._get_task(task_id)
        if not task:
            return

        assignees: List[str] = task.get("assignees", [])
        task_name = task.get("name") or "a task"
        entity_name = task.get("entity_name") or ""
        display_name = f"{entity_name} / {task_name}" if entity_name else task_name

        await self._notify_users(
            assignees,
            title=f"✅ {display_name}",
            body="Your submission is now pending review.",
            data={"type": "task:to-review", "task_id": task_id, "project_id": task.get("project_id", "")},
        )

    async def _handle_assign(self, data: Dict) -> None:
        task_id = data.get("task_id")
        person_id = data.get("person_id")
        if not task_id or not person_id:
            return

        task = await self._get_task(task_id)
        if not task:
            return

        task_name = task.get("name") or "a task"
        entity_name = task.get("entity_name") or ""
        display_name = f"{entity_name} / {task_name}" if entity_name else task_name

        await self._notify_users(
            [person_id],
            title=f"📋 Assigned: {display_name}",
            body="You have been assigned to this task.",
            data={"type": "task:assign", "task_id": task_id, "project_id": task.get("project_id", "")},
        )

    async def _handle_preview_new(self, data: Dict) -> None:
        task_id = data.get("task_id")
        if not task_id:
            return

        task = await self._get_task(task_id)
        if not task:
            return

        assignees: List[str] = task.get("assignees", [])
        task_name = task.get("name") or "a task"
        entity_name = task.get("entity_name") or ""
        display_name = f"{entity_name} / {task_name}" if entity_name else task_name

        await self._notify_users(
            assignees,
            title=f"🎬 New preview: {display_name}",
            body="A new revision has been uploaded.",
            data={"type": "preview-file:new", "task_id": task_id, "project_id": task.get("project_id", "")},
        )

    # ── Socket.IO connection loop ──────────────────────────────────────────────

    async def run(self) -> None:
        self._token = await self._login()
        asyncio.create_task(self._token_refresh_loop())

        while True:
            try:
                await self._sio.connect(
                    self.config.kitsu_socket_url,
                    socketio_path="/socket.io",
                    headers={"Authorization": f"Bearer {self._token}"},
                    transports=["websocket"],
                    namespaces=["/events"],
                )
                await self._sio.wait()
            except socketio.exceptions.ConnectionError as e:
                logger.error("Socket.IO connection failed: %s — retrying in %ds", e, RECONNECT_DELAY)
            except Exception as e:
                logger.error("Socket.IO unexpected error: %s — retrying in %ds", e, RECONNECT_DELAY)
            finally:
                try:
                    await self._sio.disconnect()
                except Exception:
                    pass

            await asyncio.sleep(RECONNECT_DELAY)
