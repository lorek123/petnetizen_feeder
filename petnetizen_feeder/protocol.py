"""
Low-level BLE protocol implementation for Petnetizen feeders.

This module handles the Tuya BLE protocol encoding/decoding and device communication.
"""

import asyncio
import logging
import time
from datetime import datetime
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Optional, List, Tuple
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

# A device-initiated CMD_HEARTBEAT arriving this soon after we sent one is the
# feeder's reply to ours, not a ping that needs an echo.
HEARTBEAT_ACK_WINDOW_S = 5.0

# How long send_verification_code() waits for the SET_FAMILY_ID reply.
VERIFICATION_REPLY_TIMEOUT_S = 2.0

# decode_notification() keys that describe the frame rather than device state.
_FRAME_KEYS = frozenset(
    {
        "raw", "raw_bytes", "header", "command", "command_name", "footer",
        "length", "crc", "data_hex", "data_bytes", "error",
    }
)

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

# F14B V2 real_tag overrides (whole DP table is remapped on this model)
CMD_SYNC_TIME_F14B = "02"  # real_tag "02" = sync_time on F14B (= RESTORE_FACTORY on standard)

# CP01B (DU-CP01B interactive cat toy) real_tag command bytes from DpCp01b enum
CP01B_ROTATION_MODE  = "2F"
CP01B_PLAYBACK_FREQ  = "31"
CP01B_SOUND_EFFECT   = "32"
CP01B_OPERATION_MODE = "33"
CP01B_VOLUME         = "34"
CP01B_AUTO_COUNTDOWN = "35"
CP01B_PROMPT_SOUND   = "36"
CP01B_FUN_MODE       = "3E"

# Du-TC02 (laser cat teaser) real_tag command bytes from ble-device-type.json dpMappings
TC02_OPERATION_MODE  = "50"
TC02_ROTATION_MODE   = "51"
TC02_COLOR_RGB       = "52"  # 3-byte payload: R G B (each 0-255)
TC02_MOOD_LIGHT_MODE = "53"
TC02_LED_COLOR       = "54"
TC02_SOUND_EFFECT    = "55"
TC02_PLAYBACK_FREQ   = "56"
TC02_VOLUME          = "57"
TC02_AUTO_COUNTDOWN  = "59"  # 2-byte payload: total_minutes remaining_minutes


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
    elif "CP01B" in name_upper:
        return "cp01b"
    elif "F14B" in name_upper:
        return "f14b"
    elif "TC02" in name_upper:
        return "tc02"
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


def decode_fault_flags(fault_code: int) -> dict:
    """Split a FAULT (0A) code into the problems the app shows (FeederFaultBean bits).

    The app treats ``low_battery`` as meaningful only while running on batteries.
    """
    return {
        "stuck_grain": bool(fault_code & 0x01),
        "food_blocked": bool(fault_code & 0x02),
        "out_of_food": bool(fault_code & 0x04),
        "low_battery": bool(fault_code & 0x10),
    }


class FeederBLEProtocol:
    """Low-level BLE protocol handler for feeder devices"""

    def __init__(
        self,
        device_address: str,
        device_type: Optional[str] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.device_address = device_address
        self.device_type = device_type or detect_device_type()
        self._managed_connection = False
        # Wall-clock time for the feeder (it keeps local time, no time zone).
        self.clock: Callable[[], datetime] = clock or datetime.now

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

        # Link bookkeeping, used to log exactly when and how a link died.
        self.connected_at: Optional[float] = None  # time.monotonic()
        self.last_notification_at: Optional[float] = None
        self.last_error: Optional[str] = None
        # Set by FeederDevice once the session is fully up; drops before that
        # are part of connection setup and only logged at DEBUG.
        self.session_ready = False
        self.dropped_after: Optional[float] = None  # seconds after link-up
        self._expected_disconnect = False
        self._last_heartbeat_sent_at: Optional[float] = None
        self._verification_result: Optional[bool] = None
        self._verification_event = asyncio.Event()

        # Device state decoded from every notification (pushed or requested),
        # and when each command was last seen (time.monotonic()).
        self.last_state: dict = {}
        self.last_seen: dict = {}
        self._waiters: list = []  # (frozenset of command codes, Future)
        self._state_listeners: list = []  # callables taking the decoded state fields

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
        # Whatever we send (query or set) makes the cached value for that
        # command stale until the feeder answers.
        self.last_seen.pop(command, None)
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
                    # Same codes as the app's FeederStatus enum.
                    status_map = {0: "Idle", 1: "Feeding", 2: "Done", 3: "Failed"}
                    result["feeding_status"] = data_section[0]
                    result["feeding_status_text"] = status_map.get(
                        data_section[0], f"Unknown({data_section[0]})"
                    )
                    if len(data_section) >= 6 and data_section[5] <= 100:
                        result["battery_level"] = data_section[5]
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
                elif command_hex in ("0B", "0C") and len(data_section) >= 9:
                    # Feed records, 9 bytes each, newest first (app FeederResultBean):
                    # YY MM DD hh mm ss, portions dispensed, portions requested, fault bits.
                    # 0B = scheduled feeds, 0C = manual feeds.
                    feed_type = "Plan" if command_hex == "0B" else "Manual"
                    feed_records = []
                    for offset in range(0, len(data_section) - 8, 9):
                        record = data_section[offset : offset + 9]
                        timestamp = f"20{record[0]:02d}-{record[1]:02d}-{record[2]:02d} {record[3]:02d}:{record[4]:02d}:{record[5]:02d}"
                        dispensed, requested, fault = record[6], record[7], record[8]
                        feed_records.append(
                            {
                                "timestamp": timestamp,
                                "portions": dispensed,
                                "requested": requested,
                                "feed_type": feed_type,
                                "status": "Success"
                                if fault == 0 and dispensed >= requested
                                else "Failed",
                                "fault_code": fault,
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
                elif command_hex == CP01B_ROTATION_MODE and len(data_section) >= 1:
                    result["rotation_mode"] = data_section[0]
                elif command_hex == CP01B_PLAYBACK_FREQ and len(data_section) >= 1:
                    result["playback_frequency"] = data_section[0]
                elif command_hex == CP01B_SOUND_EFFECT and len(data_section) >= 1:
                    result["sound_effect"] = data_section[0]
                elif command_hex == CP01B_OPERATION_MODE and len(data_section) >= 1:
                    result["operation_mode"] = data_section[0]
                elif command_hex == CP01B_VOLUME and len(data_section) >= 1:
                    result["volume"] = data_section[0]
                elif command_hex == CP01B_AUTO_COUNTDOWN and len(data_section) >= 1:
                    result["auto_mode_countdown"] = data_section[0]
                elif command_hex == CP01B_PROMPT_SOUND and len(data_section) >= 1:
                    result["cp01b_prompt_sound"] = bool(data_section[0])
                elif command_hex == CP01B_FUN_MODE and len(data_section) >= 1:
                    result["fun_mode"] = data_section[0]
                # TC02 (Du-TC02 laser cat teaser) DPs — real_tags 50-59
                elif command_hex == TC02_OPERATION_MODE and len(data_section) >= 1:
                    result["tc02_operation_mode"] = data_section[0]
                elif command_hex == TC02_ROTATION_MODE and len(data_section) >= 1:
                    result["tc02_rotation_mode"] = data_section[0]
                elif command_hex == TC02_COLOR_RGB and len(data_section) >= 3:
                    result["tc02_color_r"] = data_section[0]
                    result["tc02_color_g"] = data_section[1]
                    result["tc02_color_b"] = data_section[2]
                elif command_hex == TC02_MOOD_LIGHT_MODE and len(data_section) >= 1:
                    result["tc02_mood_light_mode"] = data_section[0]
                elif command_hex == TC02_LED_COLOR and len(data_section) >= 1:
                    result["tc02_led_color"] = data_section[0]
                elif command_hex == TC02_SOUND_EFFECT and len(data_section) >= 1:
                    result["tc02_sound_effect"] = data_section[0]
                elif command_hex == TC02_PLAYBACK_FREQ and len(data_section) >= 1:
                    result["tc02_playback_frequency"] = data_section[0]
                elif command_hex == TC02_VOLUME and len(data_section) >= 1:
                    result["tc02_volume"] = data_section[0]
                elif command_hex == TC02_AUTO_COUNTDOWN and len(data_section) >= 2:
                    result["tc02_auto_countdown_total"] = data_section[0]
                    result["tc02_auto_countdown_remaining"] = data_section[1]
        except Exception as e:
            result["error"] = str(e)

        return result

    def clear_notifications(self) -> None:
        """Clear accumulated notification data."""
        self.received_data.clear()

    def notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        """Handle notifications from the device."""
        _LOGGER.debug(
            "[%s] Notification received: %s (%d bytes)",
            self.device_address,
            data.hex().upper(),
            len(data),
        )
        self.received_data.append(data)
        now = self.last_notification_at = time.monotonic()

        decoded = self.decode_notification(data)
        code = decoded.get("command")
        if code and "error" not in decoded:
            self.last_seen[code] = now
            fields = {k: v for k, v in decoded.items() if k not in _FRAME_KEYS}
            self.last_state.update(fields)
            # Track the newest feed (manual 0C or scheduled 0B) across all reports.
            if decoded.get("feed_records"):
                newest = max(decoded["feed_records"], key=lambda r: r["timestamp"])
                current = self.last_feed_result
                if current is None or newest["timestamp"] >= current["timestamp"]:
                    self.last_feed_result = newest
            for codes, fut in self._waiters:
                if code in codes and not fut.done():
                    fut.set_result(decoded)
            if fields:
                for listener in list(self._state_listeners):
                    try:
                        listener(fields)
                    except Exception:
                        _LOGGER.exception(
                            "[%s] State listener failed", self.device_address
                        )

        if len(data) >= 2:
            cmd = f"{data[1]:02X}"

            # Reply to SET_FAMILY_ID: data byte 01 = verification code accepted.
            # Only visible when notifications are enabled before the code is sent.
            if cmd == "06" and len(data) >= 4:
                self._verification_result = data[3] == 1
                self._verification_event.set()

            # The feeder firmware pushes CMD_SYNC_TIME ("05") as a device-initiated
            # request for the current time.  The Android app responds immediately via
            # BleSyncTimeCodec.handlerController → BleDeviceController.syncTime().
            # If we ignore it the feeder clock stays wrong (e.g. after a power outage).
            if cmd == "05":
                _LOGGER.debug(
                    "[%s] Device requested time sync — scheduling auto-response",
                    self.device_address,
                )
                try:
                    asyncio.ensure_future(self.send_sync_time())
                except RuntimeError:
                    pass  # No event loop running (unlikely in BLE context)

            # Some firmware builds initiate CMD_HEARTBEAT ("03") from the device side
            # and expect an echo ACK.  The Android product-test controller replies with
            # the same command / length 0.  We mirror that here to keep parity.
            # The feeder also answers each of *our* heartbeats with EB 03 00 00 AE;
            # echoing that would ping-pong forever, so ignore replies to ours.
            elif cmd == "03":
                sent_at = self._last_heartbeat_sent_at
                if (
                    sent_at is not None
                    and time.monotonic() - sent_at < HEARTBEAT_ACK_WINDOW_S
                ):
                    _LOGGER.debug(
                        "[%s] Heartbeat acknowledged", self.device_address
                    )
                    return
                _LOGGER.debug(
                    "[%s] Device heartbeat ping — scheduling echo ACK",
                    self.device_address,
                )
                try:
                    asyncio.ensure_future(self.send_heartbeat())
                except RuntimeError:
                    pass

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

    async def _do_start_notify(self) -> bool:
        """Write CCCD 0x0001 to enable notifications, with retry on transient failure."""
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            if not self.client.is_connected:
                self.last_error = "link dropped before notifications were enabled"
                _LOGGER.debug(
                    "[%s] Device disconnected before start_notify (attempt %d)",
                    self.device_address,
                    attempt,
                )
                return False
            try:
                if attempt > 1:
                    await asyncio.sleep(2.0)
                await self.client.start_notify(
                    self.notify_characteristic, self.notification_handler
                )
                _LOGGER.debug(
                    "[%s] Notifications enabled (attempt %d/%d)",
                    self.device_address,
                    attempt,
                    max_attempts,
                )
                return True
            except Exception as exc:
                self.last_error = f"enabling notifications failed: {exc}"
                _LOGGER.debug(
                    "[%s] start_notify failed (attempt %d/%d): %s",
                    self.device_address,
                    attempt,
                    max_attempts,
                    exc,
                )
                if attempt == max_attempts:
                    return False
        return False

    async def enable_notifications(self) -> bool:
        """Enable BLE notifications.  Call after connect(enable_notifications=False)."""
        return await self._do_start_notify()

    async def connect(
        self,
        timeout: float = 10.0,
        ble_client: Optional[BleakClient] = None,
        enable_notifications: bool = True,
    ) -> bool:
        """Connect to the device. If ble_client is provided (e.g. from bleak_retry_connector), use it.

        Failure details are logged at DEBUG and kept in ``last_error``; the
        caller decides whether a failure is worth a warning.
        """
        self.last_error = None
        self.session_ready = False
        self.connected_at = None
        self.dropped_after = None
        self._expected_disconnect = False
        self._verification_result = None
        self._verification_event.clear()
        if ble_client is not None:
            _LOGGER.debug("[%s] Using provided BleakClient", self.device_address)
            self.client = ble_client
        else:
            _LOGGER.debug(
                "[%s] Creating BleakClient (timeout=%ss)",
                self.device_address,
                timeout,
            )
            self.client = BleakClient(
                self.device_address,
                disconnected_callback=self.handle_disconnect,
                timeout=timeout,
            )
            try:
                await self.client.connect()
            except Exception as exc:
                self.last_error = f"BLE connect failed: {exc}"
                _LOGGER.debug(
                    "[%s] BLE connect failed: %s",
                    self.device_address,
                    exc,
                )
                return False
        self.connected_at = time.monotonic()
        self.last_notification_at = None

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
            self.last_error = f"service discovery failed: {exc}"
            _LOGGER.debug(
                "[%s] Service discovery failed: %s",
                self.device_address,
                exc,
            )
            return False

        try:
            service = services.get_service(self.service_uuid)
            if not service:
                self.last_error = f"service {self.service_uuid} not found on device"
                _LOGGER.debug(
                    "[%s] Service %s not found on device",
                    self.device_address,
                    self.service_uuid,
                )
                return False

            write_char = service.get_characteristic(self.write_uuid)
            notify_char = service.get_characteristic(self.notify_uuid)

            if not write_char or not notify_char:
                self.last_error = "required characteristics not found"
                _LOGGER.debug(
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

            if not enable_notifications:
                # Caller will enable notifications and then authenticate
                # (send the verification code) before using the connection.
                return True

            return await self._do_start_notify()

        except Exception as exc:
            self.last_error = f"post-connect setup failed: {exc}"
            _LOGGER.debug(
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
        self._expected_disconnect = True
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

    async def replace_client(
        self, ble_client: BleakClient, enable_notifications: bool = True
    ) -> bool:
        """Replace BleakClient with a freshly connected one (for integration-level reconnection)."""
        self._expected_disconnect = True
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
        return await self.connect(
            ble_client=ble_client, enable_notifications=enable_notifications
        )

    async def send_verification_code(
        self, code: str = DEFAULT_VERIFICATION_CODE
    ) -> Optional[bool]:
        """Send the verification code and wait for the feeder's verdict.

        Notifications must already be enabled, otherwise the SET_FAMILY_ID
        reply is lost.  Returns True if the code was accepted, False if it was
        rejected, or None if the write failed or no reply arrived in time.
        """
        command = self.encode_command(CMD_SET_FAMILY_ID, length=4, action_hex=code)
        self._verification_result = None
        self._verification_event.clear()
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            _LOGGER.debug("[%s] Verification code sent", self.device_address)
        except Exception as exc:
            self.last_error = f"sending verification code failed: {exc}"
            _LOGGER.debug(
                "[%s] Failed to send verification code: %s",
                self.device_address,
                exc,
            )
            return None
        try:
            await asyncio.wait_for(
                self._verification_event.wait(), VERIFICATION_REPLY_TIMEOUT_S
            )
        except TimeoutError:
            _LOGGER.debug(
                "[%s] No verification reply within %.1fs",
                self.device_address,
                VERIFICATION_REPLY_TIMEOUT_S,
            )
            return None
        _LOGGER.debug(
            "[%s] Verification code %s",
            self.device_address,
            "accepted" if self._verification_result else "rejected",
        )
        return self._verification_result

    async def request(
        self,
        send: Callable[[], Awaitable[Any]],
        codes: Iterable[str],
        timeout: float,
    ) -> Optional[dict]:
        """Run ``send()`` and return the first decoded reply whose command is in ``codes``.

        Returns as soon as the reply arrives, or None after ``timeout`` seconds.
        """
        fut = asyncio.get_running_loop().create_future()
        entry = (frozenset(codes), fut)
        self._waiters.append(entry)
        try:
            await send()
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            return None
        finally:
            self._waiters.remove(entry)

    def handle_disconnect(self, client: BleakClient) -> None:
        """Bleak ``disconnected_callback``: log when and how the link died.

        Pass this (or ``FeederDevice.disconnected_callback``) to whatever
        creates the BleakClient, e.g. ``establish_connection``.
        """
        if client is not self.client:
            return  # a client we already replaced
        now = time.monotonic()
        if self._expected_disconnect:
            _LOGGER.debug("[%s] BLE link closed", self.device_address)
            return
        if self.connected_at is not None:
            self.dropped_after = now - self.connected_at
        since_connect = (
            f"{self.dropped_after:.1f}s after connect"
            if self.dropped_after is not None
            else "while connecting"
        )
        since_rx = (
            f"{now - self.last_notification_at:.1f}s ago"
            if self.last_notification_at
            else "never"
        )
        _LOGGER.log(
            logging.INFO if self.session_ready else logging.DEBUG,
            "[%s] BLE link dropped by feeder at %s, %s (last notification: %s)",
            self.device_address,
            datetime.now().isoformat(timespec="milliseconds"),
            since_connect,
            since_rx,
        )

    async def query_feeder_plan(self):
        """Query the feed schedule (response via notification, command 11)."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_QUERY_FEEDER_PLAN, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to query feed plan: %s",
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
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to query name/version: %s",
                self.device_address,
                exc,
            )

    async def send_sync_time(self, dt: Optional[datetime] = None):
        """Send current time to the device.

        Standard (V1): EA 05 06 [YY MM DD HH mm ss] 00 AE
        F14B (V2):     EA 02 07 [YY MM DD HH mm ss DOW] 00 AE
        DOW = isoweekday() % 7 → Mon=1 … Sat=6, Sun=0
        """
        if not await self._ensure_connected():
            return
        if dt is None:
            dt = self.clock()
        if self.device_type == "f14b":
            dow = dt.isoweekday() % 7  # Mon=1..Sat=6, Sun=0
            action_bytes = bytes([
                dt.year % 100, dt.month, dt.day,
                dt.hour, dt.minute, dt.second, dow,
            ])
            command = self.encode_command(
                CMD_SYNC_TIME_F14B, length=7, action_hex=action_bytes.hex().upper()
            )
            _LOGGER.debug(
                "[%s] Sync time V2 to %s dow=%d", self.device_address, dt, dow
            )
        else:
            action_bytes = bytes([
                dt.year % 100, dt.month, dt.day,
                dt.hour, dt.minute, dt.second,
            ])
            command = self.encode_command(
                CMD_SYNC_TIME, length=6, action_hex=action_bytes.hex().upper()
            )
            _LOGGER.debug("[%s] Sync time V1 to %s", self.device_address, dt)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
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
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to query fault status: %s",
                self.device_address,
                exc,
            )


    async def query_command(self, cmd_hex: str):
        """Send a length-0 query for ``cmd_hex``; the reply arrives as a notification."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(cmd_hex, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to query %s: %s",
                self.device_address,
                cmd_hex,
                exc,
            )

    async def query_power_mode(self):
        """Query power source (response via notification, command 0E)"""
        if not await self._ensure_connected():
            return
        command = self.encode_command(CMD_POWER_SUPPLY_METHOD, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to query power source: %s",
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
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Failed to query feeding status: %s",
                self.device_address,
                exc,
            )

    async def query_cp01b_dp(self, cmd_hex: str) -> None:
        """Query a CP01B data-point by its command byte and wait for the notification."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(cmd_hex, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] CP01B query cmd=%s failed: %s", self.device_address, cmd_hex, exc
            )

    async def set_cp01b_dp(self, cmd_hex: str, value: int) -> None:
        """Write a 1-byte CP01B data-point."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(cmd_hex, length=1, action_hex=f"{value:02X}")
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(0.5)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] CP01B set cmd=%s val=%d failed: %s",
                self.device_address, cmd_hex, value, exc,
            )

    async def query_tc02_dp(self, cmd_hex: str) -> None:
        """Query a TC02 data-point by its command byte and wait for the notification."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(cmd_hex, length=0)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] TC02 query cmd=%s failed: %s", self.device_address, cmd_hex, exc
            )

    async def set_tc02_dp(self, cmd_hex: str, value: int) -> None:
        """Write a 1-byte TC02 data-point."""
        if not await self._ensure_connected():
            return
        command = self.encode_command(cmd_hex, length=1, action_hex=f"{value:02X}")
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(0.5)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] TC02 set cmd=%s val=%d failed: %s",
                self.device_address, cmd_hex, value, exc,
            )

    async def set_tc02_color_rgb(self, r: int, g: int, b: int) -> None:
        """Write RGB color to TC02 (3-byte payload)."""
        if not await self._ensure_connected():
            return
        action_hex = f"{r:02X}{g:02X}{b:02X}"
        command = self.encode_command(TC02_COLOR_RGB, length=3, action_hex=action_hex)
        try:
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            await asyncio.sleep(0.5)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] TC02 set_color_rgb(%d,%d,%d) failed: %s",
                self.device_address, r, g, b, exc,
            )

    async def send_heartbeat(self) -> bool:
        """Send a heartbeat packet (CMD_HEARTBEAT, no payload) to keep the BLE link alive.

        The feeder answers with EB 03 00 00 AE.  Measured on a Du-W11B
        (fw 110), an established link survives well over two minutes with no
        traffic at all, so this is a liveness probe more than a keep-alive.

        Returns True if the write succeeded, False otherwise (connection lost).
        """
        if not self.client or not self.client.is_connected:
            return False
        command = self.encode_command(CMD_HEARTBEAT, length=0)
        try:
            self._last_heartbeat_sent_at = time.monotonic()
            await self.client.write_gatt_char(self.write_uuid, command, response=False)
            _LOGGER.debug("[%s] Heartbeat sent", self.device_address)
            return True
        except Exception as exc:
            _LOGGER.debug("[%s] Heartbeat write failed: %s", self.device_address, exc)
            return False

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
