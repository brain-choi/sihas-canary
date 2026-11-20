"""Four-channel RCM relay support, verified on RCM firmware 03.07."""

from threading import Lock
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .errors import ModbusNotEnabledError, PacketSizeError
from .packet_builder import packet_builder
from .sender import send


class RcmRelayController:
    """Serialize reads/writes and validate acknowledgements before reporting success."""

    def __init__(self, ip: str) -> None:
        self.ip = ip
        self.lock = Lock()

    def read(self) -> list[int]:
        with self.lock:
            return self._read()

    def _read(self) -> list[int]:
        request = packet_builder.poll()
        response = send(request, self.ip, retry=3)
        # The firmware echoes the request's unit/checksum byte even though the
        # response length differs. Validate the transaction, size and function.
        if (
            len(response) != 137
            or response[:2] != request[:2]
            or response[2:4] != b"\x00\x00"
            or response[4:6] != b"\x00\x83"
            or response[7:9] != b"\x03\x80"
        ):
            raise ValueError("Unexpected RCM read response")
        return packet_builder.extract_registers(response)

    def set_channel(self, channel: int, enabled: bool) -> list[int]:
        if type(channel) is not int or channel not in range(4):
            raise ValueError("RCM channel must be between 0 and 3")
        if type(enabled) is not bool:
            raise ValueError("RCM relay state must be a boolean")
        with self.lock:
            request = packet_builder.command(channel, int(enabled))
            response = send(request, self.ip, retry=3)
            if response != request:
                raise ValueError("RCM write acknowledgement did not match request")
            registers = self._read()
            if registers[channel] != int(enabled):
                raise ValueError("RCM relay readback did not match requested state")
            return registers


class RcmRelay(SwitchEntity):
    """One physical output; register addresses 0..3, off=0, on=1."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:electric-switch"
    _attr_available = False

    def __init__(
        self, controller: RcmRelayController, mac: str, name: str, channel: int
    ) -> None:
        if type(channel) is not int or channel not in range(4):
            raise ValueError("RCM channel must be between 0 and 3")
        self.controller = controller
        self.channel = channel
        # Keep the identifiers used by the initial local patch. Changing them
        # would create new entities and break existing automation references.
        self._attr_unique_id = f"RCM-{mac}-relay-{channel + 1}"
        self._attr_name = f"릴레이 {channel + 1}"
        self._attr_available = False
        self._attr_is_on = None
        self._attr_device_info = {
            "identifiers": {(DOMAIN, mac)},
            "manufacturer": "SiHAS",
            "model": "RCM-300",
            "name": name,
        }

    def _apply(self, registers: list[int]) -> None:
        if registers[self.channel] not in (0, 1):
            raise ValueError("Unexpected nonbinary RCM relay state")
        self._attr_is_on = registers[self.channel] == 1
        self._attr_available = True

    def update(self) -> None:
        try:
            self._apply(self.controller.read())
        except (OSError, ValueError, IndexError, ModbusNotEnabledError, PacketSizeError):
            self._attr_available = False

    def _set(self, enabled: bool) -> None:
        try:
            self._apply(self.controller.set_channel(self.channel, enabled))
        except Exception as err:
            self._attr_available = False
            raise HomeAssistantError(f"RCM relay command failed: {err}") from err

    def turn_on(self, **kwargs: Any) -> None:
        self._set(True)

    def turn_off(self, **kwargs: Any) -> None:
        self._set(False)
