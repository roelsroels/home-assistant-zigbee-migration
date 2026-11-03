"""Zigbee2MQTT MQTT request and event client."""

from __future__ import annotations

import json
from queue import Empty, Queue
import threading
import time
from typing import Any
import uuid

import paho.mqtt.client as mqtt

from .config import MqttConfig


class Z2MError(RuntimeError):
    """Raised for MQTT or Zigbee2MQTT failures."""


class Z2MClient:
    def __init__(self, config: MqttConfig) -> None:
        self.config = config
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"ha-zigbee-migration-{uuid.uuid4().hex[:8]}",
            protocol=mqtt.MQTTv311,
        )
        if config.username:
            self.client.username_pw_set(config.username, config.password)
        if config.tls:
            self.client.tls_set()
        self.connected = threading.Event()
        self.messages: dict[str, Any] = {}
        self.condition = threading.Condition()
        self.events: Queue[dict[str, Any]] = Queue()
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def __enter__(self) -> "Z2MClient":
        self.client.connect(self.config.host, self.config.port, keepalive=30)
        self.client.loop_start()
        if not self.connected.wait(timeout=15):
            self.client.disconnect()
            self.client.loop_stop()
            raise Z2MError("Timed out connecting to MQTT")
        return self

    def __exit__(self, *_args: object) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    @property
    def base(self) -> str:
        return self.config.base_topic

    def _on_connect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            return
        self.connected.set()

    def _on_message(
        self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage
    ) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = message.payload.decode("utf-8", errors="replace")
        with self.condition:
            self.messages[message.topic] = payload
            self.condition.notify_all()
        if message.topic == f"{self.base}/bridge/event" and isinstance(payload, dict):
            self.events.put(payload)

    def retained(self, suffix: str, timeout: float = 10) -> Any:
        topic = f"{self.base}/{suffix.strip('/')}"
        self.client.subscribe(topic)
        deadline = time.monotonic() + timeout
        with self.condition:
            while topic not in self.messages:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Z2MError(f"No retained payload received for {topic}")
                self.condition.wait(timeout=remaining)
            return self.messages[topic]

    def request(self, path: str, payload: dict[str, Any], timeout: float = 20) -> Any:
        transaction = uuid.uuid4().hex
        request_topic = f"{self.base}/bridge/request/{path.strip('/')}"
        response_topic = f"{self.base}/bridge/response/{path.strip('/')}"
        body = dict(payload)
        body["transaction"] = transaction
        self.client.subscribe(response_topic)
        self.client.publish(request_topic, json.dumps(body), qos=1)
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                response = self.messages.get(response_topic)
                if isinstance(response, dict) and response.get("transaction") == transaction:
                    if response.get("status") != "ok":
                        raise Z2MError(f"Z2M request {path} failed: {response}")
                    return response.get("data")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Z2MError(f"Timed out waiting for {response_topic}")
                self.condition.wait(timeout=remaining)

    def snapshot(self) -> dict[str, Any]:
        return {
            "bridge_info": self.retained("bridge/info"),
            "devices": self.retained("bridge/devices"),
            "groups": self.retained("bridge/groups"),
            "bridge_state": self.retained("bridge/state"),
        }

    def close_join(self) -> None:
        self.request("permit_join", {"time": 0})

    def supervised_pair(self, seconds: int) -> dict[str, Any]:
        """Open coordinator-only joining and return one successfully interviewed device."""
        existing = {
            device.get("ieee_address")
            for device in self.retained("bridge/devices")
            if isinstance(device, dict)
        }
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except Empty:
                break
        self.client.subscribe(f"{self.base}/bridge/event")
        self.request(
            "permit_join",
            {"time": seconds, "device": "coordinator"},
        )
        print(
            f"Joining is open through the coordinator for {seconds} seconds. "
            "Factory-reset exactly one intended device now."
        )
        successes: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                try:
                    event = self.events.get(timeout=min(2, deadline - time.monotonic()))
                except Empty:
                    continue
                if event.get("type") != "device_interview":
                    continue
                data = event.get("data") or {}
                if data.get("status") != "successful":
                    continue
                ieee = data.get("ieee_address")
                if ieee and ieee not in existing:
                    successes[ieee] = data
                    # Keep listening briefly so an accidental second join is detected.
                    grace_deadline = min(deadline, time.monotonic() + 4)
                    while time.monotonic() < grace_deadline:
                        try:
                            extra = self.events.get(
                                timeout=min(1, grace_deadline - time.monotonic())
                            )
                        except Empty:
                            continue
                        if extra.get("type") != "device_interview":
                            continue
                        extra_data = extra.get("data") or {}
                        extra_ieee = extra_data.get("ieee_address")
                        if (
                            extra_data.get("status") == "successful"
                            and extra_ieee
                            and extra_ieee not in existing
                        ):
                            successes[extra_ieee] = extra_data
                    break
        finally:
            self.close_join()
            print("Joining has been closed.")
        if len(successes) != 1:
            raise Z2MError(
                f"Expected exactly one newly interviewed device, got {len(successes)}"
            )
        return next(iter(successes.values()))

    def rename_device(self, device_id: str, friendly_name: str) -> Any:
        return self.request(
            "device/rename",
            {
                "from": device_id,
                "to": friendly_name,
                "homeassistant_rename": True,
            },
        )

    def backup_data(self) -> bytes:
        import base64

        data = self.request("backup", {})
        encoded = (data or {}).get("zip")
        if not encoded:
            raise Z2MError("Z2M backup response did not include a zip payload")
        return base64.b64decode(encoded)
