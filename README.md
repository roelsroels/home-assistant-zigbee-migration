# Supervised deCONZ → Zigbee2MQTT migration toolkit

This toolkit migrates devices one at a time from the existing deCONZ network to
the **existing Zigbee2MQTT network on the ConBee II**.

It deliberately does **not**:

- change Zigbee2MQTT's serial port, adapter, channel, PAN ID or network key;
- back up, restore, clone or migrate a coordinator;
- edit Home Assistant `.storage` files;
- silently replace entity or device IDs.

Every mutating operation is dry-run first or requires an explicit `Y/N`
confirmation. Pressing Enter selects the safe default, `No`. The first safety
step pins the current Z2M coordinator IEEE address and network identity. Later
pairing commands stop if that identity changes.

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

Edit `config.toml`. The `token_env`, `username_env`, and `password_env` fields
must remain environment-variable names; never paste credentials into those
fields.

For persistent local credentials, copy the ignored secrets template:

```bash
cp secrets.env.example secrets.env
```

Put the real values in `secrets.env`. `run.sh` loads it automatically. You can
alternatively set credentials for only the current terminal:

```bash
export HA_TOKEN='your-long-lived-token'
export MQTT_USERNAME='your-mqtt-user'
export MQTT_PASSWORD='your-mqtt-password'
```

## Recommended workflow

After configuring the connection and exporting the credentials, normal use is:

```bash
./run.sh initialize
./run.sh status
./run.sh migrate-next
```

Run `./run.sh` without a command at any time to display a concise list of all
available commands and their purpose.

Run `initialize` once. It checks connectivity, confirms the external backups,
pins the current ConBee II, downloads the Z2M data backup, creates the inventory
and worksheets, and runs the reference audit when the HA config path is
available. It never overwrites an existing baseline or plan.

Then run `migrate-next`. The wizard:

1. Selects the first alphabetically planned device that is not yet verified or
   deliberately skipped.
2. Shows its name, current entities, target name, status and overall progress.
3. Asks for `Y/N`; `Y` proceeds and `N` or Enter cancels without starting.
4. Resumes the last completed stage if a previous run was interrupted.
5. Opens the short coordinator-only join window only when actually required.
6. Asks you to confirm the interviewed IEEE/model before renaming it.
7. Suggests entity mappings conservatively and shows them before applying.
8. Requires a separate `Y/N` confirmation before entity IDs are changed.
9. Verifies that the replacement entities are healthy and no longer deCONZ.
10. Marks the device verified and displays the next suggested device.

The original deCONZ IEEE address is read from the initialization snapshot. If a
pairing command is interrupted or misses the final MQTT event, the next run
checks Z2M's retained device inventory. A matching successfully interviewed
device is recovered without opening joining or requiring another factory reset.
After a new device appears, joining closes immediately while the script keeps
waiting separately for its interview to finish.
If the recovered device already has the requested Z2M friendly name, the rename
stage is treated as complete instead of sending a duplicate rename request.
If the intended device joins but its interview fails, the device is marked
`retry` and no rename or entity mapping is attempted. Run the same migration
command again. For sleepy battery devices, keep the device close to the
coordinator and briefly press its wake/pair button every 2-3 seconds until the
interview succeeds. On a retry, an unchanged failed entry already retained in
Zigbee2MQTT is ignored; only a fresh join/interview event, changed inventory
state, or successful interview can close joining and advance the migration.

Coordinator-only joining remains the default. If a device cannot be brought
close to the coordinator, list the already migrated mains-powered routers:

```bash
./run.sh join-routers
```

Then select one nearby router by its exact Z2M friendly name:

```bash
./run.sh migrate D024 --join-via "nearby router name"
```

The wizard requires an additional Y/N confirmation for router-assisted joining.
This changes only the temporary pairing path; the pinned coordinator and network
identity remain unchanged. `--join-via all` is also supported but is broader and
should be used only when a specific nearby router is not known.

If a mapping is ambiguous, the wizard stops at that device and identifies the
few unresolved worksheet cells. It will not advance or guess.

An `unknown` diagnostic entity, such as an Aqara battery reading that may take
many hours to arrive, is reported as a warning and does not block verification
when the device's functional entities are healthy. An `unavailable` diagnostic
entity still blocks verification.

After mapping, functional MQTT entities that are still `unknown` receive a
configurable grace period (120 seconds by default) to publish their first
measurement. Missing, `unavailable`, or deCONZ-backed entities still fail
immediately. Set `safety.verification_grace_seconds` to a value from 0 through
600 to adjust or disable this wait. If the grace period expires and the only
remaining issue is an `unknown` first reading, the wizard offers a final Y/N
choice to accept the pending reading and finish the migration. That exception is
recorded in the verification report. Missing entities, `unavailable` states, and
deCONZ ownership can never be waived.

If Home Assistant drops its WebSocket connection during a run, the toolkit now
prints a short resumable error instead of a Python traceback. Completed pairing,
rename, and mapping checkpoints remain on disk; rerun the same command to resume.
Pressing Ctrl-C is also handled as a normal cancellation: coordinator joining is
closed by the pairing cleanup, no traceback is printed, completed checkpoints
are retained, and the command exits with the conventional status code 130.

To choose **any** particular planned device instead of accepting the next
suggestion, pass its ID from `migration-plan.csv`:

```bash
./run.sh migrate DXXX
```

For example, `./run.sh migrate D001`, `./run.sh migrate D020`, or
`./run.sh migrate D127`. The ID is not hard-coded. The command runs or resumes
the requested row through the same guarded workflow. Other incomplete rows keep
their existing status and are not marked skipped.

To inspect the complete local migration tracker without contacting Home
Assistant, MQTT, or deCONZ, run:

```bash
./run.sh status
```

It groups devices as not started, partly completed and resumable, successfully
verified, or deliberately skipped. The summary count for remaining devices
includes both not-started and partly completed devices.

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
channel, PAN ID and extended PAN ID, then requires an explicit `Y` confirmation.

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
2. You must answer `Y` to the pairing confirmation.
3. The live coordinator must match the pinned ConBee II baseline.
4. Joining opens through the coordinator only, for at most 60 seconds.
5. Joining closes in a `finally` block even on errors.
6. Exactly one new successful interview is accepted.
7. You must confirm its IEEE address.
8. The interview is written to disk before any later confirmation. If you stop
   here, rerunning `pair D001` resumes without opening joining again.
9. Renaming requires another `Y/N` confirmation.
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

This requires an explicit `Y` confirmation. Only that single device is considered.
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
status                    List remaining, partial, verified, and skipped devices
migrate-next              Select, migrate, map and verify the next device
migrate DXXX              Migrate or resume any explicitly selected plan ID
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
