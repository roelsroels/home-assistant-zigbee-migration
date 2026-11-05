# Per-device migration checklist

Keep the **ConBee II connected to Zigbee2MQTT** throughout this project and do not restore coordinator data.

## Once, before the first device

- [ ] Create a full Home Assistant backup.
- [ ] Create and download a Phoscon/deCONZ backup.
- [ ] Run `./run.sh initialize`.
- [ ] Confirm the displayed coordinator is the ConBee II used by Z2M.
- [ ] Review the generated reference audit, especially device-ID references.

## Single-device migration

- [ ] Run `./run.sh migrate-next` and review its suggested device.
- [ ] Answer `Y` to proceed, or answer `N`/press Enter to cancel safely.
- [ ] Confirm no other Zigbee device is in factory-reset/pairing mode.
- [ ] Factory-reset only that physical device while the short join window is open.
- [ ] Check that the reported IEEE/model is the expected physical device.
- [ ] Answer `Y` to accept the device and rename only when both are correct.
- [ ] Review the suggested old-to-new entity mappings.
- [ ] Confirm the entity-ID change only if every displayed mapping is correct.
- [ ] If the wizard reports ambiguity, fill only those worksheet rows and rerun it.
- [ ] Test dashboards, automations, scenes, scripts and voice commands.
- [ ] Note the next suggestion only after the current device is marked verified.

## Batch migration for identical devices

- [ ] Select at least two rows with the same saved manufacturer and model.
- [ ] Run `./run.sh migrate-batch D001 D002 [D003 ...]`.
- [ ] If the devices may use different routers, append `--join-via all`.
- [ ] Do not use `--join-all`; the valid syntax is `--join-via all`.
- [ ] Review every displayed migration ID, name, target name and IEEE address.
- [ ] Answer `Y` only if the complete batch is correct.
- [ ] Wait until the script reports that joining is open.
- [ ] Factory-reset all and only the selected physical devices together.
- [ ] Do not assume discovery order identifies a device; the script matches each
      row using the IEEE address saved during initialization.
- [ ] If an unexpected device appears, stop and investigate; joining will already
      have been closed automatically.
- [ ] Complete the separate rename, mapping and verification confirmations for
      each selected device.
- [ ] If the batch pauses, resolve the reported device and rerun the same command.
      Already verified/interviewed devices will not be paired again.

## Optional join paths

- [ ] Prefer the default coordinator-only join path when practical.
- [ ] Run `./run.sh join-routers` to list exact router names.
- [ ] Use `--join-via "router name"` for one selected router.
- [ ] Use `--join-via all` only when all coordinator/router paths are needed.
- [ ] Remember that join-path selection is temporary and does not change the
      pinned coordinator or Zigbee network identity.

## Stop conditions

Stop immediately if the coordinator IEEE, serial port, channel, PAN ID or
extended PAN ID differs from the pinned baseline; if a device outside the
explicitly selected single device or batch joins; or if a new device's
model/IEEE is not one expected by the plan. Do not solve any of these by
replacing the baseline until the cause is understood.
