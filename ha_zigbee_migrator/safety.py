"""Safety interlocks and durable file helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any


class SafetyError(RuntimeError):
    """Raised when a safety condition fails."""


def timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def confirm_yes_no(message: str) -> bool:
    """Ask for a default-safe, case-insensitive Y/N confirmation."""
    print(f"\nSAFETY CONFIRMATION: {message}")
    while True:
        entered = input("Continue? [y/N]: ").strip().lower()
        if entered in {"y", "yes"}:
            return True
        if entered in {"", "n", "no"}:
            return False
        print("Please answer Y or N.")


def require_confirmation(message: str) -> None:
    """Require an affirmative Y/N answer before a mutating operation."""
    if not confirm_yes_no(message):
        raise SafetyError("Cancelled; no change was made")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "zigbee_device"


def coordinator_identity(info: dict[str, Any]) -> dict[str, Any]:
    coordinator = info.get("coordinator") or {}
    network = info.get("network") or {}
    config = info.get("config") or {}
    serial = config.get("serial") or {}
    advanced = config.get("advanced") or {}
    return {
        "ieee_address": coordinator.get("ieee_address"),
        "type": coordinator.get("type"),
        "meta": coordinator.get("meta"),
        "serial_port": serial.get("port"),
        "serial_adapter": serial.get("adapter"),
        "channel": network.get("channel", advanced.get("channel")),
        "pan_id": network.get("pan_id", advanced.get("pan_id")),
        "extended_pan_id": network.get(
            "extended_pan_id", advanced.get("ext_pan_id")
        ),
    }


def assert_coordinator_matches(
    current_info: dict[str, Any], baseline: dict[str, Any]
) -> None:
    current = coordinator_identity(current_info)
    expected = baseline.get("coordinator_identity") or {}
    keys = (
        "ieee_address",
        "type",
        "serial_port",
        "serial_adapter",
        "channel",
        "pan_id",
        "extended_pan_id",
    )
    differences = [
        f"{key}: expected {expected.get(key)!r}, got {current.get(key)!r}"
        for key in keys
        if expected.get(key) != current.get(key)
    ]
    if differences:
        raise SafetyError(
            "Z2M coordinator identity or network changed. Refusing to continue:\n- "
            + "\n- ".join(differences)
        )
