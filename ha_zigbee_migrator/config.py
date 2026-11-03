"""Configuration loading."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tomllib
from typing import Any
from urllib.parse import urlparse, urlunparse


class ConfigError(RuntimeError):
    """Raised when configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class HomeAssistantConfig:
    url: str
    token: str
    verify_tls: bool

    @property
    def websocket_url(self) -> str:
        parsed = urlparse(self.url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = parsed.path.rstrip("/") + "/api/websocket"
        return urlunparse((scheme, parsed.netloc, path, "", "", ""))


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    base_topic: str
    tls: bool


@dataclass(frozen=True)
class PathsConfig:
    output_dir: Path
    home_assistant_config: Path | None


@dataclass(frozen=True)
class SafetyConfig:
    permit_join_seconds: int
    expected_coordinator_type_contains: str


@dataclass(frozen=True)
class AppConfig:
    home_assistant: HomeAssistantConfig
    mqtt: MqttConfig
    paths: PathsConfig
    safety: SafetyConfig


def _env_value(name: str, required: bool) -> str | None:
    value = os.environ.get(name)
    if required and not value:
        raise ConfigError(f"Required environment variable {name!r} is not set")
    return value


def _env_name(value: Any, setting: str, default: str) -> str:
    """Validate that an *_env setting contains a variable name, not a secret."""
    name = str(value or default).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ConfigError(
            f"{setting} must be an environment-variable name such as {default!r}, "
            "not the credential itself"
        )
    return name


def load_config(path: str | Path) -> AppConfig:
    """Load a TOML configuration file without storing secrets in it."""
    config_path = Path(path).expanduser().resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    ha_raw = raw.get("home_assistant", {})
    mqtt_raw = raw.get("mqtt", {})
    paths_raw = raw.get("paths", {})
    safety_raw = raw.get("safety", {})

    ha_url = str(ha_raw.get("url", "")).rstrip("/")
    if not ha_url.startswith(("http://", "https://")):
        raise ConfigError("home_assistant.url must start with http:// or https://")

    token_env = _env_name(
        ha_raw.get("token_env"), "home_assistant.token_env", "HA_TOKEN"
    )
    mqtt_user_env = _env_name(
        mqtt_raw.get("username_env"), "mqtt.username_env", "MQTT_USERNAME"
    )
    mqtt_password_env = _env_name(
        mqtt_raw.get("password_env"), "mqtt.password_env", "MQTT_PASSWORD"
    )
    mqtt_host = str(mqtt_raw.get("host", "")).strip()
    if not mqtt_host:
        raise ConfigError("mqtt.host must not be empty")

    output_value = str(paths_raw.get("output_dir", "./migration-output"))
    output_dir = (config_path.parent / output_value).resolve()
    ha_config_value = str(paths_raw.get("home_assistant_config", "")).strip()
    ha_config_path = (
        Path(ha_config_value).expanduser().resolve() if ha_config_value else None
    )

    permit_seconds = int(safety_raw.get("permit_join_seconds", 60))
    if permit_seconds < 15 or permit_seconds > 120:
        raise ConfigError("permit_join_seconds must be between 15 and 120")

    return AppConfig(
        home_assistant=HomeAssistantConfig(
            url=ha_url,
            token=_env_value(token_env, required=True) or "",
            verify_tls=bool(ha_raw.get("verify_tls", True)),
        ),
        mqtt=MqttConfig(
            host=mqtt_host,
            port=int(mqtt_raw.get("port", 1883)),
            username=_env_value(mqtt_user_env, required=False),
            password=_env_value(mqtt_password_env, required=False),
            base_topic=str(mqtt_raw.get("base_topic", "zigbee2mqtt")).strip("/"),
            tls=bool(mqtt_raw.get("tls", False)),
        ),
        paths=PathsConfig(
            output_dir=output_dir,
            home_assistant_config=ha_config_path,
        ),
        safety=SafetyConfig(
            permit_join_seconds=permit_seconds,
            expected_coordinator_type_contains=str(
                safety_raw.get("expected_coordinator_type_contains", "ConBee2")
            ),
        ),
    )
