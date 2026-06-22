# kitsu-mobile-review-push-bridge

A lightweight Python service that bridges [Kitsu](https://www.cg-wire.com/en/kitsu.html) production events to iOS push notifications.

It connects to your Kitsu server over Socket.IO, listens for events, and forwards notifications to a hosted relay service that handles the Apple APNs delivery.

Designed to pair with [Kitsu Mobile Review](https://github.com/PeteDraper/kitsu-mobile-review) — available on the App Store.

> **Requirements:**
> - The bridge must be installed on the **same server** as your Kitsu/Zou instance, served by the same nginx.
> - No Apple Developer account needed. Push delivery is handled by the Kitsu Mobile Review relay — you only need the credentials below.

---

## How it works

```
Kitsu server  ──Socket.IO──▶  Push Bridge  ──HTTPS──▶  KMR Relay  ──APNs──▶  iOS device
                                    ▲
                              iOS app registers
                              APNs device token
                              on login (HTTP POST)
```

1. The bridge logs into Kitsu as a service account and maintains a persistent Socket.IO connection to the `/events` namespace.
2. The primary trigger is `notification:new` — Kitsu fires this for each person who should be notified.
3. The bridge fetches the notification record, builds the message, and POSTs to the relay over HTTPS.
4. The relay (hosted as part of the Kitsu Mobile Review platform) signs and delivers the notification to Apple APNs.
5. The iOS app registers its raw APNs device token with the bridge after login and unregisters on logout.
6. Dead tokens (app uninstalled, device reset) are removed automatically.

---

## Notification format

| Field | Content |
|-------|---------|
| **Title** | `Kitsu Mobile Review` |
| **Subtitle** | Event description (see table below) |
| **Body** | `Project / Entity Path / Task Type` |

| Kitsu type | Subtitle |
|------------|---------|
| Comment | `{Author} commented` |
| Comment with revision | `{Author} published a preview` |
| Mention in comment | `{Author} mentioned you` |
| Reply to comment | `{Author} replied` |
| Task assigned | `{Author} assigned you` |
| Playlist ready | `{Playlist name} is ready` |

**Tapping a notification** opens the task review screen directly in the app.

---

## Installation

### Step 1 — Install the bridge on your Kitsu server

SSH into your Kitsu server, then:

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git

cd /opt
sudo git clone https://github.com/PeteDraper/kitsu-mobile-review-push-bridge.git kitsu-push-bridge
sudo chown -R $USER:$USER /opt/kitsu-push-bridge

cd /opt/kitsu-push-bridge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

### Step 2 — Configure the bridge

```bash
sudo nano /opt/kitsu-push-bridge/.env
```

Paste and fill in your values:

```env
# Your Kitsu server — full URL with protocol, no trailing slash
KITSU_URL=http://192.168.1.2

# A Kitsu account with admin access used by the bridge as a service account
KITSU_EMAIL=admin@yourstudio.com
KITSU_PASSWORD=your-kitsu-password

# true = TestFlight / development builds  |  false = App Store builds
APNS_SANDBOX=true

# Push relay — same for all installations (see kitsu-mobile-review documentation)
RELAY_URL=https://YOUR_RELAY_DOMAIN/api/notify
RELAY_SECRET=YOUR_RELAY_SECRET
```

```bash
sudo chmod 600 /opt/kitsu-push-bridge/.env
```

`RELAY_URL` and `RELAY_SECRET` are provided in the Kitsu Mobile Review setup documentation. They are the same for every studio installation.

---

### Step 3 — Test manually

```bash
cd /opt/kitsu-push-bridge
source venv/bin/activate
python3 main.py
```

You should see:

```
INFO  bridge.main  Kitsu Push Bridge starting  kitsu=http://192.168.1.2 ...
INFO  bridge.kitsu  Logged into Kitsu as admin@yourstudio.com
INFO  bridge.kitsu  Socket.IO connected to Kitsu (/events)
```

Press `Ctrl+C` to stop.

---

### Step 4 — Add to your Nginx config

The bridge listens on `localhost:9090`. Add a `location` block to your existing Kitsu nginx config:

```nginx
location /push-bridge/ {
    proxy_pass         http://127.0.0.1:9090/;
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_read_timeout 30s;
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
curl http://your-server-ip/push-bridge/health
# → {"status": "ok"}
```

---

### Step 5 — Run as a system service

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

The directory is owned by `www-data`, so git commands must run as that user to avoid a "dubious ownership" error.

```bash
cd /opt/kitsu-push-bridge
sudo -u www-data git pull
sudo systemctl restart kitsu-push-bridge
```

If `requirements.txt` changed:

```bash
sudo -u www-data git pull
sudo -u www-data /opt/kitsu-push-bridge/venv/bin/pip install -r requirements.txt
sudo systemctl restart kitsu-push-bridge
```

---

## APNs environment

`APNS_SANDBOX=true` is required for TestFlight and Xcode development builds.
Set `APNS_SANDBOX=false` for App Store production builds.

A device token is environment-specific — a sandbox token will not work with production APNs and vice versa.

---

## HTTP API reference

### `POST /push-tokens` — Register a device

```json
{
  "kitsu_user_id": "<uuid>",
  "device_token": "<64-char hex APNs token>",
  "kitsu_token": "<user's JWT from Kitsu login>"
}
```

The bridge verifies the `kitsu_token` against Kitsu before storing. Returns `204 No Content`.

### `DELETE /push-tokens` — Unregister a device

```json
{
  "device_token": "<64-char hex APNs token>"
}
```

`kitsu_token` is accepted but not required — the device token alone is sufficient to identify the registration. Returns `204 No Content`.

### `GET /health` — Liveness check

Returns `{"status": "ok"}`.

---

## Security notes

- Every token registration is verified against the Kitsu API — a valid Kitsu JWT is required.
- The relay secret only grants permission to deliver push notifications to the Kitsu Mobile Review app. No account data can be accessed.
- Keep your `.env` outside version control (`chmod 600`).
- Use HTTPS (Nginx + Let's Encrypt) in production.
- The token database (`bridge_tokens.db`) contains APNs device tokens — protect it with appropriate file permissions.

---

## Troubleshooting

**Bridge will not connect to Kitsu**
```bash
curl http://192.168.1.2/api/health
```

**Notifications do not arrive**
- Set `LOG_LEVEL=DEBUG`, restart, watch logs while triggering an event in Kitsu.
- Confirm `Socket.IO connected` and `Registered APNs token for user` appear in the logs.
- Check `APNS_SANDBOX` matches your build type.
- Confirm `RELAY_URL` and `RELAY_SECRET` are set correctly.

**Notifications arrive but tapping does not open the task**
- Ensure the iOS app is up to date.

---

## License

MIT
