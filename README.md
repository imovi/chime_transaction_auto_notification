# Chime Deposit Detection & Telegram Alert Daemon

An automated, ultra-lightweight system for monitoring inbound deposits and transfers on Chime accounts running on GeeLark Cloud Phones, with instant real-time Telegram alerts, interactive multi-admin controls, and multi-device concurrent tracking.

---

## 🌟 Key Features

- **⚡️ Real-Time Deposit Alerts**: Automatically detects inbound transfers and deposits (`+$...`), extracting sender, amount, time, note, and account balance.
- **🛡 Anti-Duplicate Engine**: Multi-dimensional SHA-256 fingerprinting using `(device_serial + sender + amount + time + note + balance_after)` guarantees zero missed transactions and zero duplicate notifications, even when multiple identical-amount transfers arrive consecutively from the same sender.
- **📱 Multi-Device Concurrent Monitoring**: Built-in multi-threading (`ThreadPoolExecutor`) polls and monitors multiple GeeLark cloud phones in parallel every 30 seconds.
- **👑 Multi-Admin & Member Access Control**:
  - Full Admin Controllers have access to bot management, device controls, balance inquiries, and screen captures.
  - Regular Members receive clean payment alerts only (zero menus/keyboards).
  - Primary Super Admin is protected from demotion or removal.
- **🎛 Interactive Telegram Bot Control Panel**:
  - Check live balances and transaction history.
  - On-demand screen capture and pull-refresh.
  - Power ON / Power OFF cloud phones directly via GeeLark OpenAPI.
  - Set Chime app passcodes per device (`/setpin <serial> <pin>`).
  - Interactive User Directory (`/users`) to approve, promote, demote, or remove members.
- **🪶 Ultra Lightweight**: Uses only ~36 MB RAM, < 1% CPU, and runs on the lowest-tier VPS (512MB RAM / 1 vCPU).

---

## 📁 Project Structure

``
.
├── automation/
│   ├── chime_flow.py            # Android UI automator and unlock flow
│   ├── chime_monitor.py         # Dual-layer detection and polling loop
│   ├── device_manager.py        # Multi-device registry and PIN store
│   ├── seen_store.py            # SHA-256 deduplication store
│   ├── telegram_bot_service.py  # Interactive bot controls & multi-admin listener
│   └── transaction_parser.py    # Robust backward-scanning transaction parser
├── database/
│   └── db.py                    # SQLite multi-tenant subscriber and transaction store
├── geelark/
│   ├── client.py                # GeeLark OpenAPI HTTP client with signature auth
│   ├── phone.py                 # GeeLark cloud phone lifecycle and status API
│   └── shell.py                 # Remote ADB shell execution engine
├── config.py                    # Environment configuration loader
├── telegram_notifier.py         # Telegram Bot API dispatcher with rich Markdown
├── run_monitor.py               # Main CLI runner launching monitor and Telegram bot
└── requirements.txt             # Minimal dependencies (requests, python-dotenv)
``

---

## 🚀 Quick Setup & Installation

### 1. Clone the Repository
``bash
git clone https://github.com/imovi/chime_transaction_auto_notification.git
cd chime_transaction_auto_notification
``

### 2. Set Up Virtual Environment & Dependencies
``bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
``

### 3. Configure Environment Variables
Copy `.env.example` to `.env`:
``bash
cp .env.example .env
``
Edit `.env` with your GeeLark credentials and Telegram Bot information:
``ini
# GeeLark OpenAPI Configuration
GEELARK_BASE_URL=https://openapi.geelark.com
GEELARK_AUTH_MODE=token
GEELARK_BEARER_TOKEN=your_geelark_bearer_token

# Telegram Configuration
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_admin_chat_id

# Target Phone Configuration
CHIME_TARGET_PHONE_SERIAL=226
CHIME_TARGET_PHONE_NAME=Katie-Smith-18
CHIME_APP_PIN=1122
CHIME_POLL_INTERVAL=30
``

---

## 🏃 Running the Daemon

### Development / Local Run
``bash
python run_monitor.py
``

### 24/7 Background Service (Linux Systemd)
Create `/etc/systemd/system/chime-monitor.service`:
``ini
[Unit]
Description=Chime Deposit Telegram Monitor Daemon
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/chime_transaction_auto_notification
ExecStart=/opt/chime_transaction_auto_notification/venv/bin/python3 run_monitor.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
``
Start and enable the service:
``bash
sudo systemctl daemon-reload
sudo systemctl enable --now chime-monitor
``

---

## 💬 Telegram Bot Commands

| Command | Description |
|---|---|
| /menu | Open the main interactive control panel |
| /devices | List all cloud phones with status, balance, and Connect/Power buttons |
| /scan_devices | Scan GeeLark account and sync newly added cloud phones |
| /setpin <serial> <pin> | Set Chime passcode for a specific device |
| /users or /members | Open interactive user management directory |
| /add_admin <chat_id> [name] | Register a user as an Admin |
| /add_member <chat_id> [name] | Register a user as a notification-only Member (no menu) |
| /promote <chat_id> | Promote a member to Admin |
| /demote <chat_id> | Demote an Admin to Member |
| /remove <chat_id> | Delete a user from the system |

---

## 🧪 Testing

Run the automated test suite:
``bash
python -m unittest discover tests
``

---

## 📄 License
MIT License.
