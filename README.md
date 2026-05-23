# kitsu-mobile-review-push-bridge

A lightweight Python service that bridges [Kitsu](https://www.cg-wire.com/en/kitsu.html) production events to iOS push notifications via Apple APNs — **no Expo relay, no third-party push service, fully self-hosted**.

It connects to your Kitsu server over Socket.IO, listens for events, and sends native iOS push notifications directly to registered devices using Apple's HTTP/2 APNs API.

Designed to pair with [Kitsu Mobile Review](https://github.com/PeteDraper/kitsu-mobile-review) (Expo/React Native iOS client).

> **Requirement:** The bridge must be installed on the **same server** as your Kitsu/Zou instance and served by the same nginx. The iOS app always looks for the bridge at `{your-kitsu-host}/push-bridge/` automatically — no separate URL needed at login.

---

## How it works

```
Kitsu server  ──Socket.IO──▶  Push Bridge  ──APNs HTTP/2──▶  Apple  ──▶  iOS device
                                    ▲
                              iOS app registers
                              APNs device token
                              on login (HTTP POST)
```

1. The bridge logs into Kitsu as a service account and maintains a persistent Socket.IO connection to the `/events` namespace.
2. The primary trigger is `notification:new` — Kitsu fires this for each person who should be notified, so no recipient logic is needed in the bridge.
3. The bridge fetches the notification record and task details, builds the message, and pushes to all APNs tokens registered for that person.
4. The iOS app registers its raw APNs device token with the bridge after login and unregisters on logout.
5. Dead tokens (app uninstalled, device reset) are removed automatically when APNs returns `GONE` or `BadDeviceToken`.

---

## Notification format

Notifications mirror Kitsu's own notification page exactly — same wording, same breadcrumb structure.

**APNs fields:**

| Field | Content |
|-------|---------|
| **Title** | `Kitsu Mobile Review` (always — identifies the app) |
| **Subtitle** | Event description (see table below) |
| **Body** | `Project / Entity Path / Task Type` |

**Subtitle strings by notification type:**

| Kitsu type | Subtitle |
|------------|---------|
| Comment | `{Author} commented` |
| Comment with revision (publish) | `{Author} published a preview` |
| Mention in comment | `{Author} mentioned you` |
| Reply to comment | `{Author} replied` |
| Reply that mentions you | `{Author} mentioned you` |
| Task assigned | `{Author} assigned you` |
| Playlist ready | `{Playlist name} is ready` |

**Body breadcrumb** (mirrors Zou's `names_service.get_full_entity_name`):

- Shot with episode: `Project / Episode / Sequence / Shot / Task Type`
- Shot without episode: `Project / Sequence / Shot / Task Type`
- Asset: `Project / Asset Type / Asset / Task Type`

**Tapping a notification** opens the task review screen directly in the app. The `notification_id` is included in the payload so the app can mark the notification read on open.

---

## What you need before starting

- The server your Kitsu/Zou instance already runs on (Ubuntu 18.04 or later).
- An **Apple Developer account** (paid, $99/year) to create the APNs key.

> **Connecting via SSH from your Mac or Windows PC:**
> Open Terminal (Mac) or PowerShell (Windows) and type:
> `ssh youruser@your-server-ip`
> Press Enter, type your password when asked. You are now controlling the server remotely.

---

## Step 1 — Get your Apple APNs key

This is a credential file (`.p8`) that proves to Apple you are allowed to send push notifications to your app. You only do this once.

1. Go to [developer.apple.com](https://developer.apple.com) and sign in.
2. Go to **Account → Certificates, Identifiers & Profiles → Keys**.
3. Click **+**, give it a name (e.g. `KitsuPushBridge`), tick **Apple Push Notifications service (APNs)**, then **Continue → Register**.
4. Click **Download** — save the `.p8` file somewhere safe on your computer. **You can only download it once.**
5. Note the **Key ID** — a 10-character code like `AB12CD34EF`.
6. Your **Team ID** is shown in the top-right corner when signed into developer.apple.com — also a 10-character code.

---

## Step 2 — Install the bridge on your Kitsu server

```bash
# Install Python if needed
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git

# Download the bridge
cd /opt
sudo git clone https://github.com/PeteDraper/kitsu-mobile-review-push-bridge.git kitsu-push-bridge
sudo chown -R $USER:$USER /opt/kitsu-push-bridge

# Create a Python virtual environment and install dependencies
cd /opt/kitsu-push-bridge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Step 3 — Upload your APNs key

Copy the `.p8` file you downloaded from Apple to a secure location on the server. From your local machine:

```bash
scp /path/to/AuthKey_XXXXXXXXXX.p8 youruser@your-server-ip:~
```

Then on the server:

```bash
sudo mkdir -p /etc/kitsu-push-bridge
sudo mv ~/AuthKey_XXXXXXXXXX.p8 /etc/kitsu-push-bridge/
sudo chmod 600 /etc/kitsu-push-bridge/AuthKey_XXXXXXXXXX.p8
```

> **Important:** The `.p8` file must end with a newline character. If APNs rejects your key, run:
> `echo "" >> /etc/kitsu-push-bridge/AuthKey_XXXXXXXXXX.p8`

---

## Step 4 — Configure the bridge

```bash
sudo nano /opt/kitsu-push-bridge/.env
```

Paste and fill in your values:

```env
# Kitsu server — full URL with protocol, no trailing slash
# Must be reachable from this server (localhost works if on the same machine)
KITSU_URL=http://192.168.1.2

# A Kitsu account with admin access used by the bridge as a service account
KITSU_EMAIL=admin@yourstudio.com
KITSU_PASSWORD=your-kitsu-password

# APNs credentials from developer.apple.com
APNS_KEY_PATH=/etc/kitsu-push-bridge/AuthKey_XXXXXXXXXX.p8
APNS_KEY_ID=XXXXXXXXXX
APNS_TEAM_ID=XXXXXXXXXX
APNS_BUNDLE_ID=com.kitsureview.app

# Use true for TestFlight/development builds, false for App Store production
APNS_SANDBOX=true

# Bridge HTTP server — listens on localhost, Nginx proxies externally
BRIDGE_HOST=127.0.0.1
BRIDGE_PORT=9090

# Token database location
DB_PATH=/opt/kitsu-push-bridge/bridge_tokens.db

# Logging: DEBUG, INFO, WARNING, ERROR
LOG_LEVEL=INFO
```

```bash
sudo chmod 600 /opt/kitsu-push-bridge/.env
```

---

## Step 5 — Test manually

```bash
cd /opt/kitsu-push-bridge
source venv/bin/activate
python3 main.py
```

You should see:

```
2026-01-01 12:00:00  INFO  bridge.main  Kitsu Push Bridge starting  kitsu=http://192.168.1.2 ...
2026-01-01 12:00:01  INFO  bridge.kitsu  Logged into Kitsu as admin@yourstudio.com
2026-01-01 12:00:01  INFO  bridge.kitsu  Socket.IO connected to Kitsu (/events)
```

Press `Ctrl+C` to stop.

---

## Step 6 — Add the bridge to your existing Nginx config

The bridge listens on `localhost:9090`. Add a `location` block to your **existing** Kitsu nginx config so the bridge is served under the same host.

```bash
sudo nano /etc/nginx/sites-available/kitsu   # or wherever your Kitsu nginx config lives
```

Add inside the `server {}` block that serves Kitsu:

```nginx
location /push-bridge/ {
    proxy_pass         http://127.0.0.1:9090/;
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_read_timeout 30s;
}
```

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Test it:

```bash
curl http://your-server-ip/push-bridge/health
# → {"status": "ok"}
```

---

## Step 7 — Run as a system service

```bash
sudo nano /etc/systemd/system/kitsu-push-bridge.service
```

```ini
[Unit]
Description=Kitsu Push Bridge
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/kitsu-push-bridge
ExecStart=/opt/kitsu-push-bridge/venv/bin/python3 main.py
EnvironmentFile=/opt/kitsu-push-bridge/.env
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo chown -R www-data:www-data /opt/kitsu-push-bridge
sudo chmod 600 /opt/kitsu-push-bridge/.env

sudo systemctl daemon-reload
sudo systemctl enable kitsu-push-bridge
sudo systemctl start kitsu-push-bridge
sudo systemctl status kitsu-push-bridge
```

View live logs:

```bash
sudo journalctl -u kitsu-push-bridge -f
```

---

## Updating the bridge

```bash
cd /opt/kitsu-push-bridge
sudo git pull
sudo systemctl restart kitsu-push-bridge
```

If `requirements.txt` changed:

```bash
sudo -u www-data /opt/kitsu-push-bridge/venv/bin/pip install -r requirements.txt
sudo systemctl restart kitsu-push-bridge
```

---

## HTTP API reference

The bridge exposes a minimal REST API used by the iOS app automatically on login/logout.

### `POST /push-tokens` — Register a device

```json
{
  "kitsu_user_id": "<uuid>",
  "device_token": "<64-char hex APNs token>",
  "kitsu_token": "<user's JWT from Kitsu login>"
}
```

The bridge verifies the `kitsu_token` against `/api/auth/authenticated` before storing the token. Returns `204 No Content`.

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

## APNs environment

`APNS_SANDBOX=true` is required for TestFlight and Xcode development builds.
Set `APNS_SANDBOX=false` for App Store production builds.

A device token is environment-specific — a sandbox token will not work with production APNs and vice versa. You will need to redeploy with `APNS_SANDBOX=false` when you publish to the App Store.

---

## Security notes

- Every token registration is verified against the Kitsu API — a valid Kitsu JWT is required. Only users with an active Kitsu session can register their device.
- Keep your `.p8` key file and `.env` outside version control. Use `chmod 600` on both.
- Use HTTPS (Nginx + Let's Encrypt) in production.
- The token database (`bridge_tokens.db`) contains APNs device tokens — protect it with appropriate file permissions.

---

## Troubleshooting

**Bridge will not connect to Kitsu**
```bash
curl http://192.168.1.2/api/health
curl -X POST http://192.168.1.2/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@yourstudio.com","password":"yourpassword"}'
```

**APNs rejects the key**
- Ensure the `.p8` file ends with a newline: `echo "" >> /etc/kitsu-push-bridge/AuthKey_XXXXXXXXXX.p8`
- Confirm `APNS_KEY_ID` and `APNS_TEAM_ID` match exactly what developer.apple.com shows.
- Confirm `APNS_BUNDLE_ID` matches your app's bundle identifier exactly (e.g. `com.kitsureview.app`).
- Check you are using the correct sandbox/production setting for your build type.

**Notifications do not arrive**
- Set `LOG_LEVEL=DEBUG`, restart, and watch `journalctl -u kitsu-push-bridge -f` while triggering an event in Kitsu.
- Confirm `Socket.IO connected to Kitsu (/events)` appears in the logs.
- Confirm `Registered APNs token for user` appeared when the iOS app logged in.

**Notifications arrive but tapping does not open the task**
- Ensure the iOS app build includes the notification tap handler.
- Confirm `notification_id` and `task_id` appear in the notification payload (visible in logs at DEBUG level).

---

## License

MIT
