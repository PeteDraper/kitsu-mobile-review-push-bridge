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

    async def _get_person(self, person_id: str) -> Optional[Dict]:
        return await self._api_get(f"/data/persons/{person_id}")

    async def _get_comment(self, comment_id: str) -> Optional[Dict]:
        return await self._api_get(f"/data/comments/{comment_id}")

    async def _get_playlist(self, playlist_id: str) -> Optional[Dict]:
        return await self._api_get(f"/data/playlists/{playlist_id}")

    # ── Display helpers — mirrors Kitsu's buildBody / buildTitle exactly ───────

    @staticmethod
    def _full_entity_name(task: Dict) -> str:
        """
        Mirrors Zou's names_service.get_full_entity_name().
        Shot with episode  → "Episode / Sequence / Shot"
        Shot without ep    → "Sequence / Shot"
        Asset              → "AssetType / Asset"
        Other              → "Entity"
        The task dict from /data/tasks/{id}?relations=true carries
        episode_name (blank when sequence has no parent), sequence_name,
        entity_name, entity_type_name, and sequence_name doubles as the
        asset-type name for assets.
        """
        entity_type = task.get("entity_type_name") or ""
        entity      = task.get("entity_name") or ""
        sequence    = task.get("sequence_name") or ""
        episode     = task.get("episode_name") or ""

        if entity_type in ("Shot", "Scene"):
            if episode:
                return f"{episode} / {sequence} / {entity}"
            return f"{sequence} / {entity}"
        elif entity_type == "Asset":
            # sequence_name carries the asset-type name in Zou's data model
            if sequence:
                return f"{sequence} / {entity}"
            return entity
        else:
            return entity

    @staticmethod
    def _build_body(task: Dict) -> str:
        """
        Mirrors Kitsu's buildBody(): project_name / full_entity_name / task_type_name
        """
        project   = task.get("project_name") or ""
        task_type = task.get("task_type_name") or ""
        entity    = KitsuClient._full_entity_name(task)

        parts = [p for p in [project, entity, task_type] if p]
        return " / ".join(parts)

    @staticmethod
    def _build_subtitle(ntype: str, author_name: str, is_publish: bool = False) -> str:
        """
        Mirrors Kitsu's buildTitle() using exact English locale strings:
          comment_title:    '{name} commented'
          publish_title:    '{name} published a preview'
          mention_title:    '{name} mentioned you'
          reply_title:      '{name} replied'
          assignation_title:'{name} assigned you'
        reply-mention shares the mention wording.
        """
        name = author_name or "Someone"
        if ntype in ("mention", "reply-mention"):
            return f"{name} mentioned you"
        if ntype == "reply":
            return f"{name} replied"
        if ntype == "assignation":
            return f"{name} assigned you"
        if ntype == "comment":
            if is_publish:
                return f"{name} published a preview"
            return f"{name} commented"
        # fallback
        return f"{name} commented"

    # ── Notification dispatch ──────────────────────────────────────────────────

    _APP_NAME = "Kitsu Mobile Review"

    async def _notify_users(
        self,
        user_ids: List[str],
        subtitle: str,
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
                    "Notifying user %s (%d token(s)): %s", user_id, len(tokens), subtitle
                )
                await self.pusher.send(
                    tokens=tokens,
                    title=self._APP_NAME,
                    subtitle=subtitle,
                    body=body,
                    data=data,
                )

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

        @sio.on("*", namespace=NS)
        async def on_any(event, data):
            logger.debug("Socket.IO event: %s  data=%s", event, str(data)[:200])

        @sio.on("notification:new", namespace=NS)
        async def on_notification_new(data):
            asyncio.create_task(self._handle_notification_new(data))

        @sio.on("comment:new", namespace=NS)
        async def on_comment_new(data):
            asyncio.create_task(self._handle_comment_new(data))

        @sio.on("task:update", namespace=NS)
        async def on_task_update(data):
            asyncio.create_task(self._handle_task_update(data))

        @sio.on("task:to-review", namespace=NS)
        async def on_to_review(data):
            asyncio.create_task(self._handle_to_review(data))

        @sio.on("task:assign", namespace=NS)
        async def on_assign(data):
            asyncio.create_task(self._handle_assign(data))

        @sio.on("preview-file:new", namespace=NS)
        async def on_preview_new(data):
            asyncio.create_task(self._handle_preview_new(data))

    async def _handle_notification_new(self, data: Dict) -> None:
        """
        PRIMARY push trigger.  Kitsu fires notification:new with person_id
        already resolved — the server decides who gets notified.

        Notification types (Zou): comment, mention, reply, reply-mention,
        assignation, playlist-ready.
        A 'comment' notification is treated as 'publish' when its linked
        comment has a preview file attached.

        Subtitle mirrors Kitsu's desktop notification title strings exactly.
        Body mirrors Kitsu's buildBody(): project / entity_path / task_type.
        Data payload includes notification_id so the app can mark it read on tap
        and navigate directly to the task.
        """
        person_id       = data.get("person_id")
        notification_id = data.get("notification_id")
        project_id      = data.get("project_id", "")
        if not person_id:
            return

        tokens = await self.store.get_tokens_for_user(person_id)
        if not tokens:
            return

        subtitle = "New notification"
        body     = "You have new activity."
        task_id  = ""

        if not notification_id:
            # Nothing to enrich — send the bare minimum
            logger.info("Notifying user %s (no notification_id)", person_id)
            await self.pusher.send(
                tokens=tokens,
                title=self._APP_NAME,
                subtitle=subtitle,
                body=body,
                data={"type": "notification:new", "task_id": task_id, "project_id": project_id,
                      "notification_id": ""},
            )
            return

        notif = await self._api_get(f"/data/notifications/{notification_id}")
        if not notif:
            return

        ntype      = notif.get("notification_type") or notif.get("type") or "comment"
        task_id    = notif.get("task_id") or ""
        author_id  = notif.get("author_id") or ""
        comment_id = notif.get("comment_id") or ""
        playlist_id= notif.get("playlist_id") or ""

        async def _noop() -> None:
            return None

        # Fetch author name and task concurrently
        results = await asyncio.gather(
            self._get_person(author_id) if author_id else _noop(),
            self._get_task(task_id) if task_id else _noop(),
            return_exceptions=True,
        )
        person_result = results[0] if not isinstance(results[0], Exception) else None
        task_result   = results[1] if not isinstance(results[1], Exception) else None

        author_name = ""
        if person_result:
            author_name = person_result.get("full_name") or person_result.get("name") or ""

        # Build body from task breadcrumb (matches Kitsu's buildBody)
        if task_result:
            body = self._build_body(task_result)

        # Detect publish: a comment notification where the comment has a preview
        is_publish = False
        if ntype == "comment" and comment_id:
            comment = await self._get_comment(comment_id)
            if comment:
                is_publish = bool(
                    comment.get("preview_file_id")
                    or comment.get("previews")
                )

        # playlist-ready: no task — use playlist name as body
        if ntype == "playlist-ready" and playlist_id:
            playlist = await self._get_playlist(playlist_id)
            playlist_name = (playlist or {}).get("name") or "Playlist"
            subtitle = f"{playlist_name} is ready"
            body     = (playlist or {}).get("project_name") or body
        else:
            subtitle = self._build_subtitle(ntype, author_name, is_publish)

        logger.info("notification:new → user %s  subtitle=%r  body=%r", person_id, subtitle, body)
        await self.pusher.send(
            tokens=tokens,
            title=self._APP_NAME,
            subtitle=subtitle,
            body=body,
            data={
                "type": "notification:new",
                "task_id": task_id,
                "project_id": project_id,
                "notification_id": notification_id,
            },
        )

    async def _handle_comment_new(self, data: Dict) -> None:
        """
        Fallback for comment:new events not already covered by notification:new.
        Uses the same subtitle/body format as _handle_notification_new.
        """
        task_id = data.get("task_id") or (data.get("comment") or {}).get("task_id")
        if not task_id:
            return

        comment      = data.get("comment") or {}
        commenter_id = comment.get("person_id")
        is_publish   = bool(comment.get("preview_file_id") or comment.get("previews"))

        task = await self._get_task(task_id)
        if not task:
            return

        assignees: List[str] = task.get("assignees", [])
        body = self._build_body(task)

        author_name = ""
        if commenter_id:
            person = await self._get_person(commenter_id)
            if person:
                author_name = person.get("full_name") or person.get("name") or ""

        subtitle = self._build_subtitle("comment", author_name, is_publish)

        await self._notify_users(
            assignees,
            subtitle=subtitle,
            body=body,
            data={"type": "comment:new", "task_id": task_id,
                  "project_id": task.get("project_id", ""), "notification_id": ""},
            exclude_user_id=commenter_id,
        )

    async def _handle_task_update(self, data: Dict) -> None:
        """
        task:update fires for many internal changes.  notification:new already
        handles targeted pushes for status changes, so this is a no-op to
        avoid double-notifying.
        """
        pass

    async def _handle_to_review(self, data: Dict) -> None:
        task_id = data.get("task_id")
        if not task_id:
            return

        task = await self._get_task(task_id)
        if not task:
            return

        assignees: List[str] = task.get("assignees", [])
        body = self._build_body(task)

        await self._notify_users(
            assignees,
            subtitle="Sent to review",
            body=body,
            data={"type": "task:to-review", "task_id": task_id,
                  "project_id": task.get("project_id", ""), "notification_id": ""},
        )

    async def _handle_assign(self, data: Dict) -> None:
        task_id   = data.get("task_id")
        person_id = data.get("person_id")
        if not task_id or not person_id:
            return

        task = await self._get_task(task_id)
        if not task:
            return

        body = self._build_body(task)

        # Fetch assigner name to match "{name} assigned you"
        assigner_id   = data.get("assigner_id") or ""
        assigner_name = ""
        if assigner_id:
            assigner = await self._get_person(assigner_id)
            if assigner:
                assigner_name = assigner.get("full_name") or assigner.get("name") or ""

        subtitle = self._build_subtitle("assignation", assigner_name)

        await self._notify_users(
            [person_id],
            subtitle=subtitle,
            body=body,
            data={"type": "task:assign", "task_id": task_id,
                  "project_id": task.get("project_id", ""), "notification_id": ""},
        )

    async def _handle_preview_new(self, data: Dict) -> None:
        task_id = data.get("task_id")
        if not task_id:
            return

        task = await self._get_task(task_id)
        if not task:
            return

        assignees: List[str] = task.get("assignees", [])
        body = self._build_body(task)

        await self._notify_users(
            assignees,
            subtitle="New revision uploaded",
            body=body,
            data={"type": "preview-file:new", "task_id": task_id,
                  "project_id": task.get("project_id", ""), "notification_id": ""},
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
