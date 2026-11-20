"""Offline protocol/entity tests; no Home Assistant server or relay is contacted.

Only HA's entity base and exception interfaces are replaced. The production RCM
controller, packet builder, UDP sender and register parser are loaded unchanged.
Real Home Assistant lifecycle and hardware checks are performed separately.
"""

import concurrent.futures
import importlib
import json
from pathlib import Path
import socket
import sys
import time
import types
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_modules():
    package = types.ModuleType("_rcm_test_sihas")
    package.__path__ = [str(ROOT / "custom_components/sihas")]
    constants = types.ModuleType("_rcm_test_sihas.const")
    constants.DOMAIN = "sihas"
    constants.ENDIAN = "big"
    constants.PORT = 502
    constants.BUF_SIZE = 1024
    constants.DEFAULT_TIMEOUT = 0.5
    constants.DEFAULT_DEBOUNCE_DURATION = 3
    ha = types.ModuleType("homeassistant")
    ha.__path__ = []
    components = types.ModuleType("homeassistant.components")
    components.__path__ = []
    switch = types.ModuleType("homeassistant.components.switch")
    switch.SwitchEntity = type("SwitchEntity", (), {})
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    with patch.dict(sys.modules, {
        package.__name__: package,
        constants.__name__: constants,
        "homeassistant": ha,
        "homeassistant.components": components,
        "homeassistant.components.switch": switch,
        "homeassistant.exceptions": exceptions,
    }):
        rcm = importlib.import_module("_rcm_test_sihas.rcm")
        builder = importlib.import_module("_rcm_test_sihas.packet_builder")
        sender = importlib.import_module("_rcm_test_sihas.sender")
        errors = importlib.import_module("_rcm_test_sihas.errors")
    return rcm, builder.packet_builder, sender, errors


rcm, builder, sender, errors = load_modules()


class FakeDevice:
    def __init__(self):
        self.registers = [0] * 64
        self.calls = []
        self.response_filter = lambda request, response: response
        self.timeout_count = 0
        self.online = True
        self.apply_writes = True
        self.yield_on_write = False

    def respond(self, request):
        function = request[7]
        self.calls.append((function, request[8:]))
        if not self.online or self.timeout_count:
            self.timeout_count = max(0, self.timeout_count - 1)
            raise socket.timeout("simulated lost packet")
        if function == 3:
            payload = b"".join(value.to_bytes(2, "big") for value in self.registers)
            response = request[:4] + b"\x00\x83" + request[6:7] + b"\x03\x80" + payload
        elif function == 6:
            index = int.from_bytes(request[8:10], "big")
            value = int.from_bytes(request[10:12], "big")
            if self.apply_writes:
                self.registers[index] = value
            if self.yield_on_write:
                time.sleep(0.001)
            response = request
        else:
            raise AssertionError("unexpected function")
        return self.response_filter(request, response)

    def make_socket(self, *args, **kwargs):
        device = self

        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def settimeout(self, timeout):
                pass

            def sendto(self, data, address):
                if address != ("192.0.2.1", 502):
                    raise AssertionError(f"unexpected endpoint {address!r}")
                try:
                    self.response = device.respond(data)
                except socket.timeout as error:
                    self.response = error

            def recv(self, size):
                if isinstance(self.response, Exception):
                    raise self.response
                return self.response

        return FakeSocket()


class RcmTests(unittest.TestCase):
    def setUp(self):
        builder._pid = 0
        self.device = FakeDevice()
        self.socket_patch = patch.object(sender.socket, "socket", self.device.make_socket)
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)
        self.controller = rcm.RcmRelayController("192.0.2.1")
        self.entity = rcm.RcmRelay(
            self.controller, "00:00:00:00:00:01", "Test RCM", 0
        )

    def test_known_wire_vectors(self):
        self.assertEqual(builder.poll().hex(), "000100000006070300000040")
        self.assertEqual(builder.command(3, 1).hex(), "000200000006080600030001")

    def test_read_returns_all_64_registers(self):
        self.device.registers[63] = 65535
        self.assertEqual(self.controller.read(), self.device.registers)

    def test_all_four_channels_on_and_off_without_touching_neighbours(self):
        for channel in range(4):
            with self.subTest(channel=channel):
                expected = [0] * 64
                expected[channel] = 1
                self.assertEqual(self.controller.set_channel(channel, True), expected)
                self.assertEqual(self.controller.set_channel(channel, False), [0] * 64)

    def test_invalid_channels_do_not_send_packets(self):
        for channel in (-1, 4, 1.5, "0", None, True):
            with self.subTest(channel=channel):
                with self.assertRaises(ValueError):
                    self.controller.set_channel(channel, True)
        self.assertEqual(self.device.calls, [])

    def test_nonboolean_commands_do_not_send_packets(self):
        for enabled in (0, 1, 2, "on", None):
            with self.subTest(enabled=enabled):
                with self.assertRaises(ValueError):
                    self.controller.set_channel(0, enabled)
        self.assertEqual(self.device.calls, [])

    def test_wrong_write_acknowledgement_is_rejected(self):
        for offset in (0, 7, 9, 11):
            with self.subTest(offset=offset):
                def corrupt(request, response):
                    if request[7] == 6:
                        data = bytearray(response)
                        data[offset] ^= 1
                        return bytes(data)
                    return response
                self.device.response_filter = corrupt
                with self.assertRaises(ValueError):
                    self.controller.set_channel(0, True)

    def test_unapplied_write_is_detected_by_readback(self):
        self.device.apply_writes = False
        with self.assertRaisesRegex(ValueError, "readback"):
            self.controller.set_channel(0, True)

    def test_stale_read_transaction_is_rejected(self):
        self.device.response_filter = lambda req, resp: b"\xff\xff" + resp[2:]
        with self.assertRaises(ValueError):
            self.controller.read()

    def test_malformed_read_headers_are_rejected(self):
        for offset, value in ((2, 1), (5, 130), (7, 4), (8, 127)):
            with self.subTest(offset=offset):
                def corrupt(request, response):
                    data = bytearray(response)
                    data[offset] = value
                    return bytes(data)
                self.device.response_filter = corrupt
                with self.assertRaises(ValueError):
                    self.controller.read()

    def test_short_and_oversized_reads_are_rejected(self):
        for size in (0, 7, 8, 12, 136, 138):
            with self.subTest(size=size):
                self.device.response_filter = lambda req, resp: (resp + b"\x00")[:size]
                with self.assertRaises((ValueError, IndexError)):
                    self.controller.read()

    def test_three_lost_packets_raise_timeout(self):
        self.device.online = False
        with self.assertRaises(TimeoutError):
            self.controller.read()
        self.assertEqual(len(self.device.calls), 3)

    def test_transient_packet_loss_retries(self):
        self.device.timeout_count = 2
        self.assertEqual(self.controller.read(), [0] * 64)
        self.assertEqual(len(self.device.calls), 3)

    def test_disabled_local_protocol_makes_entity_unavailable(self):
        self.device.response_filter = lambda req, resp: req[:7] + b"\x0b\x00"
        self.entity.update()
        self.assertFalse(self.entity._attr_available)

    def test_modbus_exception_does_not_report_a_state(self):
        self.device.response_filter = lambda req, resp: req[:7] + b"\x83\x00\x00\x00\x00"
        self.entity.update()
        self.assertFalse(self.entity._attr_available)
        self.assertIsNone(self.entity._attr_is_on)

    def test_entity_recovers_after_timeout(self):
        self.device.online = False
        self.entity.update()
        self.assertFalse(self.entity._attr_available)
        self.device.online = True
        self.device.registers[0] = 1
        self.entity.update()
        self.assertTrue(self.entity._attr_available)
        self.assertTrue(self.entity._attr_is_on)

    def test_entity_recovers_after_malformed_response(self):
        self.device.response_filter = lambda req, resp: b""
        self.entity.update()
        self.assertFalse(self.entity._attr_available)
        self.device.response_filter = lambda req, resp: resp
        self.entity.update()
        self.assertTrue(self.entity._attr_available)

    def test_nonbinary_state_is_not_reported_as_off(self):
        self.device.registers[0] = 2
        self.entity.update()
        self.assertFalse(self.entity._attr_available)
        self.assertIsNone(self.entity._attr_is_on)

    def test_entity_turn_on_and_off_use_readback(self):
        self.entity.turn_on()
        self.assertTrue(self.entity._attr_is_on)
        self.entity.turn_off()
        self.assertFalse(self.entity._attr_is_on)
        self.assertTrue(self.entity._attr_available)

    def test_failed_command_raises_and_marks_unavailable(self):
        self.device.apply_writes = False
        with self.assertRaisesRegex(rcm.HomeAssistantError, "readback"):
            self.entity.turn_on()
        self.assertFalse(self.entity._attr_available)
        self.assertIsNone(self.entity._attr_is_on)

    def test_device_and_entity_identifiers_match_initial_patch(self):
        for channel in range(4):
            entity = rcm.RcmRelay(self.controller, "00:00:00:00:00:01", "Test RCM", channel)
            self.assertEqual(
                entity._attr_unique_id, f"RCM-00:00:00:00:00:01-relay-{channel + 1}"
            )
            self.assertEqual(
                entity._attr_device_info["identifiers"], {("sihas", "00:00:00:00:00:01")}
            )

    def test_command_and_readback_are_serialized(self):
        self.device.yield_on_write = True
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.controller.set_channel, i % 4, True) for i in range(16)]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual([fc for fc, _ in self.device.calls], [6, 3] * 16)

    def test_release_keeps_integration_domain(self):
        manifest = json.loads((ROOT / "custom_components/sihas/manifest.json").read_text())
        self.assertEqual(manifest["domain"], "sihas")
        version_config = tomllib.loads((ROOT / ".bumpversion.toml").read_text())
        self.assertEqual(manifest["version"], version_config["tool"]["bumpversion"]["current_version"])
        self.assertEqual(manifest["iot_class"], "local_polling")


if __name__ == "__main__":
    unittest.main()
