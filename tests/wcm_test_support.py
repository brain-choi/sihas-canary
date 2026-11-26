"""Small HA interface substitutes and a simulated UDP WCM for offline tests.

The production client, sender, packet builder, coordinator and entities are used.
These substitutes do not validate Home Assistant's scheduler or platform runtime.
"""

import asyncio
import importlib
import importlib.util
from pathlib import Path
import socket
import sys
import time
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class HomeAssistantError(Exception):
    pass


class ServiceValidationError(HomeAssistantError):
    pass


class UpdateFailed(Exception):
    pass


class Entity:
    pass


class CoordinatorEntity(Entity):
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator

    @property
    def available(self):
        return self.coordinator.last_update_success


class DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, **kwargs):
        self.hass = hass
        self.data = None
        self.last_update_success = True
        self.options = kwargs
        self.shutdown = False
        self.published = []

    async def async_config_entry_first_refresh(self):
        self.async_set_updated_data(await self._async_update_data())

    async def async_refresh(self):
        try:
            data = await self._async_update_data()
        except UpdateFailed as err:
            self.async_set_update_error(err)
        else:
            self.async_set_updated_data(data)

    def async_set_updated_data(self, data):
        self.data = data
        self.last_update_success = True
        self.published.append(data)

    def async_set_update_error(self, err):
        self.last_update_success = False
        self.last_exception = err

    async def async_shutdown(self):
        self.shutdown = True


class Hass:
    def __init__(self):
        self.data = {}
        self.config_entries = ConfigEntries()

    async def async_add_executor_job(self, func, *args):
        return await asyncio.to_thread(func, *args)


class ConfigEntries:
    fail_forward = False
    unload_ok = True

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded = list(platforms)
        if self.fail_forward:
            raise RuntimeError("simulated platform setup failure")

    async def async_unload_platforms(self, entry, platforms):
        return self.unload_ok


def entry():
    return types.SimpleNamespace(
        entry_id="wcm-test",
        data={"type": "WCM", "ip": "192.0.2.1", "mac": "AA:BB:CC:DD:EE:FF", "name": "Test WCM", "cfg": 4},
    )


def load_modules():
    package = types.ModuleType("_wcm_test_sihas")
    package.__path__ = [str(ROOT / "custom_components/sihas")]
    constants = types.ModuleType(package.__name__ + ".const")
    for key, value in {
        "DOMAIN": "sihas", "ENDIAN": "big", "PORT": 502, "BUF_SIZE": 1024,
        "DEFAULT_TIMEOUT": 0.5, "DEFAULT_DEBOUNCE_DURATION": 3,
        "CONF_IP": "ip", "CONF_MAC": "mac", "CONF_NAME": "name", "CONF_TYPE": "type",
        "CONF_CFG": "cfg", "DEFAULT_PARALLEL_UPDATES": 0,
        "ICON_POWER_SOCKET": "mdi:power-socket", "SIHAS_PLATFORM_SCHEMA": None,
    }.items():
        setattr(constants, key, value)
    modules = {package.__name__: package, constants.__name__: constants}
    for name in [
        "homeassistant", "homeassistant.components", "homeassistant.components.number",
        "homeassistant.components.sensor",
        "homeassistant.components.switch", "homeassistant.config_entries", "homeassistant.const",
        "homeassistant.core", "homeassistant.exceptions", "homeassistant.helpers",
        "homeassistant.helpers.entity", "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.update_coordinator",
    ]:
        modules[name] = types.ModuleType(name)
        modules[name].__path__ = []
    modules["homeassistant.components.sensor"].SensorEntity = type("SensorEntity", (Entity,), {})
    modules["homeassistant.components.sensor"].SensorDeviceClass = types.SimpleNamespace(
        POWER="power", VOLTAGE="voltage", CURRENT="current", FREQUENCY="frequency", ENERGY="energy"
    )
    modules["homeassistant.components.sensor"].SensorStateClass = types.SimpleNamespace(
        MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing"
    )
    modules["homeassistant.components.switch"].SwitchEntity = type("SwitchEntity", (Entity,), {})
    modules["homeassistant.components.switch"].SwitchDeviceClass = types.SimpleNamespace(OUTLET="outlet")
    number_api = modules["homeassistant.components.number"]
    number_api.NumberEntity = type("NumberEntity", (Entity,), {})
    number_api.NumberDeviceClass = types.SimpleNamespace(POWER="power", TEMPERATURE="temperature", DURATION="duration")
    number_api.NumberMode = types.SimpleNamespace(BOX="box")
    modules["homeassistant.config_entries"].ConfigEntry = types.SimpleNamespace
    modules["homeassistant.const"].EntityCategory = types.SimpleNamespace(CONFIG="config")
    modules["homeassistant.const"].UnitOfPower = types.SimpleNamespace(WATT="W", KILO_WATT="kW")
    modules["homeassistant.const"].UnitOfElectricPotential = types.SimpleNamespace(VOLT="V")
    modules["homeassistant.const"].UnitOfElectricCurrent = types.SimpleNamespace(AMPERE="A")
    modules["homeassistant.const"].UnitOfEnergy = types.SimpleNamespace(KILO_WATT_HOUR="kWh")
    modules["homeassistant.const"].UnitOfFrequency = types.SimpleNamespace(HERTZ="Hz")
    modules["homeassistant.const"].UnitOfTemperature = types.SimpleNamespace(CELSIUS="°C")
    modules["homeassistant.const"].UnitOfTime = types.SimpleNamespace(MINUTES="min")
    modules["homeassistant.core"].HomeAssistant = Hass
    modules["homeassistant.exceptions"].HomeAssistantError = HomeAssistantError
    modules["homeassistant.exceptions"].ServiceValidationError = ServiceValidationError
    modules["homeassistant.helpers.entity"].DeviceInfo = dict
    modules["homeassistant.helpers.entity_platform"].AddEntitiesCallback = object
    modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = CoordinatorEntity
    modules["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = DataUpdateCoordinator
    modules["homeassistant.helpers.update_coordinator"].UpdateFailed = UpdateFailed
    for name, cls in [("bcm", "BcmCoordinator"), ("gcm", "GcmCoordinator")]:
        module = types.ModuleType(package.__name__ + "." + name)
        setattr(module, cls, type(cls, (), {}))
        modules[module.__name__] = module
    bcm = modules[package.__name__ + ".bcm"]
    bcm.BcmEntity = type("BcmEntity", (Entity,), {})
    bcm.ROOM, bcm.ONDOL, bcm.HOT_WATER = "room", "ondol", "hot_water"
    bcm.TARGET_REGISTERS = {}
    bcm.BATH_BIT = 1
    bcm.Register = object
    bcm.get_coordinator = lambda hass, config: hass.data["sihas"][config.entry_id]
    sihas_base = types.ModuleType(package.__name__ + ".sihas_base")
    sihas_base.SihasEntity = type("SihasEntity", (Entity,), {})
    modules[sihas_base.__name__] = sihas_base
    with patch.dict(sys.modules, modules):
        wcm = importlib.import_module(package.__name__ + ".wcm")
        sender = importlib.import_module(package.__name__ + ".sender")
        builder = importlib.import_module(package.__name__ + ".packet_builder").packet_builder
        spec = importlib.util.spec_from_file_location(
            package.__name__ + ".entrypoint", ROOT / "custom_components/sihas/__init__.py"
        )
        integration = importlib.util.module_from_spec(spec)
        integration.__package__ = package.__name__
        sys.modules[spec.name] = integration
        spec.loader.exec_module(integration)
        number_platform = importlib.import_module(package.__name__ + ".number")
        switch_platform = importlib.import_module(package.__name__ + ".switch")
    return wcm, sender, builder, integration, number_platform, switch_platform


wcm, sender, builder, integration, number_platform, switch_platform = load_modules()


class FakeDevice:
    def __init__(self):
        self.registers = [0] * 64
        self.calls = []
        self.addresses = []
        self.online = True
        self.timeout_count = 0
        self.apply_writes = True
        self.yield_on_write = False
        self.response_filter = lambda request, response: response

    def respond(self, request):
        function = request[7]
        self.calls.append(request)
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
            raise AssertionError("Unexpected UDP function")
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
                if address[1] != 502 or not address[0].startswith("192.0.2."):
                    raise AssertionError(f"Unexpected endpoint {address!r}")
                device.addresses.append(address)
                try:
                    self.response = device.respond(data)
                except socket.timeout as error:
                    self.response = error

            def recv(self, size):
                if isinstance(self.response, Exception):
                    raise self.response
                return self.response

        return FakeSocket()
