import os
from dataclasses import dataclass


@dataclass
class Config:
    # Kitsu
    kitsu_url: str
    kitsu_email: str
    kitsu_password: str
    # APNs
    apns_key_path: str
    apns_key_id: str
    apns_team_id: str
    apns_bundle_id: str
    apns_sandbox: bool
    # Bridge API
    bridge_host: str
    bridge_port: int
    bridge_secret: str
    # Storage
    db_path: str
    log_level: str

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
            apns_key_path=require("APNS_KEY_PATH"),
            apns_key_id=require("APNS_KEY_ID"),
            apns_team_id=require("APNS_TEAM_ID"),
            apns_bundle_id=require("APNS_BUNDLE_ID"),
            apns_sandbox=os.environ.get("APNS_SANDBOX", "false").strip().lower() == "true",
            bridge_host=os.environ.get("BRIDGE_HOST", "127.0.0.1"),
            bridge_port=int(os.environ.get("BRIDGE_PORT", "9090")),
            bridge_secret=os.environ.get("BRIDGE_SECRET", ""),
            db_path=os.environ.get("DB_PATH", "./bridge_tokens.db"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )

    @property
    def kitsu_api_url(self) -> str:
        return f"{self.kitsu_url}/api"

    @property
    def kitsu_socket_url(self) -> str:
        return self.kitsu_url
