# Weather → Heltec Mesh Bridge

🇬🇧 English · [🇷🇺 Русский](README.ru.md)

A Raspberry Pi bot that broadcasts weather updates into a Meshtastic mesh network through a Heltec Mesh Node V4 — on a schedule, with a clean web UI.

- weather source — **Open-Meteo** (no API key required)
- Heltec link — **USB cable** or **Wi-Fi (TCP)** via the official `meshtastic` Python library
- Flask web UI with two tabs: **Settings** and **Chat**
- chat shows incoming mesh messages (subscribed via `pypubsub`), supports replies, reactions and shows hop/RSSI/SNR for every received packet
- scheduler — **APScheduler** with cron triggers, any number of slots with their own field set, days of week and timezone
- long messages are auto-split into `(1/N)`-prefixed chunks with a configurable inter-chunk pause
- one-line systemd autostart
- HTTPS out of the box with an auto-generated self-signed certificate (so browser notifications work even on a LAN-only deployment)

## What goes into a message

You pick the fields independently per slot:

- temperature + sky condition
- "feels like" temperature
- humidity
- pressure (mmHg)
- wind (m/s + direction + gusts)
- precipitation
- today's forecast (min/max + chance of rain)
- tomorrow morning & evening forecast (09:00 and 21:00 in the location's timezone)

Emojis are an opt-in toggle; the bot also has a "Header" toggle (`Weather — City` line). Example with emojis off:

```
Weather — Moscow
+3.4°C, partly cloudy
Feels like +0.8°C
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
2. Copy the project folder onto the Pi (e.g. `/home/pi/weather-mesh-bridge`).
3. Run the installer:
   ```bash
   cd /home/pi/weather-mesh-bridge
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

### Settings tab

1. **City** — type a name in any language, pick from the list. Coordinates and timezone are filled in for you.
2. **Message style** — toggles for emojis and for the header line.
3. **Heltec V4** — pick the connection type:
   - **USB cable** — `auto` finds the device by itself.
   - **Wi-Fi (TCP)** — IP and port (default 4403). To put Heltec on Wi-Fi, open the Meshtastic mobile app → Module Config → Wi-Fi, set SSID/password, reboot. The OLED screen will show the IP.
   - **Chunk delay** — pause between parts of an over-sized message. 10 s is a safe default for LoRa; raise to 12–15 s for crowded networks.
   - **Test connection** actively opens the link and shows the exact error if it fails.
4. **Schedule** — slots with time, timezone, weekdays and a field set. Edits are auto-saved. Each slot shows its next run time.
5. **Send now** — manual test. Preview button builds the message without sending.

### Chat tab

Shows the last ~200 text messages your node hears. Sender name is resolved from Meshtastic's node table. You can reply directly from the UI — the message goes broadcast on the channel selected in Heltec settings.

Each received message has a small RF metadata strip below it:

- **hops** — number of mesh relays the packet went through (0 means direct contact)
- **RSSI** — last-hop signal strength in dBm, color-coded (green ≥ −90, yellow −90…−110, red below)
- **SNR** — signal-to-noise ratio

When someone reacts to a message in the Meshtastic app (long-press → emoji), the reaction appears as a small chip under the original message instead of as a separate text line. Identical reactions stack with a counter.

If you're on the Settings tab and a new message arrives, the Chat tab gets an unread counter badge.

## REST API

| Method | Path | What it does |
|---|---|---|
| GET    | `/api/config` | Current config |
| POST   | `/api/config` | Partial update (location / mesh / message) |
| GET    | `/api/cities?q=...` | City search |
| GET    | `/api/fields` | List of available message fields |
| GET    | `/api/schedules` | All slots |
| POST   | `/api/schedules` | Create a slot |
| PATCH  | `/api/schedules/<id>` | Update a slot |
| DELETE | `/api/schedules/<id>` | Remove a slot |
| POST   | `/api/preview` | Build a message without sending it |
| POST   | `/api/send` | Build & send right now |
| GET    | `/api/mesh/status` | Connection state |
| POST   | `/api/mesh/connect` | Force-open the connection |
| GET    | `/api/chat/messages?since=<id>` | New chat messages |
| POST   | `/api/chat/send` | Send free-form text into the mesh |
| GET    | `/api/scheduler/jobs` | Active jobs + next-run timestamps |

## File layout

```
.
├── app.py                       # Flask + APScheduler
├── weather.py                   # Open-Meteo client + formatter
├── meshbridge.py                # meshtastic-python wrapper, message buffer
├── config.example.json          # template for config.json
├── requirements.txt
├── install.sh
├── weather-mesh-bridge.service  # systemd unit
├── templates/index.html
└── static/
    ├── style.css
    └── app.js
```

## Troubleshooting

- **`Permission denied: '/dev/ttyUSB0'`** — your user is not in the `dialout` group. Add and reboot:
  `sudo usermod -aG dialout $USER && sudo reboot`
- **`Heltec USB device not found`** — check the cable (charge-only ones won't work), `lsusb` and `dmesg | tail` after plugging in. Heltec V4 enumerates as CP210x or CH9102.
- **`No route to host` over Wi-Fi** — Heltec changed its DHCP IP, or your router has client/AP isolation enabled. Reserve the Heltec IP in your router's DHCP table to avoid surprises.
- **`Data payload too big`** — the bot now auto-chunks long messages, but if you still see this, drop a field from the slot or shorten the city name.
- **Schedule didn't fire** — open `/api/scheduler/jobs` in the browser to see `next_run`. Most likely the bot was offline at exactly that minute (default 1-hour misfire grace covers brief restarts).
- **Wrong send time** — check the slot's timezone, then `timedatectl` on the Pi to make sure clock is synchronized.

## License

MIT — see [LICENSE](LICENSE).
