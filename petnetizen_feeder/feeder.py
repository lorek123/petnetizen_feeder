"""
Petnetizen Feeder BLE Device Controller

Main library class for controlling feeder devices.
"""

import asyncio
from datetime import datetime
from typing import List, Dict, Optional
from .protocol import (
    FeederBLEProtocol,
    discover_feeders,
    CMD_FEEDING,
    CMD_SET_FEEDER_PLAN,
    CMD_CHILD_LOCK,
    CMD_REMINDER_TONE,
    CMD_QUERY_FEEDER_PLAN,
    CMD_QUERY_NAME_VERSION,
    DEFAULT_VERIFICATION_CODE,
)

# Weekday bitmask values (from FeedInfo.Companion.getWeekValue)
WEEKDAY_BITMASK = {
    "sun": 1,
    "mon": 2,
    "tue": 4,
    "wed": 8,
    "thu": 16,
    "fri": 32,
    "sat": 64,
}


class Weekday:
    """Weekday constants for schedule"""
    SUNDAY = "sun"
    MONDAY = "mon"
    TUESDAY = "tue"
    WEDNESDAY = "wed"
    THURSDAY = "thu"
    FRIDAY = "fri"
    SATURDAY = "sat"

    ALL_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
    WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
    WEEKEND = ["sat", "sun"]


class FeedSchedule:
    """Represents a single feed schedule entry"""

    def __init__(self, weekdays: List[str], time: str, portions: int, enabled: bool = True):
        """
        Args:
            weekdays: List of weekday names (e.g., ["mon", "wed", "fri"])
            time: Time in HH:MM format (e.g., "08:00")
            portions: Number of portions to feed (1-15)
            enabled: Whether this schedule is enabled
        """
        self.weekdays = weekdays
        self.time = time
        self.portions = portions
        self.enabled = enabled

    def to_bytes(self) -> bytes:
        """Convert schedule to protocol format"""
        # Calculate week bitmask
        week_value = 0
        for day in self.weekdays:
            day_lower = day.lower()
            if day_lower in WEEKDAY_BITMASK:
                week_value |= WEEKDAY_BITMASK[day_lower]

        # Parse time
        hour, minute = map(int, self.time.split(":"))

        # Format: week(1 hex) + hour(1 hex) + minute(1 hex) + count(1 hex) + enabled(1 hex)
        return bytes([
            week_value,
            hour,
            minute,
            self.portions,
            1 if self.enabled else 0
        ])


class FeederDevice:
    """
    Main class for controlling Petnetizen feeder devices via BLE.

    Example:
        async def main():
            feeder = FeederDevice("E6:C0:07:09:A3:D3")
            await feeder.connect()
            await feeder.feed(portions=2)
            await feeder.disconnect()

        asyncio.run(main())
    """

    def __init__(
        self,
        address: str,
        verification_code: str = DEFAULT_VERIFICATION_CODE,
        device_type: Optional[str] = None,
    ):
        """
        Initialize feeder device controller.

        Args:
            address: BLE device address (e.g., "E6:C0:07:09:A3:D3")
            verification_code: Verification code (default: "00000000")
            device_type: Optional "standard", "jk", or "ali" (auto-detected from name if not set)
        """
        self.address = address
        self.verification_code = verification_code
        self._protocol = FeederBLEProtocol(address, device_type=device_type)
        self._connected = False

    async def connect(self) -> bool:
        """
        Connect to the feeder device.

        Returns:
            True if connection successful, False otherwise
        """
        if await self._protocol.connect():
            # Send verification code
            await self._protocol.send_verification_code(self.verification_code)
            await asyncio.sleep(0.5)
            self._connected = True
            return True
        return False

    async def disconnect(self):
        """Disconnect from the device"""
        await self._protocol.disconnect()
        self._connected = False

    async def feed(self, portions: int = 1) -> bool:
        """
        Trigger manual feed with specified number of portions.

        Args:
            portions: Number of portions to feed (1-15, typically 1-3)

        Returns:
            True if feed command was acknowledged, False otherwise

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected:
            raise RuntimeError("Not connected to device. Call connect() first.")

        # Ensure connection is still active
        if not await self._protocol._ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")

        # Check device state
        await self._protocol.query_fault()
        await asyncio.sleep(0.5)
        await self._protocol.query_child_lock()
        await asyncio.sleep(0.5)
        await self._protocol.query_feeding_status()
        await asyncio.sleep(0.5)

        # Send feed command
        # Format: EA + 08 + 01 + portions(1 byte) + 00 + AE
        command = self._protocol.encode_command(CMD_FEEDING, length=1, action_hex=f"{portions:02X}")

        notification_count_before = len(self._protocol.received_data)

        try:
            await self._protocol.client.write_gatt_char(
                self._protocol.write_uuid, command, response=False
            )

            # Wait for response
            feed_triggered = False
            for _ in range(20):  # Wait up to 10 seconds
                await asyncio.sleep(0.5)
                if len(self._protocol.received_data) > notification_count_before:
                    new_notifications = self._protocol.received_data[notification_count_before:]
                    for data in new_notifications:
                        decoded = self._protocol.decode_notification(data)
                        cmd = decoded.get("command", "")

                        if cmd == "08":  # FEEDING response
                            feed_triggered = True
                        elif cmd == "0C":  # MANUAL_FEED_RESULT
                            return True  # Feed completed

            return feed_triggered
        except Exception as e:
            raise RuntimeError(f"Failed to send feed command: {e}") from e

    async def set_schedule(self, schedules: List[FeedSchedule]) -> bool:
        """
        Set feed schedule.

        Args:
            schedules: List of FeedSchedule objects

        Returns:
            True if command was sent successfully

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected:
            raise RuntimeError("Not connected to device. Call connect() first.")

        if not await self._protocol._ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")

        # Build schedule data: each entry is 5 bytes
        schedule_data = bytearray()
        for schedule in schedules:
            schedule_data.extend(schedule.to_bytes())

        # Send command
        # Format: EA + 07 + length + schedule_data + 00 + AE
        command = self._protocol.encode_command(
            CMD_SET_FEEDER_PLAN,
            length=len(schedule_data),
            action_hex=schedule_data.hex().upper()
        )

        try:
            await self._protocol.client.write_gatt_char(
                self._protocol.write_uuid, command, response=False
            )
            await asyncio.sleep(1)
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to set schedule: {e}") from e

    async def set_child_lock(self, locked: bool) -> bool:
        """
        Set child lock state.

        Args:
            locked: True to lock, False to unlock

        Returns:
            True if command was sent successfully

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected:
            raise RuntimeError("Not connected to device. Call connect() first.")

        if not await self._protocol._ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")

        # Format: EA + 0D + 01 + value(00/01) + 00 + AE
        value = "01" if locked else "00"
        command = self._protocol.encode_command(CMD_CHILD_LOCK, length=1, action_hex=value)

        try:
            await self._protocol.client.write_gatt_char(
                self._protocol.write_uuid, command, response=False
            )
            await asyncio.sleep(1)
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to set child lock: {e}") from e

    async def set_sound(self, enabled: bool) -> bool:
        """
        Set reminder tone/sound state.

        Args:
            enabled: True to enable sound, False to disable

        Returns:
            True if command was sent successfully

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected:
            raise RuntimeError("Not connected to device. Call connect() first.")

        if not await self._protocol._ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")

        # Format: EA + 12 + 01 + value(00/01) + 00 + AE
        value = "01" if enabled else "00"
        command = self._protocol.encode_command(CMD_REMINDER_TONE, length=1, action_hex=value)

        try:
            await self._protocol.client.write_gatt_char(
                self._protocol.write_uuid, command, response=False
            )
            await asyncio.sleep(1)
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to set sound: {e}") from e

    async def query_schedule(self) -> List[Dict]:
        """
        Query current feed schedule.

        Returns:
            List of schedule dictionaries

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected:
            raise RuntimeError("Not connected to device. Call connect() first.")

        if not await self._protocol._ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")

        # Send query command
        command = self._protocol.encode_command(CMD_QUERY_FEEDER_PLAN, length=0)

        notification_count_before = len(self._protocol.received_data)

        try:
            await self._protocol.client.write_gatt_char(
                self._protocol.write_uuid, command, response=False
            )
            await asyncio.sleep(2)

            # Parse response
            if len(self._protocol.received_data) > notification_count_before:
                new_notifications = self._protocol.received_data[notification_count_before:]
                for data in new_notifications:
                    decoded = self._protocol.decode_notification(data)
                    if decoded.get("command") == "11":  # QUERY_FEEDER_PLAN
                        # TODO: Parse schedule data
                        return []

            return []
        except Exception as e:
            raise RuntimeError(f"Failed to query schedule: {e}") from e

    async def get_device_info(self) -> Dict:
        """
        Query device name and firmware version.

        Returns:
            Dict with "device_name", "device_version" (or empty strings if not available).
        """
        if not self._connected:
            raise RuntimeError("Not connected to device. Call connect() first.")
        if not await self._protocol._ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")
        before = len(self._protocol.received_data)
        await self._protocol.query_name_version()
        await asyncio.sleep(2)
        result: Dict = {"device_name": "", "device_version": ""}
        for data in self._protocol.received_data[before:]:
            decoded = self._protocol.decode_notification(data)
            if decoded.get("command") == "00":
                result["device_name"] = decoded.get("device_name", "") or ""
                result["device_version"] = decoded.get("device_version", "") or ""
                break
        return result

    async def sync_time(self, dt: Optional[datetime] = None) -> None:
        """
        Sync device clock to the given time (default: now).

        Args:
            dt: Time to set on the device; defaults to datetime.now().
        """
        if not self._connected:
            raise RuntimeError("Not connected to device. Call connect() first.")
        await self._protocol.send_sync_time(dt)

    @property
    def is_connected(self) -> bool:
        """Check if device is connected"""
        return self._connected and (
            self._protocol.client is not None and self._protocol.client.is_connected
        )
