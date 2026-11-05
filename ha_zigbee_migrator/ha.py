"""Minimal Home Assistant WebSocket API client."""

from __future__ import annotations

import json
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import websocket

from .config import HomeAssistantConfig


class HomeAssistantError(RuntimeError):
    """Raised for Home Assistant API failures."""


class HomeAssistantClient:
    def __init__(self, config: HomeAssistantConfig) -> None:
        self.config = config
        self.socket: websocket.WebSocket | None = None
        self.next_id = 1

    def __enter__(self) -> "HomeAssistantClient":
        sslopt = None
        if not self.config.verify_tls:
            sslopt = {"cert_reqs": ssl.CERT_NONE}
        try:
            self.socket = websocket.create_connection(
                self.config.websocket_url,
                timeout=20,
                sslopt=sslopt,
            )
            required = self._receive()
            if required.get("type") != "auth_required":
                raise HomeAssistantError(
                    f"Unexpected WebSocket greeting: {required}"
                )
            self.socket.send(
                json.dumps({"type": "auth", "access_token": self.config.token})
            )
            result = self._receive()
            if result.get("type") != "auth_ok":
                raise HomeAssistantError("Home Assistant authentication failed")
            return self
        except HomeAssistantError:
            self._close_safely()
            raise
        except (OSError, websocket.WebSocketException) as err:
            self._close_safely()
            raise HomeAssistantError(
                "Unable to connect to the Home Assistant WebSocket at "
                f"{self.config.websocket_url}. Check that this computer is on the "
                "correct LAN or VPN and that Home Assistant is reachable. "
                f"Original error: {err}"
            ) from err

    def __exit__(self, *_args: object) -> None:
        self._close_safely()

    def _close_safely(self) -> None:
        socket = self.socket
        self.socket = None
        if socket is not None:
            try:
                socket.close()
            except (OSError, websocket.WebSocketException):
                pass

    def _receive(self) -> dict[str, Any]:
        if self.socket is None:
            raise HomeAssistantError("WebSocket is not connected")
        return json.loads(self.socket.recv())

    def call(self, command_type: str, **payload: Any) -> Any:
        if self.socket is None:
            raise HomeAssistantError("WebSocket is not connected")
        command_id = self.next_id
        self.next_id += 1
        message = {"id": command_id, "type": command_type, **payload}
        try:
            self.socket.send(json.dumps(message))
            while True:
                response = self._receive()
                if response.get("id") != command_id:
                    continue
                if not response.get("success"):
                    raise HomeAssistantError(
                        f"{command_type} failed: {response.get('error')}"
                    )
                return response.get("result")
        except HomeAssistantError:
            raise
        except (OSError, websocket.WebSocketException) as err:
            self._close_safely()
            raise HomeAssistantError(
                "The Home Assistant WebSocket connection was lost while running "
                f"{command_type!r}. The completed migration checkpoint is retained; "
                "rerun the same command to resume safely. "
                f"Original error: {err}"
            ) from err

    def snapshot(self) -> dict[str, Any]:
        return {
            "config": self.call("get_config"),
            "states": self.call("get_states"),
            "entities": self.call("config/entity_registry/list"),
            "devices": self.call("config/device_registry/list"),
            "areas": self.call("config/area_registry/list"),
        }

    def entities(self) -> list[dict[str, Any]]:
        return self.call("config/entity_registry/list")

    def devices(self) -> list[dict[str, Any]]:
        return self.call("config/device_registry/list")

    def states(self) -> list[dict[str, Any]]:
        return self.call("get_states")

    def config_entries(self) -> list[dict[str, Any]]:
        """Return Home Assistant configuration entries."""
        return self.call("config_entries/get")

    def energy_preferences(self) -> dict[str, Any]:
        """Return the Energy dashboard configuration."""
        return self.call("energy/get_prefs")

    def delete_config_entry(self, entry_id: str) -> dict[str, Any]:
        """Delete one explicitly selected configuration entry via HA's REST API."""
        url = (
            f"{self.config.url.rstrip('/')}"
            f"/api/config/config_entries/entry/{quote(entry_id, safe='')}"
        )
        request = Request(
            url,
            method="DELETE",
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Accept": "application/json",
            },
        )
        context = None
        if not self.config.verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        try:
            with urlopen(request, timeout=60, context=context) as response:
                body = response.read()
        except HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace").strip()
            raise HomeAssistantError(
                "Home Assistant rejected removal of config entry "
                f"{entry_id!r} with HTTP {err.code}: {detail or err.reason}"
            ) from err
        except (OSError, URLError) as err:
            raise HomeAssistantError(
                "The Home Assistant REST connection failed while removing "
                f"config entry {entry_id!r}. No entity rename was attempted; "
                f"rerun the same command after connectivity is restored. "
                f"Original error: {err}"
            ) from err

        if not body:
            return {}
        try:
            result = json.loads(body)
        except json.JSONDecodeError as err:
            raise HomeAssistantError(
                "Home Assistant returned an invalid response after removing "
                f"config entry {entry_id!r}"
            ) from err
        if not isinstance(result, dict):
            raise HomeAssistantError(
                "Home Assistant returned an unexpected response after removing "
                f"config entry {entry_id!r}: {result!r}"
            )
        return result

    def remove_entity(self, entity_id: str) -> Any:
        """Remove one explicitly selected orphaned entity-registry entry."""
        return self.call("config/entity_registry/remove", entity_id=entity_id)

    def rename_entity(self, entity_id: str, new_entity_id: str) -> dict[str, Any]:
        return self.call(
            "config/entity_registry/update",
            entity_id=entity_id,
            new_entity_id=new_entity_id,
        )
