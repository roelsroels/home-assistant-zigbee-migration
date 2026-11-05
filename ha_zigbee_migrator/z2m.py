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

    def supervised_pair(
        self,
        seconds: int,
        expected_ieee: str | None = None,
        interview_timeout: int = 90,
        join_via: str = "coordinator",
    ) -> dict[str, Any]:
        """Join exactly one device and return its successful interview."""
        expected = {expected_ieee} if expected_ieee else None
        results = self._supervised_pair_expected(
            seconds=seconds,
            expected_ieees=expected,
            expected_count=1,
            interview_timeout=interview_timeout,
            join_via=join_via,
        )
        return next(iter(results.values()))

    def supervised_pair_many(
        self,
        seconds: int,
        expected_ieees: set[str],
        interview_timeout: int = 90,
        join_via: str = "coordinator",
    ) -> dict[str, dict[str, Any]]:
        """Join a fixed set of known devices in one guarded pairing window."""
        normalized = {
            str(ieee).strip().lower() for ieee in expected_ieees if str(ieee).strip()
        }
        if not normalized:
            raise Z2MError("expected_ieees cannot be empty")
        return self._supervised_pair_expected(
            seconds=seconds,
            expected_ieees=normalized,
            expected_count=len(normalized),
            interview_timeout=interview_timeout,
            join_via=join_via,
        )

    def _supervised_pair_expected(
        self,
        seconds: int,
        expected_ieees: set[str] | None,
        expected_count: int,
        interview_timeout: int,
        join_via: str,
    ) -> dict[str, dict[str, Any]]:
        """Observe events and retained inventory until all expected devices finish."""
        devices_topic = f"{self.base}/bridge/devices"
        initial_devices = self.retained("bridge/devices")

        def inventory_fingerprint(device: dict[str, Any]) -> tuple[Any, ...]:
            """Capture fields that change when a retained device is re-interviewed."""
            return (
                str(device.get("interview_state", "")).upper(),
                bool(device.get("interview_completed")),
                bool(device.get("interviewing")),
                device.get("network_address"),
                device.get("supported"),
                device.get("model_id"),
            )

        initial_by_ieee = {
            str(device.get("ieee_address")).lower(): device
            for device in initial_devices
            if isinstance(device, dict) and device.get("ieee_address")
        }
        existing = set(initial_by_ieee)
        initial_fingerprints = {
            ieee: inventory_fingerprint(device)
            for ieee, device in initial_by_ieee.items()
        }
        expected_ieees = (
            {ieee.lower() for ieee in expected_ieees}
            if expected_ieees is not None
            else None
        )
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except Empty:
                break
        self.client.subscribe(f"{self.base}/bridge/event")
        join_via = join_via.strip()
        if not join_via:
            raise Z2MError("join_via cannot be empty")
        permit_payload: dict[str, Any] = {"time": seconds}
        if join_via.casefold() != "all":
            permit_payload["device"] = (
                "coordinator"
                if join_via.casefold() == "coordinator"
                else join_via
            )
        self.request("permit_join", permit_payload)
        join_description = (
            "all available coordinator/router paths"
            if join_via.casefold() == "all"
            else join_via
        )
        print(
            f"Joining is open through {join_description} for {seconds} seconds. "
            f"Factory-reset exactly {expected_count} intended device"
            f"{'s' if expected_count != 1 else ''} now."
        )
        successes: dict[str, dict[str, Any]] = {}
        candidates: set[str] = set()
        unexpected: set[str] = set()
        failed: set[str] = set()
        observed: list[dict[str, Any]] = []
        join_deadline = time.monotonic() + seconds
        interview_deadline: float | None = None
        join_closed = False
        fresh_event_ieees: set[str] = set()

        def all_candidates_seen() -> bool:
            if expected_ieees is None:
                return len(candidates) >= expected_count
            return expected_ieees.issubset(candidates)

        def all_successful() -> bool:
            if expected_ieees is None:
                return len(successes) >= expected_count
            return expected_ieees.issubset(successes)

        def close_after_candidates() -> None:
            nonlocal join_closed, interview_deadline
            if join_closed or not all_candidates_seen():
                return
            self.close_join()
            join_closed = True
            interview_deadline = time.monotonic() + interview_timeout
            if expected_count == 1:
                print(
                    "The expected device has appeared; joining is closed while its "
                    "interview finishes."
                )
            else:
                print(
                    f"All {expected_count} expected devices have appeared; joining "
                    "is closed while their interviews finish."
                )

        def inspect_inventory() -> None:
            inventory = self.messages.get(devices_topic)
            if not isinstance(inventory, list):
                return
            for device in inventory:
                if not isinstance(device, dict):
                    continue
                raw_ieee = device.get("ieee_address")
                if not raw_ieee:
                    continue
                ieee = str(raw_ieee).lower()
                if expected_ieees is not None and ieee not in expected_ieees:
                    if ieee not in existing:
                        unexpected.add(ieee)
                    continue
                if expected_ieees is None and ieee in existing:
                    continue
                successful = bool(device.get("interview_completed")) or str(
                    device.get("interview_state", "")
                ).upper() == "SUCCESSFUL"
                if ieee in existing:
                    changed = (
                        inventory_fingerprint(device)
                        != initial_fingerprints.get(ieee)
                    )
                    if not (
                        successful or changed or ieee in fresh_event_ieees
                    ):
                        continue
                candidates.add(ieee)
                if successful:
                    successes[ieee] = {
                        "ieee_address": ieee,
                        "friendly_name": device.get("friendly_name", ieee),
                        "status": "successful",
                        "supported": device.get("supported"),
                        "definition": device.get("definition"),
                        "source": "bridge/devices",
                    }

        try:
            while True:
                now = time.monotonic()
                deadline = interview_deadline or join_deadline
                if now >= deadline:
                    break
                try:
                    event = self.events.get(timeout=min(2, deadline - now))
                except Empty:
                    inspect_inventory()
                    if unexpected and not join_closed:
                        self.close_join()
                        join_closed = True
                        break
                    close_after_candidates()
                    if all_successful() or failed:
                        break
                    continue
                event_type = event.get("type")
                data = event.get("data") or {}
                raw_ieee = data.get("ieee_address")
                ieee = str(raw_ieee).lower() if raw_ieee else None
                observed.append(
                    {
                        "type": event_type,
                        "ieee_address": data.get("ieee_address"),
                        "status": data.get("status"),
                    }
                )
                if event_type not in {"device_joined", "device_interview"} or not ieee:
                    continue
                if expected_ieees is not None and ieee not in expected_ieees:
                    if ieee not in existing:
                        unexpected.add(ieee)
                        if not join_closed:
                            self.close_join()
                            join_closed = True
                        break
                    continue
                if expected_ieees is None and ieee in existing:
                    continue
                fresh_event_ieees.add(ieee)
                candidates.add(ieee)
                close_after_candidates()
                if event_type == "device_interview" and data.get("status") == "successful":
                    successes[ieee] = data
                    inspect_inventory()
                    if all_successful():
                        break
                if event_type == "device_interview" and data.get("status") == "failed":
                    failed.add(ieee)
                    inspect_inventory()
                    break
                inspect_inventory()
        finally:
            if not join_closed:
                self.close_join()
            print("Joining has been closed.")
        inspect_inventory()
        if unexpected:
            raise Z2MError(
                "An unexpected device appeared, so joining was closed immediately. "
                f"Expected={sorted(expected_ieees or [])}, "
                f"unexpected={sorted(unexpected)}, observed_events={observed}"
            )
        if failed:
            raise Z2MError(
                f"The intended device interview failed for {sorted(failed)}. No "
                "rename or entity mapping was attempted. Rerun the same migration "
                "command and keep sleepy battery devices awake with a brief button "
                "press every 2-3 seconds until all interviews finish."
            )
        if not candidates and not observed:
            retained_expected = (
                expected_ieees.intersection(existing)
                if expected_ieees is not None
                else set()
            )
            if retained_expected:
                detail = (
                    "An unchanged failed device entry already retained by "
                    "Zigbee2MQTT was correctly ignored so it could not close "
                    "joining prematurely."
                )
            else:
                detail = (
                    "The planned device never appeared in Zigbee2MQTT's inventory, "
                    "so this is a reset, range, or join-path problem rather than a "
                    "missed script event."
                )
            raise Z2MError(
                "No fresh device join or interview was observed. "
                + detail
                + " Rerun the same migration command and begin the factory reset "
                "only after the script says joining is open. If the device is not "
                "close to the coordinator, select a nearby router with --join-via."
            )
        if expected_ieees is not None:
            missing_candidates = expected_ieees - candidates
            missing_successes = expected_ieees - set(successes)
        else:
            missing_candidates = set()
            missing_successes = set()
        if (
            len(candidates) != expected_count
            or len(successes) != expected_count
            or missing_candidates
            or missing_successes
        ):
            raise Z2MError(
                f"Expected exactly {expected_count} successfully interviewed "
                f"device{'s' if expected_count != 1 else ''}; "
                f"candidates={sorted(candidates)}, successful={sorted(successes)}, "
                f"missing_candidates={sorted(missing_candidates)}, "
                f"missing_successes={sorted(missing_successes)}, "
                f"observed_events={observed}"
            )
        return successes

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
