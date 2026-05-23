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
        self._bridge_person_id: Optional[str] = None   # set on login; used to suppress service-account name
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

                # Cache the bridge service account's own person_id so we can
                # suppress it from appearing as an author name in notifications.
                user = data.get("user") or {}
                self._bridge_person_id = user.get("id") or user.get("person_id")
                logger.info(
                    "Logged into Kitsu as %s (person_id=%s)",
                    self.config.kitsu_email,
                    self._bridge_person_id,
                )
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

    # ── Display helpers — mirrors Kitsu notification page exactly ──────────────

    @staticmethod
    def _full_entity_name(task: Dict) -> str:
        """
        Mirrors Zou's names_service.get_full_entity_name():
          Shot with episode  → "Episode / Sequence / Shot"
          Shot without ep    → "Sequence / Shot"
          Asset              → "AssetType / Asset"   (sequence_name = asset type in Zou)
          Other              → entity_name
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
            if sequence:
                return f"{sequence} / {entity}"
            return entity
        else:
            return entity

    @staticmethod
    def _build_body(task: Dict) -> str:
        """
        Mirrors Kitsu's buildBody():  project_name / full_entity_name / task_type_name
        """
        project   = task.get("project_name") or ""
        task_type = task.get("task_type_name") or ""
        entity    = KitsuClient._full_entity_name(task)
        parts     = [p for p in [project, entity, task_type] if p]
        return " / ".join(parts)

    def _build_subtitle(
        self,
        ntype: str,
        author_id: str,
        author_name: str,
        is_publish: bool = False,
    ) -> str:
        """
        Mirrors Kitsu's desktop notification title strings (English locale).
        When the author is the bridge's own service account, the name is
        suppressed and a clean verb-only form is used instead.

        With name:    "{Name} commented", "{Name} mentioned you", etc.
        Without name: "New comment", "Mentioned you", etc.
        """
        # Suppress service-account name so it never appears in a notification
        is_service_account = bool(
            self._bridge_person_id and author_id == self._bridge_person_id
        )
        name = "" if is_service_account else (author_name or "")

        if ntype in ("mention", "reply-mention"):
            return f"{name} mentioned you" if name else "Mentioned you"
        if ntype == "reply":
            return f"{name} replied" if name else "Replied"
        if ntype == "assignation":
            return f"{name} assigned you" if name else "Assigned to you"
        if ntype == "comment":
            if is_publish:
                return f"{name} published a preview" if name else "Preview published"
            return f"{name} commented" if name else "New comment"
        # Fallback — treat as a comment
        return f"{name} commented" if name else "New comment"

    # ── Notification dispatch ──────────────────────────────────────────────────

    _APP_NAME = "Kitsu Mobile Review"

    async def _notify(
        self,
        tokens: List[str],
        subtitle: str,
        body: str,
        data: Dict[str, Any],
    ) -> None:
        """Send to an already-resolved list of tokens."""
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

        # PRIMARY: Kitsu already resolves recipients — one event per person.
        @sio.on("notification:new", namespace=NS)
        async def on_notification_new(data):
            asyncio.create_task(self._handle_notification_new(data))

        # SECONDARY: kept as no-ops.  notification:new covers all cases that
        # matter. Listening to these as well would cause duplicate pushes.
        @sio.on("comment:new",      namespace=NS)
        async def on_comment_new(data):    pass

        @sio.on("task:update",      namespace=NS)
        async def on_task_update(data):    pass

        @sio.on("task:to-review",   namespace=NS)
        async def on_to_review(data):      pass

        @sio.on("task:assign",      namespace=NS)
        async def on_assign(data):         pass

        @sio.on("preview-file:new", namespace=NS)
        async def on_preview_new(data):    pass

    # ── Primary handler ────────────────────────────────────────────────────────

    async def _handle_notification_new(self, data: Dict) -> None:
        """
        Kitsu fires notification:new once per recipient — it has already
        resolved who should see the notification.

        Socket event payload: { notification_id, person_id }
        Notification types (Zou): comment, mention, reply, reply-mention,
                                   assignation, playlist-ready.
        A "comment" notification is treated as "publish" when the linked
        comment has a preview file attached.

        Subtitle mirrors Kitsu's desktop notification title strings exactly.
        Body mirrors Kitsu's buildBody(): project / entity_path / task_type.
        notification_id is included in the payload so the app can mark it
        read on tap and navigate directly to the task.
        """
        person_id       = data.get("person_id")
        notification_id = data.get("notification_id") or ""
        if not person_id:
            return

        tokens = await self.store.get_tokens_for_user(person_id)
        if not tokens:
            logger.debug("notification:new — no tokens for user %s, skipping", person_id)
            return

        # Defaults used when the notification record cannot be enriched
        subtitle = "New notification"
        body     = ""
        task_id  = ""

        if not notification_id:
            logger.warning("notification:new — missing notification_id in event data; sending bare push")
            await self._notify(
                tokens, subtitle, body,
                {"type": "notification:new", "task_id": "", "project_id": "",
                 "notification_id": ""},
            )
            return

        notif = await self._api_get(f"/data/notifications/{notification_id}")
        if not notif:
            logger.warning("notification:new — could not fetch notification %s", notification_id)
            return

        ntype       = notif.get("notification_type") or notif.get("type") or "comment"
        task_id     = notif.get("task_id") or ""
        author_id   = notif.get("author_id") or ""
        comment_id  = notif.get("comment_id") or ""
        playlist_id = notif.get("playlist_id") or ""

        logger.debug(
            "notification:new — id=%s type=%s task_id=%s author_id=%s comment_id=%s",
            notification_id, ntype, task_id, author_id, comment_id,
        )

        # Fetch author and task concurrently
        async def _noop() -> None:
            return None

        person_result, task_result = await asyncio.gather(
            self._get_person(author_id) if author_id else _noop(),
            self._get_task(task_id)     if task_id  else _noop(),
            return_exceptions=True,
        )
        if isinstance(person_result, Exception):
            logger.warning("Failed to fetch author %s: %s", author_id, person_result)
            person_result = None
        if isinstance(task_result, Exception):
            logger.warning("Failed to fetch task %s: %s", task_id, task_result)
            task_result = None

        author_name = ""
        if person_result:
            author_name = person_result.get("full_name") or person_result.get("name") or ""

        # Build body breadcrumb from task (project / entity path / task type)
        if task_result:
            body = self._build_body(task_result)
            if not body:
                logger.warning(
                    "notification:new — _build_body returned empty for task %s "
                    "(project_name=%r entity_type_name=%r entity_name=%r "
                    "sequence_name=%r episode_name=%r task_type_name=%r)",
                    task_id,
                    task_result.get("project_name"),
                    task_result.get("entity_type_name"),
                    task_result.get("entity_name"),
                    task_result.get("sequence_name"),
                    task_result.get("episode_name"),
                    task_result.get("task_type_name"),
                )
        else:
            logger.warning(
                "notification:new — no task result for task_id=%r (notification=%s)",
                task_id, notification_id,
            )

        # Detect publish: a comment notification whose comment has a preview file
        is_publish = False
        if ntype == "comment" and comment_id:
            comment = await self._get_comment(comment_id)
            if comment:
                is_publish = bool(
                    comment.get("preview_file_id") or comment.get("previews")
                )

        # playlist-ready is not task-linked — use playlist name as body
        if ntype == "playlist-ready" and playlist_id:
            playlist = await self._get_playlist(playlist_id)
            playlist_name = (playlist or {}).get("name") or "Playlist"
            subtitle = f"{playlist_name} is ready"
            if not body:
                body = (playlist or {}).get("project_name") or ""
        else:
            subtitle = self._build_subtitle(ntype, author_id, author_name, is_publish)

        logger.info(
            "notification:new → user=%s  subtitle=%r  body=%r  task_id=%s",
            person_id, subtitle, body, task_id,
        )

        await self._notify(
            tokens, subtitle, body,
            {
                "type":            "notification:new",
                "task_id":         task_id,
                "project_id":      (task_result or {}).get("project_id") or "",
                "notification_id": notification_id,
            },
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
