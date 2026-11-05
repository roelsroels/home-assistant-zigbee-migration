"""Planning helpers for reconciling stale PowerCalc entity-ID collisions."""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any


COLLISION_SUFFIX = re.compile(r"^(?P<base>.+)_(?P<number>[2-9][0-9]*)$")


def energy_dashboard_entities(preferences: dict[str, Any]) -> set[str]:
    """Return entity/statistic IDs used as individual electrical devices."""
    return {
        str(item["stat_consumption"])
        for item in preferences.get("device_consumption", [])
        if item.get("stat_consumption")
    }


def _state_by_entity(states: list[dict[str, Any]]) -> dict[str, str | None]:
    return {
        str(item["entity_id"]): item.get("state")
        for item in states
        if item.get("entity_id")
    }


def _powercalc_groups(
    entities: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entity in entities:
        if entity.get("platform") != "powercalc":
            continue
        entry_id = entity.get("config_entry_id")
        if entry_id:
            groups[str(entry_id)].append(entity)
    return groups


def build_powercalc_reconciliation_plan(
    entities: list[dict[str, Any]],
    states: list[dict[str, Any]],
    config_entries: list[dict[str, Any]],
    energy_preferences: dict[str, Any],
) -> list[dict[str, Any]]:
    """Find safe stale-entry/replacement pairs without changing Home Assistant.

    A normal pair consists of an unavailable PowerCalc config entry and a newer,
    healthy PowerCalc config entry whose entity IDs have the same base plus one
    Home Assistant collision suffix (for example ``sensor.foo_energy_2``).

    The second pass supports resuming after the stale entry was already removed:
    a suffixed live energy entity may reclaim a currently missing base ID only
    when that base ID is still explicitly referenced by the Energy dashboard.
    """
    state_by_entity = _state_by_entity(states)
    registry_by_id = {
        str(entity["entity_id"]): entity
        for entity in entities
        if entity.get("entity_id")
    }
    groups = _powercalc_groups(entities)
    titles = {
        str(entry["entry_id"]): str(entry.get("title") or entry["entry_id"])
        for entry in config_entries
        if entry.get("domain") == "powercalc" and entry.get("entry_id")
    }
    dashboard_ids = energy_dashboard_entities(energy_preferences)
    existing_entry_ids = set(titles)
    plan: list[dict[str, Any]] = []
    used_new_entries: set[str] = set()

    for old_entry_id, old_entities in sorted(groups.items()):
        old_ids = sorted(str(entity["entity_id"]) for entity in old_entities)
        if not old_ids or any(
            state_by_entity.get(entity_id) != "unavailable"
            for entity_id in old_ids
        ):
            continue

        matches: list[tuple[str, str, str]] = []
        for old_id in old_ids:
            candidates = []
            for entity_id, entity in registry_by_id.items():
                if not entity_id.startswith(f"{old_id}_"):
                    continue
                suffix = entity_id[len(old_id) :]
                if not re.fullmatch(r"_[2-9][0-9]*", suffix):
                    continue
                if entity.get("platform") != "powercalc":
                    continue
                if state_by_entity.get(entity_id) == "unavailable":
                    continue
                candidates.append((entity_id, str(entity.get("config_entry_id"))))
            if len(candidates) != 1:
                matches = []
                break
            replacement_id, new_entry_id = candidates[0]
            matches.append((replacement_id, old_id, new_entry_id))

        new_entry_ids = {match[2] for match in matches}
        if not matches or len(new_entry_ids) != 1:
            continue
        new_entry_id = next(iter(new_entry_ids))
        if not new_entry_id or new_entry_id == old_entry_id:
            continue

        plan.append(
            {
                "old_config_entry_id": (
                    old_entry_id if old_entry_id in existing_entry_ids else None
                ),
                "old_title": titles.get(old_entry_id),
                "orphan_entity_ids": (
                    [] if old_entry_id in existing_entry_ids else old_ids
                ),
                "new_config_entry_id": new_entry_id,
                "new_title": titles.get(new_entry_id, new_entry_id),
                "resume_only": False,
                "mappings": [
                    {
                        "from": source,
                        "to": target,
                        "current_state": state_by_entity.get(source),
                        "energy_dashboard": target in dashboard_ids,
                    }
                    for source, target, _entry_id in sorted(matches)
                ],
            }
        )
        used_new_entries.add(new_entry_id)

    # Resume safely if the stale entry disappeared but the rename stage did not
    # finish. Requiring a missing target that is still referenced by the Energy
    # dashboard avoids guessing about legitimate suffixed entity IDs.
    registry_ids = set(registry_by_id)
    for new_entry_id, new_entities in sorted(groups.items()):
        if new_entry_id in used_new_entries:
            continue
        energy_candidates: list[tuple[str, str, str]] = []
        for entity in new_entities:
            source = str(entity["entity_id"])
            match = COLLISION_SUFFIX.fullmatch(source)
            if not match:
                continue
            target = match.group("base")
            suffix = source[len(target) :]
            if (
                target in dashboard_ids
                and target not in registry_ids
                and state_by_entity.get(source) != "unavailable"
            ):
                energy_candidates.append((source, target, suffix))
        if len(energy_candidates) != 1:
            continue

        suffix = energy_candidates[0][2]
        mappings: list[dict[str, Any]] = []
        safe = True
        for entity in new_entities:
            source = str(entity["entity_id"])
            if not source.endswith(suffix):
                safe = False
                break
            target = source[: -len(suffix)]
            if target in registry_ids or state_by_entity.get(source) == "unavailable":
                safe = False
                break
            mappings.append(
                {
                    "from": source,
                    "to": target,
                    "current_state": state_by_entity.get(source),
                    "energy_dashboard": target in dashboard_ids,
                }
            )
        if safe and mappings:
            plan.append(
                {
                    "old_config_entry_id": None,
                    "old_title": "already removed",
                    "orphan_entity_ids": [],
                    "new_config_entry_id": new_entry_id,
                    "new_title": titles.get(new_entry_id, new_entry_id),
                    "resume_only": True,
                    "mappings": sorted(mappings, key=lambda item: item["from"]),
                }
            )

    return sorted(
        plan,
        key=lambda item: (item["new_title"].lower(), item["new_config_entry_id"]),
    )
