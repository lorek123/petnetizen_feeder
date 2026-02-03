"""
Low-level BLE protocol implementation for Petnetizen feeders.

This module handles the Tuya BLE protocol encoding/decoding and device communication.
"""

import asyncio
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice

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

    def hex_string_to_bytes(self, hex_string: str) -> bytes:
        """Convert hex string to bytes"""
        return bytes.fromhex(hex_string.replace(" ", "").replace("-", ""))

    def bytes_to_hex_string(self, data: bytes) -> str:
        """Convert bytes to hex string"""
        return data.hex().upper()

    def encode_command(self, command: str, length: Optional[int] = None,
                      action_hex: str = "") -> bytes:
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

        result = {
            "raw": self.bytes_to_hex_string(data),
            "raw_bytes": data
        }

        try:
            header = data[0]
            command_byte = data[1]
            command_hex = f"{command_byte:02X}"

            result["header"] = f"{header:02X}"
            result["command"] = command_hex

            command_map = {
                "00": "NAME_AND_VERSION", "01": "SET_NAME", "02": "RESTORE_FACTORY",
                "03": "HEARTBEAT", "04": "QUERY_MAC", "05": "SYNC_TIME",
                "06": "SET_FAMILY_ID", "07": "SET_FEEDER_PLAN", "08": "FEEDING",
                "09": "FEEDING_STATUS", "0A": "FAULT", "0B": "PLAN_FEED_RESULT",
                "0C": "MANUAL_FEED_RESULT", "0D": "CHILD_LOCK", "0E": "POWER_SUPPLY_METHOD",
                "0F": "CONTROL_LED", "10": "AUTO_LOCK", "11": "QUERY_FEEDER_PLAN",
                "12": "REMINDER_TONE", "13": "ATMOSPHERE_LIGHT",
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
                        name = data_section[:12].decode('utf-8', errors='ignore').strip('\x00').strip()
                        if name:
                            result["device_name"] = name
                        if len(data_section) > 12:
                            version = data_section[12:].decode('utf-8', errors='ignore').strip('\x00').strip()
                            if version:
                                result["device_version"] = version
                    except Exception:
                        pass
                elif command_hex == "0A" and len(data_section) >= 1:
                    result["fault_code"] = data_section[0]
                elif command_hex == "0E" and len(data_section) >= 1:
                    result["power_mode"] = "Battery" if data_section[0] == 0 else "DC Power"
                elif command_hex == "09" and len(data_section) >= 1:
                    status_map = {0: "Idle", 1: "Feeding", 2: "Error"}
                    result["feeding_status"] = data_section[0]
                    result["feeding_status_text"] = status_map.get(data_section[0], f"Unknown({data_section[0]})")
                elif command_hex == "0D" and len(data_section) >= 1:
                    result["child_lock"] = data_section[0]
                    result["child_lock_text"] = "LOCKED" if data_section[0] == 1 else "UNLOCKED"
                elif command_hex == "12" and len(data_section) >= 1:
                    result["prompt_sound"] = data_section[0]
                    result["prompt_sound_text"] = "ON" if data_section[0] == 1 else "OFF"
                elif command_hex == "08" and len(data_section) >= 1:
                    result["feed_response"] = data_section[0]
                    result["feed_response_text"] = "Triggered" if data_section[0] == 1 else f"Status({data_section[0]})"
                elif command_hex == "0C" and len(data_section) >= 9:
                    # Parse feed records (9 bytes each)
                    feed_records = []
                    num_records = len(data_section) // 9
                    for i in range(num_records):
                        offset = i * 9
                        if offset + 9 <= len(data_section):
                            record = data_section[offset:offset+9]
                            timestamp = f"20{record[0]:02d}-{record[1]:02d}-{record[2]:02d} {record[3]:02d}:{record[4]:02d}:{record[5]:02d}"
                            feed_records.append({
                                "timestamp": timestamp,
                                "portions": record[6],
                                "feed_type": "Manual" if record[7] == 1 else "Plan" if record[7] == 2 else f"Unknown({record[7]})",
                                "status": "Success" if record[8] == 0 else "Failed" if record[8] == 1 else f"Unknown({record[8]})"
                            })
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
                        week_val, hour, minute, portions, enabled = data_section[offset : offset + 5]
                        weekdays = [
                            d for bit, d in [
                                (1, "sun"), (2, "mon"), (4, "tue"), (8, "wed"),
                                (16, "thu"), (32, "fri"), (64, "sat"),
                            ]
                            if week_val & bit
                        ]
                        slots.append({
                            "weekdays": weekdays,
                            "time": f"{hour:02d}:{minute:02d}",
                            "portions": portions,
                            "enabled": bool(enabled),
                        })
                        offset += 5
                    if slots:
                        result["feed_plan_slots"] = slots
        except Exception as e:
            result["error"] = str(e)

        return result

    def notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        """Handle notifications from the device"""
        self.received_data.append(data)

    async def connect(self, timeout: float = 10.0, ble_client: Optional[BleakClient] = None) -> bool:
        """Connect to the device. If ble_client is provided (e.g. from bleak_retry_connector), use it."""
        if ble_client is not None:
            self.client = ble_client
        else:
            self.client = BleakClient(self.device_address, timeout=timeout)
            try:
                await self.client.connect()
                await asyncio.sleep(0.5)
            except Exception:
                return False

        try:
            try:
                services = self.client.services if hasattr(self.client, 'services') else await asyncio.wait_for(
                    self.client.get_services(), timeout=5.0
                )
            except (asyncio.TimeoutError, AttributeError):
                return False

            service = services.get_service(self.service_uuid)
            if not service:
                return False

            write_char = service.get_characteristic(self.write_uuid)
            notify_char = service.get_characteristic(self.notify_uuid)

            if not write_char or not notify_char:
                return False

            self.write_characteristic = write_char
            self.notify_characteristic = notify_char

            if hasattr(write_char, 'properties'):
                props = write_char.properties
                if isinstance(props, list):
                    self.supports_write_response = 'write' in props or 'write-with-response' in props
                    self.supports_write_no_response = 'write-without-response' in props

            await self.client.start_notify(notify_char, self.notification_handler)
            return True
        except Exception:
            return False

    async def disconnect(self):
        """Disconnect from the device"""
        if self.client and self.client.is_connected:
            await self.client.stop_notify(self.notify_uuid)
            await self.client.disconnect()

    async def send_verification_code(self, code: str = DEFAULT_VERIFICATION_CODE):
        """Send verification code to the device"""
        command = self.encode_command(CMD_SET_FAMILY_ID, length=4, action_hex=code)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(2)
        except Exception:
            pass

    async def query_name_version(self):
        """Query device name and firmware version (response via notification, command 00)."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_QUERY_NAME_VERSION, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1.5)
        except Exception:
            pass

    async def send_sync_time(self, dt: Optional[datetime] = None):
        """Send current time to the device. Format: YY MM DD HH MM SS (6 bytes, year as 2 digits)."""
        if not await self._ensure_connected():
            return
        if dt is None:
            dt = datetime.now()
        # 6 bytes: year%100, month, day, hour, minute, second
        action_bytes = bytes([
            dt.year % 100,
            dt.month,
            dt.day,
            dt.hour,
            dt.minute,
            dt.second,
        ])
        command = self.encode_command(CMD_SYNC_TIME, length=6, action_hex=action_bytes.hex().upper())
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1)
        except Exception:
            pass

    async def query_fault(self):
        """Query fault status"""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_FAULT, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1)
        except Exception:
            pass

    async def query_child_lock(self):
        """Query child lock status"""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_CHILD_LOCK, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1)
        except Exception:
            pass

    async def query_reminder_tone(self):
        """Query prompt sound / reminder tone status"""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_REMINDER_TONE, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1)
        except Exception:
            pass

    async def query_feeding_status(self):
        """Query feeding status"""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_FEEDING_STATUS, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(1)
        except Exception:
            pass

    async def _ensure_connected(self) -> bool:
        """Ensure connection is still active"""
        if not self.client or not self.client.is_connected:
            return await self.connect()

        try:
            if hasattr(self.client, 'services'):
                if hasattr(self, 'notify_characteristic') and self.notify_characteristic:
                    try:
                        await self.client.stop_notify(self.notify_uuid)
                        await asyncio.sleep(0.1)
                        await self.client.start_notify(self.notify_characteristic, self.notification_handler)
                    except Exception:
                        pass
            return True
        except Exception:
            await self.disconnect()
            return await self.connect()
