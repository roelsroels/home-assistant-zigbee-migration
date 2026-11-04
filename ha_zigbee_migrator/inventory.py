"""Inventory, planning, auditing and verification."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable

from .safety import slugify, write_json


@dataclass(frozen=True)
class PlanRow:
    migration_id: str
    old_device_id: str
    old_device_name: str
    old_entity_ids: tuple[str, ...]
    target_friendly_name: str
    status: str
    notes: str


PLAN_FIELDS = (
    "migration_id",
    "old_device_id",
    "old_device_name",
    "old_entity_ids",
    "target_friendly_name",
    "status",
    "notes",
)


def create_plan(snapshot: dict[str, Any], plan_path: Path, map_path: Path) -> int:
    """Create initial deCONZ device and entity mapping files without overwriting."""
    if plan_path.exists() or map_path.exists():
        raise RuntimeError(
            "Migration plan already exists; it was not overwritten. Back it up or "
            "remove it deliberately before generating a new plan."
        )
    ha = snapshot["home_assistant"]
    entities = [e for e in ha["entities"] if e.get("platform") == "deconz"]
    devices_by_id = {d.get("id"): d for d in ha["devices"]}
    by_device: dict[str, list[dict[str, Any]]] = {}
    for entity in entities:
        device_id = entity.get("device_id") or "entity-only:" + entity["entity_id"]
        by_device.setdefault(device_id, []).append(entity)

    plan_path.parent.mkdir(parents=True, exist_ok=True)
    map_rows: list[dict[str, str]] = []
    prepared_devices: list[tuple[str, str, list[dict[str, Any]]]] = []
    for device_id, device_entities in by_device.items():
        device = devices_by_id.get(device_id, {})
        name = (
            device.get("name_by_user")
            or device.get("name")
            or device_entities[0].get("name")
            or device_entities[0]["entity_id"]
        )
        prepared_devices.append((str(name), device_id, device_entities))

    with plan_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PLAN_FIELDS)
        writer.writeheader()
        for index, (name, device_id, device_entities) in enumerate(
            sorted(prepared_devices, key=lambda item: item[0].casefold()), start=1
        ):
            migration_id = f"D{index:03d}"
            entity_ids = sorted(entity["entity_id"] for entity in device_entities)
            writer.writerow(
                {
                    "migration_id": migration_id,
                    "old_device_id": device_id,
                    "old_device_name": name,
                    "old_entity_ids": ";".join(entity_ids),
                    "target_friendly_name": slugify(name),
                    "status": "pending",
                    "notes": "",
                }
            )
            for entity_id in entity_ids:
                map_rows.append(
                    {
                        "migration_id": migration_id,
                        "old_entity_id": entity_id,
                        "new_entity_id": "",
                        "status": "pending",
                        "notes": "",
                    }
                )

    with map_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "migration_id",
                "old_entity_id",
                "new_entity_id",
                "status",
                "notes",
            ),
        )
        writer.writeheader()
        writer.writerows(map_rows)
    return len(by_device)


def load_plan(plan_path: Path) -> list[PlanRow]:
    with plan_path.open(newline="", encoding="utf-8") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            rows.append(
                PlanRow(
                    migration_id=raw["migration_id"],
                    old_device_id=raw["old_device_id"],
                    old_device_name=raw["old_device_name"],
                    old_entity_ids=tuple(
                        item for item in raw["old_entity_ids"].split(";") if item
                    ),
                    target_friendly_name=raw["target_friendly_name"],
                    status=raw["status"],
                    notes=raw["notes"],
                )
            )
        return rows


def update_plan_status(
    plan_path: Path, migration_id: str, status: str, notes: str = ""
) -> None:
    with plan_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    found = False
    for row in rows:
        if row["migration_id"] == migration_id:
            row["status"] = status
            row["notes"] = notes
            found = True
    if not found:
        raise RuntimeError(f"Migration ID {migration_id!r} not found")
    temporary = plan_path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PLAN_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(plan_path)


def scan_references(
    root: Path, needles: Iterable[tuple[str, str]], output_path: Path
) -> int:
    """Scan configuration files without modifying them."""
    allowed_suffixes = {".yaml", ".yml", ".json", ".storage", ".txt"}
    ignored_parts = {".git", "deps", "__pycache__", "migration-output"}
    found: list[dict[str, Any]] = []
    needle_list = [(kind, needle) for kind, needle in needles if needle]
    for path in root.rglob("*"):
        if not path.is_file() or any(part in ignored_parts for part in path.parts):
            continue
        if path.suffix not in allowed_suffixes and ".storage" not in path.parts:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            for kind, needle in needle_list:
                if needle in line:
                    found.append(
                        {
                            "kind": kind,
                            "reference": needle,
                            "path": str(path),
                            "line": line_number,
                            "text": line.strip()[:500],
                        }
                    )
    write_json(output_path, found)
    return len(found)


def load_entity_map(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_entity_map(path: Path, rows: list[dict[str, str]]) -> None:
    """Atomically write the mapping worksheet."""
    fields = ("migration_id", "old_entity_id", "new_entity_id", "status", "notes")
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def backup_entity_id(entity_id: str, existing: set[str]) -> str:
    domain, object_id = entity_id.split(".", 1)
    candidate = f"{domain}.{object_id}_deconz_old"
    counter = 2
    while candidate in existing:
        candidate = f"{domain}.{object_id}_deconz_old_{counter}"
        counter += 1
    return candidate


def suggest_entity_mappings(
    old_entities: list[dict[str, Any]], new_entities: list[dict[str, Any]]
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Return conservative one-to-one entity suggestions and unresolved old IDs.

    Suggestions are made only for a unique Home Assistant collision suffix,
    mutually unique original names, mutually unique trailing entity-ID tokens, or
    the sole remaining entity in a domain. This is intentionally conservative: a
    missing suggestion is safer than a wrong rename.
    """
    old_by_id = {item["entity_id"]: item for item in old_entities}
    new_by_id = {item["entity_id"]: item for item in new_entities}
    unmatched_old = set(old_by_id)
    unmatched_new = set(new_by_id)
    suggestions: dict[str, tuple[str, str]] = {}

    def domain(entity_id: str) -> str:
        return entity_id.split(".", 1)[0]

    def normalized_original(item: dict[str, Any]) -> str:
        return str(item.get("original_name") or "").strip().casefold()

    def device_class(item: dict[str, Any]) -> str:
        attributes = item.get("state_attributes") or {}
        value = str(attributes.get("device_class") or "").strip().casefold()
        aliases = {
            "pressure": "atmospheric_pressure",
        }
        return aliases.get(value, value)

    def tail(item: dict[str, Any]) -> str:
        entity_id = item["entity_id"]
        return entity_id.split(".", 1)[1].rsplit("_", 1)[-1].casefold()

    def assign_unique(key_function: Any, reason: str) -> None:
        assignments: list[tuple[str, str]] = []
        for old_id in sorted(unmatched_old):
            old_key = key_function(old_by_id[old_id])
            if not old_key:
                continue
            candidates = [
                new_id
                for new_id in unmatched_new
                if domain(new_id) == domain(old_id)
                and key_function(new_by_id[new_id]) == old_key
            ]
            if len(candidates) != 1:
                continue
            new_id = candidates[0]
            reverse = [
                other_old
                for other_old in unmatched_old
                if domain(other_old) == domain(new_id)
                and key_function(old_by_id[other_old]) == old_key
            ]
            if len(reverse) == 1:
                assignments.append((old_id, new_id))
        for old_id, new_id in assignments:
            if old_id not in unmatched_old or new_id not in unmatched_new:
                continue
            suggestions[old_id] = (new_id, reason)
            unmatched_old.remove(old_id)
            unmatched_new.remove(new_id)

    collision_assignments: list[tuple[str, str]] = []
    for old_id in sorted(unmatched_old):
        pattern = re.compile(rf"^{re.escape(old_id)}_([2-9][0-9]*)$")
        candidates = [
            new_id for new_id in unmatched_new if pattern.fullmatch(new_id)
        ]
        if len(candidates) == 1:
            collision_assignments.append((old_id, candidates[0]))
    for old_id, new_id in collision_assignments:
        if old_id not in unmatched_old or new_id not in unmatched_new:
            continue
        suggestions[old_id] = (
            new_id,
            "Home Assistant collision suffix of original entity ID",
        )
        unmatched_old.remove(old_id)
        unmatched_new.remove(new_id)

    assign_unique(device_class, "same unique device class")
    assign_unique(normalized_original, "same unique original name")
    assign_unique(tail, "same unique ID suffix")

    for old_id in sorted(tuple(unmatched_old)):
        candidates = [
            new_id for new_id in unmatched_new if domain(new_id) == domain(old_id)
        ]
        reverse = [
            other_old
            for other_old in unmatched_old
            if domain(other_old) == domain(old_id)
        ]
        if len(candidates) == 1 and len(reverse) == 1:
            new_id = candidates[0]
            suggestions[old_id] = (new_id, "only remaining entity in domain")
            unmatched_old.remove(old_id)
            unmatched_new.remove(new_id)

    return suggestions, sorted(unmatched_old)


def verify_expected_entities(
    entities: list[dict[str, Any]],
    states: list[dict[str, Any]],
    entity_map: list[dict[str, str]],
) -> dict[str, Any]:
    registry = {entity["entity_id"]: entity for entity in entities}
    state_map = {state["entity_id"]: state for state in states}
    results = []
    for row in entity_map:
        if row.get("status") not in {"applied", "verified"}:
            continue
        entity_id = row["old_entity_id"]
        entry = registry.get(entity_id)
        state = state_map.get(entity_id)
        state_value = state.get("state") if state else None
        diagnostic_pending = bool(
            entry
            and entry.get("entity_category") == "diagnostic"
            and state_value == "unknown"
        )
        healthy = bool(
            entry
            and entry.get("platform") != "deconz"
            and state
            and (
                state_value not in {"unknown", "unavailable"}
                or diagnostic_pending
            )
        )
        results.append(
            {
                "entity_id": entity_id,
                "exists": entry is not None,
                "platform": entry.get("platform") if entry else None,
                "entity_category": entry.get("entity_category") if entry else None,
                "state": state_value,
                "diagnostic_pending": diagnostic_pending,
                "healthy": healthy,
            }
        )
    return {
        "results": results,
        "healthy": all(item["healthy"] for item in results) if results else False,
    }
