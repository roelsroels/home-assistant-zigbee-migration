# Supervised deCONZ → Zigbee2MQTT migration toolkit

![#f03c15](https://placehold.co/15x15/f03c15/f03c15.png) ***`This is not a magic-all-in-one tool, it requires HA knowledge and probably still some aftercare`*** ![#f03c15](https://placehold.co/15x15/f03c15/f03c15.png)

This toolkit safely migrates devices from an existing deCONZ network to the
**existing Zigbee2MQTT network on the ConBee II**. It supports both:

- **single-device migration**, with guided pairing, entity-ID preservation and
  verification; and
- **batch migration of identical devices**, pairing multiple matching devices in
  one window while tracking each device by its saved IEEE address.

```bash
# Migrate one selected device
./run.sh migrate D020

# Migrate four identical devices in one guarded pairing window
./run.sh migrate-batch D007 D008 D009 D010 --join-via all
```

Batch pairing does not merge the devices or guess their identities from join
order. Renaming, entity mapping and verification remain separately tracked for
every device, and interrupted batches can be resumed without re-pairing devices
that already succeeded.

It deliberately does **not**:

- change Zigbee2MQTT's serial port, adapter, channel, PAN ID or network key;
- back up, restore, clone or migrate a coordinator;
- edit Home Assistant `.storage` files;
- silently replace entity or device IDs.

Every mutating operation is dry-run first or requires an explicit `Y/N`
confirmation. Pressing Enter selects the safe default, `No`. The first safety
step pins the current Z2M coordinator IEEE address and network identity. Later
pairing commands stop if that identity changes.

Interactive terminal output uses restrained colors to distinguish progress,
warnings, successful checkpoints, and errors. Redirected output stays plain
text. Set `NO_COLOR=1` if you prefer plain text in an interactive terminal too.

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
7. Suggests entity mappings conservatively. For each entity that has no safe
   match, it explains why and asks whether to deliberately skip it.
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

### Migrating identical devices together

Two or more devices with the same saved manufacturer and model can share one
pairing window. Pass every selected migration ID explicitly. For example, four
identical IKEA lamps can be migrated with:

```bash
./run.sh migrate-batch D007 D008 D009 D010 --join-via all
```

After the script says joining is open, reset all selected lamps together (for
these IKEA lamps, use the shared switch to perform the required six off/on
cycles). The command does **not** assign identities based on the order in which
lamps appear. It expects the exact IEEE addresses saved during initialization,
keeps joining open until every expected lamp appears, and closes joining
immediately if an unexpected new device appears.

Once all interviews succeed, each device is renamed, mapped, and verified in
its own guarded workflow with the normal Y/N confirmations. The batch is
resumable: verified devices are skipped, successfully interviewed devices are
not paired again, and only still-missing devices are included in a later join
window. Mixed manufacturer/model selections and duplicate IEEE addresses are
refused.

If a batch stops partway through, rerun the exact same command. Devices already
verified are skipped, successfully interviewed devices continue without another
factory reset, and only devices still missing from Z2M are placed in the next
join window. The batch pauses at the first device whose mapping or verification
needs attention; it never advances later rows past an unresolved device.

### Selecting the temporary join path

The option is `--join-via`, followed by one of these values:

```bash
# Default: accept joins through the coordinator only
./run.sh migrate D024

# Accept joins through one existing Z2M router
./run.sh migrate D024 --join-via "nearby router name"

# Accept joins through all available coordinator/router paths
./run.sh migrate D024 --join-via all
```

There is no separate `--join-all` flag. The correct all-path form is
`--join-via all`. In this mode the toolkit sends Zigbee2MQTT's global
`permit_join` request without a `device` selector; it does not send `all` as if
that were a router name. This is the form to use for a batch of devices that may
join through different routers:

```bash
./run.sh migrate-batch D007 D008 D009 D010 --join-via all
```

All-path joining is intentionally broader than coordinator-only or
single-router joining, so the wizard requires an additional Y/N confirmation.
The exact saved IEEE allowlist remains active: an unexpected fresh device closes
joining immediately and fails the batch safely.

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
identity remain unchanged.

If a mapping is ambiguous, the wizard will not guess. It explains whether no
matching entity was created or whether multiple candidates remain, shows useful
references found by the latest configuration audit, and asks separately for
each unresolved entity whether it should be marked `skip`. The safe default is
`N`: the row stays unresolved and the workflow stops at that device. Answering
`Y` records the deliberate skip in `entity-map.csv`; after all unresolved rows
are either mapped or explicitly skipped, the same device workflow continues.
Skipping means that old entity ID is not preserved, so any displayed references
must be reviewed manually.
Configuration and diagnostic entities are excluded when the wizard evaluates an
otherwise unique functional entity in the same domain. For example, a plug's
`child_lock` configuration switch cannot make its primary on/off switch appear
ambiguous.

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

Device-based triggers need separate review because preserving an entity ID does
not rewrite an automation's old deCONZ `device_id`. When the reference audit
finds old-device references, verification shows their files and line numbers and
stops by default. Update or deliberately retire them, reload Automations in Home
Assistant (or restart Core when appropriate), physically test the control, then
answer `Y` on the next run. That acknowledgement is stored as a durable report.

If a device was already marked `verified` but later proves incomplete, its
status is stale. Reopen only that row without re-pairing or undoing mappings:

```bash
./run.sh reopen D033
./run.sh migrate D033
```

The reopen command explains the recovery sequence and returns the row to the
review stage. Correct the displayed references, reload the affected Home
Assistant configuration, test the physical device, and confirm the review; only
then is it marked `verified` again.

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

To check cleanup readiness at any point, run:

```bash
./run.sh cleanup-audit
```

This is read-only. It protects pending and deliberately skipped devices, counts
the old deCONZ entities belonging to verified migrations, refreshes the
configuration-reference scan, and writes a detailed report under
`migration-output/reports/`. References belonging to pending devices are shown
separately as expected protected dependencies; they are not mislabeled as stale
cleanup work because the migration normally preserves their entity IDs.

If deliberately skipped deCONZ resources should disappear during final cleanup,
record that decision explicitly (nothing is deleted by this command):

```bash
./run.sh approve-retirement D002 D015 D016
```

Revoke an approval with the same IDs plus `--undo`.

### Reconcile PowerCalc after entity migration

When PowerCalc rediscovers a migrated light, Home Assistant can retain the old
unavailable PowerCalc entry and assign `_2` entity IDs to the healthy replacement.
The Energy dashboard then continues referencing the unavailable original energy
sensor. Preview safe one-to-one repairs with:

```bash
./run.sh powercalc-reconcile
```

The command only proposes a repair when every unavailable entity from one stale
PowerCalc entry has exactly one healthy suffixed replacement belonging to one new
PowerCalc entry. It marks target IDs currently used by the Energy dashboard and
writes a JSON report without changing Home Assistant.

After creating a fresh Home Assistant backup, apply the reviewed plan with:

```bash
./run.sh powercalc-reconcile --apply
```

The apply step asks for `Y/N` confirmation, writes a complete pre-change snapshot,
removes only the listed stale PowerCalc configuration entries (or explicitly
listed orphaned entity-registry records when an old entry is already absent),
waits for Home Assistant to release their entity IDs, and renames the healthy
replacements back to the original IDs. It deliberately leaves the Energy
dashboard configuration unchanged so existing statistic IDs and historical data
continue to be used. If an earlier run removed a stale entry but stopped before
renaming, rerunning the same command can safely resume when the missing target is
still referenced by the Energy dashboard.

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

### 5a. Audit deCONZ cleanup readiness

```bash
./run.sh cleanup-audit
```

The command reads the live Home Assistant entity and device registries, but does
not delete or change anything. The configured `paths.home_assistant_config` must
point to a **current** read-only copy or mount of Home Assistant's `/config`; an
old copy can miss recently changed automations, scripts, scenes, or dashboards.

Once the command reports that cleanup readiness passed, create a fresh Home
Assistant backup and remove the deCONZ integration through **Settings > Devices
& services**. This is safer than deleting deCONZ registry entries one by one:
Home Assistant removes all entities and devices owned by the integration as one
consistent operation. Afterward, rerun `cleanup-audit`; zero deCONZ entities and
zero old deCONZ devices are reported as **Cleanup complete**.

### 6. Pair one device

Choose a pending migration ID from `migration-plan.csv`:

```bash
./run.sh pair D001
```

Safety behaviour:

1. The complete old device and entity list is displayed.
2. You must answer `Y` to the pairing confirmation.
3. The live coordinator must match the pinned ConBee II baseline.
4. Joining opens through the selected path for the configured guarded interval.
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

The normal `migrate` and `migrate-next` workflows suggest mappings
automatically. When a safe mapping is impossible, they explain the reason and
offer a default-safe Y/N choice to record that row as deliberately skipped.

For manual review, open the pair-result JSON and `entity-map.csv`. Fill
`new_entity_id` for each old entity you want to preserve, or set its status to
`skip` only after deciding that the old entity can be retired.

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
reopen DXXX               Reopen a stale verified row for reference review
migrate-next              Select, migrate, map and verify the next device
migrate DXXX              Migrate or resume any explicitly selected plan ID
migrate DXXX --join-via ROUTER
                          Pair through one exact existing Z2M router
migrate DXXX --join-via all
                          Pair through all coordinator/router paths
migrate-batch D001 D002 [D003 ...]
                          Migrate identical devices in one pairing window
migrate-batch D001 D002 --join-via all
                          Batch migration through all available join paths
join-routers              List valid Z2M router names for --join-via
preflight                 Read-only connectivity and coordinator check
baseline                  Pin the current ConBee II/Z2M network identity
backup-z2m                Download and checksum a Z2M data backup
snapshot --create-plan    Export HA/Z2M inventory and create worksheets
audit                     Find entity/device references without editing files
cleanup-audit             Check deCONZ removal readiness; never deletes anything
powercalc-reconcile       Preview stale PowerCalc collision repairs
powercalc-reconcile --apply
                          Remove stale PowerCalc entries and restore original IDs
approve-retirement DXXX [DXXX ...]
                          Allow skipped resources in final deCONZ removal
pair D001                 Supervised pairing of one planned device
apply-entity-map D001          Dry-run one device's entity registry rename plan
apply-entity-map D001 --apply  Apply reviewed mappings with rollback attempt
verify D001                    Validate one device and coordinator identity
```

## Protocol references

- [Zigbee2MQTT MQTT topics and requests](https://www.zigbee2mqtt.io/guide/usage/mqtt_topics_and_messages.html)
- [Zigbee2MQTT device pairing guidance](https://www.zigbee2mqtt.io/getting_started/pairing_devices.html)

The toolkit uses the documented transaction-matched bridge request/response
topics, guarded `permit_join` requests, `device_interview` events,
`device/rename` with Home Assistant rename discovery, and the data-folder backup
request. Coordinator-only joining sends `device: coordinator`; a named router
sends that router as `device`; and `--join-via all` deliberately omits the
`device` field so Zigbee2MQTT opens all available paths.
