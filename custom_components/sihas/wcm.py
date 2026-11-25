"""Local WCM-300-W outlets, electrical measurements and settings.

Outlet registers 0..3 were verified on firmware 03.08, CFG=04. Power
registers 4..7 matched the app, including simultaneous outlet 3/4 loads.
Register 60 is voltage in tenths of a volt, verified against the app.
Register 61 is current in milliamps, confirmed by the user after the kettle test.
Register 62 is frequency in tenths of a hertz, confirmed by the user.
Settings on outlets 2 and 4 match app changes and restoration. The user
approved applying the corresponding layout to outlets 1 and 3.
Auto-off timer registers 26 and 29 match app settings on outlets 1 and 4.
The user confirmed adopting registers 27 and 28 for outlets 2 and 3.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
import logging
from threading import Lock
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import CONF_IP, CONF_MAC, CONF_NAME, DOMAIN
from .packet_builder import POLL_RESPONSE_LENGTH, packet_builder
from .sender import send
from .util import MacConv

_LOGGER = logging.getLogger(__name__)
CHANNEL_COUNT = 4
POWER_REGISTER_START = 4
VERIFIED_POWER_CHANNELS = frozenset((0, 1, 2, 3))
VERIFIED_SETTING_CHANNELS = frozenset((1, 3))
VERIFIED_TIMER_CHANNELS = frozenset((0, 3))
AUTO_OFF_REGISTERS = (26, 27, 28, 29)
VOLTAGE_REGISTER = 60
CURRENT_REGISTER = 61
FREQUENCY_REGISTER = 62


def _validate_channel(channel: int) -> None:
    if type(channel) is not int or channel not in range(CHANNEL_COUNT):
        raise ValueError("WCM channel must be between 0 and 3")


class WcmSetting(StrEnum):
    BUTTON_LOCK = "button-lock"
    OVERLOAD_CUTOFF = "overload-cutoff"
    STANDBY_CUTOFF = "standby-cutoff"
    AUTO_OFF_ENABLED = "auto-off-timer"
    AUTO_OFF_MINUTES = "auto-off-time"


@dataclass(frozen=True)
class _SettingLayout:
    registers: tuple[int, int, int, int]
    shifts: tuple[int, int, int, int]
    value_mask: int
    maximum: int
    minimum: int = 0


# Only the bits needed to represent each app setting belong to the field.
# Preserve the other bits, including the adjacent outlet in the overload word.
_SETTING_LAYOUTS = {
    WcmSetting.BUTTON_LOCK: _SettingLayout((8, 8, 8, 8), (0, 1, 2, 3), 0x0001, 1),
    WcmSetting.OVERLOAD_CUTOFF: _SettingLayout((9, 9, 10, 10), (0, 8, 0, 8), 0x0007, 5),
    WcmSetting.STANDBY_CUTOFF: _SettingLayout((11, 12, 13, 14), (0, 0, 0, 0), 0x07FF, 2000),
    WcmSetting.AUTO_OFF_ENABLED: _SettingLayout(AUTO_OFF_REGISTERS, (15, 15, 15, 15), 0x0001, 1),
    WcmSetting.AUTO_OFF_MINUTES: _SettingLayout(AUTO_OFF_REGISTERS, (0, 0, 0, 0), 0x07FF, 1800, minimum=1),
}
_TIMER_SETTINGS = frozenset((WcmSetting.AUTO_OFF_ENABLED, WcmSetting.AUTO_OFF_MINUTES))
_SWITCH_NAMES = {
    WcmSetting.BUTTON_LOCK: "버튼잠금",
    WcmSetting.AUTO_OFF_ENABLED: "자동 OFF 타이머",
}
_NUMBER_DETAILS = {
    WcmSetting.OVERLOAD_CUTOFF: ("과부하 차단", UnitOfPower.KILO_WATT, NumberDeviceClass.POWER),
    WcmSetting.STANDBY_CUTOFF: ("대기전력 차단", UnitOfPower.WATT, NumberDeviceClass.POWER),
    WcmSetting.AUTO_OFF_MINUTES: ("자동 OFF 시간", UnitOfTime.MINUTES, NumberDeviceClass.DURATION),
}


def _validate_setting_value(setting: WcmSetting, value: int) -> None:
    layout = _SETTING_LAYOUTS[setting]
    if type(value) is not int or not layout.minimum <= value <= layout.maximum:
        raise ValueError(f"WCM {setting} must be an integer from {layout.minimum} to {layout.maximum}")


@dataclass(frozen=True)
class WcmState:
    """One immutable snapshot shared by all entities for the device."""

    registers: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.registers) != 64:
            raise ValueError("Expected 64 WCM registers")
        if any(value not in (0, 1) for value in self.registers[:CHANNEL_COUNT]):
            raise ValueError("Unexpected nonbinary WCM outlet state")

    def outlet_on(self, channel: int) -> bool:
        _validate_channel(channel)
        return self.registers[channel] == 1

    def power_w(self, channel: int) -> int:
        _validate_channel(channel)
        return self.registers[POWER_REGISTER_START + channel]

    def setting_value(self, channel: int, setting: WcmSetting) -> int | None:
        _validate_channel(channel)
        layout = _SETTING_LAYOUTS[setting]
        word = self.registers[layout.registers[channel]]
        value = (word >> layout.shifts[channel]) & layout.value_mask
        return value if layout.minimum <= value <= layout.maximum else None

    @property
    def total_power_w(self) -> int:
        return sum(self.registers[POWER_REGISTER_START:POWER_REGISTER_START + CHANNEL_COUNT])

    @property
    def voltage_v(self) -> float:
        return self.registers[VOLTAGE_REGISTER] / 10

    @property
    def current_a(self) -> float:
        return self.registers[CURRENT_REGISTER] / 1000

    @property
    def frequency_hz(self) -> float:
        return self.registers[FREQUENCY_REGISTER] / 10


class WcmClient:
    """Validate SiHAS responses and serialize each write with its readback."""

    def __init__(self, ip: str) -> None:
        self.ip = ip
        self._lock = Lock()

    def read(self) -> WcmState:
        with self._lock:
            return self._read()

    def _read(self) -> WcmState:
        request = packet_builder.poll()
        response = send(request, self.ip, retry=3)
        # SiHAS echoes the request's unit/checksum byte in its response.
        if (
            len(response) != POLL_RESPONSE_LENGTH
            or response[:2] != request[:2]
            or response[2:6] != b"\x00\x00\x00\x83"
            or response[6] != request[6]
            or response[7:9] != b"\x03\x80"
        ):
            raise ValueError("Unexpected WCM read response; check local communication (ST/HA)")
        return WcmState(tuple(packet_builder.extract_registers(response)))

    def set_channel(self, channel: int, enabled: bool) -> WcmState:
        _validate_channel(channel)
        if type(enabled) is not bool:
            raise ValueError("WCM outlet state must be a boolean")
        with self._lock:
            state = self._write_and_read(channel, int(enabled))
            if state.outlet_on(channel) != enabled:
                raise ValueError("WCM outlet readback did not match requested state")
            return state

    def set_setting(self, channel: int, setting: WcmSetting, value: int) -> WcmState:
        _validate_channel(channel)
        _validate_setting_value(setting, value)
        layout = _SETTING_LAYOUTS[setting]
        register = layout.registers[channel]
        shift = layout.shifts[channel]
        mask = layout.value_mask << shift
        with self._lock:
            before = self._read()
            if setting is not WcmSetting.AUTO_OFF_MINUTES and before.setting_value(channel, setting) is None:
                raise ValueError(f"WCM {setting} has an unexpected value in register {register}")
            if (
                setting is WcmSetting.AUTO_OFF_ENABLED
                and value == 1
                and before.setting_value(channel, WcmSetting.AUTO_OFF_MINUTES) is None
            ):
                raise ValueError("Set a valid WCM auto-off time before enabling the timer")
            previous = before.registers[register]
            updated = (previous & ~mask) | (value << shift)
            if updated == previous:
                return before
            after = self._write_and_read(register, updated)
            if after.registers[register] != updated:
                raise ValueError(f"WCM setting readback did not match register {register}")
            return after

    def _write_and_read(self, register: int, word: int) -> WcmState:
        request = packet_builder.command(register, word)
        if send(request, self.ip, retry=3) != request:
            raise ValueError("WCM write acknowledgement did not match request")
        return self._read()


class WcmCoordinator(DataUpdateCoordinator[WcmState]):
    """Poll once for all outlets, sensors and settings every five seconds."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"WCM-{entry.data[CONF_MAC]}",
            config_entry=entry,
            update_interval=timedelta(seconds=5),
            always_update=False,
        )
        self.entry = entry
        self.mac = MacConv.insert_colon(entry.data[CONF_MAC]).lower()
        self.client = WcmClient(entry.data[CONF_IP])
        self._io_lock = asyncio.Lock()

    async def _async_update_data(self) -> WcmState:
        async with self._io_lock:
            # Discovery may update the config entry when DHCP changes the IP.
            self.client.ip = self.entry.data[CONF_IP]
            try:
                return await self.hass.async_add_executor_job(self.client.read)
            except Exception as err:
                raise UpdateFailed(f"WCM read failed: {err}") from err

    async def async_set_channel(self, channel: int, enabled: bool) -> None:
        _validate_channel(channel)
        if type(enabled) is not bool:
            raise ValueError("WCM outlet state must be a boolean")
        await self._async_command(self.client.set_channel, channel, enabled)

    async def async_set_setting(self, channel: int, setting: WcmSetting, value: int) -> None:
        _validate_channel(channel)
        _validate_setting_value(setting, value)
        await self._async_command(self.client.set_setting, channel, setting, value)

    async def _async_command(self, command: Callable[..., WcmState], *args: Any) -> None:
        async with self._io_lock:
            self.client.ip = self.entry.data[CONF_IP]
            try:
                state = await self.hass.async_add_executor_job(command, *args)
            except Exception as err:
                self.async_set_update_error(UpdateFailed(f"WCM command failed: {err}"))
                raise HomeAssistantError(f"WCM command failed: {err}") from err
            self.async_set_updated_data(state)


class WcmEntity(CoordinatorEntity[WcmCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: WcmCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"WCM-{coordinator.mac}-{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.mac)},
            manufacturer="SiHAS",
            model="WCM-300-W",
            name=coordinator.entry.data.get(CONF_NAME) or "WCM-300-W",
        )


class WcmOutlet(WcmEntity, SwitchEntity):
    """One independently controlled physical outlet."""

    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(self, coordinator: WcmCoordinator, channel: int) -> None:
        _validate_channel(channel)
        super().__init__(coordinator, f"outlet-{channel + 1}")
        self.channel = channel
        self._attr_name = f"콘센트 {channel + 1}"

    @property
    def is_on(self) -> bool | None:
        if not self.available or self.coordinator.data is None:
            return None
        return self.coordinator.data.outlet_on(self.channel)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_channel(self.channel, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_channel(self.channel, False)


class _WcmSettingEntity(WcmEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WcmCoordinator, channel: int, setting: WcmSetting) -> None:
        _validate_channel(channel)
        super().__init__(coordinator, f"{setting}-{channel + 1}")
        self.channel = channel
        self.setting = setting
        if setting in _TIMER_SETTINGS:
            verified = channel in VERIFIED_TIMER_CHANNELS
            status = "verified" if verified else "confirmed"
            basis = "app_readback" if verified else "user_confirmed"
        else:
            verified = channel in VERIFIED_SETTING_CHANNELS
            status = "verified" if verified else "inferred"
            basis = "app_readback" if verified else "user_accepted_channel_pattern"
        self._attr_extra_state_attributes = {
            "setting_register": _SETTING_LAYOUTS[setting].registers[channel],
            "register_mapping": status,
            "mapping_basis": basis,
        }

    @property
    def _setting_value(self) -> int | None:
        if not self.available or self.coordinator.data is None:
            return None
        return self.coordinator.data.setting_value(self.channel, self.setting)

    async def _async_set_setting_value(self, value: int) -> None:
        await self.coordinator.async_set_setting(self.channel, self.setting, value)


class WcmSettingSwitch(_WcmSettingEntity, SwitchEntity):
    """A channel's button lock or auto-off timer enable switch."""

    def __init__(self, coordinator: WcmCoordinator, channel: int, setting: WcmSetting) -> None:
        name = _SWITCH_NAMES[setting]
        super().__init__(coordinator, channel, setting)
        self._attr_name = f"콘센트 {channel + 1} {name}"

    @property
    def is_on(self) -> bool | None:
        value = self._setting_value
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_setting_value(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_setting_value(0)


class WcmSettingNumber(_WcmSettingEntity, NumberEntity):
    """A channel's cutoff threshold or auto-off time."""

    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    def __init__(self, coordinator: WcmCoordinator, channel: int, setting: WcmSetting) -> None:
        name, unit, device_class = _NUMBER_DETAILS[setting]
        super().__init__(coordinator, channel, setting)
        self._attr_name = f"콘센트 {channel + 1} {name}"
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        layout = _SETTING_LAYOUTS[setting]
        self._attr_native_min_value = layout.minimum
        self._attr_native_max_value = layout.maximum

    @property
    def native_value(self) -> int | None:
        return self._setting_value

    async def async_set_native_value(self, value: float) -> None:
        minimum = self._attr_native_min_value
        maximum = self._attr_native_max_value
        if type(value) not in (float, int) or not minimum <= value <= maximum or value % 1 != 0:
            raise ServiceValidationError(f"{self._attr_name}: {minimum}~{maximum} 사이의 정수를 입력하세요")
        await self._async_set_setting_value(int(value))


class WcmPowerSensor(WcmEntity, SensorEntity):
    """Per-outlet watts, or their calculated sum when channel is None."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: WcmCoordinator, channel: int | None) -> None:
        if channel is not None:
            _validate_channel(channel)
        key = "power-total" if channel is None else f"power-{channel + 1}"
        super().__init__(coordinator, key)
        self.channel = channel
        if channel is None:
            self._attr_name = "합계 소비전력"
            self._attr_extra_state_attributes = {
                "calculation": "sum_of_channels",
                "contains_inferred_channels": len(VERIFIED_POWER_CHANNELS) < CHANNEL_COUNT,
            }
        else:
            self._attr_name = f"콘센트 {channel + 1} 소비전력"
            self._attr_extra_state_attributes = {
                "power_register": POWER_REGISTER_START + channel,
                "register_mapping": (
                    "verified" if channel in VERIFIED_POWER_CHANNELS else "inferred"
                ),
            }

    @property
    def native_value(self) -> int | None:
        if not self.available or self.coordinator.data is None:
            return None
        if self.channel is None:
            return self.coordinator.data.total_power_w
        return self.coordinator.data.power_w(self.channel)


class WcmVoltageSensor(WcmEntity, SensorEntity):
    """Shared mains voltage; register 60 is expressed in 0.1 V."""

    _attr_name = "전압"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: WcmCoordinator) -> None:
        super().__init__(coordinator, "voltage")
        self._attr_extra_state_attributes = {
            "voltage_register": VOLTAGE_REGISTER,
            "register_mapping": "verified",
        }

    @property
    def native_value(self) -> float | None:
        if not self.available or self.coordinator.data is None:
            return None
        return self.coordinator.data.voltage_v


class WcmCurrentSensor(WcmEntity, SensorEntity):
    """Shared current; a raw milliamp value is divided by 1000 for amps."""

    _attr_name = "전류"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: WcmCoordinator) -> None:
        super().__init__(coordinator, "current")
        self._attr_extra_state_attributes = {
            "current_register": CURRENT_REGISTER,
            "raw_unit": "mA",
            "register_mapping": "verified",
            "mapping_basis": "user_confirmed",
        }

    @property
    def native_value(self) -> float | None:
        if not self.available or self.coordinator.data is None:
            return None
        return self.coordinator.data.current_a


class WcmFrequencySensor(WcmEntity, SensorEntity):
    """Mains frequency; register 62 uses 0.1 Hz per the confirmed mapping."""

    _attr_name = "주파수"
    _attr_device_class = SensorDeviceClass.FREQUENCY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfFrequency.HERTZ
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: WcmCoordinator) -> None:
        super().__init__(coordinator, "frequency")
        self._attr_extra_state_attributes = {
            "frequency_register": FREQUENCY_REGISTER,
            "register_mapping": "verified",
            "mapping_basis": "user_confirmed",
        }

    @property
    def native_value(self) -> float | None:
        if not self.available or self.coordinator.data is None:
            return None
        return self.coordinator.data.frequency_hz


def get_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> WcmCoordinator:
    return hass.data[DOMAIN][entry.entry_id]
