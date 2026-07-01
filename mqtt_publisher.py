"""Publish weather + mesh telemetry + alerts to an MQTT broker with Home
Assistant MQTT Discovery, so everything shows up in HA as devices/sensors
without any manual YAML.

Topics (base_topic default "weather-mesh"):
  <base>/status                availability (online/offline, LWT)
  <base>/weather/state         JSON: temperature, humidity, wind_speed, …
  <base>/node/<num>/state      JSON: battery, snr, online, name
  <base>/alert/state           JSON: text, key, ts
Discovery configs go to <discovery_prefix>/<component>/<obj>/config (retained).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Optional

try:
    import paho.mqtt.client as mqtt
    PAHO_AVAILABLE = True
except Exception:  # pragma: no cover
    mqtt = None
    PAHO_AVAILABLE = False

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "host": "127.0.0.1",
    "port": 1883,
    "username": "",
    "password": "",
    "base_topic": "weather-mesh",
    "discovery_prefix": "homeassistant",
    "interval_s": 60,
    "publish_weather": True,
    "publish_nodes": True,
    "publish_alerts": True,
}

# key → (Friendly name, unit, device_class or None)
_WEATHER_SENSORS = {
    "temperature":          ("Температура", "°C", "temperature"),
    "apparent_temperature": ("Ощущается",   "°C", "temperature"),
    "humidity":             ("Влажность",   "%",  "humidity"),
    "wind_speed":           ("Ветер",       "m/s", "wind_speed"),
    "pressure":             ("Давление",    "hPa", "pressure"),
    "precipitation":        ("Осадки",      "mm", "precipitation"),
}


def _new_client(client_id: str):
    try:
        return mqtt.Client(client_id=client_id,
                           callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=client_id)


class MqttPublisher:
    def __init__(self, get_cfg: Callable[[], dict],
                 get_weather: Callable[[], Optional[dict]],
                 get_nodes: Callable[[], list],
                 get_last_alert: Callable[[], Optional[dict]]):
        self._get_cfg = get_cfg
        self._get_weather = get_weather
        self._get_nodes = get_nodes
        self._get_last_alert = get_last_alert
        self._client = None
        self._lock = threading.Lock()
        self._connected = False
        self._announced_nodes: set = set()
        self._weather_announced = False
        self._alert_announced = False
        self._sig = None                 # connection-params signature
        self._last_publish_ts = 0
        self._last_error = ""

    # ------------------------------------------------------------------

    def _cfg(self) -> dict:
        return {**DEFAULTS, **(self._get_cfg().get("mqtt") or {})}

    def status(self) -> dict:
        c = self._cfg()
        return {
            "available": PAHO_AVAILABLE,
            "enabled": bool(c.get("enabled")),
            "connected": self._connected,
            "last_publish_ts": self._last_publish_ts,
            "last_error": self._last_error,
            "host": c.get("host"),
            "port": c.get("port"),
        }

    def reconfigure(self) -> None:
        """Force a reconnect on the next loop tick (params may have changed)."""
        with self._lock:
            self._disconnect_locked()

    def start_worker(self) -> threading.Thread:
        t = threading.Thread(target=self._loop, daemon=True, name="mqtt-pub")
        t.start()
        return t

    # ------------------------------------------------------------------

    def _loop(self):
        time.sleep(20)
        while True:
            interval = 60
            try:
                c = self._cfg()
                interval = max(10, int(c.get("interval_s", 60)))
                if c.get("enabled") and PAHO_AVAILABLE:
                    self._ensure_connected(c)
                    if self._connected:
                        self._publish_all(c)
                else:
                    with self._lock:
                        self._disconnect_locked()
            except Exception as exc:
                self._last_error = str(exc)
                log.exception("MQTT loop error (will retry)")
            time.sleep(interval)

    def _ensure_connected(self, c: dict) -> None:
        sig = (c.get("host"), c.get("port"), c.get("username"), c.get("password"))
        with self._lock:
            if self._client is not None and sig != self._sig:
                self._disconnect_locked()          # params changed → reconnect
            if self._connected and self._client is not None:
                return
            base = c.get("base_topic") or "weather-mesh"
            client = _new_client(f"{base}-bridge")
            if c.get("username"):
                client.username_pw_set(c.get("username"), c.get("password") or None)
            client.will_set(f"{base}/status", "offline", retain=True)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            try:
                client.connect(c.get("host") or "127.0.0.1", int(c.get("port") or 1883), keepalive=60)
                client.loop_start()
                self._client = client
                self._sig = sig
                self._last_error = ""
            except Exception as exc:
                self._last_error = f"connect: {exc}"
                self._connected = False
                try:
                    client.loop_stop()
                except Exception:
                    pass
                self._client = None

    def _on_connect(self, client, userdata, flags, *args):
        # Reason code is args[0] in v2; treat presence of client as success.
        self._connected = True
        self._last_error = ""
        base = self._cfg().get("base_topic") or "weather-mesh"
        try:
            client.publish(f"{base}/status", "online", retain=True)
        except Exception:
            pass
        # Force discovery re-announce after a (re)connect.
        self._announced_nodes.clear()
        self._weather_announced = False
        self._alert_announced = False
        log.info("MQTT connected to broker")

    def _on_disconnect(self, client, userdata, *args):
        self._connected = False

    def _disconnect_locked(self):
        if self._client is not None:
            try:
                base = self._cfg().get("base_topic") or "weather-mesh"
                self._client.publish(f"{base}/status", "offline", retain=True)
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
        self._client = None
        self._connected = False
        self._sig = None

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def _pub(self, topic: str, payload, retain: bool = True):
        try:
            data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            self._client.publish(topic, data, retain=retain)
        except Exception as exc:
            self._last_error = f"publish: {exc}"

    def _device(self, ident: str, name: str) -> dict:
        return {
            "identifiers": [ident],
            "name": name,
            "manufacturer": "weather-mesh-bridge",
            "model": "Weather → Mesh",
        }

    def _publish_all(self, c: dict):
        base = c.get("base_topic") or "weather-mesh"
        disc = c.get("discovery_prefix") or "homeassistant"
        avail = f"{base}/status"

        if c.get("publish_weather", True):
            self._publish_weather(base, disc, avail)
        if c.get("publish_nodes", True):
            self._publish_nodes(base, disc, avail)
        if c.get("publish_alerts", True):
            self._publish_alert(base, disc, avail)
        self._last_publish_ts = int(time.time())

    def _publish_weather(self, base, disc, avail):
        try:
            state = self._get_weather()
        except Exception as exc:
            self._last_error = f"weather: {exc}"
            return
        if not state:
            return
        dev = self._device(f"{base}_weather", "Weather Bridge")
        if not self._weather_announced:
            for key, (name, unit, dclass) in _WEATHER_SENSORS.items():
                cfg = {
                    "name": name,
                    "unique_id": f"{base}_weather_{key}",
                    "state_topic": f"{base}/weather/state",
                    "value_template": f"{{{{ value_json.{key} }}}}",
                    "unit_of_measurement": unit,
                    "availability_topic": avail,
                    "device": dev,
                }
                if dclass:
                    cfg["device_class"] = dclass
                    cfg["state_class"] = "measurement"
                self._pub(f"{disc}/sensor/{base}_weather/{key}/config", cfg)
            self._weather_announced = True
        self._pub(f"{base}/weather/state", state)

    def _publish_nodes(self, base, disc, avail):
        try:
            nodes = self._get_nodes() or []
        except Exception as exc:
            self._last_error = f"nodes: {exc}"
            return
        now = int(time.time())
        for n in nodes:
            num = n.get("num")
            if num is None:
                continue
            nid = f"{base}_node_{num}"
            nname = n.get("long_name") or n.get("short_name") or n.get("node_id") or f"Node {num}"
            last_heard = int(n.get("last_heard") or 0)
            online = "online" if last_heard and (now - last_heard) < 7200 else "offline"
            state = {
                "name": nname,
                "battery": n.get("battery_level"),
                "voltage": n.get("voltage"),
                "snr": n.get("snr"),
                "online": online,
            }
            if num not in self._announced_nodes:
                dev = self._device(nid, f"Mesh: {nname}")
                self._pub(f"{disc}/sensor/{nid}/battery/config", {
                    "name": "Battery", "unique_id": f"{nid}_battery",
                    "state_topic": f"{base}/node/{num}/state",
                    "value_template": "{{ value_json.battery }}",
                    "unit_of_measurement": "%", "device_class": "battery",
                    "state_class": "measurement", "availability_topic": avail, "device": dev,
                })
                self._pub(f"{disc}/sensor/{nid}/snr/config", {
                    "name": "SNR", "unique_id": f"{nid}_snr",
                    "state_topic": f"{base}/node/{num}/state",
                    "value_template": "{{ value_json.snr }}",
                    "unit_of_measurement": "dB", "state_class": "measurement",
                    "availability_topic": avail, "device": dev,
                })
                self._pub(f"{disc}/binary_sensor/{nid}/online/config", {
                    "name": "Online", "unique_id": f"{nid}_online",
                    "state_topic": f"{base}/node/{num}/state",
                    "value_template": "{{ value_json.online }}",
                    "payload_on": "online", "payload_off": "offline",
                    "device_class": "connectivity", "availability_topic": avail, "device": dev,
                })
                self._announced_nodes.add(num)
            self._pub(f"{base}/node/{num}/state", state)

    def _publish_alert(self, base, disc, avail):
        try:
            alert = self._get_last_alert()
        except Exception:
            alert = None
        dev = self._device(f"{base}_weather", "Weather Bridge")
        if not self._alert_announced:
            self._pub(f"{disc}/sensor/{base}_alert/last/config", {
                "name": "Последний алерт",
                "unique_id": f"{base}_alert_last",
                "state_topic": f"{base}/alert/state",
                "value_template": "{{ value_json.text }}",
                "availability_topic": avail, "device": dev, "icon": "mdi:alert",
            })
            self._alert_announced = True
        if alert:
            self._pub(f"{base}/alert/state", {
                "text": (alert.get("text") or "")[:255],
                "key": alert.get("key") or "",
                "ts": alert.get("ts") or 0,
            })


def test_connection(c: dict) -> dict:
    """One-shot connect + test publish. c is the `mqtt` config section."""
    if not PAHO_AVAILABLE:
        return {"ok": False, "error": "paho-mqtt не установлен на сервере"}
    c = {**DEFAULTS, **(c or {})}
    client = _new_client((c.get("base_topic") or "wmb") + "-test")
    if c.get("username"):
        client.username_pw_set(c.get("username"), c.get("password") or None)
    try:
        client.connect(c.get("host") or "127.0.0.1", int(c.get("port") or 1883), keepalive=10)
        client.loop_start()
        for _ in range(30):
            if client.is_connected():
                break
            time.sleep(0.1)
        ok = client.is_connected()
        if ok:
            client.publish((c.get("base_topic") or "weather-mesh") + "/test", "ok")
        client.loop_stop()
        client.disconnect()
        return {"ok": ok, "error": "" if ok else "не удалось подключиться (проверь хост/порт/логин)"}
    except Exception as exc:
        try:
            client.loop_stop()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}


__all__ = ["MqttPublisher", "test_connection", "DEFAULTS", "PAHO_AVAILABLE"]
