"""Command-line interface for the supervised migration toolkit."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from typing import Any

from .console import (
    BOLD,
    GREEN,
    RED,
    YELLOW,
    heading as section_heading,
    print_error,
    status_value,
    styled,
    success,
    warning,
)
from .config import AppConfig, ConfigError, load_config
from .ha import HomeAssistantClient
from .inventory import (
    backup_entity_id,
    create_plan,
    load_entity_map,
    load_plan,
    scan_references,
    suggest_entity_mappings,
    update_plan_status,
    verify_expected_entities,
    write_entity_map,
)
from .safety import (
    SafetyError,
    assert_coordinator_matches,
    confirm_yes_no,
    coordinator_identity,
    read_json,
    require_confirmation,
    sha256_file,
    timestamp_slug,
    write_json,
)
from .z2m import Z2MClient, Z2MError


def paths(config: AppConfig) -> dict[str, Path]:
    root = config.paths.output_dir
    return {
        "root": root,
        "baseline": root / "coordinator-baseline.json",
        "plan": root / "migration-plan.csv",
        "map": root / "entity-map.csv",
        "snapshots": root / "snapshots",
        "pair_results": root / "pair-results",
        "reports": root / "reports",
        "backups": root / "backups",
    }


def confirm_or_cancel(message: str) -> bool:
    """Offer an explicit, default-safe proceed/cancel decision."""
    if not confirm_yes_no(message):
        print("Cancelled. No new operation was started.")
        return False
    return True


def next_incomplete_row(config: AppConfig) -> Any | None:
    """Return the first planned device that is neither verified nor skipped."""
    return next(
        (
            row
            for row in load_plan(paths(config)["plan"])
            if row.status not in {"verified", "skip"}
        ),
        None,
    )


def plan_row_by_id(config: AppConfig, migration_id: str) -> Any:
    """Load one current plan row by its stable migration ID."""
    row = next(
        (
            item
            for item in load_plan(paths(config)["plan"])
            if item.migration_id == migration_id
        ),
        None,
    )
    if row is None:
        raise SafetyError(f"Migration ID {migration_id!r} does not exist in the plan")
    return row


def print_suggested_device(
    config: AppConfig, row: Any, heading: str = "Suggested next device:"
) -> None:
    plan = load_plan(paths(config)["plan"])
    complete = sum(item.status in {"verified", "skip"} for item in plan)
    print(
        f"\n{section_heading('Migration progress')}: "
        f"{complete}/{len(plan)} devices complete"
    )
    print(section_heading(heading))
    print(f"  ID:     {row.migration_id}")
    print(f"  Name:   {row.old_device_name}")
    print(f"  Status: {status_value(row.status)}")
    print(f"  Target: {row.target_friendly_name}")
    print("  Existing entities:")
    for entity_id in row.old_entity_ids:
        print(f"    - {entity_id}")


def group_plan_by_progress(plan: list[Any]) -> dict[str, list[Any]]:
    """Split plan rows into mutually exclusive user-facing progress groups."""
    groups: dict[str, list[Any]] = {
        "pending": [],
        "partial": [],
        "verified": [],
        "skipped": [],
    }
    for row in plan:
        if row.status == "verified":
            groups["verified"].append(row)
        elif row.status == "skip":
            groups["skipped"].append(row)
        elif row.status == "pending":
            groups["pending"].append(row)
        else:
            groups["partial"].append(row)
    return groups


def command_status(config: AppConfig, _args: argparse.Namespace) -> None:
    """Show local migration progress without contacting HA, MQTT, or deCONZ."""
    plan_path = paths(config)["plan"]
    if not plan_path.exists():
        raise SafetyError("No migration plan exists. Run ./run.sh initialize first.")

    plan = load_plan(plan_path)
    groups = group_plan_by_progress(plan)
    remaining = len(groups["pending"]) + len(groups["partial"])
    print(section_heading("Migration status"))
    print(f"  Total planned:          {len(plan)}")
    print(warning(f"  Remaining to migrate:   {remaining}"))
    print(success(f"  Finished successfully:  {len(groups['verified'])}"))
    print(warning(f"  Deliberately skipped:   {len(groups['skipped'])}"))

    sections = (
        ("Remaining — not started", groups["pending"]),
        ("Partly completed — resume required", groups["partial"]),
        ("Finished successfully", groups["verified"]),
        ("Deliberately skipped", groups["skipped"]),
    )
    for heading, rows in sections:
        print(f"\n{section_heading(heading)} ({len(rows)}):")
        if not rows:
            print("  (none)")
            continue
        for row in rows:
            print(
                f"  {row.migration_id}  {row.old_device_name} "
                f"[status: {status_value(row.status)}; "
                f"target: {row.target_friendly_name}]"
            )


def command_reopen(config: AppConfig, args: argparse.Namespace) -> None:
    """Reopen a stale verified row for trigger/reference review."""
    row = plan_row_by_id(config, args.migration_id)
    if row.status != "verified":
        raise SafetyError(
            f"{args.migration_id} has status {row.status!r}; reopen is only for a "
            "device whose verified status has become stale"
        )
    if not confirm_yes_no(
        f"Reopen stale status for {args.migration_id} ({row.old_device_name})? "
        "This changes only the local migration status to mapped; it does not "
        "re-pair the device or undo entity-ID mappings.",
        prompt="Reopen for review? [y/N]: ",
    ):
        print("Cancelled. The verified status was not changed.")
        return
    write_json(
        device_reference_review_path(config, args.migration_id),
        {
            "accepted": False,
            "migration_id": args.migration_id,
            "old_device_id": row.old_device_id,
            "reopened_at": datetime.now(timezone.utc).isoformat(),
            "reason": "Verified status was explicitly reopened for review",
        },
    )
    update_plan_status(
        paths(config)["plan"],
        args.migration_id,
        "mapped",
        "Reopened because verified status was stale; references need review",
    )
    print(success(f"{args.migration_id} has been reopened for review."))
    print("Next steps:")
    print("  1. Run the migration command shown below.")
    print("  2. Update or retire every displayed old-device reference.")
    print("  3. Reload Automations in Home Assistant (or restart Core).")
    print("  4. Physically test the device, then confirm the review in the wizard.")
    print(f"\n  ./run.sh migrate {args.migration_id}")


def command_join_routers(config: AppConfig, _args: argparse.Namespace) -> None:
    """List Z2M routers that can be selected as a pairing path."""
    with Z2MClient(config.mqtt) as z2m:
        require_baseline(config, z2m)
        devices = z2m.retained("bridge/devices")
    routers = sorted(
        (
            device
            for device in devices
            if isinstance(device, dict) and device.get("type") == "Router"
        ),
        key=lambda device: str(device.get("friendly_name", "")).casefold(),
    )
    print("Available Zigbee2MQTT pairing routers:")
    if not routers:
        print("  (none)")
        return
    for device in routers:
        definition = device.get("definition") or {}
        description = definition.get("description") or "unknown device"
        print(
            f"  {device.get('friendly_name')}  "
            f"[{device.get('ieee_address')}; {description}]"
        )
    print(
        "\nUse one exact friendly name with: "
        './run.sh migrate DXXX --join-via "FRIENDLY NAME"'
    )


def normalize_ieee(value: str) -> str:
    """Normalize HA colon-separated Zigbee addresses to Z2M's 0x form."""
    compact = value.lower().replace(":", "").replace("-", "")
    if compact.startswith("0x"):
        compact = compact[2:]
    return f"0x{compact}"


def planned_ieee_address(config: AppConfig, row: Any) -> str | None:
    """Resolve the original deCONZ IEEE address from the saved HA snapshot."""
    snapshots = sorted(paths(config)["snapshots"].glob("snapshot-*.json"))
    if not snapshots:
        return None
    snapshot = read_json(snapshots[-1])
    device = next(
        (
            item
            for item in snapshot["home_assistant"]["devices"]
            if item.get("id") == row.old_device_id
        ),
        None,
    )
    if not device:
        return None
    for connection_type, value in device.get("connections") or []:
        if connection_type == "zigbee" and value:
            return normalize_ieee(str(value))
    for identifier_type, value in device.get("identifiers") or []:
        if identifier_type == "deconz" and value:
            return normalize_ieee(str(value))
    return None


def snapshot_device_for_row(config: AppConfig, row: Any) -> dict[str, Any]:
    """Return the saved HA device record used to build one migration row."""
    snapshots = sorted(paths(config)["snapshots"].glob("snapshot-*.json"))
    if not snapshots:
        raise SafetyError("No initialization snapshot exists. Run initialize first.")
    snapshot = read_json(snapshots[-1])
    device = next(
        (
            item
            for item in snapshot["home_assistant"]["devices"]
            if item.get("id") == row.old_device_id
        ),
        None,
    )
    if device is None:
        raise SafetyError(
            f"Saved device record for {row.migration_id} no longer exists in the "
            "initialization snapshot"
        )
    return device


def batch_device_signature(device: dict[str, Any]) -> tuple[str, str]:
    """Return the manufacturer/model identity required for guarded batches."""
    return (
        str(device.get("manufacturer") or "").strip(),
        str(device.get("model") or "").strip(),
    )


def validate_join_via(
    bridge_devices: list[dict[str, Any]], requested: str
) -> str:
    """Validate and normalize a coordinator, all, or exact router join path."""
    join_via = requested.strip()
    if not join_via:
        raise SafetyError("--join-via cannot be empty")
    if join_via.casefold() in {"coordinator", "all"}:
        return join_via.casefold()
    matching_router = next(
        (
            device
            for device in bridge_devices
            if isinstance(device, dict)
            and device.get("type") == "Router"
            and join_via
            in {
                str(device.get("friendly_name", "")),
                str(device.get("ieee_address", "")),
            }
        ),
        None,
    )
    if matching_router is None:
        raise SafetyError(
            f"--join-via {join_via!r} is not a current Z2M router. Run "
            "./run.sh join-routers and copy one exact friendly name."
        )
    return join_via


def require_nondefault_join_confirmation(join_via: str, device_count: int) -> None:
    """Confirm broader or router-assisted joining separately from device choice."""
    if join_via == "coordinator":
        return
    route_description = (
        "all available coordinator/router paths"
        if join_via == "all"
        else f"Z2M router {join_via!r}"
    )
    require_confirmation(
        f"Open joining through {route_description} and migrate exactly "
        f"{device_count} selected physical device"
        f"{'s' if device_count != 1 else ''}. Do not reset any other Zigbee "
        "device. This changes only the temporary join path, not the coordinator."
    )


def successful_z2m_device(
    devices: list[dict[str, Any]], ieee: str | None
) -> dict[str, Any] | None:
    """Return an already successfully interviewed target from bridge/devices."""
    if not ieee:
        return None
    return next(
        (
            device
            for device in devices
            if str(device.get("ieee_address", "")).lower() == ieee.lower()
            and (
                device.get("interview_completed")
                or str(device.get("interview_state", "")).upper() == "SUCCESSFUL"
            )
        ),
        None,
    )


def already_has_target_name(result: dict[str, Any], target: str) -> bool:
    """Return whether the interviewed Z2M device already has the target name."""
    return str(result.get("friendly_name") or "") == target


def ha_device_ids_for_ieee(devices: list[dict[str, Any]], ieee: str) -> set[str]:
    """Find HA device-registry IDs whose identifiers contain the target IEEE."""
    needle = ieee.lower().replace("0x", "").replace(":", "")
    matches: set[str] = set()
    for device in devices:
        values = [
            str(value).lower().replace("0x", "").replace(":", "")
            for _kind, value in (device.get("identifiers") or [])
        ]
        values.extend(
            str(value).lower().replace("0x", "").replace(":", "")
            for _kind, value in (device.get("connections") or [])
        )
        if any(needle in value for value in values):
            matches.add(device["id"])
    return matches


def require_baseline(config: AppConfig, z2m: Z2MClient) -> dict[str, Any]:
    baseline_path = paths(config)["baseline"]
    if not baseline_path.exists():
        raise SafetyError("No coordinator baseline exists. Run the baseline command first.")
    baseline = read_json(baseline_path)
    info = z2m.retained("bridge/info")
    assert_coordinator_matches(info, baseline)
    return baseline


def command_preflight(config: AppConfig, _args: argparse.Namespace) -> None:
    with HomeAssistantClient(config.home_assistant) as ha, Z2MClient(config.mqtt) as z2m:
        snapshot = ha.snapshot()
        z2m_snapshot = z2m.snapshot()
    deconz_count = sum(
        1 for item in snapshot["entities"] if item.get("platform") == "deconz"
    )
    mqtt_count = sum(
        1 for item in snapshot["entities"] if item.get("platform") == "mqtt"
    )
    identity = coordinator_identity(z2m_snapshot["bridge_info"])
    expected = config.safety.expected_coordinator_type_contains.lower()
    coordinator_type = str(identity.get("type") or "")
    if expected not in coordinator_type.lower():
        raise SafetyError(
            f"Coordinator type {coordinator_type!r} does not contain expected "
            f"text {config.safety.expected_coordinator_type_contains!r}"
        )
    if not identity.get("ieee_address"):
        raise SafetyError("Z2M did not report a coordinator IEEE address")
    if z2m_snapshot["bridge_info"].get("permit_join"):
        raise SafetyError(
            "Z2M currently has permit-join enabled. Close it before continuing."
        )
    print(success("Preflight successful"))
    print(f"Home Assistant: {snapshot['config'].get('location_name')}")
    print(f"deCONZ entities: {deconz_count}")
    print(f"MQTT entities: {mqtt_count}")
    print(f"Z2M coordinator: {identity}")
    print("No settings were changed.")


def command_baseline(config: AppConfig, args: argparse.Namespace) -> None:
    baseline_path = paths(config)["baseline"]
    if baseline_path.exists() and not args.replace:
        raise SafetyError(
            f"Baseline already exists at {baseline_path}; use --replace deliberately"
        )
    with Z2MClient(config.mqtt) as z2m:
        info = z2m.retained("bridge/info")
    identity = coordinator_identity(info)
    print("Current Z2M coordinator identity:")
    print(identity)
    expected = config.safety.expected_coordinator_type_contains.lower()
    if expected not in str(identity.get("type") or "").lower():
        raise SafetyError("The current Z2M coordinator does not match the expected type")
    if not identity.get("ieee_address"):
        raise SafetyError("Cannot pin a coordinator without its IEEE address")
    if args.replace:
        require_confirmation(
            "Replacing the coordinator baseline weakens its main safety interlock. "
            "Continue only if this is still the original ConBee II."
        )
    require_confirmation(
        "Pin this coordinator and Zigbee network as the only permitted Z2M baseline. "
        "The toolkit will refuse later pairing if it changes."
    )
    write_json(
        baseline_path,
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "coordinator_identity": identity,
            "bridge_info": info,
            "policy": "Keep the current ConBee II as the sole Z2M coordinator",
        },
    )
    print(f"Baseline written to {baseline_path}")


def gather_snapshot(config: AppConfig) -> dict[str, Any]:
    with HomeAssistantClient(config.home_assistant) as ha, Z2MClient(config.mqtt) as z2m:
        require_baseline(config, z2m)
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "home_assistant": ha.snapshot(),
            "zigbee2mqtt": z2m.snapshot(),
        }


def command_snapshot(config: AppConfig, args: argparse.Namespace) -> None:
    output = paths(config)
    snapshot = gather_snapshot(config)
    snapshot_path = output["snapshots"] / f"snapshot-{timestamp_slug()}.json"
    write_json(snapshot_path, snapshot)
    print(f"Snapshot written to {snapshot_path}")
    if args.create_plan:
        count = create_plan(snapshot, output["plan"], output["map"])
        print(f"Created migration plan for {count} deCONZ devices")
        print(f"Plan: {output['plan']}")
        print(f"Entity mapping worksheet: {output['map']}")


def command_audit(config: AppConfig, _args: argparse.Namespace) -> None:
    output = paths(config)
    config_root = config.paths.home_assistant_config
    if config_root is None or not config_root.exists():
        raise ConfigError(
            "paths.home_assistant_config must point to an accessible read-only copy "
            "or mount of the Home Assistant config directory"
        )
    plan = load_plan(output["plan"])
    needles: list[tuple[str, str]] = []
    for row in plan:
        needles.append(("device_id", row.old_device_id))
        needles.extend(("entity_id", entity_id) for entity_id in row.old_entity_ids)
    report_path = output["reports"] / f"reference-audit-{timestamp_slug()}.json"
    count = scan_references(config_root, needles, report_path)
    print(f"Found {count} references; report written to {report_path}")
    print("No configuration files were changed.")


def command_cleanup_audit(config: AppConfig, _args: argparse.Namespace) -> None:
    """Report whether the deCONZ integration can be removed safely.

    This deliberately performs no registry deletions. Removing individual deCONZ
    entities while the integration still owns them can make them reappear. Once
    every wanted device has moved and references are clear, removing the deCONZ
    integration lets Home Assistant clean up its registry consistently.
    """
    output = paths(config)
    plan_path = output["plan"]
    if not plan_path.exists():
        raise SafetyError("No migration plan exists. Run ./run.sh initialize first.")

    plan = load_plan(plan_path)
    by_device = {row.old_device_id: row for row in plan}

    config_root = config.paths.home_assistant_config
    reference_report: Path | None = None
    if config_root is not None and config_root.exists():
        needles: list[tuple[str, str]] = []
        for row in plan:
            needles.append(("device_id", row.old_device_id))
            needles.extend(("entity_id", entity_id) for entity_id in row.old_entity_ids)
        reference_report = (
            output["reports"] / f"reference-audit-{timestamp_slug()}.json"
        )
        scan_references(config_root, needles, reference_report)

    with HomeAssistantClient(config.home_assistant) as ha:
        entities = ha.entities()
        devices = ha.devices()

    live_deconz = [
        entity for entity in entities if entity.get("platform") == "deconz"
    ]
    planned_entities = [
        entity for entity in live_deconz if entity.get("device_id") in by_device
    ]
    unknown_entities = [
        entity for entity in live_deconz if entity.get("device_id") not in by_device
    ]
    live_by_device: dict[str, list[dict[str, Any]]] = {}
    for entity in planned_entities:
        live_by_device.setdefault(str(entity.get("device_id")), []).append(entity)

    protected_rows = [
        by_device[device_id]
        for device_id in live_by_device
        if by_device[device_id].status != "verified"
    ]
    verified_rows = [
        by_device[device_id]
        for device_id in live_by_device
        if by_device[device_id].status == "verified"
    ]

    references: list[dict[str, Any]] = []
    if reference_report is not None:
        report_rows = read_json(reference_report)
        live_deconz_ids = {entity["entity_id"] for entity in live_deconz}
        old_device_ids = set(by_device)
        ignored_fragments = (
            "/.storage/core.entity_registry",
            "/.storage/core.device_registry",
            "/.storage/core.config_entries",
            "/.storage/trace.saved_traces",
            "/.migration_backup/",
        )
        references = [
            hit
            for hit in report_rows
            if (
                (
                    hit.get("kind") == "device_id"
                    and hit.get("reference") in old_device_ids
                )
                or (
                    hit.get("kind") == "entity_id"
                    and hit.get("reference") in live_deconz_ids
                )
            )
            and not any(
                fragment in str(hit.get("path", "")).replace("\\", "/")
                for fragment in ignored_fragments
            )
            and not Path(str(hit.get("path", ""))).name.startswith("tmp")
        ]

    old_device_registry_ids = {
        device.get("id")
        for device in devices
        if device.get("id") in by_device
    }
    ready = bool(reference_report) and not (
        protected_rows or unknown_entities or references
    )
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "configuration_source": str(config_root) if config_root else None,
        "reference_report": str(reference_report) if reference_report else None,
        "ready_to_remove_deconz": ready,
        "counts": {
            "live_deconz_entities": len(live_deconz),
            "verified_cleanup_entities": sum(
                len(live_by_device[row.old_device_id]) for row in verified_rows
            ),
            "verified_cleanup_devices": len(verified_rows),
            "protected_devices": len(protected_rows),
            "unknown_deconz_entities": len(unknown_entities),
            "configuration_references": len(references),
            "old_devices_in_registry": len(old_device_registry_ids),
        },
        "protected_devices": [
            {
                "migration_id": row.migration_id,
                "name": row.old_device_name,
                "status": row.status,
                "entities": sorted(
                    entity["entity_id"] for entity in live_by_device[row.old_device_id]
                ),
            }
            for row in sorted(protected_rows, key=lambda item: item.migration_id)
        ],
        "verified_cleanup_devices": [
            {
                "migration_id": row.migration_id,
                "name": row.old_device_name,
                "entities": sorted(
                    entity["entity_id"] for entity in live_by_device[row.old_device_id]
                ),
            }
            for row in sorted(verified_rows, key=lambda item: item.migration_id)
        ],
        "unknown_deconz_entities": sorted(
            entity["entity_id"] for entity in unknown_entities
        ),
        "configuration_references": references,
    }
    report_path = output["reports"] / f"cleanup-audit-{timestamp_slug()}.json"
    write_json(report_path, report)

    print(section_heading("deCONZ cleanup audit"))
    print(f"  Live deCONZ entities:       {len(live_deconz)}")
    print(
        f"  Verified cleanup set:       {report['counts']['verified_cleanup_entities']} "
        f"entities on {len(verified_rows)} devices"
    )
    print(warning(f"  Protected devices:          {len(protected_rows)}"))
    print(warning(f"  Configuration references:  {len(references)}"))
    print(warning(f"  Unplanned deCONZ entities:  {len(unknown_entities)}"))

    print(f"\n{section_heading('Protected — do not remove')}")
    if not protected_rows:
        print("  (none)")
    for row in sorted(protected_rows, key=lambda item: item.migration_id):
        print(
            f"  {row.migration_id}  {row.old_device_name} "
            f"[status: {status_value(row.status)}]"
        )
        for entity in sorted(
            live_by_device[row.old_device_id], key=lambda item: item["entity_id"]
        ):
            print(f"    - {entity['entity_id']}")

    if references:
        print(f"\n{section_heading('References to update or deliberately retire')}")
        for hit in references:
            print(
                f"  - {hit.get('reference')}  "
                f"[{hit.get('path')}:{hit.get('line')}]"
            )

    print(f"\nDetailed report: {report_path}")
    if config_root is None or not config_root.exists():
        print(
            warning(
                "No accessible Home Assistant configuration copy was scanned. "
                "Set paths.home_assistant_config and rerun this command."
            )
        )
    else:
        print(f"Configuration source scanned: {config_root}")
        print(
            "Make sure this is a current copy or read-only mount of /config; an "
            "old copy can miss recently edited automations, scripts, scenes, or "
            "dashboards."
        )

    if ready:
        print(
            success(
                "Cleanup readiness passed. Create a fresh Home Assistant backup, "
                "then remove the deCONZ integration in Settings > Devices & "
                "services. Home Assistant will remove its remaining deCONZ "
                "entities and devices together."
            )
        )
    else:
        print(
            warning(
                "Not ready to remove deCONZ. Finish or deliberately retire every "
                "protected device, resolve the references above, refresh the "
                "configuration copy, and rerun ./run.sh cleanup-audit."
            )
        )
    print("No Home Assistant configuration or registry entry was changed.")


def command_backup(config: AppConfig, _args: argparse.Namespace) -> None:
    output = paths(config)
    with Z2MClient(config.mqtt) as z2m:
        require_baseline(config, z2m)
        archive = z2m.backup_data()
    path = output["backups"] / f"z2m-data-{timestamp_slug()}.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(archive)
    print(f"Z2M data backup written to {path}")
    print(f"SHA-256: {sha256_file(path)}")
    print("This is a Z2M data backup; it does not migrate or replace the coordinator.")


def command_pair(config: AppConfig, args: argparse.Namespace) -> None:
    output = paths(config)
    rows = {row.migration_id: row for row in load_plan(output["plan"])}
    row = rows.get(args.migration_id)
    if row is None:
        raise RuntimeError(f"Migration ID {args.migration_id!r} was not found")
    if row.status not in {"pending", "retry", "interviewed", "renamed"}:
        raise SafetyError(
            f"Plan row status is {row.status!r}; set it to retry deliberately if needed"
        )
    print(f"Migration ID: {row.migration_id}")
    print(f"Old deCONZ device: {row.old_device_name} ({row.old_device_id})")
    print("Old entity IDs:")
    for entity_id in row.old_entity_ids:
        print(f"  - {entity_id}")
    print(f"Target Z2M friendly name: {row.target_friendly_name}")
    with HomeAssistantClient(config.home_assistant) as ha, Z2MClient(config.mqtt) as z2m:
        require_baseline(config, z2m)
        result_path: Path
        if row.status in {"interviewed", "renamed"}:
            matches = sorted(output["pair_results"].glob(f"{row.migration_id}-*.json"))
            if not matches:
                raise SafetyError(
                    "Plan says pairing already progressed, but no pair-result file "
                    "exists; inspect "
                    "the plan and Z2M before doing anything else"
                )
            result_path = matches[-1]
            saved = read_json(result_path)
            result = saved["interview"]
            before_entities = set(saved["before_home_assistant_entity_ids"])
            print(f"Resuming the recorded {row.status} stage from {result_path}")
            print("Joining will not be opened again.")
        else:
            before_registry = ha.entities()
            before_entities = {item["entity_id"] for item in before_registry}
            old_registry_entities = [
                item
                for item in before_registry
                if item["entity_id"] in row.old_entity_ids
            ]
            expected_ieee = planned_ieee_address(config, row)
            bridge_devices = z2m.retained("bridge/devices")
            recovered = successful_z2m_device(bridge_devices, expected_ieee)
            if recovered:
                print(
                    f"The planned IEEE {expected_ieee} is already successfully "
                    "interviewed in Z2M. The earlier attempt succeeded; joining will "
                    "not be opened again."
                )
                result = {
                    "ieee_address": recovered["ieee_address"],
                    "friendly_name": recovered.get(
                        "friendly_name", recovered["ieee_address"]
                    ),
                    "status": "successful",
                    "supported": recovered.get("supported"),
                    "definition": recovered.get("definition"),
                    "source": "recovered from bridge/devices",
                }
            else:
                join_via = validate_join_via(
                    bridge_devices,
                    getattr(args, "join_via", "coordinator"),
                )
                if join_via != "coordinator":
                    require_nondefault_join_confirmation(join_via, 1)
                elif not getattr(args, "wizard_confirmed", False):
                    require_confirmation(
                        "Open coordinator-only joining and migrate exactly this one "
                        "physical device. Ensure every other Zigbee device is powered "
                        "normally and do not reset any other device."
                    )
                try:
                    result = z2m.supervised_pair(
                        config.safety.permit_join_seconds,
                        expected_ieee=expected_ieee,
                        join_via=join_via,
                    )
                except Z2MError as err:
                    update_plan_status(
                        output["plan"],
                        row.migration_id,
                        "retry",
                        f"Pair/interview retry required: {str(err)[:500]}",
                    )
                    raise
            result_path = (
                output["pair_results"]
                / f"{row.migration_id}-{timestamp_slug()}.json"
            )
            write_json(
                result_path,
                {
                    "migration_id": row.migration_id,
                    "stage": "interviewed",
                    "old_device_id": row.old_device_id,
                    "old_entity_ids": row.old_entity_ids,
                    "target_friendly_name": row.target_friendly_name,
                    "expected_ieee_address": expected_ieee,
                    "interview": result,
                    "before_home_assistant_entity_ids": sorted(before_entities),
                    "old_home_assistant_entities": old_registry_entities,
                    "new_home_assistant_entities": [],
                },
            )
            update_plan_status(
                output["plan"],
                row.migration_id,
                "interviewed",
                f"Unconfirmed interview: {result_path.name}",
            )
            print(f"Interview recorded immediately in {result_path}")
        ieee = result.get("ieee_address") or result.get("friendly_name")
        if not ieee:
            raise SafetyError("Interview result has no IEEE address; refusing to rename")
        print(f"New device interviewed successfully: {result}")
        if row.status != "renamed":
            if already_has_target_name(result, row.target_friendly_name):
                print(
                    f"The device already has friendly name "
                    f"{row.target_friendly_name!r}; the rename request is skipped."
                )
                rename_result = {
                    "from": row.target_friendly_name,
                    "to": row.target_friendly_name,
                    "already_named": True,
                }
            else:
                if getattr(args, "wizard_confirmed", False):
                    require_confirmation(
                        "Confirm that this IEEE/model is the intended physical device "
                        f"and rename it to {row.target_friendly_name!r}."
                    )
                else:
                    require_confirmation(
                        "Accept this newly joined device as the intended migration "
                        "target."
                    )
                    require_confirmation(
                        f"Rename {ieee} to {row.target_friendly_name!r} and request "
                        "HA discovery rename."
                    )
                rename_result = z2m.rename_device(ieee, row.target_friendly_name)
                print(f"Rename response: {rename_result}")
            saved = read_json(result_path)
            saved["stage"] = "renamed"
            saved["rename_response"] = rename_result
            write_json(result_path, saved)
            update_plan_status(
                output["plan"],
                row.migration_id,
                "renamed",
                f"Renamed; discovery pending: {result_path.name}",
            )
            print("The completed rename has been recorded and can be resumed safely.")
        else:
            rename_result = read_json(result_path).get("rename_response")
            print("Rename was already completed; it will not be sent again.")
        print("Waiting 12 seconds for Home Assistant MQTT discovery...")
        time.sleep(12)
        after = ha.entities()
        discovered_entities = [
            item for item in after if item["entity_id"] not in before_entities
        ]
        ha_device_ids = ha_device_ids_for_ieee(ha.devices(), str(ieee))
        target_entities = [
            item
            for item in after
            if item.get("platform") == "mqtt"
            and item.get("device_id") in ha_device_ids
        ]
        new_entities_by_id = {
            item["entity_id"]: item
            for item in discovered_entities + target_entities
        }
        new_entities = list(new_entities_by_id.values())
        saved = read_json(result_path)
        saved["stage"] = "paired_and_renamed"
        saved["rename_response"] = rename_result
        saved["new_home_assistant_entities"] = new_entities
        write_json(result_path, saved)
    update_plan_status(
        output["plan"],
        row.migration_id,
        "paired",
        f"Pair result: {result_path.name}",
    )
    print(f"Pair result written to {result_path}")
    print("Review the candidate entities, then fill new_entity_id in entity-map.csv.")


def command_apply_entity_map(config: AppConfig, args: argparse.Namespace) -> None:
    output = paths(config)
    plan_rows = {row.migration_id: row for row in load_plan(output["plan"])}
    plan_row = plan_rows.get(args.migration_id)
    if plan_row is None:
        raise SafetyError(f"Unknown migration ID {args.migration_id!r}")
    if plan_row.status not in {"paired", "mapped"}:
        raise SafetyError(
            f"{args.migration_id} has status {plan_row.status!r}; expected paired"
        )
    mappings = [
        row
        for row in load_entity_map(output["map"])
        if row.get("migration_id") == args.migration_id
        and row.get("new_entity_id")
        and row.get("status") in {"pending", "retry"}
    ]
    if not mappings:
        all_for_device = [
            row
            for row in load_entity_map(output["map"])
            if row["migration_id"] == args.migration_id
        ]
        if all_for_device and all(
            row["status"] in {"applied", "verified", "skip"}
            for row in all_for_device
        ):
            update_plan_status(output["plan"], args.migration_id, "mapped")
            print("All mappings were already applied or deliberately skipped.")
            return
        print("No pending rows with new_entity_id values were found.")
        return
    with HomeAssistantClient(config.home_assistant) as ha:
        entities = ha.entities()
        registry = {item["entity_id"]: item for item in entities}
        existing = set(registry)
        operations = []
        for row in mappings:
            old_id = row["old_entity_id"]
            new_id = row["new_entity_id"]
            if old_id not in registry:
                raise SafetyError(f"Old entity {old_id} does not exist")
            if new_id not in registry:
                raise SafetyError(f"New entity {new_id} does not exist")
            if old_id.split(".", 1)[0] != new_id.split(".", 1)[0]:
                raise SafetyError(
                    f"Domain mismatch cannot be renamed safely: {new_id} -> {old_id}"
                )
            if registry[old_id].get("platform") != "deconz":
                raise SafetyError(f"Old entity {old_id} is no longer a deCONZ entity")
            backup_id = backup_entity_id(old_id, existing)
            existing.add(backup_id)
            operations.append((row, old_id, backup_id, new_id))

        print("Proposed entity registry operations:")
        for _row, old_id, backup_id, new_id in operations:
            print(f"  1. {old_id} -> {backup_id}")
            print(f"  2. {new_id} -> {old_id}")
        if not args.apply:
            print("Dry run only. Re-run with --apply after reviewing every operation.")
            return
        require_confirmation(
            "Apply the displayed entity registry renames. Each failed second rename "
            "will trigger an attempted rollback of the old entity."
        )
        applied = []
        journal_path = (
            output["reports"]
            / f"entity-map-apply-{args.migration_id}-{timestamp_slug()}.json"
        )
        journal: dict[str, Any] = {
            "migration_id": args.migration_id,
            "status": "started",
            "operations": [
                {
                    "old_entity_id": old_id,
                    "deconz_backup_entity_id": backup_id,
                    "new_entity_id_before": new_id,
                    "status": "planned",
                }
                for _row, old_id, backup_id, new_id in operations
            ],
        }
        write_json(journal_path, journal)
        worksheet_rows = load_entity_map(output["map"])
        for row, old_id, backup_id, new_id in operations:
            operation = next(
                item
                for item in journal["operations"]
                if item["old_entity_id"] == old_id
            )
            operation["status"] = "moving_old_entity"
            write_json(journal_path, journal)
            ha.rename_entity(old_id, backup_id)
            operation["status"] = "old_entity_moved"
            write_json(journal_path, journal)
            try:
                ha.rename_entity(new_id, old_id)
            except Exception as err:
                operation["status"] = "second_rename_failed; attempting_rollback"
                operation["error"] = str(err)
                write_json(journal_path, journal)
                try:
                    ha.rename_entity(backup_id, old_id)
                    operation["status"] = "rolled_back"
                except Exception as rollback_err:
                    operation["status"] = "ROLLBACK_FAILED"
                    operation["rollback_error"] = str(rollback_err)
                journal["status"] = "failed"
                write_json(journal_path, journal)
                raise
            row["status"] = "applied"
            operation["status"] = "applied"
            applied.append(
                {
                    "migration_id": row["migration_id"],
                    "old_entity_id": old_id,
                    "new_entity_id_before": new_id,
                    "deconz_backup_entity_id": backup_id,
                }
            )
            for worksheet_row in worksheet_rows:
                if (
                    worksheet_row["migration_id"] == row["migration_id"]
                    and worksheet_row["old_entity_id"] == old_id
                ):
                    worksheet_row["status"] = "applied"
            write_entity_map(output["map"], worksheet_rows)
            write_json(journal_path, journal)

        journal["status"] = "complete"
        write_json(journal_path, journal)

    all_rows = load_entity_map(output["map"])
    applied_keys = {
        (row["migration_id"], row["old_entity_id"]) for row, *_rest in operations
    }
    for map_row in all_rows:
        key = (map_row["migration_id"], map_row["old_entity_id"])
        if key in applied_keys:
            map_row["status"] = "applied"
    write_entity_map(output["map"], all_rows)
    remaining = [
        row
        for row in all_rows
        if row["migration_id"] == args.migration_id
        and row["status"] not in {"applied", "verified", "skip"}
    ]
    if not remaining:
        update_plan_status(output["plan"], args.migration_id, "mapped")
    print(f"Applied {len(applied)} mappings; durable journal: {journal_path}")


def verification_is_waiting_for_unknown_states(report: dict[str, Any]) -> bool:
    """Return whether every blocking result is an MQTT entity still unknown."""
    blocking = [item for item in report["results"] if not item["healthy"]]
    return bool(blocking) and all(
        item.get("exists")
        and item.get("platform") != "deconz"
        and item.get("state") == "unknown"
        for item in blocking
    )


def command_verify(config: AppConfig, _args: argparse.Namespace) -> bool:
    output = paths(config)
    selected_map = [
        row
        for row in load_entity_map(output["map"])
        if row["migration_id"] == _args.migration_id
    ]
    if not selected_map:
        raise SafetyError(f"No entity mappings found for {_args.migration_id}")
    incomplete = [
        row
        for row in selected_map
        if row["status"] not in {"applied", "verified", "skip"}
    ]
    if incomplete:
        raise SafetyError(
            "Mapping worksheet still has pending rows for this device. Map them or "
            "set their status to skip after deliberate review."
        )
    if not review_old_device_references(config, _args.migration_id):
        return False
    with HomeAssistantClient(config.home_assistant) as ha, Z2MClient(config.mqtt) as z2m:
        require_baseline(config, z2m)
        report = verify_expected_entities(
            ha.entities(), ha.states(), selected_map
        )
        if report.get("all_entities_skipped"):
            print(
                warning(
                    "Every entity for this device was deliberately skipped; "
                    "there are no replacement entity IDs to verify."
                )
            )
        grace_seconds = config.safety.verification_grace_seconds
        if grace_seconds and verification_is_waiting_for_unknown_states(report):
            waiting_ids = [
                item["entity_id"]
                for item in report["results"]
                if not item["healthy"]
            ]
            print(
                f"Waiting up to {grace_seconds} seconds for first readings from: "
                + ", ".join(waiting_ids)
            )
            print(
                "For a sleepy battery device, briefly press its wake/pair button "
                "once if the readings do not arrive. Do not factory-reset it."
            )
            deadline = time.monotonic() + grace_seconds
            next_notice = time.monotonic() + 30
            while verification_is_waiting_for_unknown_states(report):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(5, remaining))
                report = verify_expected_entities(
                    ha.entities(), ha.states(), selected_map
                )
                now = time.monotonic()
                if (
                    verification_is_waiting_for_unknown_states(report)
                    and now >= next_notice
                ):
                    print(
                        "Still waiting for first readings "
                        f"({max(0, round(deadline - now))} seconds remaining)..."
                    )
                    next_notice = now + 30
            if report["healthy"]:
                print("The required first readings have arrived.")
    for item in report["results"]:
        if item.get("diagnostic_pending"):
            marker = "WARN"
        else:
            marker = "OK" if item["healthy"] else "CHECK"
        marker_color = (
            YELLOW if marker == "WARN" else GREEN if marker == "OK" else RED
        )
        rendered_marker = styled(f"[{marker}]", BOLD, marker_color)
        print(
            f"{rendered_marker} {item['entity_id']}: platform={item['platform']}, "
            f"state={item['state']}"
        )
        if item.get("diagnostic_pending"):
            print(
                "       Diagnostic reading has not arrived yet; this does not block "
                "migration verification."
            )

    accepted_pending_states = False
    if not report["healthy"] and verification_is_waiting_for_unknown_states(report):
        pending_ids = [
            item["entity_id"]
            for item in report["results"]
            if not item["healthy"]
        ]
        accepted_pending_states = confirm_yes_no(
            "The entity mapping and MQTT ownership checks passed, but these sleepy "
            "device readings are still unknown: "
            + ", ".join(pending_ids)
            + ". Mark this device verified without waiting for those first live "
            "readings?"
        )
        report["pending_state_override"] = {
            "accepted": accepted_pending_states,
            "entity_ids": pending_ids,
        }

    path = output["reports"] / f"verification-{timestamp_slug()}.json"
    write_json(path, report)
    print(f"Verification report written to {path}")
    if not report["healthy"] and not accepted_pending_states:
        if verification_is_waiting_for_unknown_states(report):
            print(
                f"{_args.migration_id} remains mapped but not verified. Wake the "
                "device and rerun the same migration command when ready."
            )
            return False
        raise SafetyError(
            "Verification found a missing, unavailable, or deCONZ entity. This "
            "structural safety check cannot be skipped."
        )
    all_rows = load_entity_map(output["map"])
    for row in all_rows:
        if row["migration_id"] == _args.migration_id and row["status"] == "applied":
            row["status"] = "verified"
    write_entity_map(output["map"], all_rows)
    update_plan_status(output["plan"], _args.migration_id, "verified")
    if accepted_pending_states:
        print(
            success(
                f"{_args.migration_id} is now marked verified with pending live "
                "readings recorded in the verification report."
            )
        )
    else:
        print(success(f"{_args.migration_id} is now marked verified."))
    return True


def meaningful_reference_hits(
    reports_dir: Path,
    entity_id: str,
) -> list[dict[str, Any]]:
    """Return useful config references, excluding HA registry and trace noise."""
    reports = sorted(reports_dir.glob("reference-audit-*.json"))
    if not reports:
        return []
    try:
        rows = read_json(reports[-1])
    except (OSError, ValueError, TypeError):
        return []
    ignored_fragments = (
        "/.storage/core.entity_registry",
        "/.storage/core.device_registry",
        "/.storage/core.config_entries",
        "/.storage/trace.saved_traces",
        "/.migration_backup/",
    )
    useful = []
    for row in rows if isinstance(rows, list) else []:
        path = str(row.get("path") or "").replace("\\", "/")
        if row.get("reference") != entity_id:
            continue
        if any(fragment in path for fragment in ignored_fragments):
            continue
        if Path(path).name.startswith("tmp"):
            continue
        useful.append(row)
    return useful


def device_reference_review_path(config: AppConfig, migration_id: str) -> Path:
    """Return the durable acknowledgement path for old device-ID references."""
    return paths(config)["reports"] / f"device-reference-review-{migration_id}.json"


def review_old_device_references(config: AppConfig, migration_id: str) -> bool:
    """Require review of automations that still name the old deCONZ device."""
    row = plan_row_by_id(config, migration_id)
    output = paths(config)
    references = meaningful_reference_hits(
        output["reports"],
        row.old_device_id,
    )
    if not references:
        return True

    review_path = device_reference_review_path(config, migration_id)
    if review_path.exists():
        prior = read_json(review_path)
        if (
            prior.get("accepted") is True
            and prior.get("references") == references
        ):
            return True

    pair_results = sorted(
        output["pair_results"].glob(f"{migration_id}-*.json")
    )
    new_device_ids: set[str] = set()
    if pair_results:
        pair_result = read_json(pair_results[-1])
        new_device_ids = {
            item["device_id"]
            for item in pair_result.get("new_home_assistant_entities", [])
            if item.get("device_id")
        }

    print(
        "\n"
        + warning(
            f"Old deCONZ device references still require review for {migration_id}"
        )
    )
    print(
        "  Entity-ID preservation cannot update device-based automation triggers."
    )
    print(f"  Old deCONZ device ID: {row.old_device_id}")
    if new_device_ids:
        print("  New MQTT device ID(s): " + ", ".join(sorted(new_device_ids)))
    print("  References from the latest configuration audit:")
    for hit in references[:10]:
        print(f"    - {hit.get('path')}:{hit.get('line')}  {hit.get('text', '')}")
    if len(references) > 10:
        print(f"    ...and {len(references) - 10} more")
    print("\n  Before continuing:")
    print("    1. Replace or deliberately retire every old device trigger.")
    print("    2. When editing YAML, reload Automations in Home Assistant (or restart Core).")
    print("    3. Test the physical button and confirm its intended action runs.")
    print(
        "  The audit can be a saved configuration snapshot, so the script asks for "
        "your explicit confirmation after the live system has been corrected."
    )
    accepted = confirm_yes_no(
        "Have all listed old-device references been updated or deliberately "
        "retired, reloaded, and physically tested?",
        prompt="References reviewed and tested? [y/N]: ",
    )
    write_json(
        review_path,
        {
            "accepted": accepted,
            "migration_id": migration_id,
            "old_device_id": row.old_device_id,
            "new_device_ids": sorted(new_device_ids),
            "references": references,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    if accepted:
        print(success("Device-trigger review recorded."))
        return True
    print(
        warning(
            f"{migration_id} remains open for review. Correct and reload the "
            "automations, test the device, then rerun the same migration command."
        )
    )
    return False


def unresolved_mapping_details(
    old_entity: dict[str, Any],
    new_entities: list[dict[str, Any]],
    reserved_new_ids: set[str],
    reference_hits: list[dict[str, Any]],
    reference_audit_available: bool = True,
) -> list[str]:
    """Explain why an old entity has no safe automatic replacement."""
    old_id = old_entity["entity_id"]
    domain = old_id.split(".", 1)[0]
    label = (
        old_entity.get("name")
        or old_entity.get("original_name")
        or old_id
    )
    category = old_entity.get("entity_category") or "standard"
    same_domain = sorted(
        item["entity_id"]
        for item in new_entities
        if item["entity_id"].split(".", 1)[0] == domain
        and item["entity_id"] not in reserved_new_ids
    )
    details = [f"Role: {label} ({domain}; {category} entity)"]
    if same_domain:
        details.append(
            "Reason: no unique semantic match could be proven. Unused MQTT "
            f"candidate{'s' if len(same_domain) != 1 else ''}: "
            + ", ".join(same_domain)
        )
    else:
        details.append(
            f"Reason: Zigbee2MQTT did not create an unused {domain}.* entity "
            "that can safely inherit this entity ID."
        )
    if category == "config" or domain in {"number", "select", "button"}:
        details.append(
            "This looks like a device setting. Zigbee2MQTT may expose the same "
            "setting only in its device options, not as a Home Assistant entity."
        )
    elif category == "diagnostic":
        details.append(
            "This is diagnostic data; the new integration may not expose an "
            "equivalent entity."
        )
    if reference_hits:
        details.append(
            f"Important: the latest reference audit found {len(reference_hits)} "
            "configuration reference(s):"
        )
        for hit in reference_hits[:3]:
            details.append(f"  {hit.get('path')}:{hit.get('line')}")
        if len(reference_hits) > 3:
            details.append(f"  ...and {len(reference_hits) - 3} more")
    elif reference_audit_available:
        details.append(
            "The latest reference audit found no external configuration references "
            "(Home Assistant registry and trace history are ignored)."
        )
    else:
        details.append(
            "No reference-audit report is available. Review Home Assistant "
            "configuration references manually before skipping."
        )
    return details


def offer_unresolved_mapping_skips(
    map_path: Path,
    reports_dir: Path,
    migration_id: str,
    worksheet: list[dict[str, str]],
    unresolved: list[str],
    old_entities: list[dict[str, Any]],
    new_entities: list[dict[str, Any]],
) -> list[str]:
    """Explain unresolved rows and offer an explicit, durable skip per row."""
    old_by_id = {item["entity_id"]: item for item in old_entities}
    reserved_new_ids = {
        row["new_entity_id"]
        for row in worksheet
        if row["migration_id"] == migration_id and row.get("new_entity_id")
    }
    reference_audit_available = any(
        reports_dir.glob("reference-audit-*.json")
    )
    remaining = []
    for entity_id in unresolved:
        entity = old_by_id.get(entity_id, {"entity_id": entity_id})
        references = meaningful_reference_hits(reports_dir, entity_id)
        print("\n" + warning(f"No safe automatic mapping for {entity_id}"))
        for detail in unresolved_mapping_details(
            entity,
            new_entities,
            reserved_new_ids,
            references,
            reference_audit_available,
        ):
            print(f"  {detail}")
        print(
            warning(
                "  Skipping means this old entity ID will not be preserved. Any "
                "listed references must be reviewed manually."
            )
        )
        should_skip = confirm_yes_no(
            f"Mark {entity_id} as deliberately skipped so this device migration "
            "can continue?",
            prompt="Mark as skipped? [y/N]: ",
        )
        if not should_skip:
            remaining.append(entity_id)
            print(warning("Left unresolved; the workflow will stop safely."))
            continue
        for row in worksheet:
            if (
                row["migration_id"] == migration_id
                and row["old_entity_id"] == entity_id
            ):
                row["new_entity_id"] = ""
                row["status"] = "skip"
                row["notes"] = (
                    "User confirmed skip: no safe MQTT replacement was inferred"
                )
                break
        write_entity_map(map_path, worksheet)
        print(success(f"Recorded {entity_id} as deliberately skipped."))
    return remaining


def suggest_mappings_for_device(config: AppConfig, migration_id: str) -> list[str]:
    """Populate only conservative mapping suggestions for one paired device."""
    output = paths(config)
    matches = sorted(output["pair_results"].glob(f"{migration_id}-*.json"))
    if not matches:
        raise SafetyError(f"No pair result exists for {migration_id}")
    result = read_json(matches[-1])
    plan_row = next(
        row
        for row in load_plan(output["plan"])
        if row.migration_id == migration_id
    )
    old_entities = result.get("old_home_assistant_entities") or []
    before_ids = set(result.get("before_home_assistant_entity_ids") or [])
    with HomeAssistantClient(config.home_assistant) as ha:
        current_entities = ha.entities()
        current_states = ha.states()
    state_by_id = {item["entity_id"]: item for item in current_states}

    def add_state_attributes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched = []
        for item in items:
            copy = dict(item)
            copy["state_attributes"] = (
                state_by_id.get(item["entity_id"], {}).get("attributes") or {}
            )
            enriched.append(copy)
        return enriched

    refreshed_new = [
        item for item in current_entities if item["entity_id"] not in before_ids
    ]
    if refreshed_new:
        result["new_home_assistant_entities"] = refreshed_new
        write_json(matches[-1], result)
    new_entities = [
        item
        for item in result.get("new_home_assistant_entities", [])
        if item.get("platform") == "mqtt"
    ]
    device_ids = {
        item.get("device_id") for item in new_entities if item.get("device_id")
    }
    if len(device_ids) > 1:
        print(
            "More than one new MQTT device appeared during discovery. Automatic "
            "mapping is disabled to avoid choosing the wrong device."
        )
        new_entities = []
    elif len(device_ids) == 1:
        selected_device_id = next(iter(device_ids))
        new_entities = [
            item for item in new_entities if item.get("device_id") == selected_device_id
        ]
    if not old_entities:
        old_entities = [
            item
            for item in current_entities
            if item["entity_id"] in plan_row.old_entity_ids
        ]
    old_entities = add_state_attributes(old_entities)
    new_entities = add_state_attributes(new_entities)
    suggestions, _algorithm_unresolved = suggest_entity_mappings(
        old_entities, new_entities
    )
    worksheet = load_entity_map(output["map"])
    for row in worksheet:
        if row["migration_id"] != migration_id or row.get("new_entity_id"):
            continue
        suggestion = suggestions.get(row["old_entity_id"])
        if suggestion:
            row["new_entity_id"] = suggestion[0]
            row["notes"] = f"auto-suggested: {suggestion[1]}"
    write_entity_map(output["map"], worksheet)
    unresolved = [
        row["old_entity_id"]
        for row in worksheet
        if row["migration_id"] == migration_id
        and not row.get("new_entity_id")
        and row["status"] not in {"applied", "verified", "skip"}
    ]

    print("\n" + section_heading("Suggested entity mappings"))
    for old_id, (new_id, reason) in sorted(suggestions.items()):
        print(success(f"  {old_id} <- {new_id}") + f"  ({reason})")
    if unresolved:
        unresolved = offer_unresolved_mapping_skips(
            output["map"],
            output["reports"],
            migration_id,
            worksheet,
            unresolved,
            old_entities,
            new_entities,
        )
    return unresolved


def run_device_workflow(
    config: AppConfig,
    row: Any,
    selection_message: str,
    resume_command: str,
    join_via: str = "coordinator",
    selection_confirmed: bool = False,
    show_next_suggestion: bool = True,
) -> None:
    """Run or resume one explicitly selected plan row through all safe stages."""
    if row.status == "verified":
        print_suggested_device(config, row, "Selected device:")
        print("This device is already verified; no operation was started.")
        print(
            warning(
                "If that status is stale—for example, old deCONZ button triggers "
                "still do not work—reopen it with:"
            )
        )
        print(f"  ./run.sh reopen {row.migration_id}")
        return
    if row.status == "skip":
        raise SafetyError(
            f"{row.migration_id} is deliberately marked skip. Change its plan status "
            "only after reviewing why it was skipped."
        )
    print_suggested_device(config, row, "Selected device:")
    if not selection_confirmed and not confirm_or_cancel(selection_message):
        return

    migration_id = row.migration_id

    if row.status in {"pending", "retry", "interviewed", "renamed"}:
        command_pair(
            config,
            argparse.Namespace(
                migration_id=row.migration_id,
                wizard_confirmed=True,
                join_via=join_via,
            ),
        )
        row = plan_row_by_id(config, migration_id)

    if row.status == "paired":
        unresolved = suggest_mappings_for_device(config, row.migration_id)
        if unresolved:
            print(
                warning(
                    f"\nStopped safely at {row.migration_id}. One or more entities "
                    "were left unresolved. Rerun the command to review them again, "
                    f"or edit only those rows in {paths(config)['map']}."
                )
            )
            return
        command_apply_entity_map(
            config,
            argparse.Namespace(migration_id=row.migration_id, apply=True),
        )
        row = plan_row_by_id(config, migration_id)
        if row.status != "mapped":
            print(
                f"\nStopped safely at {row.migration_id}: its mapping stage is not "
                f"complete yet. Run {resume_command} again after reviewing "
                "entity-map.csv."
            )
            return

    if row.status == "mapped":
        verified = command_verify(
            config,
            argparse.Namespace(migration_id=row.migration_id),
        )
        if not verified:
            print(
                f"Stopped safely at {row.migration_id}. No other device was "
                "advanced."
            )
            return

    row = plan_row_by_id(config, migration_id)
    if row.status != "verified":
        raise SafetyError(
            f"{migration_id} stopped with status {row.status!r}; it was not marked "
            "successful and no other device was advanced"
        )

    if show_next_suggestion:
        next_row = next_incomplete_row(config)
        if next_row is None:
            print("\nMigration plan complete: every device is verified or skipped.")
        else:
            print(
                "\nThis device verified successfully. On the next run, the wizard "
                "will suggest:"
            )
            print_suggested_device(config, next_row)


def command_migrate_next(config: AppConfig, args: argparse.Namespace) -> None:
    """Select and run the first incomplete device."""
    row = next_incomplete_row(config)
    if row is None:
        print("All planned devices are verified or deliberately skipped.")
        return
    run_device_workflow(
        config,
        row,
        "The wizard selected the first incomplete plan row. It will not select a "
        "later device until this one verifies successfully.",
        "./run.sh migrate-next",
        join_via=args.join_via,
    )


def command_migrate(config: AppConfig, args: argparse.Namespace) -> None:
    """Run or resume a specific migration ID requested by the user."""
    row = plan_row_by_id(config, args.migration_id)
    run_device_workflow(
        config,
        row,
        f"You explicitly selected {row.migration_id}. Other unfinished rows will "
        "remain unchanged.",
        f"./run.sh migrate {row.migration_id}",
        join_via=args.join_via,
    )


def command_migrate_batch(config: AppConfig, args: argparse.Namespace) -> None:
    """Pair a fixed set of identical devices together, then migrate each safely."""
    requested_ids = list(args.migration_ids)
    if len(requested_ids) < 2:
        raise SafetyError("migrate-batch requires at least two migration IDs")
    if len(set(requested_ids)) != len(requested_ids):
        raise SafetyError("Each migration ID may appear only once in a batch")

    rows = [plan_row_by_id(config, migration_id) for migration_id in requested_ids]
    skipped = [row.migration_id for row in rows if row.status == "skip"]
    if skipped:
        raise SafetyError(
            "Deliberately skipped rows cannot be batch-migrated: "
            + ", ".join(skipped)
        )

    devices = {
        row.migration_id: snapshot_device_for_row(config, row) for row in rows
    }
    signatures = {
        batch_device_signature(devices[row.migration_id]) for row in rows
    }
    if len(signatures) != 1:
        details = "; ".join(
            f"{row.migration_id}={batch_device_signature(devices[row.migration_id])!r}"
            for row in rows
        )
        raise SafetyError(
            "Batch migration is limited to devices with the same saved "
            f"manufacturer and model. Selected: {details}"
        )
    manufacturer, model = next(iter(signatures))
    if not manufacturer or not model:
        raise SafetyError(
            "Every batch device must have a saved manufacturer and model; use "
            "single-device migration for unidentified hardware"
        )

    expected_by_id: dict[str, str] = {}
    for row in rows:
        ieee = planned_ieee_address(config, row)
        if not ieee:
            raise SafetyError(
                f"No saved Zigbee IEEE address exists for {row.migration_id}"
            )
        expected_by_id[row.migration_id] = ieee.lower()
    if len(set(expected_by_id.values())) != len(expected_by_id):
        raise SafetyError("The selected rows do not have unique saved IEEE addresses")

    print(f"\nBatch migration plan: {len(rows)} identical devices")
    print(f"  Hardware: {manufacturer} — {model}")
    for row in rows:
        print(
            f"  {row.migration_id}: {row.old_device_name} -> "
            f"{row.target_friendly_name} [{expected_by_id[row.migration_id]}; "
            f"status: {row.status}]"
        )
    if not confirm_or_cancel(
        "Pair these exact saved IEEE addresses in one joining window, then "
        "rename, map, and verify each selected device separately."
    ):
        return

    output = paths(config)
    to_pair: list[Any] = []
    recovered_ids: list[str] = []
    with Z2MClient(config.mqtt) as z2m:
        require_baseline(config, z2m)
        bridge_devices = z2m.retained("bridge/devices")
        join_via = validate_join_via(bridge_devices, args.join_via)
        for row in rows:
            if row.status == "verified":
                continue
            if row.status not in {"pending", "retry"}:
                continue
            recovered = successful_z2m_device(
                bridge_devices, expected_by_id[row.migration_id]
            )
            if recovered:
                recovered_ids.append(row.migration_id)
            else:
                to_pair.append(row)

        if recovered_ids:
            print(
                "Already interviewed successfully; these will not re-enter "
                f"pairing: {', '.join(recovered_ids)}"
            )
        if to_pair:
            require_nondefault_join_confirmation(join_via, len(to_pair))
            pairing_ids = [row.migration_id for row in to_pair]
            print(
                "The one shared reset should now target: "
                + ", ".join(pairing_ids)
            )
            try:
                interviews = z2m.supervised_pair_many(
                    config.safety.permit_join_seconds,
                    expected_ieees={
                        expected_by_id[row.migration_id] for row in to_pair
                    },
                    join_via=join_via,
                )
            except Z2MError as err:
                for row in to_pair:
                    update_plan_status(
                        output["plan"],
                        row.migration_id,
                        "retry",
                        f"Batch pair/interview retry required: {str(err)[:500]}",
                    )
                write_json(
                    output["reports"]
                    / f"batch-pair-failed-{timestamp_slug()}.json",
                    {
                        "migration_ids": pairing_ids,
                        "expected_ieee_addresses": {
                            row.migration_id: expected_by_id[row.migration_id]
                            for row in to_pair
                        },
                        "join_via": join_via,
                        "error": str(err),
                    },
                )
                raise
            write_json(
                output["reports"] / f"batch-pair-{timestamp_slug()}.json",
                {
                    "migration_ids": pairing_ids,
                    "expected_ieee_addresses": {
                        row.migration_id: expected_by_id[row.migration_id]
                        for row in to_pair
                    },
                    "join_via": join_via,
                    "interviews": interviews,
                },
            )
            print(
                f"All {len(to_pair)} missing devices interviewed successfully. "
                "Their exact IEEE identities are retained for the individual "
                "rename and mapping stages."
            )
        else:
            print(
                "No shared joining window is needed: every selected pending "
                "device is already interviewed, or its workflow is further along."
            )

    resume_command = "./run.sh migrate-batch " + " ".join(requested_ids)
    if args.join_via != "coordinator":
        resume_command += f" --join-via {args.join_via!r}"
    for migration_id in requested_ids:
        row = plan_row_by_id(config, migration_id)
        if row.status == "verified":
            print(f"\n{migration_id} is already verified; skipping it.")
            continue
        print(f"\nContinuing the individual workflow for {migration_id}...")
        run_device_workflow(
            config,
            row,
            "Batch device selection was already confirmed.",
            resume_command,
            join_via=args.join_via,
            selection_confirmed=True,
            show_next_suggestion=False,
        )
        refreshed = plan_row_by_id(config, migration_id)
        if refreshed.status != "verified":
            print(
                f"\nBatch paused safely at {migration_id} with status "
                f"{refreshed.status!r}. Rerun {resume_command} after resolving "
                "that device; completed devices will be skipped and successfully "
                "interviewed devices will not be paired again."
            )
            return

    print(
        success(
            "\nBatch complete: every selected device is verified. Other plan rows "
            "were left unchanged."
        )
    )


def command_initialize(config: AppConfig, _args: argparse.Namespace) -> None:
    """Run the one-time preparation sequence without overwriting prior work."""
    output = paths(config)
    require_confirmation(
        "Confirm that a current full Home Assistant backup and a current "
        "Phoscon/deCONZ backup have been downloaded and retained."
    )
    command_preflight(config, argparse.Namespace())

    if output["baseline"].exists():
        with Z2MClient(config.mqtt) as z2m:
            require_baseline(config, z2m)
        print(f"Existing ConBee II baseline verified: {output['baseline']}")
    else:
        command_baseline(config, argparse.Namespace(replace=False))

    existing_backups = sorted(output["backups"].glob("z2m-data-*.zip"))
    if existing_backups:
        print(f"Existing Z2M backup retained: {existing_backups[-1]}")
    else:
        command_backup(config, argparse.Namespace())

    if output["plan"].exists() and output["map"].exists():
        print("Existing migration plan and entity worksheet retained unchanged.")
    elif output["plan"].exists() or output["map"].exists():
        raise SafetyError(
            "Only one of migration-plan.csv and entity-map.csv exists. Restore the "
            "missing matching file before continuing."
        )
    else:
        command_snapshot(config, argparse.Namespace(create_plan=True))

    config_root = config.paths.home_assistant_config
    if config_root is not None and config_root.exists():
        command_audit(config, argparse.Namespace())
    else:
        print(
            "Reference audit skipped because paths.home_assistant_config is not set. "
            "The wizard can continue, but device-ID based logic needs manual review."
        )

    row = next_incomplete_row(config)
    print("\nInitialization complete. No Zigbee device was migrated.")
    if row is not None:
        print("Run ./run.sh migrate-next when ready. It will suggest:")
        print_suggested_device(config, row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="./run.sh",
        description=(
            "Supervised migration of devices from deCONZ to the existing Z2M "
            "network. This tool never migrates or changes the coordinator."
        )
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="configuration file (default: config.toml)",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.add_parser(
        "initialize",
        help="prepare backups, baseline, inventory, and migration plan",
    )
    subparsers.add_parser(
        "status",
        help="list remaining, partial, verified, and skipped devices",
    )
    reopen = subparsers.add_parser(
        "reopen",
        help="reopen a stale verified DXXX device for reference review",
    )
    reopen.add_argument("migration_id", metavar="DXXX")
    subparsers.add_parser(
        "join-routers",
        help="list Z2M routers available for router-assisted pairing",
    )
    subparsers.add_parser(
        "preflight",
        help="check connectivity and coordinator without changing anything",
    )
    migrate_next = subparsers.add_parser(
        "migrate-next",
        help="migrate or resume the next unfinished device",
    )
    migrate_next.add_argument(
        "--join-via",
        default="coordinator",
        metavar="ROUTER",
        help="temporary join path: coordinator (default), all, or a Z2M router",
    )
    migrate = subparsers.add_parser(
        "migrate",
        help="migrate or resume a specific DXXX device",
    )
    migrate.add_argument(
        "migration_id",
        metavar="DXXX",
        help="any migration ID present in migration-plan.csv (for example D001)",
    )
    migrate.add_argument(
        "--join-via",
        default="coordinator",
        metavar="ROUTER",
        help="temporary join path: coordinator (default), all, or a Z2M router",
    )
    migrate_batch = subparsers.add_parser(
        "migrate-batch",
        help="migrate two or more identical DXXX devices in one pairing window",
    )
    migrate_batch.add_argument(
        "migration_ids",
        nargs="+",
        metavar="DXXX",
        help="migration IDs for identical devices with unique saved IEEE addresses",
    )
    migrate_batch.add_argument(
        "--join-via",
        default="coordinator",
        metavar="ROUTER",
        help="temporary join path: coordinator (default), all, or a Z2M router",
    )
    baseline = subparsers.add_parser(
        "baseline",
        help="pin the current ConBee II and Zigbee network identity",
    )
    baseline.add_argument("--replace", action="store_true")
    snapshot = subparsers.add_parser(
        "snapshot",
        help="export the current HA and Z2M inventory",
    )
    snapshot.add_argument("--create-plan", action="store_true")
    subparsers.add_parser(
        "audit",
        help="scan configuration references without editing files",
    )
    subparsers.add_parser(
        "cleanup-audit",
        help="check whether deCONZ can be removed; never deletes anything",
    )
    subparsers.add_parser(
        "backup-z2m",
        help="download and checksum a Zigbee2MQTT data backup",
    )
    pair = subparsers.add_parser(
        "pair",
        help="run the guarded pairing stage for one DXXX device",
    )
    pair.add_argument("migration_id")
    pair.add_argument(
        "--join-via",
        default="coordinator",
        metavar="ROUTER",
        help="temporary join path: coordinator (default), all, or a Z2M router",
    )
    apply_map = subparsers.add_parser(
        "apply-entity-map",
        help="preview or apply entity-ID mappings for one DXXX device",
    )
    apply_map.add_argument("migration_id")
    apply_map.add_argument("--apply", action="store_true")
    verify = subparsers.add_parser(
        "verify",
        help="verify one mapped DXXX device and mark it successful",
    )
    verify.add_argument("migration_id")
    return parser


COMMANDS = {
    "initialize": command_initialize,
    "status": command_status,
    "reopen": command_reopen,
    "join-routers": command_join_routers,
    "preflight": command_preflight,
    "migrate-next": command_migrate_next,
    "migrate": command_migrate,
    "migrate-batch": command_migrate_batch,
    "baseline": command_baseline,
    "snapshot": command_snapshot,
    "audit": command_audit,
    "cleanup-audit": command_cleanup_audit,
    "backup-z2m": command_backup,
    "pair": command_pair,
    "apply-entity-map": command_apply_entity_map,
    "verify": command_verify,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        config = load_config(args.config)
        config.paths.output_dir.mkdir(parents=True, exist_ok=True)
        COMMANDS[args.command](config, args)
        return 0
    except KeyboardInterrupt:
        print(
            warning(
                "\nCancelled by user. Any active Zigbee joining request has been "
                "closed where possible, and completed migration checkpoints remain "
                "available for a safe retry.",
                stream=sys.stderr,
            ),
            file=sys.stderr,
        )
        return 130
    except (ConfigError, SafetyError, RuntimeError) as err:
        print_error(str(err))
        return 2
