# Kitsu Mobile Review — Push Bridge

A lightweight service that connects your [Kitsu](https://www.cg-wire.com/en/kitsu.html) server to the **Kitsu Mobile Review** iOS app, delivering real-time push notifications to your team.

> **Requirements**
> - A Kitsu / Zou server running on Linux
> - The bridge must be installed on the same server as your Kitsu instance
> - [Kitsu Mobile Review](https://apps.apple.com/app/kitsu-mobile-review) installed from the App Store

---

## How it works

```
Kitsu server  ──Socket.IO──▶  Push Bridge  ──HTTPS──▶  KMR Relay  ──APNs──▶  iOS device
```

The bridge runs as a background service on your Kitsu server. It monitors Kitsu for activity and forwards notifications to the Kitsu Mobile Review relay, which handles Apple delivery. No Apple Developer account is needed.

---

## Notifications

| Event | Notification |
|-------|-------------|
| New comment | `{Author} commented` |
| Preview published | `{Author} published a preview` |
| Mentioned in comment | `{Author} mentioned you` |
| Reply to comment | `{Author} replied` |
| Task assigned | `{Author} assigned you` |
| Playlist ready | `{Playlist name} is ready` |

Tapping a notification opens the relevant task directly in the app.

---

## Installation

### Step 1 — Install the bridge

SSH into your Kitsu server:

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git

sudo git clone https://github.com/PeteDraper/kitsu-mobile-review-push-bridge.git /opt/kitsu-push-bridge
sudo chown -R $USER:$USER /opt/kitsu-push-bridge

cd /opt/kitsu-push-bridge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

### Step 2 — Configure

```bash
nano /opt/kitsu-push-bridge/.env
```

```env
# Full URL to your Kitsu instance — no trailing slash
KITSU_URL=http://192.168.1.2

# A Kitsu admin account used exclusively by the bridge
KITSU_EMAIL=push-bridge@yourstudio.com
KITSU_PASSWORD=your-password
```

```bash
chmod 600 /opt/kitsu-push-bridge/.env
```

---

### Step 3 — Test

```bash
cd /opt/kitsu-push-bridge
source venv/bin/activate
python3 main.py
```

You should see:

```
INFO  Kitsu Push Bridge starting ...
INFO  Logged into Kitsu as push-bridge@yourstudio.com
INFO  Socket.IO connected to Kitsu (/events)
```

Press `Ctrl+C` to stop.

---

### Step 4 — Add to Nginx

Add a `location` block to your existing Kitsu nginx config:

```nginx
location /push-bridge/ {
    proxy_pass         http://0.0.0.0:9090/;
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

### Step 5 — Run as a service

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

## Updating

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

## Troubleshooting

**Bridge will not connect to Kitsu**
```bash
curl http://your-kitsu-ip/api/health
```
Check that `KITSU_URL` matches the address your server is actually reachable on.

**Notifications not arriving**
- Confirm `Socket.IO connected` appears in the logs.
- Confirm `Registered APNs token for user` appears after a team member logs in.
- Check `sudo journalctl -u kitsu-push-bridge -f` while triggering an event in Kitsu.
- Ensure the app was installed from the App Store (not a dev/TestFlight build).

**Notifications arrive but tapping does not open the task**
- Ensure the app is up to date from the App Store.

---

## Security

- Token registrations are verified against the Kitsu API — a valid session is required.
- Keep `.env` protected: `chmod 600 /opt/kitsu-push-bridge/.env`
- The token database (`bridge_tokens.db`) contains device identifiers — keep it on the same restricted path.
- HTTPS on your Kitsu server (nginx + Let's Encrypt) is strongly recommended.

---

## License

MIT
