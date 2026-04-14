"""
Low-level BLE protocol implementation for Petnetizen feeders.

This module handles the Tuya BLE protocol encoding/decoding and device communication.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Tuple
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

_LOGGER = logging.getLogger(__name__)

# BLE UUIDs for different device types
FEEDER_SERVICE_UUID = "0000ae30-0000-1000-8000-00805f9b34fb"
FEEDER_WRITE_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
FEEDER_NOTIFY_UUID = "0000ae02-0000-1000-8000-00805f9b34fb"

JK_FEEDER_SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
JK_FEEDER_WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
JK_FEEDER_NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

ALI_FEEDER_SERVICE_UUID = "0000ffff-0000-1000-8000-00805f9b34fb"
ALI_FEEDER_WRITE_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
ALI_FEEDER_NOTIFY_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

DEFAULT_VERIFICATION_CODE = "00000000"

# Command IDs
CMD_QUERY_NAME_VERSION = "00"
CMD_SET_NAME = "01"
CMD_RESTORE_FACTORY = "02"
CMD_HEARTBEAT = "03"
CMD_QUERY_MAC = "04"
CMD_SYNC_TIME = "05"
CMD_SET_FAMILY_ID = "06"
CMD_SET_FEEDER_PLAN = "07"
CMD_FEEDING = "08"
CMD_FEEDING_STATUS = "09"
CMD_FAULT = "0A"
CMD_PLAN_FEED_RESULT = "0B"
CMD_MANUAL_FEED_RESULT = "0C"
CMD_CHILD_LOCK = "0D"
CMD_POWER_SUPPLY_METHOD = "0E"
CMD_CONTROL_LED = "0F"
CMD_AUTO_LOCK = "10"
CMD_QUERY_FEEDER_PLAN = "11"
CMD_REMINDER_TONE = "12"
CMD_ATMOSPHERE_LIGHT = "13"
CMD_DO_NOT_DISTURB_STATUS = "17"
CMD_DO_NOT_DISTURB = "18"
CMD_LONG_RING = "19"


# Service UUIDs for connection (per device type)
FEEDER_SERVICE_UUIDS = [
    FEEDER_SERVICE_UUID,
    JK_FEEDER_SERVICE_UUID,
    ALI_FEEDER_SERVICE_UUID,
]

# Name prefixes for discovery (Android app uses unfiltered scan + name prefix match via DeviceType.bleNames)
FEEDER_NAME_PREFIXES = ("Du", "JK", "ALI", "PET", "FEED")


def detect_device_type(device_name: Optional[str] = None) -> str:
    """Detect device type from name"""
    if not device_name:
        return "standard"

    name_upper = device_name.upper()
    if "JK" in name_upper:
        return "jk"
    elif "ALI" in name_upper or "ALIBABA" in name_upper:
        return "ali"
    return "standard"


def _is_feeder_by_name(name: str) -> bool:
    """True if the advertised name matches a known feeder name prefix (Android-style)."""
    if not name or not name.strip():
        return False
    name_upper = name.strip().upper()
    return any(name_upper.startswith(p.upper()) for p in FEEDER_NAME_PREFIXES)


async def discover_feeders(timeout: float = 10.0) -> List[Tuple[str, str, str]]:
    """
    Scan for Petnetizen feeder devices via BLE.

    Uses unfiltered BLE scan (no service-UUID filter), then recognizes feeders by
    advertised name prefix, matching the Android app behavior (bleNames / getDeviceTypeByName).

    Returns:
        List of (address, name, device_type) for each feeder found.
        address is normalized (e.g. "E6:C0:07:09:A3:D3"), name is the advertised name,
        device_type is "standard", "jk", or "ali".
    """
    devices = await BleakScanner.discover(timeout=timeout)
    result: List[Tuple[str, str, str]] = []
    seen: set = set()
    for d in devices:
        name = (d.name or "").strip()
        if not _is_feeder_by_name(name):
            continue
        addr = d.address if isinstance(d.address, str) else str(d.address)
        if len(addr) == 12 and ":" not in addr:
            addr = ":".join(addr[i : i + 2] for i in range(0, 12, 2))
        if addr in seen:
            continue
        seen.add(addr)
        dev_type = detect_device_type(name)
        result.append((addr, name, dev_type))
    return result


class FeederBLEProtocol:
    """Low-level BLE protocol handler for feeder devices"""

    def __init__(self, device_address: str, device_type: Optional[str] = None):
        self.device_address = device_address
        self.device_type = device_type or detect_device_type()
        self._managed_connection = False

        # Select UUIDs based on device type
        if self.device_type == "jk":
            self.service_uuid = JK_FEEDER_SERVICE_UUID
            self.write_uuid = JK_FEEDER_WRITE_UUID
            self.notify_uuid = JK_FEEDER_NOTIFY_UUID
        elif self.device_type == "ali":
            self.service_uuid = ALI_FEEDER_SERVICE_UUID
            self.write_uuid = ALI_FEEDER_WRITE_UUID
            self.notify_uuid = ALI_FEEDER_NOTIFY_UUID
        else:  # standard
            self.service_uuid = FEEDER_SERVICE_UUID
            self.write_uuid = FEEDER_WRITE_UUID
            self.notify_uuid = FEEDER_NOTIFY_UUID

        self.client: Optional[BleakClient] = None
        self.received_data = []
        self.write_characteristic = None
        self.notify_characteristic = None
        self.supports_write_response = False
        self.supports_write_no_response = True
        self.last_feed_result: Optional[dict] = None

    def hex_string_to_bytes(self, hex_string: str) -> bytes:
        """Convert hex string to bytes"""
        return bytes.fromhex(hex_string.replace(" ", "").replace("-", ""))

    def bytes_to_hex_string(self, data: bytes) -> str:
        """Convert bytes to hex string"""
        return data.hex().upper()

    def encode_command(
        self, command: str, length: Optional[int] = None, action_hex: str = ""
    ) -> bytes:
        """
        Encode a command according to Tuya BLE protocol.

        Format: EA + Command + Length + Data + CRC(00) + AE
        """
        if length is None:
            length = len(action_hex) // 2

        command_int = int(command, 16)
        command_bytes = bytearray()
        command_bytes.append(0xEA)  # Header
        command_bytes.append(command_int)  # Command byte
        command_bytes.append(length)  # Length byte

        if action_hex:
            data_bytes = self.hex_string_to_bytes(action_hex)
            command_bytes.extend(data_bytes)

        command_bytes.append(0x00)  # CRC placeholder
        command_bytes.append(0xAE)  # Footer

        return bytes(command_bytes)

    def decode_notification(self, data: bytearray) -> dict:
        """Decode notification data"""
        if len(data) < 4:
            return {"error": "Data too short"}

        result = {"raw": self.bytes_to_hex_string(data), "raw_bytes": data}

        try:
            header = data[0]
            command_byte = data[1]
            command_hex = f"{command_byte:02X}"

            result["header"] = f"{header:02X}"
            result["command"] = command_hex

            command_map = {
                "00": "NAME_AND_VERSION",
                "01": "SET_NAME",
                "02": "RESTORE_FACTORY",
                "03": "HEARTBEAT",
                "04": "QUERY_MAC",
                "05": "SYNC_TIME",
                "06": "SET_FAMILY_ID",
                "07": "SET_FEEDER_PLAN",
                "08": "FEEDING",
                "09": "FEEDING_STATUS",
                "0A": "FAULT",
                "0B": "PLAN_FEED_RESULT",
                "0C": "MANUAL_FEED_RESULT",
                "0D": "CHILD_LOCK",
                "0E": "POWER_SUPPLY_METHOD",
                "0F": "CONTROL_LED",
                "10": "AUTO_LOCK",
                "11": "QUERY_FEEDER_PLAN",
                "12": "REMINDER_TONE",
                "13": "ATMOSPHERE_LIGHT",
                "17": "DO_NOT_DISTURB_STATUS",
                "18": "DO_NOT_DISTURB",
                "19": "LONG_RING",
            }
            result["command_name"] = command_map.get(command_hex, "UNKNOWN")

            if len(data) >= 6:
                footer = data[-1]
                result["footer"] = f"{footer:02X}"
                length_byte = data[2]
                result["length"] = length_byte

                crc_byte = data[-2] if len(data) > 2 else None
                if crc_byte is not None:
                    result["crc"] = f"{crc_byte:02X}"

                data_section = data[3:-2] if len(data) > 5 else data[3:-1]
                result["data_hex"] = self.bytes_to_hex_string(data_section)
                result["data_bytes"] = bytes(data_section)

                # Parse specific commands
                if command_hex == "00" and len(data_section) >= 12:
                    try:
                        name = (
                            data_section[:12]
                            .decode("utf-8", errors="ignore")
                            .strip("\x00")
                            .strip()
                        )
                        if name:
                            result["device_name"] = name
                        if len(data_section) > 12:
                            version = (
                                data_section[12:]
                                .decode("utf-8", errors="ignore")
                                .strip("\x00")
                                .strip()
                            )
                            if version:
                                result["device_version"] = version
                    except Exception:
                        pass
                elif command_hex == "0A" and len(data_section) >= 1:
                    result["fault_code"] = data_section[0]
                elif command_hex == "0E" and len(data_section) >= 1:
                    result["power_mode"] = (
                        "Battery" if data_section[0] == 0 else "DC Power"
                    )
                elif command_hex == "09" and len(data_section) >= 1:
                    status_map = {0: "Idle", 1: "Feeding", 2: "Error"}
                    result["feeding_status"] = data_section[0]
                    result["feeding_status_text"] = status_map.get(
                        data_section[0], f"Unknown({data_section[0]})"
                    )
                elif command_hex == "0D" and len(data_section) >= 1:
                    result["child_lock"] = data_section[0]
                    result["child_lock_text"] = (
                        "LOCKED" if data_section[0] == 1 else "UNLOCKED"
                    )
                elif command_hex == "0F" and len(data_section) >= 1:
                    result["led"] = bool(data_section[0])
                elif command_hex == "10" and len(data_section) >= 1:
                    result["auto_lock"] = bool(data_section[0])
                elif command_hex == "12" and len(data_section) >= 1:
                    result["prompt_sound"] = data_section[0]
                    result["prompt_sound_text"] = (
                        "ON" if data_section[0] == 1 else "OFF"
                    )
                elif command_hex == "13" and len(data_section) >= 1:
                    result["atmosphere_light"] = bool(data_section[0])
                elif command_hex in ("17", "18") and len(data_section) >= 5:
                    result["do_not_disturb"] = bool(data_section[0])
                    result["dnd_start"] = f"{data_section[1]:02d}:{data_section[2]:02d}"
                    result["dnd_end"] = f"{data_section[3]:02d}:{data_section[4]:02d}"
                elif command_hex == "19" and len(data_section) >= 1:
                    result["long_ring"] = bool(data_section[0])
                elif command_hex == "08" and len(data_section) >= 1:
                    result["feed_response"] = data_section[0]
                    result["feed_response_text"] = (
                        "Triggered"
                        if data_section[0] == 1
                        else f"Status({data_section[0]})"
                    )
                elif command_hex == "0C" and len(data_section) >= 9:
                    # Parse feed records (9 bytes each)
                    feed_records = []
                    num_records = len(data_section) // 9
                    for i in range(num_records):
                        offset = i * 9
                        if offset + 9 <= len(data_section):
                            record = data_section[offset : offset + 9]
                            timestamp = f"20{record[0]:02d}-{record[1]:02d}-{record[2]:02d} {record[3]:02d}:{record[4]:02d}:{record[5]:02d}"
                            feed_records.append(
                                {
                                    "timestamp": timestamp,
                                    "portions": record[6],
                                    "feed_type": "Manual"
                                    if record[7] == 1
                                    else "Plan"
                                    if record[7] == 2
                                    else f"Unknown({record[7]})",
                                    "status": "Success"
                                    if record[8] == 0
                                    else "Failed"
                                    if record[8] == 1
                                    else f"Unknown({record[8]})",
                                }
                            )
                    if feed_records:
                        result["feed_records"] = feed_records
                elif command_hex == "06" and len(data_section) >= 1:
                    result["verification_response"] = data_section[0]
                    result["verification_success"] = data_section[0] == 1
                elif command_hex == "11":
                    # QUERY_FEEDER_PLAN response: 5 bytes per slot (week, hour, minute, portions, enabled)
                    # Some firmwares send [num_slots] + slots; others send slots only
                    slots = []
                    offset = 0
                    if len(data_section) >= 1 and 1 <= data_section[0] <= 15:
                        # First byte might be slot count
                        n = data_section[0]
                        if len(data_section) >= 1 + 5 * n:
                            offset = 1
                    while offset + 5 <= len(data_section):
                        week_val, hour, minute, portions, enabled = data_section[
                            offset : offset + 5
                        ]
                        weekdays = [
                            d
                            for bit, d in [
                                (1, "sun"),
                                (2, "mon"),
                                (4, "tue"),
                                (8, "wed"),
                                (16, "thu"),
                                (32, "fri"),
                                (64, "sat"),
                            ]
                            if week_val & bit
                        ]
                        slots.append(
                            {
                                "weekdays": weekdays,
                                "time": f"{hour:02d}:{minute:02d}",
                                "portions": portions,
                                "enabled": bool(enabled),
                            }
                        )
                        offset += 5
                    if slots:
                        result["feed_plan_slots"] = slots
        except Exception as e:
            result["error"] = str(e)

        return result

    def clear_notifications(self) -> None:
        """Clear accumulated notification data."""
        self.received_data.clear()

    def notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        """Handle notifications from the device"""
        _LOGGER.debug(
            "[%s] Notification received: %s (%d bytes)",
            self.device_address,
            data.hex().upper(),
            len(data),
        )
        self.received_data.append(data)
        # Track last completed manual feed result inline so it's always current
        if len(data) >= 2 and f"{data[1]:02X}" == "0C":
            decoded = self.decode_notification(data)
            if decoded.get("feed_records"):
                self.last_feed_result = decoded["feed_records"][-1]

    async def _request_mtu(self, desired_mtu: int = 512) -> int:
        """Request an MTU exchange, mirroring the Android app's requestMtu(512).

        Tries the backend-specific ``request_mtu`` when available (e.g. the
        ESPHome BLE proxy exposes it).  Falls back to reading the current
        ``mtu_size`` property, which is always safe.
        """
        try:
            backend = getattr(self.client, "_backend", None)
            if backend and hasattr(backend, "request_mtu"):
                mtu = await backend.request_mtu(desired_mtu)
                return (
                    mtu
                    if isinstance(mtu, int)
                    else getattr(self.client, "mtu_size", 23)
                )

            if hasattr(self.client, "request_mtu"):
                mtu = await self.client.request_mtu(desired_mtu)
                return (
                    mtu
                    if isinstance(mtu, int)
                    else getattr(self.client, "mtu_size", 23)
                )

            return getattr(self.client, "mtu_size", 23)
        except Exception as exc:
            _LOGGER.debug(
                "[%s] MTU exchange failed (non-fatal): %s",
                self.device_address,
                exc,
            )
            return getattr(self.client, "mtu_size", 23)

    async def connect(
        self, timeout: float = 10.0, ble_client: Optional[BleakClient] = None
    ) -> bool:
        """Connect to the device. If ble_client is provided (e.g. from bleak_retry_connector), use it."""
        if ble_client is not None:
            _LOGGER.debug("[%s] Using provided BleakClient", self.device_address)
            self.client = ble_client
        else:
            _LOGGER.debug(
                "[%s] Creating BleakClient (timeout=%ss)",
                self.device_address,
                timeout,
            )
            self.client = BleakClient(self.device_address, timeout=timeout)
            try:
                await self.client.connect()
            except Exception as exc:
                _LOGGER.warning(
                    "[%s] BLE connect failed: %s",
                    self.device_address,
                    exc,
                )
                return False

        # Access GATT services discovered during connect().  Modern bleak (0.20+)
        # and HaBleakClientWrapper (bleak-retry-connector) expose services via
        # the client.services property; get_services() no longer exists.
        try:
            services = self.client.services
            if services is None:
                raise RuntimeError(
                    "Service discovery not complete – no services available"
                )
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Service discovery failed: %s",
                self.device_address,
                exc,
            )
            return False

        try:
            service = services.get_service(self.service_uuid)
            if not service:
                _LOGGER.warning(
                    "[%s] Service %s not found on device",
                    self.device_address,
                    self.service_uuid,
                )
                return False

            write_char = service.get_characteristic(self.write_uuid)
            notify_char = service.get_characteristic(self.notify_uuid)

            if not write_char or not notify_char:
                _LOGGER.warning(
                    "[%s] Required characteristics not found (write=%s, notify=%s)",
                    self.device_address,
                    write_char is not None,
                    notify_char is not None,
                )
                return False

            self.write_characteristic = write_char
            self.notify_characteristic = notify_char

            if hasattr(write_char, "properties"):
                props = write_char.properties
                if isinstance(props, list):
                    self.supports_write_response = (
                        "write" in props or "write-with-response" in props
                    )
                    self.supports_write_no_response = "write-without-response" in props

            # The official Android app (Nordic BLE Manager) requests MTU 512
            # before enabling notifications.  The MTU exchange is a GATT
            # round-trip that confirms the link is ready; without it the
            # subsequent CCCD write for start_notify can hit the feeder
            # before its BLE stack is prepared, causing Error 19 —
            # especially through an ESP32 BLE proxy.
            mtu = await self._request_mtu(512)
            _LOGGER.debug("[%s] MTU: %d", self.device_address, mtu)

            # Give the BLE link time to stabilise after the MTU exchange before
            # writing to the CCCD.  Through an ESP32 BLE proxy the MTU request
            # may complete immediately without a real GATT round-trip, leaving
            # the link unready — which causes the feeder to terminate the
            # connection with HCI error 19 on the first start_notify attempt.
            await asyncio.sleep(1.0)

            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                if not self.client.is_connected:
                    _LOGGER.warning(
                        "[%s] Device disconnected before start_notify (attempt %d)",
                        self.device_address,
                        attempt,
                    )
                    return False
                try:
                    if attempt > 1:
                        await asyncio.sleep(2.0)
                    await self.client.start_notify(
                        notify_char, self.notification_handler
                    )
                    _LOGGER.debug(
                        "[%s] Connected and notifications started (attempt %d/%d)",
                        self.device_address,
                        attempt,
                        max_attempts,
                    )
                    return True
                except Exception as exc:
                    _LOGGER.warning(
                        "[%s] start_notify failed (attempt %d/%d): %s",
                        self.device_address,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    if attempt == max_attempts:
                        return False
            return False
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Post-connect setup failed: %s",
                self.device_address,
                exc,
            )
            return False

    async def set_led(self, enabled: bool):
        """Set LED on/off."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(
            CMD_CONTROL_LED, length=1, action_hex="01" if enabled else "00"
        )
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(0.5)
        except Exception as exc:
            _LOGGER.warning("[%s] Failed to set LED: %s", self.device_address, exc)

    async def set_auto_lock(self, enabled: bool):
        """Set auto lock on/off."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(
            CMD_AUTO_LOCK, length=1, action_hex="01" if enabled else "00"
        )
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(0.5)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to set auto lock: %s", self.device_address, exc
            )

    async def set_atmosphere_light(self, enabled: bool):
        """Set atmosphere light on/off."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(
            CMD_ATMOSPHERE_LIGHT, length=1, action_hex="01" if enabled else "00"
        )
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(0.5)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to set atmosphere light: %s", self.device_address, exc
            )

    async def factory_reset(self):
        """Send factory reset command."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_RESTORE_FACTORY, length=1, action_hex="01")
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1.0)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to send factory reset: %s", self.device_address, exc
            )

    async def query_do_not_disturb(self):
        """Query do-not-disturb status."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_DO_NOT_DISTURB_STATUS, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1.0)
        except Exception as exc:
            _LOGGER.warning("[%s] Failed to query DND: %s", self.device_address, exc)

    async def set_do_not_disturb(
        self, enabled: bool, start_time: str = "22:00", end_time: str = "08:00"
    ):
        """Set do-not-disturb. start_time/end_time in 'HH:MM' format."""
        if not await self._ensure_connected():
            return
        try:
            sh, sm = (int(x) for x in start_time.split(":"))
            eh, em = (int(x) for x in end_time.split(":"))
        except (ValueError, AttributeError):
            _LOGGER.warning("[%s] Invalid DND time format", self.device_address)
            return
        action = f"{'01' if enabled else '00'}{sh:02X}{sm:02X}{eh:02X}{em:02X}"
        command = self.encode_command(CMD_DO_NOT_DISTURB, length=5, action_hex=action)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(0.5)
        except Exception as exc:
            _LOGGER.warning("[%s] Failed to set DND: %s", self.device_address, exc)

    async def set_long_ring(self, enabled: bool):
        """Set long ring / extended sound on/off."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(
            CMD_LONG_RING, length=1, action_hex="01" if enabled else "00"
        )
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(0.5)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to set long ring: %s", self.device_address, exc
            )

    async def disconnect(self):
        """Disconnect from the device"""
        if self.client and self.client.is_connected:
            _LOGGER.debug("[%s] Disconnecting", self.device_address)
            try:
                await self.client.stop_notify(self.notify_uuid)
            except Exception as exc:
                _LOGGER.debug(
                    "[%s] Error stopping notifications during disconnect: %s",
                    self.device_address,
                    exc,
                )
            await self.client.disconnect()
            _LOGGER.debug("[%s] Disconnected", self.device_address)
        else:
            _LOGGER.debug(
                "[%s] Disconnect called but not connected", self.device_address
            )

    async def replace_client(self, ble_client: BleakClient) -> bool:
        """Replace BleakClient with a freshly connected one (for integration-level reconnection)."""
        if self.client:
            try:
                if self.client.is_connected:
                    try:
                        await self.client.stop_notify(self.notify_uuid)
                    except Exception:
                        pass
                    await self.client.disconnect()
            except Exception:
                pass
        return await self.connect(ble_client=ble_client)

    async def send_verification_code(self, code: str = DEFAULT_VERIFICATION_CODE):
        """Send verification code to the device"""
        command = self.encode_command(CMD_SET_FAMILY_ID, length=4, action_hex=code)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            _LOGGER.debug("[%s] Verification code sent", self.device_address)
            await asyncio.sleep(2)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to send verification code: %s",
                self.device_address,
                exc,
            )

    async def query_name_version(self):
        """Query device name and firmware version (response via notification, command 00)."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_QUERY_NAME_VERSION, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1.5)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to query name/version: %s",
                self.device_address,
                exc,
            )

    async def send_sync_time(self, dt: Optional[datetime] = None):
        """Send current time to the device. Format: YY MM DD HH MM SS (6 bytes, year as 2 digits)."""
        if not await self._ensure_connected():
            return
        if dt is None:
            dt = datetime.now()
        # 6 bytes: year%100, month, day, hour, minute, second
        action_bytes = bytes(
            [
                dt.year % 100,
                dt.month,
                dt.day,
                dt.hour,
                dt.minute,
                dt.second,
            ]
        )
        command = self.encode_command(
            CMD_SYNC_TIME, length=6, action_hex=action_bytes.hex().upper()
        )
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            _LOGGER.debug("[%s] Time synced to %s", self.device_address, dt)
            await asyncio.sleep(1)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to sync time: %s",
                self.device_address,
                exc,
            )

    async def query_fault(self):
        """Query fault status"""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_FAULT, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to query fault status: %s",
                self.device_address,
                exc,
            )

    async def query_child_lock(self):
        """Query child lock status"""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_CHILD_LOCK, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to query child lock: %s",
                self.device_address,
                exc,
            )

    async def query_reminder_tone(self):
        """Query prompt sound / reminder tone status"""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_REMINDER_TONE, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to query reminder tone: %s",
                self.device_address,
                exc,
            )

    async def query_feeding_status(self):
        """Query feeding status"""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_FEEDING_STATUS, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to query feeding status: %s",
                self.device_address,
                exc,
            )

    async def _ensure_connected(self) -> bool:
        """Ensure connection is still active.

        When ``_managed_connection`` is True (integration supplied a
        connection factory), we never create a raw ``BleakClient`` here —
        just report the drop and let the higher-level code reconnect via
        the factory (which includes verification, adapter selection, etc.).
        """
        if self.client and self.client.is_connected:
            return True

        if self._managed_connection:
            _LOGGER.debug(
                "[%s] Connection lost (externally managed — not auto-reconnecting)",
                self.device_address,
            )
            return False

        _LOGGER.info(
            "[%s] Connection lost, attempting reconnect",
            self.device_address,
        )
        result = await self.connect()
        if result:
            _LOGGER.info("[%s] Reconnected successfully", self.device_address)
        else:
            _LOGGER.warning("[%s] Reconnection failed", self.device_address)
        return result
