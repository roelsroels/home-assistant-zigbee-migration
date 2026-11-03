"""Minimal Home Assistant WebSocket API client."""

from __future__ import annotations

import json
import ssl
from typing import Any

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
        self.socket = websocket.create_connection(
            self.config.websocket_url,
            timeout=20,
            sslopt=sslopt,
        )
        required = self._receive()
        if required.get("type") != "auth_required":
            raise HomeAssistantError(f"Unexpected WebSocket greeting: {required}")
        self.socket.send(
            json.dumps({"type": "auth", "access_token": self.config.token})
        )
        result = self._receive()
        if result.get("type") != "auth_ok":
            raise HomeAssistantError("Home Assistant authentication failed")
        return self

    def __exit__(self, *_args: object) -> None:
        if self.socket is not None:
            self.socket.close()
        self.socket = None

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

    def states(self) -> list[dict[str, Any]]:
        return self.call("get_states")

    def rename_entity(self, entity_id: str, new_entity_id: str) -> dict[str, Any]:
        return self.call(
            "config/entity_registry/update",
            entity_id=entity_id,
            new_entity_id=new_entity_id,
        )
