# RMA Bot — Remote Machine Administration via Telegram

> **Legal notice** — This tool is for administering **machines you own** or
> have explicit written authorisation to manage. Unauthorised deployment
> constitutes a criminal offence in most jurisdictions.

---

## Architecture

```
Telegram ←──── aiogram 3 bot (bot.py) ────┐
                        │                  │
               Telegram Admin              │
               Sends commands              ▼
                                  aiohttp WebSocket Server
                                     ws://host:8765/ws
                                           │
                      ┌──────┬─────────────┤
                      ▼      ▼             ▼
                  PC-1    PC-2         Android
                rma_client.py     rma_client.py (Kivy)
```

---

## Quick Start

### 1 — Create a Telegram Bot

1. Open [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the **BOT_TOKEN**
3. Find your Telegram user ID with [@userinfobot](https://t.me/userinfobot)

### 2 — Configure & Run the Bot Server

```bash
# Clone / copy the project
cd rma_bot/

# Install dependencies
pip install -r requirements.txt

# Set environment variables (Linux/macOS)
export BOT_TOKEN="1234567890:ABCdef..."
export ADMIN_ID="987654321"
export WS_HOST="0.0.0.0"          # bind to all interfaces
export WS_PORT="8765"
export SHARED_SECRET="$(openssl rand -hex 32)"

# Windows PowerShell
$env:BOT_TOKEN="1234567890:ABCdef..."
$env:ADMIN_ID="987654321"
$env:SHARED_SECRET="some_long_random_string_at_least_32_chars"

python bot.py
```

> **Port forwarding** — if your bot server is behind NAT, forward port 8765
> or use a reverse proxy (nginx, Cloudflare Tunnel, ngrok).

### 3 — Generate a Client Script

In Telegram, open the bot and press **"➕ Generate Client"**.
The bot sends you `rma_client.py` pre-configured with:
- Your WebSocket server URL
- The shared secret (HMAC key)
- A unique device token

### 4 — Deploy the Client (Windows)

```powershell
# On the target machine
pip install pyautogui pillow psutil websocket-client websockets

# Run directly (development)
python rma_client.py

# OR build a standalone EXE
pyinstaller --onefile --noconsole rma_client.py
# → dist/rma_client.exe
```

### 5 — Deploy the Client (Linux)

```bash
pip install pyautogui pillow psutil websocket-client websockets
python rma_client.py
# Or run as systemd service (see below)
```

#### Systemd service (Linux)

```ini
# /etc/systemd/system/rma-client.service
[Unit]
Description=RMA Client Agent
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/rma/rma_client.py
Restart=always
RestartSec=10
Environment="WS_URL=ws://your-server:8765/ws"

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now rma-client
```

---

## Telegram Commands

| Command | Action |
|---------|--------|
| `/start` | Open main dashboard |
| `/menu`  | Same as /start |

### Dashboard Buttons

| Button | Action |
|--------|--------|
| 📋 Device List | Show all connected machines |
| ➕ Generate Client | Download pre-configured client script |
| 📦 Build Installer | Download PyInstaller .spec file |
| ℹ️ Bot Status | Server stats |

### Per-Device Controls

| Button | Action |
|--------|--------|
| 📸 Screenshot | Captures and sends current screen |
| 📊 Processes | Lists running processes; tap to kill |
| 💻 Shell Command | Runs any CLI command; streams output |
| 📁 Execute File | Upload EXE/BAT/PS1/PY to run remotely |
| 🔌 Alt+F4 | Sends Alt+F4 to active window |
| ⚠️ Shutdown | Graceful OS shutdown (with confirmation) |
| 🔃 Reboot | OS reboot (with confirmation) |
| 🔒 Lock Screen | Locks the workstation |
| 📂 File Browser | Lists home/working directory |
| 🔊/🔇 Volume | Increases/decreases system volume |
| 🌐 System Info | CPU, RAM, disk, uptime, IP |

---

## Security Model

| Layer | Mechanism |
|-------|-----------|
| Telegram auth | Only `ADMIN_ID` user can issue commands |
| WS auth | HMAC-SHA256 signed hello handshake on connect |
| Message integrity | Every WS message signed with HMAC-SHA256 |
| Transport | TLS — put nginx/Caddy in front for `wss://` |
| Device isolation | Each device gets a unique `DEVICE_ID` (UUID5) |

### Recommended production hardening

1. Use `wss://` (TLS) — put nginx or Caddy in front:
   ```nginx
   location /ws {
       proxy_pass http://127.0.0.1:8765/ws;
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
   }
   ```
2. Set a strong 32+ character `SHARED_SECRET`
3. Rotate the secret periodically; regenerate clients

---

## File Structure

```
rma_bot/
├── bot.py              ← Telegram bot + WebSocket server
├── client/
│   └── rma_client.py   ← Client agent template
├── requirements.txt
└── README.md
```

---

## Android Support

The client contains a Kivy-based Android entry point.
Build with [buildozer](https://buildozer.readthedocs.io/):

```bash
pip install buildozer
buildozer init
# Edit buildozer.spec:
#   requirements = python3,kivy,websocket-client,pillow,requests
buildozer android debug deploy run
```

Available Android commands: `screenshot` (root/ADB), `sysinfo`, `shell`.

---

## Extending

Add a new command in **three steps**:

1. **Client** — add a method in `CommandRouter._register_all()`:
   ```python
   self.register("my_cmd", self._my_cmd)
   def _my_cmd(self, params: dict):
       return {"result": "hello from client"}
   ```
2. **Bot keyboard** — add a button in `kb_device_control()`:
   ```python
   b.button(text="🆕 My Command", callback_data=f"cmd:my_cmd:{d}")
   ```
3. **Bot handler** — add a branch in `cb_command()`:
   ```python
   if cmd_name == "my_cmd":
       resp = await send_command(dev, "my_cmd", timeout=10)
       await query.message.answer(str(resp))
       return
   ```
