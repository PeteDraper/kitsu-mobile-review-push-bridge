import os
from dataclasses import dataclass


@dataclass
class Config:
    # Kitsu
    kitsu_url: str
    kitsu_email: str
    kitsu_password: str
    # Push relay
    relay_url: str
    relay_secret: str
    apns_sandbox: bool  # passed to the relay so it hits sandbox vs production APNs
    # Bridge API
    bridge_host: str
    bridge_port: int
    # Storage
    db_path: str
    log_level: str

    # ── Relay defaults ─────────────────────────────────────────────────────────
    # RELAY_URL is the same for every studio installation.  Set it to the
    # deployed URL shown in your Replit dashboard after publishing the API Server.
    # Example: https://kitsu-mobile-review.YourHandle.replit.app/api/notify
    _RELAY_URL_DEFAULT = "https://CHANGEME.replit.app/api/notify"

    @classmethod
    def from_env(cls) -> "Config":
        def require(name: str) -> str:
            v = os.environ.get(name, "").strip()
            if not v:
                raise ValueError(f"{name} is required")
            return v

        return cls(
            kitsu_url=require("KITSU_URL").rstrip("/"),
            kitsu_email=require("KITSU_EMAIL"),
            kitsu_password=require("KITSU_PASSWORD"),
            relay_url=os.environ.get("RELAY_URL", cls._RELAY_URL_DEFAULT).strip(),
            relay_secret=require("RELAY_SECRET"),
            apns_sandbox=os.environ.get("APNS_SANDBOX", "true").strip().lower() == "true",
            bridge_host=os.environ.get("BRIDGE_HOST", "127.0.0.1"),
            bridge_port=int(os.environ.get("BRIDGE_PORT", "9090")),
            db_path=os.environ.get("DB_PATH", "./bridge_tokens.db"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )

    @property
    def kitsu_api_url(self) -> str:
        return f"{self.kitsu_url}/api"

    @property
    def kitsu_socket_url(self) -> str:
        return self.kitsu_url
