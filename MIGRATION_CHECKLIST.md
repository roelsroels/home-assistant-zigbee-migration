# Per-device migration checklist

Keep the **ConBee II connected to Zigbee2MQTT** throughout this project and do not restore coordinator data.

## Once, before the first device

- [ ] Create a full Home Assistant backup.
- [ ] Create and download a Phoscon/deCONZ backup.
- [ ] Run `./run.sh initialize`.
- [ ] Confirm the displayed coordinator is the ConBee II used by Z2M.
- [ ] Review the generated reference audit, especially device-ID references.

## Repeat for exactly one device

- [ ] Run `./run.sh migrate-next` and review its suggested device.
- [ ] Type `YES` to proceed, or press Enter to cancel safely.
- [ ] Confirm no other Zigbee device is in factory-reset/pairing mode.
- [ ] Factory-reset only that physical device while the short join window is open.
- [ ] Check that the reported IEEE/model is the expected physical device.
- [ ] Type the displayed `ACCEPT ... AS ...` phrase only when both are correct.
- [ ] Review the suggested old-to-new entity mappings.
- [ ] Confirm the entity-ID change only if every displayed mapping is correct.
- [ ] If the wizard reports ambiguity, fill only those worksheet rows and rerun it.
- [ ] Test dashboards, automations, scenes, scripts and voice commands.
- [ ] Note the next suggestion only after the current device is marked verified.

## Stop conditions

Stop immediately if the coordinator IEEE, serial port, channel, PAN ID or
extended PAN ID differs from the pinned baseline; if more than one device joins;
or if the new device's model/IEEE is not the one expected. Do not solve any of
these by replacing the baseline until the cause is understood.
