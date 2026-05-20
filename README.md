# Weather → Heltec Mesh Bridge

🇬🇧 English · [🇷🇺 Русский](README.ru.md)

A Raspberry Pi bot that broadcasts weather updates into a Meshtastic mesh network through a Heltec Mesh Node V4 — on a schedule, with a modern glass web UI, node map, traceroute visualisation and full Heltec configuration in the browser.

![Tabs](https://img.shields.io/badge/tabs-Dashboard%20%C2%B7%20Map%20%C2%B7%20Settings%20%C2%B7%20Chat-6aa3ff)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- 🌍 **Weather** — Open-Meteo (no API key required) for forecast, air quality, UV index, and marine sea-surface temperature
- 📡 **Heltec link** — USB cable or Wi-Fi (TCP) via the official `meshtastic` Python library
- 🎨 **Glass-style web UI** with four tabs:
  - 📊 **Dashboard** — live stats: connected nodes, RSSI, hops, messages per 24h
  - 🗺 **Map** — Leaflet map of all nodes with positions, plus traceroute overlay
  - ⚙️ **Settings** — city, message style, Heltec connection, weather alerts, schedule, manual send
  - 💬 **Chat** — multi-conversation messenger (channels + DMs) with reactions, replies, delivery indicators and RF metadata
- ⚙️ **Heltec device configuration from the browser** — change long/short name, LoRa region, role, hop limit, modem preset, TX power; reboot the node
- 🛰 **Traceroute** — measure path to any node, see forward + return hops with SNR, then visualise it on the map (red arrows there, blue arrows back)
- ✓✓ **Delivery indicators** in chat (☁ enroute / ✓✓ delivered / ⚠ error) — DMs request ACK; statuses update live
- ⚠️ **Weather alerts** — thunderstorm, strong wind, heavy rain, frost, **heat** — deduplicated per day
- ⏰ **Scheduler** — APScheduler with cron triggers, any number of slots with their own field set, days of the week and timezone
- 📡 **Auto-chunking** — long messages are split into `(i/N)`-prefixed packets with a configurable inter-chunk pause
- 🛠 **One-line systemd autostart**
- 🔒 **HTTPS** out of the box with a self-signed certificate (browser notifications work even on LAN)

## What goes into a message

You pick the fields independently per slot:

- 🌡 Temperature + sky condition
- 🤔 Apparent (feels-like) temperature
- 📊 Comparison with yesterday (`+3°C warmer` / `−2°C colder`)
- 🌊 Water temperature (marine API for coastal coords; 7-day air-temp-based estimate for inland rivers/lakes, prefixed with `≈`)
- 💧 Humidity
- ⏲ Pressure (mmHg)
- 💨 Wind (m/s + direction + gusts)
- 🌧 Precipitation
- 🌫 Air quality (European AQI + PM2.5 + PM10)
- ☀️ UV index (with risk label)
- 📅 Today's forecast (min/max + chance of rain)
- 🌅 Tomorrow morning (09:00) & evening (21:00) forecast

Emojis are an opt-in toggle; there is also a "Header" toggle (`Weather — City`). Example with emojis off:

```
Weather — Moscow
+3.4°C, partly cloudy
Feels like +0.8°C
on 5°C colder than yesterday
humidity 78% · pressure 750.2 mmHg
wind 4.2 m/s W (gusts 8)
today -1…+5°C · light rain · chance of rain 60%
Tomorrow · +5…+14°C · mostly clear
morning +7°C partly cloudy 10% · evening +11°C mostly clear
```

## Hardware / software

- Raspberry Pi 4 (or any Linux box with Python 3.10+)
- Heltec Mesh Node V4 (or compatible Heltec board) flashed with Meshtastic firmware
- Wi-Fi router or USB cable to connect the two

## Install on Raspberry Pi

> On first run the bot creates a default `config.json`. Want a clean start? Copy the example:
> ```bash
> cp config.example.json config.json
> ```
> All settings then live in `config.json` and are edited from the web UI.

1. Flash Heltec V4 with Meshtastic ([guide](https://meshtastic.org/docs/getting-started/flashing-firmware/heltec/)) and plug it into the Pi via USB (or set up Wi-Fi later).
2. Clone or copy the project onto the Pi:
   ```bash
   git clone https://github.com/Vexorter42/weather-mesh-bridge.git /home/pi/weather-mesh-bridge
   cd /home/pi/weather-mesh-bridge
   ```
3. Run the installer:
   ```bash
   bash install.sh
   ```
   It creates a venv, installs dependencies and adds your user to the `dialout` group (you must log out / reboot once).
4. Verify the device shows up:
   ```bash
   ls /dev/ttyUSB* /dev/ttyACM*
   ```
   Expect `/dev/ttyUSB0` or similar.
5. Run it manually for the first test:
   ```bash
   source .venv/bin/activate
   python app.py
   ```
6. Open `https://<pi-ip>:5000` from any device on the same LAN.
   The bot generates a self-signed certificate on first run; your browser will warn
   "Connection is not secure" — click **Advanced → Continue** once and the warning
   will be remembered. To disable HTTPS and run plain HTTP, start with
   `WMB_HTTPS=0 python app.py` (or edit the systemd unit accordingly).

## Autostart (systemd)

```bash
sudo cp weather-mesh-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now weather-mesh-bridge
sudo journalctl -u weather-mesh-bridge -f   # follow logs
```

If your username or path differ — edit `weather-mesh-bridge.service` first.

## Using the UI

### 📊 Dashboard tab

Top-of-page live overview:

- **Heltec connection** status, configured destination, latest path
- **Nodes known** / **unique senders in last 24h** / **total messages**
- Last 24h: **sent** / **received** / **avg RSSI** / **avg hops**
- **Latest mesh activity** — last incoming and last outgoing timestamps
- **Mesh nodes list** — click any node to open its profile (telemetry, position, action buttons)

### 🗺 Map tab

Leaflet map of nodes that have positions. Click a marker for a quick popup; the "Open profile" link opens the same modal as the dashboard list. After running a traceroute, the path is drawn here:

- 🔴 Red solid line with arrows — forward path (request)
- 🔵 Blue dashed line with arrows — return path (response)
- 🛰 Green pin — your bot
- 🎯 Yellow pin — destination
- Numbered blue pins — intermediate relays

### ⚙️ Settings tab

1. **City** — type a name in any language, pick from the list. Coordinates and timezone are filled in for you.
2. **Message style** — toggles for emojis, the header line, and chat commands (`/погода`, `/ping`, `/nodes`, `/help`).
3. **Heltec V4** — pick the connection type:
   - **USB cable** — `auto` finds the device by itself.
   - **Wi-Fi (TCP)** — IP and port (default 4403).
   - **Chunk delay** — pause between parts of an over-sized message.
   - **Test connection** actively opens the link and shows the exact error if it fails.
   - **⚙️ Heltec settings** — opens a modal where you can change the node's long/short name, LoRa region, role, hop limit, modem preset, TX power, and reboot the device — all without the Meshtastic mobile app.
4. **Weather alerts** — enable per-condition thresholds for thunderstorm / wind / rain / **frost** / **heat**, with a daily dedup so the channel doesn't get spammed.
5. **Schedule** — slots with time, timezone, weekdays and a field set. Edits are auto-saved. Each slot shows its next run time.
6. **Send now** — manual test. Preview builds the message without sending.

### 💬 Chat tab

Two-column messenger:

- **Sidebar** — every channel + every DM peer, with unread counts and previews.
- **Main pane** — bubbles with avatars, replies, reactions (long-press emoji in Meshtastic), and RF metadata for incoming packets (hops · RSSI · SNR, color-coded).

Outgoing messages now show a delivery indicator next to the time:

| Icon | Meaning |
|---|---|
| ☁ | **Enroute** — sent to the air, no ACK yet |
| ✓✓ | **Delivered** — destination acknowledged (DMs only). On hover: "ACK via N hops" |
| ⚠ | **Error** — routing failed |

The bot replies to chat commands when enabled in settings: `/погода`, `/ping`, `/nodes`, `/help` (and `!` works as a prefix too).

## REST API

| Method | Path | What it does |
|---|---|---|
| GET    | `/api/config` | Current config |
| POST   | `/api/config` | Partial update (location / mesh / message / alerts) |
| GET    | `/api/cities?q=...` | City search |
| GET    | `/api/fields` | List of available message fields |
| GET    | `/api/schedules` | All slots |
| POST   | `/api/schedules` | Create a slot |
| PATCH  | `/api/schedules/<id>` | Update a slot |
| POST   | `/api/schedules/<id>/run` | Run a slot's payload immediately |
| DELETE | `/api/schedules/<id>` | Remove a slot |
| POST   | `/api/preview` | Build a message without sending it |
| POST   | `/api/send` | Build & send right now |
| GET    | `/api/mesh/status` | Connection state |
| POST   | `/api/mesh/connect` | Force-open the connection |
| POST   | `/api/mesh/traceroute` | Send traceroute, return forward + return paths |
| GET    | `/api/heltec/info` | Current Heltec settings (name, region, role, ...) |
| POST   | `/api/heltec/settings` | Update Heltec settings (partial) |
| POST   | `/api/heltec/reboot` | Reboot the Heltec node |
| GET    | `/api/chat/messages?since=<id>&status_for=<ids>` | New chat messages + delivery status updates |
| POST   | `/api/chat/send` | Send free-form text into the mesh |
| POST   | `/api/chat/reply` | Reply to a previous message |
| POST   | `/api/chat/react` | React with an emoji |
| GET    | `/api/nodes` | All nodes the Heltec has heard from |
| GET    | `/api/channels` | All configured Meshtastic channels |
| GET    | `/api/alerts/status` | Last check + recent alerts |
| POST   | `/api/alerts/check` | Force a check right now |
| GET    | `/api/stats` | Dashboard counters |
| GET    | `/api/scheduler/jobs` | Active jobs + next-run timestamps |

## File layout

```
.
├── app.py                       # Flask + APScheduler
├── weather.py                   # Open-Meteo client + formatter
├── weather_alerts.py            # alerts watcher
├── meshbridge.py                # meshtastic-python wrapper, message buffer, traceroute, ACK tracking
├── chat_db.py                   # SQLite store for chat history + delivery status
├── commands.py                  # `/команды` handler
├── tls_certs.py                 # self-signed cert generator
├── config.example.json          # template for config.json
├── requirements.txt
├── install.sh
├── weather-mesh-bridge.service  # systemd unit
├── templates/index.html
└── static/
    ├── style.css                # glassmorphism theme
    └── app.js                   # all frontend logic
```

## Troubleshooting

- **`Permission denied: '/dev/ttyUSB0'`** — your user is not in the `dialout` group. Add and reboot:
  `sudo usermod -aG dialout $USER && sudo reboot`
- **`Heltec USB device not found`** — check the cable (charge-only ones won't work), `lsusb` and `dmesg | tail` after plugging in. Heltec V4 enumerates as CP210x or CH9102.
- **`No route to host` over Wi-Fi** — Heltec changed its DHCP IP, or your router has client/AP isolation enabled. Reserve the Heltec IP in your router's DHCP table to avoid surprises.
- **`Data payload too big`** — the bot now auto-chunks long messages, but if you still see this, drop a field from the slot or shorten the city name.
- **Schedule didn't fire** — open `/api/scheduler/jobs` in the browser to see `next_run`. Most likely the bot was offline at exactly that minute (default 1-hour misfire grace covers brief restarts).
- **Wrong send time** — check the slot's timezone, then `timedatectl` on the Pi to make sure clock is synchronized.
- **Delivery indicator stuck at ☁** — only DMs get end-to-end ACK; broadcasts will stay at ☁ forever, which is correct. For a DM, if it stays at ☁ for more than ~30s the destination is probably offline or unreachable.
- **Traceroute times out** — the destination may be a multi-hop away in a flaky network. Increase `hop_limit` or retry; LoRa is slow and ACKs can take 15-30 seconds.

## License

MIT — see [LICENSE](LICENSE).
