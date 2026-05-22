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

## What you need before starting

- An Ubuntu Linux server (18.04 or later). This can be the same machine your Kitsu instance runs on, or a separate one. It needs to be able to reach your Kitsu server over the network.
- A way to type commands on that server — either directly at the keyboard, or via SSH from your own computer (see the SSH tip below).
- An **Apple Developer account** (paid, $99/year) — needed to create the APNs key that lets the bridge send notifications to iPhones.
- Your Kitsu instance already running and accessible.

> **Connecting via SSH from your Mac or Windows PC:**  
> Open Terminal (Mac) or PowerShell (Windows) and type:  
> `ssh youruser@your-server-ip`  
> Press Enter, type your password when asked. You are now controlling your Ubuntu server remotely.

---

## Step 1 — Get your Apple APNs key

This is a credential file (`.p8`) that proves to Apple you are allowed to send push notifications to your app. You only do this once.

1. Go to [developer.apple.com](https://developer.apple.com) and sign in
2. Click **Account** in the top menu, then go to **Certificates, Identifiers & Profiles**
3. In the left sidebar click **Keys**
4. Click the blue **+** button to create a new key
5. Give it any name (e.g. `KitsuPushBridge`), tick **Apple Push Notifications service (APNs)**, then click **Continue** → **Register**
6. Click **Download** — save this `.p8` file somewhere safe on your computer. **You can only download it once.**
7. On the same page, note the **Key ID** — it's a 10-character code like `AB12CD34EF`
8. Your **Team ID** is shown in the top-right corner of developer.apple.com when you are signed in — another 10-character code

You will need the `.p8` file, the Key ID, and the Team ID in Step 4.

---

## Step 2 — Install the bridge on your server

Open a terminal on your Ubuntu server and run each of these commands one at a time. Lines starting with `#` are just explanations — you don't need to type those.

### Check Python is installed

```bash
python3 --version
```

You should see something like `Python 3.10.x` or higher. If you see `command not found`, install it:

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
```

> `sudo` means "run this as administrator". Ubuntu will ask for your password the first time.

### Download the bridge code

```bash
# Move to /opt — a standard place on Linux to install services
cd /opt

# Download the code from GitHub
sudo git clone https://github.com/PeteDraper/kitsu-mobile-review-push-bridge.git kitsu-push-bridge

# Move into the folder
cd /opt/kitsu-push-bridge
```

### Install Python dependencies into an isolated environment

```bash
# Create a self-contained Python environment just for this service
sudo python3 -m venv .venv

# Install the required Python packages into it
sudo .venv/bin/pip install -r requirements.txt
```

This downloads and installs everything the bridge needs without affecting anything else on your server.

---

## Step 3 — Upload your APNs key

You need to copy the `.p8` file from your computer to the server. A safe place to store it is `/etc/kitsu-push-bridge/`.

```bash
# Create the directory for the key
sudo mkdir -p /etc/kitsu-push-bridge
sudo chmod 700 /etc/kitsu-push-bridge
```

Now, **from your own computer** (not on the server), open a second terminal window and run:

```bash
scp /path/to/AuthKey_XXXXXXXXXX.p8 youruser@your-server-ip:~/
```

Replace `/path/to/AuthKey_XXXXXXXXXX.p8` with the actual location of the file on your computer (e.g. `~/Downloads/AuthKey_AB12CD34EF.p8`), and `youruser@your-server-ip` with your server login details.

> Uploading directly to `/etc/kitsu-push-bridge/` will fail with "Permission denied" because that folder is owned by root. Uploading to `~/` (your home folder) works fine — we move it in the next step.

Back on the server, move the key into place and lock it down:

```bash
sudo mv ~/AuthKey_XXXXXXXXXX.p8 /etc/kitsu-push-bridge/
sudo chmod 600 /etc/kitsu-push-bridge/AuthKey_XXXXXXXXXX.p8
```

---

## Step 4 — Configure the bridge

The bridge reads its settings from a file called `.env`. Copy the example file and fill it in:

```bash
sudo cp /opt/kitsu-push-bridge/.env.example /opt/kitsu-push-bridge/.env
sudo nano /opt/kitsu-push-bridge/.env
```

> `nano` is a simple text editor built into Ubuntu. Use the arrow keys to move around. When you are done, press `Ctrl+X`, then `Y`, then `Enter` to save.

Fill in each value:

| Setting | What to put |
|---|---|
| `KITSU_URL` | The URL of your Kitsu server, e.g. `http://192.168.1.50` or `https://kitsu.mystudio.com` — no trailing slash |
| `KITSU_EMAIL` | Email address of a Kitsu admin account. Create a dedicated one called e.g. `push-bridge@mystudio.com` rather than using a person's login |
| `KITSU_PASSWORD` | Password for that account |
| `APNS_KEY_PATH` | `/etc/kitsu-push-bridge/AuthKey_XXXXXXXXXX.p8` — use the actual filename of your key |
| `APNS_KEY_ID` | The 10-character Key ID you noted in Step 1 |
| `APNS_TEAM_ID` | The 10-character Team ID from developer.apple.com |
| `APNS_BUNDLE_ID` | The bundle ID of the iOS app, e.g. `com.mystudio.kitsu-client` |
| `APNS_SANDBOX` | `true` if you are using a development/TestFlight build, `false` for App Store builds |
| `BRIDGE_HOST` | Leave as `127.0.0.1` (the bridge will only be reachable locally, which is safer) |
| `BRIDGE_PORT` | Leave as `9090` |
| `BRIDGE_SECRET` | A random password the iOS app will send to prove it's allowed to register. Make something up, e.g. `hunter2-studio-push` |
| `DB_PATH` | Leave as `./bridge_tokens.db` |
| `LOG_LEVEL` | Leave as `INFO` |

Lock down the `.env` file so only the system can read it (it contains passwords):

```bash
sudo chmod 600 /opt/kitsu-push-bridge/.env
```

> **About `APNS_SANDBOX`:** If you built the app yourself with Expo and installed it via USB or internal TestFlight, use `true`. If you downloaded it from the App Store, use `false`. Using the wrong one causes notifications to silently fail.

---

## Step 5 — Test it manually first

Before setting it up to run automatically, let's make sure it works:

```bash
cd /opt/kitsu-push-bridge
sudo .venv/bin/python3 main.py
```

You should see output like:

```
2025-01-01 12:00:00  INFO      bridge.main  Kitsu Push Bridge starting  kitsu=http://...
2025-01-01 12:00:01  INFO      bridge.kitsu  Logged into Kitsu as push-bridge@...
2025-01-01 12:00:01  INFO      bridge.kitsu  Socket.IO connected to Kitsu
```

If you see errors, double-check your `.env` values. Press `Ctrl+C` to stop it once you're happy it's working.

---

## Step 6 — Run it automatically as a background service

Right now the bridge only runs while your terminal is open. To make it start automatically and keep running in the background (even after a reboot), we register it as a **systemd service** — Ubuntu's built-in system for managing long-running programs.

Create the service file:

```bash
sudo nano /etc/systemd/system/kitsu-push-bridge.service
```

Paste in the following (press `Ctrl+Shift+V` to paste in most terminals):

```ini
[Unit]
Description=Kitsu Push Bridge
After=network.target

[Service]
User=root
WorkingDirectory=/opt/kitsu-push-bridge
EnvironmentFile=/opt/kitsu-push-bridge/.env
ExecStart=/opt/kitsu-push-bridge/.venv/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save and close (`Ctrl+X` → `Y` → `Enter`), then activate it:

```bash
# Tell systemd about the new service
sudo systemctl daemon-reload

# Start it now AND make it start automatically on every boot
sudo systemctl enable --now kitsu-push-bridge
```

Check it's running:

```bash
sudo systemctl status kitsu-push-bridge
```

You should see `Active: active (running)` in green. To watch the live log output:

```bash
sudo journalctl -fu kitsu-push-bridge
```

Press `Ctrl+C` to stop watching the logs (the service keeps running).

---

## Step 7 — Make it reachable from outside your network (Nginx)

The bridge currently only listens on `127.0.0.1:9090` — it cannot be reached from the internet, which is intentional. If your Kitsu server is already behind Nginx (common with Kitsu installations), you can add the bridge as a sub-path so the iOS app can register from anywhere.

Check if Nginx is installed:

```bash
nginx -v
```

If it isn't: `sudo apt install -y nginx`

Find your existing Kitsu Nginx config (usually in `/etc/nginx/sites-enabled/`) and add this block inside the `server { }` section:

```nginx
location /push-bridge/ {
    proxy_pass http://127.0.0.1:9090/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

Reload Nginx to apply the change:

```bash
sudo nginx -t        # check for typos — should say "syntax is ok"
sudo systemctl reload nginx
```

The bridge is now reachable at `https://kitsu.mystudio.com/push-bridge/health` — open that URL in a browser to confirm you see `{"status":"ok"}`.

In the iOS app login screen, enter the Push Bridge URL as:
```
https://kitsu.mystudio.com/push-bridge
```

> If your Kitsu is not yet behind HTTPS, see the [Certbot / Let's Encrypt guide](https://certbot.eff.org/instructions?ws=nginx&os=ubuntufocal) to add a free SSL certificate. The iOS app requires HTTPS.

---

## Useful commands

```bash
# Check if the bridge is running
sudo systemctl status kitsu-push-bridge

# Restart it (e.g. after changing .env)
sudo systemctl restart kitsu-push-bridge

# Stop it
sudo systemctl stop kitsu-push-bridge

# Watch live logs
sudo journalctl -fu kitsu-push-bridge

# See the last 50 log lines
sudo journalctl -u kitsu-push-bridge -n 50
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

Tapping any notification opens the task review screen directly in the iOS app.

---

## HTTP API reference

The bridge exposes a minimal REST API used by the iOS app automatically — you don't need to call these yourself.

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

## Security notes

- The bridge verifies every registration request against the Kitsu API — a valid Kitsu JWT is required to store a token.
- Set `BRIDGE_SECRET` to a strong random string and configure it in the iOS app if the bridge is internet-accessible.
- Keep your `.p8` key and `.env` file outside version control and restrict file permissions (`chmod 600`).
- Use HTTPS in production (Nginx + Let's Encrypt).

---

## License

MIT
