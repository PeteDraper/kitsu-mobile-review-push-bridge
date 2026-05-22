# kitsu-mobile-review-push-bridge

A lightweight Python service that bridges [Kitsu](https://www.cg-wire.com/en/kitsu.html) production events to iOS push notifications via Apple APNs — **no Expo relay, no third-party push service, fully self-hosted**.

It connects to your Kitsu server over Socket.IO, listens for task and comment events, and sends native iOS push notifications directly to registered devices using Apple's HTTP/2 APNs API.

Designed to pair with [Kitsu Mobile Review](https://github.com/PeteDraper/kitsu-mobile-review) (Expo/React Native iOS client).

---

## How it works

```
Kitsu server  ──Socket.IO──▶  Push Bridge  ──APNs HTTP/2──▶  Apple  ──▶  iOS device
                                    ▲
                              iOS app registers
                              APNs device token
                              on login (HTTP POST)
```

1. The bridge logs into Kitsu as a service account and maintains a persistent Socket.IO connection.
2. When a relevant event fires (`comment:new`, `task:status-changed`, `task:to-review`, `task:assign`, `preview-file:new`), the bridge fetches the task details, resolves assigned users, and pushes notifications to their registered devices.
3. The iOS app registers its raw APNs device token with the bridge after login and unregisters on logout.
4. Dead tokens (app uninstalled, device reset) are removed automatically from the database when APNs returns `GONE` or `BadDeviceToken`.

---

## Requirements

- Python 3.11+
- An Apple Developer account with an **APNs Auth Key** (`.p8`) — one key works for all your apps
- A running Kitsu / Zou instance

---

## Setup

### 1. Get your APNs key

1. Sign in to [developer.apple.com](https://developer.apple.com)
2. Go to **Certificates, Identifiers & Profiles → Keys**
3. Click **+** → select **Apple Push Notifications service (APNs)** → Continue → Register
4. Download the `.p8` file — **you can only download it once**
5. Note the **Key ID** (10 characters) shown on the key detail page
6. Your **Team ID** is shown in the top-right corner when signed in

### 2. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
nano .env
```

| Variable | Description |
|---|---|
| `KITSU_URL` | Full URL of your Kitsu instance, e.g. `https://kitsu.studio.local` — no trailing slash |
| `KITSU_EMAIL` | Service account email (dedicated admin account recommended) |
| `KITSU_PASSWORD` | Service account password |
| `APNS_KEY_PATH` | Absolute path to your `.p8` auth key file |
| `APNS_KEY_ID` | 10-character Key ID from developer.apple.com |
| `APNS_TEAM_ID` | 10-character Team ID from developer.apple.com |
| `APNS_BUNDLE_ID` | Bundle ID of the iOS app, e.g. `com.yourstudio.kitsu-client` |
| `APNS_SANDBOX` | `true` for development / TestFlight builds, `false` for App Store |
| `BRIDGE_HOST` | Bind address — use `127.0.0.1` if proxied through Nginx (recommended) |
| `BRIDGE_PORT` | HTTP port for the registration API (default `9090`) |
| `BRIDGE_SECRET` | Optional shared secret — if set, the iOS app must send it as `X-Bridge-Secret` |
| `DB_PATH` | SQLite database path for token storage (default `./bridge_tokens.db`) |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` (default `INFO`) |

> **`APNS_SANDBOX`**: Development builds (Expo Go, internal TestFlight) use the sandbox endpoint. App Store / external TestFlight builds use production. If you're unsure, check your build configuration — using the wrong endpoint causes silent delivery failures.

### 4. Place the `.p8` key

Store the key somewhere safe outside the project directory and set `APNS_KEY_PATH` to its absolute path.  
**Never commit `.p8` files to version control.**

### 5. Run

```bash
python3 main.py
```

---

## Notification events

| Kitsu event | Who gets notified | Message |
|---|---|---|
| `comment:new` | All task assignees (except the commenter) | Commenter name + comment preview |
| `task:status-changed` | All task assignees (except who changed it) | New status name |
| `task:to-review` | All task assignees | "Your submission is now pending review." |
| `task:assign` | The newly assigned person | "You have been assigned to this task." |
| `preview-file:new` | All task assignees | "A new revision has been uploaded." |

Tapping any notification deep-links directly to the task review screen in the iOS app.

---

## HTTP API

The bridge exposes a minimal REST API for the iOS app.

### `POST /push-tokens` — Register a device

```json
{
  "kitsu_user_id": "<uuid>",
  "device_token": "<64-char hex APNs token>",
  "kitsu_token": "<user's JWT from Kitsu login>"
}
```

The bridge verifies the `kitsu_token` against the Kitsu API before storing the token. Returns `204 No Content`.

### `DELETE /push-tokens` — Unregister a device

```json
{
  "device_token": "<64-char hex APNs token>",
  "kitsu_token": "<user's JWT>"
}
```

Returns `204 No Content`.

### `GET /health` — Liveness check

Returns `{"status": "ok"}`.

---

## Running as a systemd service

```ini
# /etc/systemd/system/kitsu-push-bridge.service
[Unit]
Description=Kitsu Push Bridge
After=network.target

[Service]
User=kitsu-bridge
WorkingDirectory=/opt/kitsu-push-bridge
EnvironmentFile=/opt/kitsu-push-bridge/.env
ExecStart=/opt/kitsu-push-bridge/.venv/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kitsu-push-bridge
sudo journalctl -fu kitsu-push-bridge
```

---

## Nginx reverse proxy (recommended)

Expose the bridge API over HTTPS so the iOS app can reach it from outside your network:

```nginx
location /push-bridge/ {
    proxy_pass http://127.0.0.1:9090/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

Then set the Push Bridge URL in the iOS app login screen to `https://kitsu.yourstudio.com/push-bridge`.

---

## Security notes

- The bridge verifies every registration request against the Kitsu API — a valid Kitsu JWT is required to store a token.
- Set `BRIDGE_SECRET` to a strong random string and configure it in the iOS app if the bridge is internet-accessible.
- Keep your `.p8` key and `.env` file outside version control and restrict file permissions (`chmod 600`).
- Use HTTPS in production (Nginx + Let's Encrypt).

---

## License

MIT
