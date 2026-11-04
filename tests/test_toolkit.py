from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from queue import Queue
from types import MethodType, SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ha_zigbee_migrator.config import ConfigError, _env_name
from ha_zigbee_migrator.ha import HomeAssistantClient, HomeAssistantError
from ha_zigbee_migrator.cli import (
    COMMANDS,
    already_has_target_name,
    build_parser,
    group_plan_by_progress,
    main,
    normalize_ieee,
    successful_z2m_device,
    verification_is_waiting_for_unknown_states,
)
from ha_zigbee_migrator.z2m import Z2MClient, Z2MError
from ha_zigbee_migrator.inventory import (
    backup_entity_id,
    scan_references,
    suggest_entity_mappings,
    verify_expected_entities,
)
from ha_zigbee_migrator.safety import (
    SafetyError,
    assert_coordinator_matches,
    confirm_yes_no,
    coordinator_identity,
    require_confirmation,
    slugify,
)


class ToolkitTests(unittest.TestCase):
    def test_verification_waits_only_for_unknown_mqtt_states(self) -> None:
        unknown = {
            "results": [
                {
                    "entity_id": "sensor.temperature",
                    "exists": True,
                    "platform": "mqtt",
                    "state": "unknown",
                    "healthy": False,
                }
            ],
            "healthy": False,
        }
        self.assertTrue(verification_is_waiting_for_unknown_states(unknown))

        for state, exists, platform in (
            ("unavailable", True, "mqtt"),
            (None, False, None),
            ("unknown", True, "deconz"),
        ):
            with self.subTest(state=state, exists=exists, platform=platform):
                report = {
                    "results": [
                        {
                            "entity_id": "sensor.temperature",
                            "exists": exists,
                            "platform": platform,
                            "state": state,
                            "healthy": False,
                        }
                    ],
                    "healthy": False,
                }
                self.assertFalse(
                    verification_is_waiting_for_unknown_states(report)
                )

    def test_home_assistant_route_error_is_actionable(self) -> None:
        config = SimpleNamespace(
            websocket_url="wss://ha.example.com/api/websocket",
            verify_tls=True,
            token="test-token",
        )
        with patch(
            "ha_zigbee_migrator.ha.websocket.create_connection",
            side_effect=OSError(65, "No route to host"),
        ):
            with self.assertRaises(HomeAssistantError) as caught:
                with HomeAssistantClient(config):
                    pass
        message = str(caught.exception)
        self.assertIn("correct LAN or VPN", message)
        self.assertIn("No route to host", message)

    def test_home_assistant_mid_session_disconnect_is_resumable(self) -> None:
        config = SimpleNamespace(
            websocket_url="wss://ha.example.com/api/websocket",
            verify_tls=True,
            token="test-token",
        )
        client = HomeAssistantClient(config)
        client.socket = SimpleNamespace(
            send=lambda _message: None,
            recv=lambda: (_ for _ in ()).throw(
                OSError(54, "Connection reset by peer")
            ),
            close=lambda: None,
        )
        with self.assertRaises(HomeAssistantError) as caught:
            client.entities()
        message = str(caught.exception)
        self.assertIn("connection was lost", message)
        self.assertIn("rerun the same command", message)
        self.assertIn("Connection reset by peer", message)

    def test_no_command_prints_short_command_help(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            result = main([])
        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertIn("usage: ./run.sh", rendered)
        self.assertIn("status", rendered)
        self.assertIn("list remaining, partial, verified, and skipped", rendered)
        self.assertIn("migrate-next", rendered)

    def test_control_c_exits_cleanly_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(
                paths=SimpleNamespace(output_dir=Path(directory))
            )
            errors = StringIO()
            with patch(
                "ha_zigbee_migrator.cli.load_config",
                return_value=config,
            ), patch.dict(
                COMMANDS,
                {"status": lambda _config, _args: (_ for _ in ()).throw(
                    KeyboardInterrupt()
                )},
            ), redirect_stderr(errors):
                result = main(["status"])
        self.assertEqual(result, 130)
        rendered = errors.getvalue()
        self.assertIn("Cancelled by user", rendered)
        self.assertIn("joining request has been closed", rendered)
        self.assertNotIn("Traceback", rendered)

    def test_status_command_parses(self) -> None:
        args = build_parser().parse_args(["status"])
        self.assertEqual(args.command, "status")

    def test_plan_progress_groups_are_mutually_exclusive(self) -> None:
        rows = [
            SimpleNamespace(status="pending"),
            SimpleNamespace(status="interviewed"),
            SimpleNamespace(status="mapped"),
            SimpleNamespace(status="retry"),
            SimpleNamespace(status="verified"),
            SimpleNamespace(status="skip"),
        ]
        groups = group_plan_by_progress(rows)
        self.assertEqual(len(groups["pending"]), 1)
        self.assertEqual(len(groups["partial"]), 3)
        self.assertEqual(len(groups["verified"]), 1)
        self.assertEqual(len(groups["skipped"]), 1)
        self.assertEqual(sum(len(group) for group in groups.values()), len(rows))

    def test_yes_no_confirmation_accepts_short_and_long_yes(self) -> None:
        for answer in ("y", "Y", "yes", "YES"):
            with self.subTest(answer=answer), patch(
                "builtins.input", return_value=answer
            ):
                self.assertTrue(confirm_yes_no("Proceed"))

    def test_yes_no_confirmation_defaults_to_no(self) -> None:
        for answer in ("", "n", "N", "no", "NO"):
            with self.subTest(answer=answer), patch(
                "builtins.input", return_value=answer
            ):
                self.assertFalse(confirm_yes_no("Proceed"))

    def test_yes_no_confirmation_reprompts_invalid_input(self) -> None:
        with patch("builtins.input", side_effect=["maybe", "y"]) as prompt:
            self.assertTrue(confirm_yes_no("Proceed"))
        self.assertEqual(prompt.call_count, 2)

    def test_required_confirmation_cancels_on_no(self) -> None:
        with patch("builtins.input", return_value="n"):
            with self.assertRaises(SafetyError):
                require_confirmation("Proceed")

    def test_unknown_diagnostic_does_not_block_verification(self) -> None:
        report = verify_expected_entities(
            entities=[
                {
                    "entity_id": "sensor.battery",
                    "platform": "mqtt",
                    "entity_category": "diagnostic",
                }
            ],
            states=[{"entity_id": "sensor.battery", "state": "unknown"}],
            entity_map=[
                {
                    "old_entity_id": "sensor.battery",
                    "status": "applied",
                }
            ],
        )
        self.assertTrue(report["healthy"])
        self.assertTrue(report["results"][0]["diagnostic_pending"])

    def test_unavailable_diagnostic_still_blocks_verification(self) -> None:
        report = verify_expected_entities(
            entities=[
                {
                    "entity_id": "sensor.battery",
                    "platform": "mqtt",
                    "entity_category": "diagnostic",
                }
            ],
            states=[{"entity_id": "sensor.battery", "state": "unavailable"}],
            entity_map=[
                {
                    "old_entity_id": "sensor.battery",
                    "status": "applied",
                }
            ],
        )
        self.assertFalse(report["healthy"])

    def test_already_correct_friendly_name_is_idempotent(self) -> None:
        result = {"friendly_name": "sensor_room"}
        self.assertTrue(already_has_target_name(result, "sensor_room"))
        self.assertFalse(already_has_target_name(result, "sensor_kitchen"))

    def test_ieee_normalization_and_recovery_detection(self) -> None:
        ieee = normalize_ieee("00:15:8D:00:00:00:00:01")
        self.assertEqual(ieee, "0x00158d0000000001")
        device = {
            "ieee_address": ieee,
            "interview_state": "SUCCESSFUL",
        }
        self.assertIs(successful_z2m_device([device], ieee), device)

    def test_inventory_recovers_a_missed_success_event(self) -> None:
        ieee = "0x00158d0000000001"
        client = Z2MClient.__new__(Z2MClient)
        client.config = SimpleNamespace(base_topic="zigbee2mqtt")
        client.events = Queue()
        client.messages = {}
        client.client = SimpleNamespace(subscribe=lambda _topic: None)

        def retained(_self: Z2MClient, _suffix: str) -> list[dict[str, object]]:
            return []

        def request(
            self: Z2MClient, _path: str, payload: dict[str, object]
        ) -> dict[str, object]:
            if payload.get("time"):
                self.messages["zigbee2mqtt/bridge/devices"] = [
                    {
                        "ieee_address": ieee,
                        "friendly_name": ieee,
                        "interview_completed": True,
                        "interview_state": "SUCCESSFUL",
                        "supported": True,
                        "definition": {"model": "WSDCGQ11LM"},
                    }
                ]
            return {}

        client.retained = MethodType(retained, client)
        client.request = MethodType(request, client)
        result = client.supervised_pair(0.01, expected_ieee=ieee)
        self.assertEqual(result["ieee_address"], ieee)
        self.assertEqual(result["source"], "bridge/devices")

    def test_failed_interview_has_immediate_recovery_instructions(self) -> None:
        ieee = "0x00158d0000000001"
        client = Z2MClient.__new__(Z2MClient)
        client.config = SimpleNamespace(base_topic="zigbee2mqtt")
        client.events = Queue()
        client.messages = {}
        client.client = SimpleNamespace(subscribe=lambda _topic: None)

        def retained(_self: Z2MClient, _suffix: str) -> list[dict[str, object]]:
            return []

        def request(
            self: Z2MClient, _path: str, payload: dict[str, object]
        ) -> dict[str, object]:
            if payload.get("time"):
                self.events.put(
                    {
                        "type": "device_joined",
                        "data": {"ieee_address": ieee},
                    }
                )
                self.events.put(
                    {
                        "type": "device_interview",
                        "data": {"ieee_address": ieee, "status": "failed"},
                    }
                )
            return {}

        client.retained = MethodType(retained, client)
        client.request = MethodType(request, client)
        with self.assertRaises(Z2MError) as caught:
            client.supervised_pair(
                0.1,
                expected_ieee=ieee,
                interview_timeout=10,
            )
        message = str(caught.exception)
        self.assertIn("intended device", message)
        self.assertIn("interview failed", message)
        self.assertIn("Rerun the same migration command", message)
        self.assertIn("every 2-3 seconds", message)

    def test_stale_failed_inventory_does_not_become_a_fresh_candidate(self) -> None:
        ieee = "0x00158d0000000001"
        failed_device = {
            "ieee_address": ieee,
            "friendly_name": ieee,
            "interview_completed": False,
            "interview_state": "FAILED",
            "interviewing": False,
            "network_address": 12345,
            "supported": False,
        }
        client = Z2MClient.__new__(Z2MClient)
        client.config = SimpleNamespace(base_topic="zigbee2mqtt")
        client.events = Queue()
        client.messages = {
            "zigbee2mqtt/bridge/devices": [failed_device.copy()]
        }
        client.client = SimpleNamespace(subscribe=lambda _topic: None)

        def retained(_self: Z2MClient, _suffix: str) -> list[dict[str, object]]:
            return [failed_device.copy()]

        client.retained = MethodType(retained, client)
        client.request = MethodType(
            lambda _self, _path, _payload: {},
            client,
        )
        with self.assertRaises(Z2MError) as caught:
            client.supervised_pair(0.01, expected_ieee=ieee)
        message = str(caught.exception)
        self.assertIn("No fresh device join or interview", message)
        self.assertIn("ignored during retries", message)

    def test_changed_retry_inventory_can_recover_a_missed_success_event(self) -> None:
        ieee = "0x00158d0000000001"
        failed_device = {
            "ieee_address": ieee,
            "friendly_name": ieee,
            "interview_completed": False,
            "interview_state": "FAILED",
            "network_address": 12345,
            "supported": False,
        }
        client = Z2MClient.__new__(Z2MClient)
        client.config = SimpleNamespace(base_topic="zigbee2mqtt")
        client.events = Queue()
        client.messages = {
            "zigbee2mqtt/bridge/devices": [failed_device.copy()]
        }
        client.client = SimpleNamespace(subscribe=lambda _topic: None)

        def retained(_self: Z2MClient, _suffix: str) -> list[dict[str, object]]:
            return [failed_device.copy()]

        def request(
            self: Z2MClient, _path: str, payload: dict[str, object]
        ) -> dict[str, object]:
            if payload.get("time"):
                self.messages["zigbee2mqtt/bridge/devices"] = [
                    {
                        **failed_device,
                        "interview_completed": True,
                        "interview_state": "SUCCESSFUL",
                        "supported": True,
                        "definition": {"model": "WSDCGQ11LM"},
                    }
                ]
            return {}

        client.retained = MethodType(retained, client)
        client.request = MethodType(request, client)
        result = client.supervised_pair(0.01, expected_ieee=ieee)
        self.assertEqual(result["ieee_address"], ieee)
        self.assertEqual(result["source"], "bridge/devices")

    def test_specific_migration_command_parses(self) -> None:
        for migration_id in ("D001", "D020", "D127", "D999"):
            with self.subTest(migration_id=migration_id):
                args = build_parser().parse_args(["migrate", migration_id])
                self.assertEqual(args.command, "migrate")
                self.assertEqual(args.migration_id, migration_id)

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

    def test_mapping_uses_unique_home_assistant_collision_suffix(self) -> None:
        old = [
            {
                "entity_id": "switch.tablet_outlet",
                "original_name": None,
            }
        ]
        new = [
            {
                "entity_id": "switch.tablet_outlet_2",
                "original_name": None,
            },
            {
                "entity_id": "switch.tablet_outlet_child_lock",
                "original_name": "Child lock",
            },
        ]
        suggestions, unresolved = suggest_entity_mappings(old, new)
        self.assertEqual(
            suggestions["switch.tablet_outlet"][0],
            "switch.tablet_outlet_2",
        )
        self.assertEqual(unresolved, [])

    def test_mapping_does_not_guess_between_collision_suffixes(self) -> None:
        old = [{"entity_id": "switch.outlet", "original_name": None}]
        new = [
            {"entity_id": "switch.outlet_2", "original_name": None},
            {"entity_id": "switch.outlet_3", "original_name": None},
        ]
        suggestions, unresolved = suggest_entity_mappings(old, new)
        self.assertEqual(suggestions, {})
        self.assertEqual(unresolved, ["switch.outlet"])

    def test_mapping_uses_unique_device_classes(self) -> None:
        old = [
            {
                "entity_id": "sensor.temperature_16",
                "original_name": "Room sensor",
                "state_attributes": {"device_class": "temperature"},
            },
            {
                "entity_id": "sensor.pressure_18",
                "original_name": "Room sensor",
                "state_attributes": {"device_class": "pressure"},
            },
        ]
        new = [
            {
                "entity_id": "sensor.sensor_room_temperature",
                "original_name": "Temperature",
                "state_attributes": {"device_class": "temperature"},
            },
            {
                "entity_id": "sensor.sensor_room_pressure",
                "original_name": "Atmospheric pressure",
                "state_attributes": {"device_class": "atmospheric_pressure"},
            },
        ]
        suggestions, unresolved = suggest_entity_mappings(old, new)
        self.assertEqual(
            suggestions["sensor.temperature_16"][0],
            "sensor.sensor_room_temperature",
        )
        self.assertEqual(
            suggestions["sensor.pressure_18"][0],
            "sensor.sensor_room_pressure",
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
