from pathlib import Path
import tempfile
import unittest

from ha_zigbee_migrator.config import ConfigError, _env_name
from ha_zigbee_migrator.inventory import (
    backup_entity_id,
    scan_references,
    suggest_entity_mappings,
)
from ha_zigbee_migrator.safety import (
    SafetyError,
    assert_coordinator_matches,
    coordinator_identity,
    slugify,
)


class ToolkitTests(unittest.TestCase):
    def test_secret_is_rejected_as_environment_variable_name(self) -> None:
        with self.assertRaises(ConfigError):
            _env_name("not-an-env-name.with-dot", "token_env", "HA_TOKEN")
        self.assertEqual(_env_name("HA_TOKEN", "token_env", "HA_TOKEN"), "HA_TOKEN")

    def test_slugify(self) -> None:
        self.assertEqual(slugify("Hall Motion Sensor"), "hall_motion_sensor")
        self.assertEqual(slugify("  Kitchen / Left  "), "kitchen_left")

    def test_backup_entity_id_avoids_collisions(self) -> None:
        existing = {
            "light.hall",
            "light.hall_deconz_old",
            "light.hall_deconz_old_2",
        }
        self.assertEqual(
            backup_entity_id("light.hall", existing),
            "light.hall_deconz_old_3",
        )

    def test_coordinator_identity(self) -> None:
        info = {
            "coordinator": {"ieee_address": "0x123", "type": "ConBee2"},
            "network": {"channel": 15, "pan_id": 1234},
            "config": {"serial": {"port": "/dev/ttyACM0", "adapter": "deconz"}},
        }
        identity = coordinator_identity(info)
        self.assertEqual(identity["ieee_address"], "0x123")
        self.assertEqual(identity["channel"], 15)
        self.assertEqual(identity["serial_port"], "/dev/ttyACM0")

    def test_coordinator_change_is_rejected(self) -> None:
        baseline = {
            "coordinator_identity": {
                "ieee_address": "0x123",
                "type": "ConBee2",
                "serial_port": "/dev/ttyACM0",
                "serial_adapter": "deconz",
                "channel": 15,
                "pan_id": 1234,
                "extended_pan_id": "0xabc",
            }
        }
        changed = {
            "coordinator": {"ieee_address": "0x999", "type": "UnexpectedCoordinator"},
            "network": {
                "channel": 15,
                "pan_id": 1234,
                "extended_pan_id": "0xabc",
            },
            "config": {"serial": {"port": "/dev/ttyACM1", "adapter": "deconz"}},
        }
        with self.assertRaises(SafetyError):
            assert_coordinator_matches(changed, baseline)

    def test_reference_scan_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "automations.yaml"
            source.write_text("entity_id: light.hall\n", encoding="utf-8")
            output = root / "report.json"
            count = scan_references(
                root,
                [("entity_id", "light.hall")],
                output,
            )
            self.assertEqual(count, 1)
            self.assertEqual(source.read_text(encoding="utf-8"), "entity_id: light.hall\n")
            self.assertTrue(output.exists())

    def test_mapping_suggestions_are_conservative(self) -> None:
        old = [
            {"entity_id": "sensor.hall_battery", "original_name": "Battery"},
            {"entity_id": "binary_sensor.hall_motion", "original_name": "Motion"},
        ]
        new = [
            {"entity_id": "sensor.hall_new_battery", "original_name": "Battery"},
            {"entity_id": "binary_sensor.hall_new_occupancy", "original_name": "Motion"},
        ]
        suggestions, unresolved = suggest_entity_mappings(old, new)
        self.assertEqual(
            suggestions["sensor.hall_battery"][0], "sensor.hall_new_battery"
        )
        self.assertEqual(
            suggestions["binary_sensor.hall_motion"][0],
            "binary_sensor.hall_new_occupancy",
        )
        self.assertEqual(unresolved, [])

    def test_ambiguous_mapping_is_not_guessed(self) -> None:
        old = [
            {"entity_id": "sensor.old_one", "original_name": "Value"},
            {"entity_id": "sensor.old_two", "original_name": "Value"},
        ]
        new = [
            {"entity_id": "sensor.new_a", "original_name": "Value"},
            {"entity_id": "sensor.new_b", "original_name": "Value"},
        ]
        suggestions, unresolved = suggest_entity_mappings(old, new)
        self.assertEqual(suggestions, {})
        self.assertEqual(unresolved, ["sensor.old_one", "sensor.old_two"])


if __name__ == "__main__":
    unittest.main()
