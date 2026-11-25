"""Offline WCM behavior tests; never contact or switch a real device."""

import asyncio
import concurrent.futures
import json
import unittest
from unittest.mock import patch

from wcm_test_support import (
    FakeDevice,
    Hass,
    HomeAssistantError,
    ROOT,
    ServiceValidationError,
    UpdateFailed,
    builder,
    entry,
    integration,
    number_platform,
    sender,
    switch_platform,
    wcm,
)


class ClientTests(unittest.TestCase):
    def setUp(self):
        builder._pid = 0
        self.device = FakeDevice()
        patcher = patch.object(sender.socket, "socket", self.device.make_socket)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = wcm.WcmClient("192.0.2.1")

    def test_known_wire_vectors(self):
        self.client.read()
        self.client.set_channel(3, True)
        self.assertEqual(self.device.calls[0].hex(), "000100000006070300000040")
        self.assertEqual(self.device.calls[1].hex(), "000200000006080600030001")

    def test_all_outlets_and_powers_have_independent_registers(self):
        self.device.registers[4:8] = [1, 18, 37, 255]
        for channel in range(4):
            state = self.client.set_channel(channel, True)
            self.assertEqual([state.outlet_on(i) for i in range(4)], [i <= channel for i in range(4)])
            self.assertEqual([state.power_w(i) for i in range(4)], [1, 18, 37, 255])
            self.assertEqual(state.total_power_w, 311)
        for channel in range(4):
            self.client.set_channel(channel, False)
        self.assertEqual(self.device.registers[:4], [0] * 4)
        self.assertEqual(self.device.registers[4:8], [1, 18, 37, 255])

    def test_actual_captured_packets_match_reference_loads(self):
        fixtures = list((ROOT / "tests/fixtures").glob("wcm_read_*.json"))
        self.assertGreaterEqual(len(fixtures), 2)
        for path in fixtures:
            with self.subTest(fixture=path.name):
                fixture = json.loads(path.read_text())
                builder._pid = int(fixture["requestHex"][:4], 16) - 1
                self.device.response_filter = lambda req, resp: bytes.fromhex(fixture["responseHex"])
                state = self.client.read()
                self.assertEqual(list(state.registers[:4]), fixture["outletStates"])
                self.assertEqual([state.power_w(i) for i in range(4)], fixture["powerWatts"])

    def test_snapshot_does_not_change_with_later_reads(self):
        state = self.client.read()
        self.device.registers[4] = 18
        self.assertEqual(self.client.read().power_w(0), 18)
        self.assertEqual(state.power_w(0), 0)

    def test_voltage_register_from_captured_response(self):
        fixture = json.loads((ROOT / "tests/fixtures/wcm_voltage_2186.json").read_text())
        builder._pid = int(fixture["requestHex"][:4], 16) - 1
        self.device.response_filter = lambda req, resp: bytes.fromhex(fixture["responseHex"])
        self.assertEqual(self.client.read().voltage_v, fixture["expectedVoltageV"])

    def test_frequency_register_from_captured_response(self):
        fixture = json.loads((ROOT / "tests/fixtures/wcm_frequency_599.json").read_text())
        builder._pid = int(fixture["requestHex"][:4], 16) - 1
        self.device.response_filter = lambda req, resp: bytes.fromhex(fixture["responseHex"])
        self.assertEqual(self.client.read().frequency_hz, fixture["expectedFrequencyHz"])

    def test_current_register_from_captured_kettle_response(self):
        fixture = json.loads((ROOT / "tests/fixtures/wcm_current_6575.json").read_text())
        builder._pid = int(fixture["requestHex"][:4], 16) - 1
        self.device.response_filter = lambda req, resp: bytes.fromhex(fixture["responseHex"])
        self.assertEqual(self.client.read().current_a, fixture["expectedCurrentA"])

    def test_settings_from_actual_outlet_2_and_4_packets(self):
        fixtures = json.loads((ROOT / "tests/fixtures/wcm_settings_readback.json").read_text())
        for fixture in fixtures:
            with self.subTest(outlet=fixture["outlet"]):
                builder._pid = int(fixture["requestHex"][:4], 16) - 1
                self.device.response_filter = lambda req, resp: bytes.fromhex(fixture["responseHex"])
                state = self.client.read()
                channel = fixture["outlet"] - 1
                self.assertEqual(state.setting_value(channel, wcm.WcmSetting.BUTTON_LOCK), 1)
                self.assertEqual(
                    state.setting_value(channel, wcm.WcmSetting.OVERLOAD_CUTOFF), fixture["overloadKW"]
                )
                self.assertEqual(
                    state.setting_value(channel, wcm.WcmSetting.STANDBY_CUTOFF), fixture["standbyW"]
                )

    def test_timer_duration_and_enable_bit_match_actual_packets(self):
        fixtures = json.loads((ROOT / "tests/fixtures/wcm_timer_readback.json").read_text())
        for fixture in fixtures:
            with self.subTest(outlet1_enabled=fixture["outlet1Enabled"]):
                builder._pid = int(fixture["requestHex"][:4], 16) - 1
                self.device.response_filter = lambda req, resp: bytes.fromhex(fixture["responseHex"])
                state = self.client.read()
                self.assertEqual(state.setting_value(0, wcm.WcmSetting.AUTO_OFF_MINUTES), 123)
                self.assertEqual(state.setting_value(0, wcm.WcmSetting.AUTO_OFF_ENABLED), fixture["outlet1Enabled"])
                self.assertEqual(state.setting_value(3, wcm.WcmSetting.AUTO_OFF_MINUTES), 1234)
                self.assertEqual(state.setting_value(3, wcm.WcmSetting.AUTO_OFF_ENABLED), 1)
                self.assertFalse(state.outlet_on(0))

    def test_outlet_1_and_3_setting_writes_preserve_every_other_field(self):
        lock = wcm.WcmSetting.BUTTON_LOCK
        overload = wcm.WcmSetting.OVERLOAD_CUTOFF
        standby = wcm.WcmSetting.STANDBY_CUTOFF
        self.device.registers[8:15] = [0x800A, 0x6460, 0x6160, 0x3800, 0x34D2, 0x3000, 0x3007]
        for channel, setting, value, register, word in [
            (0, lock, 1, 8, 0x800B),
            (2, lock, 1, 8, 0x800F),
            (0, overload, 5, 9, 0x6465),
            (2, overload, 2, 10, 0x6162),
            (0, standby, 2000, 11, 0x3FD0),
            (2, standby, 260, 13, 0x3104),
        ]:
            with self.subTest(channel=channel, setting=setting):
                expected = list(self.device.registers)
                expected[register] = word
                state = self.client.set_setting(channel, setting, value)
                request = self.device.calls[-2]
                self.assertEqual(request[7], 6)
                self.assertEqual(int.from_bytes(request[8:10], "big"), register)
                self.assertEqual(int.from_bytes(request[10:12], "big"), word)
                self.assertEqual(list(state.registers), expected)
                self.assertEqual(state.setting_value(channel, setting), value)

    def test_setting_limits_prevent_invalid_writes(self):
        for setting, bad in [
            (wcm.WcmSetting.BUTTON_LOCK, 2),
            (wcm.WcmSetting.OVERLOAD_CUTOFF, 6),
            (wcm.WcmSetting.OVERLOAD_CUTOFF, 1.5),
            (wcm.WcmSetting.STANDBY_CUTOFF, 2001),
            (wcm.WcmSetting.STANDBY_CUTOFF, -1),
        ]:
            with self.subTest(setting=setting, bad=bad), self.assertRaises(ValueError):
                self.client.set_setting(0, setting, bad)
        self.assertEqual(self.device.calls, [])

    def test_a_setting_write_requires_matching_full_word_readback(self):
        self.device.registers[9] = 0x6460

        def drop_neighbor_value(request, response):
            if request[7] == 6:
                self.device.registers[9] = 0x6063
            return response

        self.device.response_filter = drop_neighbor_value
        with self.assertRaisesRegex(ValueError, "readback"):
            self.client.set_setting(0, wcm.WcmSetting.OVERLOAD_CUTOFF, 3)
        self.assertEqual([request[7] for request in self.device.calls], [3, 6, 3])

    def test_concurrent_cutoff_edits_do_not_overwrite_the_other_outlet(self):
        self.device.registers[9] = 0x6060
        self.device.yield_on_write = True
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self.client.set_setting, channel, wcm.WcmSetting.OVERLOAD_CUTOFF, value)
                for channel, value in [(0, 3), (1, 4)]
            ]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(self.device.registers[9], 0x6463)
        self.assertEqual([request[7] for request in self.device.calls], [3, 6, 3] * 2)

    def test_invalid_channels_and_values_send_no_packets(self):
        for channel in [-1, 4, True, 1.0, "1", None]:
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                self.client.set_channel(channel, True)
        for enabled in [0, 1, "on", None]:
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                self.client.set_channel(0, enabled)
        self.assertEqual(self.device.calls, [])

    def test_corrupted_read_headers_are_rejected(self):
        for offset, value in [(0, 255), (2, 1), (5, 130), (6, 255), (7, 4), (8, 127)]:
            with self.subTest(offset=offset):
                def corrupt(request, response):
                    data = bytearray(response)
                    data[offset] = value
                    return bytes(data)
                self.device.response_filter = corrupt
                with self.assertRaises(ValueError):
                    self.client.read()

    def test_truncated_and_oversized_reads_are_rejected(self):
        for size in [0, 7, 8, 12, 136, 138]:
            with self.subTest(size=size):
                self.device.response_filter = lambda req, resp: (resp + b"\x00")[:size]
                with self.assertRaises((ValueError, IndexError)):
                    self.client.read()

    def test_nonbinary_outlet_state_is_rejected(self):
        self.device.registers[2] = 2
        with self.assertRaisesRegex(ValueError, "nonbinary"):
            self.client.read()

    def test_wrong_write_echo_and_ignored_write_are_rejected(self):
        self.device.response_filter = lambda req, resp: resp[:-1] + b"\xff" if req[7] == 6 else resp
        with self.assertRaisesRegex(ValueError, "acknowledgement"):
            self.client.set_channel(0, True)
        self.device.response_filter = lambda req, resp: resp
        self.device.apply_writes = False
        with self.assertRaisesRegex(ValueError, "readback"):
            self.client.set_channel(1, True)

    def test_lost_packets_retry_then_timeout(self):
        self.device.timeout_count = 2
        self.assertEqual(self.client.read().power_w(0), 0)
        self.assertEqual(len(self.device.calls), 3)
        self.device.online = False
        with self.assertRaises(TimeoutError):
            self.client.read()
        self.assertEqual(len(self.device.calls), 6)

    def test_concurrent_commands_keep_each_write_and_readback_together(self):
        self.device.yield_on_write = True
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.client.set_channel, i % 4, True) for i in range(16)]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual([request[7] for request in self.device.calls], [6, 3] * 16)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        builder._pid = 0
        self.device = FakeDevice()
        patcher = patch.object(sender.socket, "socket", self.device.make_socket)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hass = Hass()
        self.entry = entry()
        self.coordinator = wcm.WcmCoordinator(self.hass, self.entry)

    async def test_wcm_platform_entities_share_one_read_and_keep_stable_ids(self):
        self.device.registers[4:8] = [1, 18, 37, 255]
        self.device.registers[8:15] = [0x000A, 0x6461, 0x6162, 0x3005, 0x34D2, 0x3009, 0x3007]
        self.device.registers[26:30] = [0x807B, 0x0001, 0x002D, 0x84D2]
        self.device.registers[60] = 2187
        self.device.registers[61] = 6575
        self.device.registers[62] = 599
        await self.coordinator.async_config_entry_first_refresh()
        self.hass.data["sihas"] = {self.entry.entry_id: self.coordinator}
        switches = []
        numbers = []
        await switch_platform.async_setup_entry(self.hass, self.entry, switches.extend)
        await number_platform.async_setup_entry(self.hass, self.entry, numbers.extend)
        outlets = [entity for entity in switches if isinstance(entity, wcm.WcmOutlet)]
        switch_settings = [entity for entity in switches if isinstance(entity, wcm.WcmSettingSwitch)]
        locks = [entity for entity in switch_settings if entity.setting is wcm.WcmSetting.BUTTON_LOCK]
        timers = [entity for entity in switch_settings if entity.setting is wcm.WcmSetting.AUTO_OFF_ENABLED]
        durations = [entity for entity in numbers if entity.setting is wcm.WcmSetting.AUTO_OFF_MINUTES]
        cutoffs = [entity for entity in numbers if entity.setting is not wcm.WcmSetting.AUTO_OFF_MINUTES]
        powers = [wcm.WcmPowerSensor(self.coordinator, i) for i in range(4)]
        total = wcm.WcmPowerSensor(self.coordinator, None)
        voltage = wcm.WcmVoltageSensor(self.coordinator)
        current = wcm.WcmCurrentSensor(self.coordinator)
        frequency = wcm.WcmFrequencySensor(self.coordinator)
        for i, entity in enumerate(outlets):
            self.assertFalse(entity.is_on)
            self.assertEqual(entity._attr_unique_id, f"WCM-aa:bb:cc:dd:ee:ff-outlet-{i+1}")
        self.assertEqual([sensor.native_value for sensor in powers], [1, 18, 37, 255])
        self.assertEqual(total.native_value, 311)
        self.assertEqual(voltage.native_value, 218.7)
        self.assertEqual(current.native_value, 6.575)
        self.assertEqual(frequency.native_value, 59.9)
        self.assertEqual([entity.is_on for entity in locks], [False, True, False, True])
        self.assertEqual([entity.native_value for entity in cutoffs], [1, 5, 4, 1234, 2, 9, 1, 7])
        self.assertEqual(
            [entity._attr_extra_state_attributes["register_mapping"] for entity in locks],
            ["inferred", "verified", "inferred", "verified"],
        )
        self.assertTrue(all(entity._attr_entity_category == "config" for entity in switch_settings + numbers))
        self.assertEqual(cutoffs[0]._attr_unique_id, "WCM-aa:bb:cc:dd:ee:ff-overload-cutoff-1")
        self.assertEqual(cutoffs[-1]._attr_unique_id, "WCM-aa:bb:cc:dd:ee:ff-standby-cutoff-4")
        self.assertEqual([entity.is_on for entity in timers], [True, False, False, True])
        self.assertEqual([entity.native_value for entity in durations], [123, 1, 45, 1234])
        self.assertEqual(
            [entity._attr_extra_state_attributes["mapping_basis"] for entity in timers],
            ["app_readback", "user_confirmed", "user_confirmed", "app_readback"],
        )
        self.assertEqual(timers[0]._attr_unique_id, "WCM-aa:bb:cc:dd:ee:ff-auto-off-timer-1")
        self.assertEqual(durations[1]._attr_unique_id, "WCM-aa:bb:cc:dd:ee:ff-auto-off-time-2")
        entities = switches + numbers + powers + [total, voltage, current, frequency]
        self.assertEqual(len({e._attr_unique_id for e in entities}), 32)
        self.assertTrue(all(e._attr_device_info["identifiers"] == {("sihas", "aa:bb:cc:dd:ee:ff")} for e in entities))
        self.assertEqual([request[7] for request in self.device.calls], [3])

    async def test_ha_controls_validate_values_and_publish_device_readback(self):
        self.device.registers[8:15] = [0x0002, 0x6460, 0x6160, 0x3000, 0x34D2, 0x3000, 0x3007]
        await self.coordinator.async_config_entry_first_refresh()
        lock = wcm.WcmSettingSwitch(self.coordinator, 2, wcm.WcmSetting.BUTTON_LOCK)
        overload = wcm.WcmSettingNumber(self.coordinator, 2, wcm.WcmSetting.OVERLOAD_CUTOFF)
        standby = wcm.WcmSettingNumber(self.coordinator, 0, wcm.WcmSetting.STANDBY_CUTOFF)
        self.assertEqual((overload._attr_native_unit_of_measurement, overload._attr_native_max_value), ("kW", 5))
        self.assertEqual((standby._attr_native_unit_of_measurement, standby._attr_native_max_value), ("W", 2000))
        for entity, bad in [(overload, 1.5), (overload, 6.0), (standby, float("nan")), (standby, 2001.0)]:
            with self.subTest(value=bad), self.assertRaises(ServiceValidationError):
                await entity.async_set_native_value(bad)
        self.assertEqual([request[7] for request in self.device.calls], [3])
        await lock.async_turn_on()
        await overload.async_set_native_value(3.0)
        await standby.async_set_native_value(123.0)
        self.assertTrue(lock.is_on)
        self.assertEqual(overload.native_value, 3)
        self.assertEqual(standby.native_value, 123)
        self.assertEqual(self.device.registers[8:12], [0x0006, 0x6460, 0x6163, 0x307B])
        self.assertEqual(self.device.registers[:4], [0, 0, 0, 0])
        await lock.async_turn_off()
        await overload.async_set_native_value(0.0)
        await standby.async_set_native_value(0.0)
        self.assertEqual(self.device.registers[8:12], [0x0002, 0x6460, 0x6160, 0x3000])

    async def test_timer_controls_use_confirmed_registers_and_preserve_duration_and_enable(self):
        self.device.registers[26:30] = [0x807B, 0x0801, 0x0000, 0x84D2]
        await self.coordinator.async_config_entry_first_refresh()
        effective = wcm.WcmSetting.AUTO_OFF_ENABLED
        minutes = wcm.WcmSetting.AUTO_OFF_MINUTES
        timer2 = wcm.WcmSettingSwitch(self.coordinator, 1, effective)
        timer3 = wcm.WcmSettingSwitch(self.coordinator, 2, effective)
        duration2 = wcm.WcmSettingNumber(self.coordinator, 1, minutes)
        duration3 = wcm.WcmSettingNumber(self.coordinator, 2, minutes)
        self.assertEqual(duration2._attr_native_unit_of_measurement, "min")
        self.assertEqual((duration2._attr_native_min_value, duration2._attr_native_max_value), (1, 1800))
        for value in (0, 1801, 1.5):
            with self.subTest(value=value), self.assertRaises(ServiceValidationError):
                await duration2.async_set_native_value(value)
        self.assertEqual([request[7] for request in self.device.calls], [3])

        await timer2.async_turn_on()
        await duration2.async_set_native_value(1800)
        self.assertEqual(self.device.registers[27], 0x8F08)
        self.assertTrue(timer2.is_on)
        await timer2.async_turn_off()
        self.assertEqual(duration2.native_value, 1800)
        self.assertFalse(timer2.is_on)

        with self.assertRaisesRegex(HomeAssistantError, "valid WCM auto-off time"):
            await timer3.async_turn_on()
        self.assertEqual(self.device.registers[28], 0)
        await duration3.async_set_native_value(123)
        await timer3.async_turn_on()
        self.assertTrue(timer3.is_on)
        self.assertEqual(self.device.registers[26:30], [0x807B, 0x0F08, 0x807B, 0x84D2])
        self.assertEqual(self.device.registers[:4], [0, 0, 0, 0])
        self.assertEqual(
            [int.from_bytes(request[8:10], "big") for request in self.device.calls if request[7] == 6],
            [27, 27, 27, 28, 28],
        )

    async def test_voltage_sensor_units_precision_and_unavailable_recovery(self):
        sensor = wcm.WcmVoltageSensor(self.coordinator)
        self.assertEqual(sensor._attr_native_unit_of_measurement, "V")
        self.assertEqual(sensor._attr_device_class, "voltage")
        self.assertEqual(sensor._attr_state_class, "measurement")
        self.assertEqual(sensor._attr_suggested_display_precision, 1)
        self.assertEqual(sensor._attr_unique_id, "WCM-aa:bb:cc:dd:ee:ff-voltage")
        self.assertEqual(sensor._attr_extra_state_attributes["register_mapping"], "verified")
        for raw, volts in [(2186, 218.6), (2187, 218.7), (2188, 218.8)]:
            self.device.registers[60] = raw
            await self.coordinator.async_refresh()
            self.assertEqual(sensor.native_value, volts)
        self.device.online = False
        await self.coordinator.async_refresh()
        self.assertFalse(sensor.available)
        self.assertIsNone(sensor.native_value)
        self.device.online = True
        await self.coordinator.async_refresh()
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, 218.8)

    async def test_frequency_sensor_conversion_metadata_and_recovery(self):
        sensor = wcm.WcmFrequencySensor(self.coordinator)
        self.assertEqual(sensor._attr_native_unit_of_measurement, "Hz")
        self.assertEqual(sensor._attr_device_class, "frequency")
        self.assertEqual(sensor._attr_state_class, "measurement")
        self.assertEqual(sensor._attr_suggested_display_precision, 1)
        self.assertEqual(sensor._attr_unique_id, "WCM-aa:bb:cc:dd:ee:ff-frequency")
        self.assertEqual(sensor._attr_extra_state_attributes["mapping_basis"], "user_confirmed")
        for raw, hz in [(598, 59.8), (599, 59.9), (600, 60.0)]:
            self.device.registers[62] = raw
            await self.coordinator.async_refresh()
            self.assertEqual(sensor.native_value, hz)
        self.device.online = False
        await self.coordinator.async_refresh()
        self.assertFalse(sensor.available)
        self.assertIsNone(sensor.native_value)
        self.device.online = True
        await self.coordinator.async_refresh()
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, 60.0)

    async def test_current_sensor_conversion_metadata_and_recovery(self):
        sensor = wcm.WcmCurrentSensor(self.coordinator)
        self.assertEqual(sensor._attr_native_unit_of_measurement, "A")
        self.assertEqual(sensor._attr_device_class, "current")
        self.assertEqual(sensor._attr_state_class, "measurement")
        self.assertEqual(sensor._attr_suggested_display_precision, 3)
        self.assertEqual(sensor._attr_unique_id, "WCM-aa:bb:cc:dd:ee:ff-current")
        self.assertEqual(sensor._attr_extra_state_attributes["raw_unit"], "mA")
        self.assertEqual(sensor._attr_extra_state_attributes["mapping_basis"], "user_confirmed")
        for raw, amps in [(0, 0.0), (229, 0.229), (6504, 6.504), (6575, 6.575)]:
            self.device.registers[61] = raw
            await self.coordinator.async_refresh()
            self.assertEqual(sensor.native_value, amps)
        self.device.online = False
        await self.coordinator.async_refresh()
        self.assertFalse(sensor.available)
        self.assertIsNone(sensor.native_value)
        self.device.online = True
        await self.coordinator.async_refresh()
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, 6.575)

    async def test_verified_power_channels_and_stable_identifiers(self):
        sensors = [wcm.WcmPowerSensor(self.coordinator, i) for i in range(4)]
        self.assertEqual([s._attr_extra_state_attributes["register_mapping"] for s in sensors], ["verified"] * 4)
        self.assertEqual([s._attr_extra_state_attributes["power_register"] for s in sensors], [4, 5, 6, 7])
        self.assertTrue(all(s._attr_native_unit_of_measurement == "W" and s._attr_state_class == "measurement" for s in sensors))
        self.assertEqual(sensors[3]._attr_unique_id, "WCM-aa:bb:cc:dd:ee:ff-power-4")
        total = wcm.WcmPowerSensor(self.coordinator, None)
        self.assertFalse(total._attr_extra_state_attributes["contains_inferred_channels"])

    async def test_outlet_action_publishes_fresh_values_to_all_entities(self):
        outlet = wcm.WcmOutlet(self.coordinator, 1)
        power = wcm.WcmPowerSensor(self.coordinator, 1)
        self.device.registers[5] = 18
        await outlet.async_turn_on()
        self.assertTrue(outlet.is_on)
        self.assertEqual(power.native_value, 18)
        await outlet.async_turn_off()
        self.assertFalse(outlet.is_on)
        self.assertEqual([r[7] for r in self.device.calls], [6, 3, 6, 3])

    async def test_failed_command_does_not_publish_success_and_marks_entities_unavailable(self):
        await self.coordinator.async_config_entry_first_refresh()
        outlet = wcm.WcmOutlet(self.coordinator, 0)
        sensor = wcm.WcmPowerSensor(self.coordinator, 0)
        self.device.apply_writes = False
        with self.assertRaisesRegex(HomeAssistantError, "readback"):
            await outlet.async_turn_on()
        self.assertFalse(outlet.available)
        self.assertIsNone(outlet.is_on)
        self.assertIsNone(sensor.native_value)
        self.assertEqual(len(self.coordinator.published), 1)

    async def test_poll_failure_and_recovery(self):
        await self.coordinator.async_config_entry_first_refresh()
        sensor = wcm.WcmPowerSensor(self.coordinator, 0)
        self.device.online = False
        await self.coordinator.async_refresh()
        self.assertFalse(sensor.available)
        self.assertIsNone(sensor.native_value)
        self.device.online = True
        self.device.registers[4] = 18
        await self.coordinator.async_refresh()
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, 18)

    async def test_malformed_poll_is_translated_to_update_failed(self):
        self.device.response_filter = lambda req, resp: b""
        with self.assertRaises(UpdateFailed):
            await self.coordinator._async_update_data()

    async def test_concurrent_polls_and_commands_share_serialized_io(self):
        self.device.yield_on_write = True
        await asyncio.gather(
            self.coordinator.async_refresh(),
            *(self.coordinator.async_set_channel(i, True) for i in range(4)),
        )
        self.assertEqual(list(self.coordinator.data.registers[:4]), [1] * 4)
        self.assertEqual([r[7] for r in self.device.calls], [3] + [6, 3] * 4)

    async def test_dhcp_ip_update_is_used_for_poll_and_command(self):
        self.entry.data = dict(self.entry.data, ip="192.0.2.2")
        await self.coordinator.async_refresh()
        await self.coordinator.async_set_channel(0, True)
        self.assertEqual(set(self.device.addresses), {("192.0.2.2", 502)})

    async def test_startup_only_reads_and_forwards_wcm_platforms(self):
        self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))
        self.assertTrue({"number", "sensor", "switch"}.issubset(self.hass.config_entries.forwarded))
        self.assertEqual([r[7] for r in self.device.calls], [3])
        coordinator = self.hass.data["sihas"][self.entry.entry_id]
        self.assertTrue(await integration.async_unload_entry(self.hass, self.entry))
        self.assertTrue(coordinator.shutdown)
        self.assertNotIn(self.entry.entry_id, self.hass.data["sihas"])

    async def test_failed_startup_and_platform_setup_clean_up_coordinator(self):
        for failure in ["read", "platform"]:
            with self.subTest(failure=failure):
                self.device.online = failure != "read"
                self.hass.config_entries.fail_forward = failure == "platform"
                with patch.object(integration, "WcmCoordinator", return_value=self.coordinator):
                    with self.assertRaises((UpdateFailed, RuntimeError)):
                        await integration.async_setup_entry(self.hass, self.entry)
                self.assertTrue(self.coordinator.shutdown)
                self.assertNotIn(self.entry.entry_id, self.hass.data.get("sihas", {}))

    async def test_failed_unload_keeps_coordinator(self):
        await integration.async_setup_entry(self.hass, self.entry)
        coordinator = self.hass.data["sihas"][self.entry.entry_id]
        self.hass.config_entries.unload_ok = False
        self.assertFalse(await integration.async_unload_entry(self.hass, self.entry))
        self.assertFalse(coordinator.shutdown)
        self.assertIs(self.hass.data["sihas"][self.entry.entry_id], coordinator)


if __name__ == "__main__":
    unittest.main()
