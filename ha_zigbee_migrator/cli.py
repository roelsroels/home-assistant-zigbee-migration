"""Command-line interface for the supervised migration toolkit."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from typing import Any

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
from .z2m import Z2MClient


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
    print(f"\nMigration progress: {complete}/{len(plan)} devices complete")
    print(heading)
    print(f"  ID:     {row.migration_id}")
    print(f"  Name:   {row.old_device_name}")
    print(f"  Status: {row.status}")
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
    print("Migration status")
    print(f"  Total planned:          {len(plan)}")
    print(f"  Remaining to migrate:   {remaining}")
    print(f"  Finished successfully:  {len(groups['verified'])}")
    print(f"  Deliberately skipped:   {len(groups['skipped'])}")

    sections = (
        ("Remaining — not started", groups["pending"]),
        ("Partly completed — resume required", groups["partial"]),
        ("Finished successfully", groups["verified"]),
        ("Deliberately skipped", groups["skipped"]),
    )
    for heading, rows in sections:
        print(f"\n{heading} ({len(rows)}):")
        if not rows:
            print("  (none)")
            continue
        for row in rows:
            print(
                f"  {row.migration_id}  {row.old_device_name} "
                f"[status: {row.status}; target: {row.target_friendly_name}]"
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
    print("Preflight successful")
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
                if not getattr(args, "wizard_confirmed", False):
                    require_confirmation(
                        "Open coordinator-only joining and migrate exactly this one "
                        "physical device. Ensure every other Zigbee device is powered "
                        "normally and do not reset any other device."
                    )
                result = z2m.supervised_pair(
                    config.safety.permit_join_seconds,
                    expected_ieee=expected_ieee,
                )
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


def command_verify(config: AppConfig, _args: argparse.Namespace) -> None:
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
    with HomeAssistantClient(config.home_assistant) as ha, Z2MClient(config.mqtt) as z2m:
        require_baseline(config, z2m)
        report = verify_expected_entities(
            ha.entities(), ha.states(), selected_map
        )
    path = output["reports"] / f"verification-{timestamp_slug()}.json"
    write_json(path, report)
    for item in report["results"]:
        if item.get("diagnostic_pending"):
            marker = "WARN"
        else:
            marker = "OK" if item["healthy"] else "CHECK"
        print(
            f"[{marker}] {item['entity_id']}: platform={item['platform']}, "
            f"state={item['state']}"
        )
        if item.get("diagnostic_pending"):
            print(
                "       Diagnostic reading has not arrived yet; this does not block "
                "migration verification."
            )
    print(f"Verification report written to {path}")
    if not report["healthy"]:
        raise SafetyError("Verification found missing, unavailable, or deCONZ entities")
    all_rows = load_entity_map(output["map"])
    for row in all_rows:
        if row["migration_id"] == _args.migration_id and row["status"] == "applied":
            row["status"] = "verified"
    write_entity_map(output["map"], all_rows)
    update_plan_status(output["plan"], _args.migration_id, "verified")
    print(f"{_args.migration_id} is now marked verified.")


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

    print("\nSuggested entity mappings:")
    for old_id, (new_id, reason) in sorted(suggestions.items()):
        print(f"  {old_id} <- {new_id}  ({reason})")
    if unresolved:
        print("Could not safely infer these mappings:")
        for entity_id in unresolved:
            print(f"  - {entity_id}")
    return unresolved


def run_device_workflow(
    config: AppConfig,
    row: Any,
    selection_message: str,
    resume_command: str,
) -> None:
    """Run or resume one explicitly selected plan row through all safe stages."""
    if row.status == "verified":
        print_suggested_device(config, row, "Selected device:")
        print("This device is already verified; no operation was started.")
        return
    if row.status == "skip":
        raise SafetyError(
            f"{row.migration_id} is deliberately marked skip. Change its plan status "
            "only after reviewing why it was skipped."
        )
    print_suggested_device(config, row, "Selected device:")
    if not confirm_or_cancel(
        selection_message
    ):
        return

    migration_id = row.migration_id

    if row.status in {"pending", "retry", "interviewed", "renamed"}:
        command_pair(
            config,
            argparse.Namespace(
                migration_id=row.migration_id,
                wizard_confirmed=True,
            ),
        )
        row = plan_row_by_id(config, migration_id)

    if row.status == "paired":
        unresolved = suggest_mappings_for_device(config, row.migration_id)
        if unresolved:
            print(
                f"\nStopped safely at {row.migration_id}. Fill only the unresolved "
                f"new_entity_id cells in {paths(config)['map']}, or mark a row skip "
                f"after deliberate review. Then run {resume_command} again."
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
        command_verify(
            config,
            argparse.Namespace(migration_id=row.migration_id),
        )

    row = plan_row_by_id(config, migration_id)
    if row.status != "verified":
        raise SafetyError(
            f"{migration_id} stopped with status {row.status!r}; it was not marked "
            "successful and no other device was advanced"
        )

    next_row = next_incomplete_row(config)
    if next_row is None:
        print("\nMigration plan complete: every device is verified or skipped.")
    else:
        print("\nThis device verified successfully. On the next run, the wizard will suggest:")
        print_suggested_device(config, next_row)


def command_migrate_next(config: AppConfig, _args: argparse.Namespace) -> None:
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
    subparsers.add_parser(
        "preflight",
        help="check connectivity and coordinator without changing anything",
    )
    subparsers.add_parser(
        "migrate-next",
        help="migrate or resume the next unfinished device",
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
        "backup-z2m",
        help="download and checksum a Zigbee2MQTT data backup",
    )
    pair = subparsers.add_parser(
        "pair",
        help="run the guarded pairing stage for one DXXX device",
    )
    pair.add_argument("migration_id")
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
    "preflight": command_preflight,
    "migrate-next": command_migrate_next,
    "migrate": command_migrate,
    "baseline": command_baseline,
    "snapshot": command_snapshot,
    "audit": command_audit,
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
    except (ConfigError, SafetyError, RuntimeError) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
