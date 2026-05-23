import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
# How long to hold a pending send while collecting duplicate events.
# Kitsu fires both "mention" and "comment" notification:new for the same
# comment, arriving within milliseconds.  We wait this long then send the
# highest-priority subtitle seen.
DEDUP_DELAY_SECS = 0.5

# Higher number = wins when competing with lower-priority type for same task.
_TYPE_PRIORITY: Dict[str, int] = {
    "mention":       6,
    "reply-mention": 5,
    "reply":         4,
    "assignation":   3,
    "playlist-ready":2,
    "comment":       1,
}


@dataclass
class _PendingSend:
    """Holds the best candidate notification while waiting for duplicates."""
    tokens: List[str]
    body: str
    payload_data: Dict[str, Any]
    best_subtitle: str
    best_priority: int
    flush_task: "asyncio.Task[None]"


class KitsuClient:
    def __init__(self, config: Config, store: TokenStore, pusher: PushSender):
        self.config = config
        self.store = store
        self.pusher = pusher
        self._token: Optional[str] = None
        # Bridge service-account person_id — cached at login so we can
        # suppress it from appearing as an author name in notifications.
        self._bridge_person_id: Optional[str] = None
        self._sio = socketio.AsyncClient(reconnection=False, logger=False)
        # Pending-send table: (person_id, task_id) → _PendingSend
        # Notifications are held for DEDUP_DELAY_SECS so that the
        # higher-priority type wins when Kitsu fires both "mention" and
        # "comment" for the same action.
        self._pending: Dict[Tuple[str, str], _PendingSend] = {}
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

    async def _get_person(self, person_id: str) -> Optional[Dict]:
        return await self._api_get(f"/data/persons/{person_id}")

    async def _get_comment(self, comment_id: str) -> Optional[Dict]:
        return await self._api_get(f"/data/comments/{comment_id}")

    async def _get_playlist(self, playlist_id: str) -> Optional[Dict]:
        return await self._api_get(f"/data/playlists/{playlist_id}")

    # ── Task enrichment ────────────────────────────────────────────────────────
    #
    # /data/tasks/{id} returns raw model fields only: entity_id (UUID),
    # task_type_id (UUID), project_id (UUID) — no human-readable names.
    # The flat _name fields only appear via _convert_rows_to_detailed_tasks
    # (a JOIN query used by list endpoints).  We resolve the breadcrumb with
    # up to three rounds of parallel API calls:
    #
    #   Round 1: task raw fields
    #   Round 2: entity + task_type + project (parallel)
    #   Round 3: entity_type + parent entity (sequence) (parallel)
    #   Round 4: grandparent entity (episode) — only when sequence has a parent

    async def _safe_get(self, path: str) -> Optional[Dict]:
        """GET with exception swallowed — safe to use inside asyncio.gather."""
        try:
            return await self._api_get(path)
        except Exception as e:
            logger.debug("_safe_get %s: %s", path, e)
            return None

    async def _get_task_info(self, task_id: str) -> Optional[Dict]:
        """
        Return a flat dict with the keys _build_body expects:
            project_id, project_name,
            entity_id, entity_name, entity_type_name,
            sequence_name, episode_name,
            task_type_id, task_type_name
        """
        # Round 1 — raw task
        raw = await self._api_get(f"/data/tasks/{task_id}")
        if not raw:
            return None

        entity_id    = raw.get("entity_id") or ""
        task_type_id = raw.get("task_type_id") or ""
        project_id   = raw.get("project_id") or ""

        # Round 2 — entity + task_type + project in parallel
        entity_r, task_type_r, project_r = await asyncio.gather(
            self._safe_get(f"/data/entities/{entity_id}")    if entity_id    else asyncio.sleep(0),
            self._safe_get(f"/data/task-types/{task_type_id}") if task_type_id else asyncio.sleep(0),
            self._safe_get(f"/data/projects/{project_id}")   if project_id   else asyncio.sleep(0),
        )

        entity_name      = (entity_r or {}).get("name") or ""
        entity_type_id   = (entity_r or {}).get("entity_type_id") or ""
        parent_id        = (entity_r or {}).get("parent_id") or ""   # sequence ID for shots
        task_type_name   = (task_type_r or {}).get("name") or ""
        project_name     = (project_r or {}).get("name") or ""

        # Round 3 — entity_type name + sequence name (parallel)
        entity_type_r, sequence_r = await asyncio.gather(
            self._safe_get(f"/data/entity-types/{entity_type_id}") if entity_type_id else asyncio.sleep(0),
            self._safe_get(f"/data/entities/{parent_id}")           if parent_id      else asyncio.sleep(0),
        )

        entity_type_name = (entity_type_r or {}).get("name") or ""
        sequence_name    = (sequence_r or {}).get("name") or ""
        seq_parent_id    = (sequence_r or {}).get("parent_id") or ""  # episode ID for TV

        # Round 4 — episode name (only for TV shows where sequence has a parent)
        episode_name = ""
        if seq_parent_id:
            episode_r = await self._safe_get(f"/data/entities/{seq_parent_id}")
            episode_name = (episode_r or {}).get("name") or ""

        return {
            "project_id":       project_id,
            "project_name":     project_name,
            "entity_id":        entity_id,
            "entity_name":      entity_name,
            "entity_type_name": entity_type_name,
            "sequence_name":    sequence_name,
            "episode_name":     episode_name,
            "task_type_id":     task_type_id,
            "task_type_name":   task_type_name,
        }

    # ── Display helpers — mirrors Kitsu notification page exactly ──────────────

    @staticmethod
    def _full_entity_name(task: Dict) -> str:
        """
        Mirrors Zou's names_service.get_full_entity_name():
          Shot with episode  → "Episode / Sequence / Shot"
          Shot without ep    → "Sequence / Shot"
          Asset              → "AssetType / Asset"
          Other              → entity_name
        """
        entity_type = task.get("entity_type_name") or ""
        entity      = task.get("entity_name") or ""
        sequence    = task.get("sequence_name") or ""
        episode     = task.get("episode_name") or ""

        if entity_type in ("Shot", "Scene"):
            if episode:
                return f"{episode} / {sequence} / {entity}"
            if sequence:
                return f"{sequence} / {entity}"
            return entity
        elif entity_type == "Asset":
            if sequence:           # sequence_name holds asset-type for assets
                return f"{sequence} / {entity}"
            return entity
        else:
            return entity

    @staticmethod
    def _build_body(task: Dict) -> str:
        """
        Body breadcrumb: project_name / full_entity_name / task_type_name
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

        When the author is the bridge's own service account the name is
        suppressed and a clean verb-only form is used instead.
        """
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
        return f"{name} commented" if name else "New comment"

    # ── Priority-based dedup send ──────────────────────────────────────────────

    async def _flush_pending(self, key: Tuple[str, str]) -> None:
        """Timer coroutine: wait, then send whatever the best candidate is."""
        await asyncio.sleep(DEDUP_DELAY_SECS)
        pending = self._pending.pop(key, None)
        if not pending:
            return
        person_id, task_id = key
        logger.info(
            "notification sent → person=%s  subtitle=%r  body=%r  task_id=%s",
            person_id, pending.best_subtitle, pending.body, task_id,
        )
        await self._notify(pending.tokens, pending.best_subtitle, pending.body, pending.payload_data)

    def _queue_send(
        self,
        person_id: str,
        task_id: str,
        tokens: List[str],
        subtitle: str,
        ntype: str,
        body: str,
        payload_data: Dict[str, Any],
    ) -> None:
        """
        Queue a notification for (person_id, task_id).

        If a higher-priority candidate already exists for this pair within the
        dedup window, the subtitle is upgraded but the timer is not reset.
        If this is a higher-priority type than the current candidate, replace
        the subtitle.  The body and payload (task breadcrumb, task_id) are
        taken from the first arrival since they are identical for all
        notification types on the same task.
        """
        priority = _TYPE_PRIORITY.get(ntype, 0)
        key = (person_id, task_id)
        existing = self._pending.get(key)
        if existing:
            if priority > existing.best_priority:
                logger.debug(
                    "dedup: upgrading subtitle %r→%r for person=%s task=%s",
                    existing.best_subtitle, subtitle, person_id, task_id,
                )
                existing.best_subtitle = subtitle
                existing.best_priority = priority
            else:
                logger.debug(
                    "dedup: suppressing lower-priority type=%s for person=%s task=%s",
                    ntype, person_id, task_id,
                )
            return

        # First arrival — schedule a flush
        flush_task = asyncio.create_task(self._flush_pending(key))
        self._pending[key] = _PendingSend(
            tokens=tokens,
            body=body,
            payload_data=payload_data,
            best_subtitle=subtitle,
            best_priority=priority,
            flush_task=flush_task,
        )

    # ── Notification dispatch ──────────────────────────────────────────────────

    _APP_NAME = "Kitsu Mobile Review"

    async def _notify(
        self,
        tokens: List[str],
        subtitle: str,
        body: str,
        data: Dict[str, Any],
    ) -> None:
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

        # PRIMARY: notification:new is the only handler that sends pushes.
        # Kitsu already resolves recipients — one event per person.
        @sio.on("notification:new", namespace=NS)
        async def on_notification_new(data):
            asyncio.create_task(self._handle_notification_new(data))

        # SECONDARY: no-ops — notification:new covers all of these.
        # Listening to them as well causes duplicate pushes.
        @sio.on("comment:new",      namespace=NS)
        async def on_comment_new(data):   pass

        @sio.on("task:update",      namespace=NS)
        async def on_task_update(data):   pass

        @sio.on("task:to-review",   namespace=NS)
        async def on_to_review(data):     pass

        @sio.on("task:assign",      namespace=NS)
        async def on_assign(data):        pass

        @sio.on("preview-file:new", namespace=NS)
        async def on_preview_new(data):   pass

    # ── Primary handler ────────────────────────────────────────────────────────

    async def _handle_notification_new(self, data: Dict) -> None:
        """
        Handle a notification:new socket event.

        Kitsu fires this once per recipient.  The event carries only
        { notification_id, person_id }.  We fetch the notification record to
        get the type, author_id, task_id and comment_id, then build the push
        content from those IDs.
        """
        person_id       = data.get("person_id") or ""
        notification_id = data.get("notification_id") or ""
        if not person_id:
            return

        tokens = await self.store.get_tokens_for_user(person_id)
        if not tokens:
            logger.debug("notification:new — no tokens for %s, skipping", person_id)
            return

        if not notification_id:
            logger.warning("notification:new — missing notification_id, skipping")
            return

        # ── Fetch notification record ──────────────────────────────────────────
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
            "notification:new — id=%s type=%s task_id=%s author_id=%s",
            notification_id, ntype, task_id, author_id,
        )

        # ── Fetch author + task info concurrently ──────────────────────────────
        person_result, task_info = await asyncio.gather(
            self._get_person(author_id) if author_id else asyncio.sleep(0),
            self._get_task_info(task_id) if task_id else asyncio.sleep(0),
        )

        author_name = (person_result or {}).get("full_name") or (person_result or {}).get("name") or ""

        # ── Build body breadcrumb ──────────────────────────────────────────────
        body = ""
        if task_info:
            body = self._build_body(task_info)
            if not body:
                logger.warning(
                    "notification:new — breadcrumb empty for task %s "
                    "(project=%r type=%r entity=%r entity_type=%r seq=%r ep=%r)",
                    task_id,
                    task_info.get("project_name"),
                    task_info.get("task_type_name"),
                    task_info.get("entity_name"),
                    task_info.get("entity_type_name"),
                    task_info.get("sequence_name"),
                    task_info.get("episode_name"),
                )
        else:
            logger.warning(
                "notification:new — could not resolve task info for task_id=%r (notification=%s)",
                task_id, notification_id,
            )

        # ── Build subtitle ─────────────────────────────────────────────────────
        # Detect publish: a comment notification whose linked comment has a
        # preview file attached.
        is_publish = False
        if ntype == "comment" and comment_id:
            comment = await self._get_comment(comment_id)
            if comment:
                is_publish = bool(comment.get("preview_file_id") or comment.get("previews"))

        if ntype == "playlist-ready" and playlist_id:
            playlist      = await self._get_playlist(playlist_id)
            playlist_name = (playlist or {}).get("name") or "Playlist"
            subtitle      = f"{playlist_name} is ready"
            if not body:
                body = (playlist or {}).get("project_name") or ""
        else:
            subtitle = self._build_subtitle(ntype, author_id, author_name, is_publish)

        logger.debug(
            "notification:new queued → person=%s  type=%s  subtitle=%r  body=%r  task_id=%s",
            person_id, ntype, subtitle, body, task_id,
        )

        # ── Priority dedup + delayed send ──────────────────────────────────────
        # Kitsu fires "mention" and "comment" notification:new within ms of
        # each other for the same action.  _queue_send holds the send for
        # DEDUP_DELAY_SECS, upgrading the subtitle if a higher-priority type
        # arrives in the window, then sends exactly one notification.
        self._queue_send(
            person_id=person_id,
            task_id=task_id or notification_id,
            tokens=tokens,
            subtitle=subtitle,
            ntype=ntype,
            body=body,
            payload_data={
                "type":            "notification:new",
                "task_id":         task_id,
                "project_id":      (task_info or {}).get("project_id") or "",
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
