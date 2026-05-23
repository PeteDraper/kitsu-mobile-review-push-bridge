import os
from dataclasses import dataclass


@dataclass
class Config:
    # Kitsu
    kitsu_url: str
    kitsu_email: str
    kitsu_password: str
    # Push relay — hardcoded; studios do not need to configure these
    apns_sandbox: bool
    # Bridge API
    bridge_host: str
    bridge_port: int
    # Storage
    db_path: str
    log_level: str

    # ── Relay (managed, not user-configurable) ─────────────────────────────────
    RELAY_URL    = "https://kitsu-review.replit.app/api/notify"
    RELAY_SECRET = "962e334d2e7ff198dd71b7f21c12a9766d285a9db47624bf"

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
            apns_sandbox=os.environ.get("APNS_SANDBOX", "false").strip().lower() == "true",
            bridge_host=os.environ.get("BRIDGE_HOST", "0.0.0.0"),
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
