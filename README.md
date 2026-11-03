# Supervised deCONZ → Zigbee2MQTT migration toolkit

This toolkit migrates devices one at a time from the existing deCONZ network to
the **existing Zigbee2MQTT network on the ConBee II**.

It deliberately does **not**:

- change Zigbee2MQTT's serial port, adapter, channel, PAN ID or network key;
- back up, restore, clone or migrate a coordinator;
- edit Home Assistant `.storage` files;
- silently replace entity or device IDs.

Every mutating operation is dry-run first or requires an exact confirmation
phrase. The first safety step pins the current Z2M coordinator IEEE address and
network identity. Later pairing commands stop if that identity changes.

## Prerequisites

- Python 3.11 or newer.
- Network access to Home Assistant and the MQTT broker.
- A Home Assistant long-lived access token.
- MQTT credentials allowed to read and publish under the Z2M base topic.
- A current full Home Assistant backup and a Phoscon/deCONZ backup.

Do not put secrets in `config.toml`. They are read from environment variables.

## Installation

```bash
chmod +x setup.sh run.sh
./setup.sh
```

Edit `config.toml`, then set credentials for the current terminal:

```bash
export HA_TOKEN='your-long-lived-token'
export MQTT_USERNAME='your-mqtt-user'
export MQTT_PASSWORD='your-mqtt-password'
```

## Recommended workflow

After configuring the connection and exporting the credentials, normal use is:

```bash
./run.sh initialize
./run.sh migrate-next
```

Run `initialize` once. It checks connectivity, confirms the external backups,
pins the current ConBee II, downloads the Z2M data backup, creates the inventory
and worksheets, and runs the reference audit when the HA config path is
available. It never overwrites an existing baseline or plan.

Then run `migrate-next`. The wizard:

1. Selects the first alphabetically planned device that is not yet verified or
   deliberately skipped.
2. Shows its name, current entities, target name, status and overall progress.
3. Lets you type `YES` to proceed or press Enter to cancel without starting.
4. Resumes the last completed stage if a previous run was interrupted.
5. Opens the short coordinator-only join window only when actually required.
6. Asks you to confirm the interviewed IEEE/model before renaming it.
7. Suggests entity mappings conservatively and shows them before applying.
8. Requires a separate exact confirmation before entity IDs are changed.
9. Verifies that the replacement entities are healthy and no longer deCONZ.
10. Marks the device verified and displays the next suggested device.

If a mapping is ambiguous, the wizard stops at that device and identifies the
few unresolved worksheet cells. It will not advance or guess.

## Individual commands for inspection and recovery

### 1. Connectivity preflight

```bash
./run.sh preflight
```

This only reads data. Verify the printed coordinator is the ConBee II currently
used by Zigbee2MQTT.

### 2. Pin the ConBee II coordinator baseline

```bash
./run.sh baseline
```

The command prints the coordinator IEEE address, type, serial port/adapter,
channel, PAN ID and extended PAN ID, then requires the exact phrase
`BASELINE CONBEE II`.

All later Z2M commands compare the live coordinator against this file.

### 3. Back up Z2M data

```bash
./run.sh backup-z2m
```

The resulting zip and SHA-256 hash are stored under `migration-output/backups`.
For ConBee adapters this is primarily the Z2M data directory; it is not a
coordinator migration backup.

### 4. Snapshot HA and generate the migration worksheets

```bash
./run.sh snapshot --create-plan
```

This creates:

- `migration-output/migration-plan.csv`
- `migration-output/entity-map.csv`
- a full read-only HA/Z2M snapshot

The plan is never overwritten automatically.

Review and edit `target_friendly_name` before pairing anything. Pair mains-powered
routers first, then battery-powered end devices.

### 5. Audit dashboards, automations and scripts

Set `paths.home_assistant_config` in `config.toml` to an accessible read-only copy
or mount of `/config`, then run:

```bash
./run.sh audit
```

The report includes entity and device ID references in YAML, JSON and `.storage`
files. Nothing is edited. Device-ID based automations require particular care,
because a newly discovered Z2M device receives a new HA device ID.

### 6. Pair one device

Choose a pending migration ID from `migration-plan.csv`:

```bash
./run.sh pair D001
```

Safety behaviour:

1. The complete old device and entity list is displayed.
2. You must type `PAIR D001`.
3. The live coordinator must match the pinned ConBee II baseline.
4. Joining opens through the coordinator only, for at most 60 seconds.
5. Joining closes in a `finally` block even on errors.
6. Exactly one new successful interview is accepted.
7. You must confirm its IEEE address.
8. The interview is written to disk before any later confirmation. If you stop
   here, rerunning `pair D001` resumes without opening joining again.
9. Renaming requires another exact confirmation phrase.
10. Candidate HA entities are saved for review.

The physical factory reset remains manual. Only reset the device shown by the
selected migration-plan row.

### 7. Prepare entity mappings

Open the pair-result JSON and `entity-map.csv`. Fill `new_entity_id` for each old
entity you want to preserve.

Run a dry-run:

```bash
./run.sh apply-entity-map D001
```

The proposed two-stage rename is printed:

1. Old deCONZ entity → `<old>_deconz_old`
2. New Z2M entity → original entity ID

Apply only after reviewing every line:

```bash
./run.sh apply-entity-map D001 --apply
```

This requires `APPLY ENTITY MAP D001`. Only that single device is considered.
If the second rename fails, the tool attempts to restore the old deCONZ entity
ID immediately.

Entity domains must match. For example, a new `sensor.*` entity cannot take an
old `binary_sensor.*` ID; use a compatibility template in that case.

### 8. Verify before continuing

```bash
./run.sh verify D001
```

The check requires mapped entities to:

- exist under their original IDs;
- no longer be provided by deCONZ;
- have a state other than `unknown` or `unavailable`;
- still be running on the pinned ConBee II/Z2M network.

Only then move to the next device.

## Rollback principles

- Keep deCONZ running until each migrated device is individually verified.
- Never remove the old deCONZ entity before the Z2M replacement works.
- Keep every snapshot, pair-result file and entity-map report.
- If pairing fails, joining is automatically closed; set the row status to
  `retry` manually only after understanding the failure.
- If entity renaming fails, inspect the HA entity registry before retrying.

## Commands

```text
initialize                One-time guarded preparation; preserves existing work
migrate-next              Select, migrate, map and verify the next device
preflight                 Read-only connectivity and coordinator check
baseline                  Pin the current ConBee II/Z2M network identity
backup-z2m                Download and checksum a Z2M data backup
snapshot --create-plan    Export HA/Z2M inventory and create worksheets
audit                     Find entity/device references without editing files
pair D001                 Supervised pairing of one planned device
apply-entity-map D001          Dry-run one device's entity registry rename plan
apply-entity-map D001 --apply  Apply reviewed mappings with rollback attempt
verify D001                    Validate one device and coordinator identity
```

## Protocol references

- [Zigbee2MQTT MQTT topics and requests](https://www.zigbee2mqtt.io/guide/usage/mqtt_topics_and_messages.html)
- [Zigbee2MQTT device pairing guidance](https://www.zigbee2mqtt.io/getting_started/pairing_devices.html)

The toolkit uses the documented transaction-matched bridge request/response
topics, coordinator-only `permit_join`, `device_interview` events,
`device/rename` with Home Assistant rename discovery, and the data-folder backup
request.
