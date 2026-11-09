"""
Petnetizen Feeder BLE Device Controller

Main library class for controlling feeder devices.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, List, Dict, Optional
from .protocol import (
    FeederBLEProtocol,
    discover_feeders,  # noqa: F401 (re-exported via __init__)
    CMD_FEEDING,
    CMD_SET_FEEDER_PLAN,
    CMD_CHILD_LOCK,
    CMD_REMINDER_TONE,
    DEFAULT_VERIFICATION_CODE,
    CP01B_ROTATION_MODE,
    CP01B_PLAYBACK_FREQ,
    CP01B_SOUND_EFFECT,
    CP01B_OPERATION_MODE,
    CP01B_VOLUME,
    CP01B_AUTO_COUNTDOWN,
    CP01B_PROMPT_SOUND,
    CP01B_FUN_MODE,
    TC02_OPERATION_MODE,
    TC02_ROTATION_MODE,
    TC02_COLOR_RGB,
    TC02_MOOD_LIGHT_MODE,
    TC02_LED_COLOR,
    TC02_SOUND_EFFECT,
    TC02_PLAYBACK_FREQ,
    TC02_VOLUME,
    TC02_AUTO_COUNTDOWN,
)

_LOGGER = logging.getLogger(__name__)

ConnectionFactory = Callable[[], Awaitable[Any]]

# Session setup waits this long (counted from link-up) and checks the link is
# still there before reporting success, so a link that dies right away is
# retried at once.  One test session (Du-W11B, fw 110, BlueZ) saw about half of
# fresh links dropped 2.2-3.3 s after link-up; later sessions saw none, so this
# is kept short.  Later drops are caught by ensure_connected() on the next call.
LINK_SETTLE_S = 1.0

# State the feeder pushed or answered this recently, in the current session, is
# served without a query.  Right after verification the feeder pushes feeding
# status, fault, child lock, prompt sound, power source, LED and auto-lock.
STATE_MAX_AGE_S = 30.0

# A query the feeder left unanswered (e.g. do-not-disturb on firmware without
# it) is not re-sent for this long within the same session; callers get None.
UNANSWERED_RETRY_S = 600.0

# Attempts per connect()/ensure_connected() call.
CONNECT_ATTEMPTS = 3

# Pause after dropping a stale client so an ESP32 proxy can free the slot.
STALE_RELEASE_DELAY_S = 2.0

_VERIFICATION_REJECTED = "verification code rejected by feeder"

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

    def __init__(
        self, weekdays: List[str], time: str, portions: int, enabled: bool = True
    ):
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
        return bytes(
            [week_value, hour, minute, self.portions, 1 if self.enabled else 0]
        )


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
        connection_factory: Optional[ConnectionFactory] = None,
    ):
        """
        Initialize feeder device controller.

        Args:
            address: BLE device address (e.g., "E6:C0:07:09:A3:D3")
            verification_code: Verification code (default: "00000000")
            device_type: Optional "standard", "jk", or "ali" (auto-detected from name if not set)
            connection_factory: Optional async callable returning a connected BleakClient.
                When provided (e.g. from Home Assistant via ``establish_connection``),
                reconnection uses this factory instead of creating a raw ``BleakClient``.
                Standalone scripts can omit this to use the built-in connection logic.
        """
        self.address = address
        self.verification_code = verification_code
        self._connection_factory = connection_factory
        self._protocol = FeederBLEProtocol(address, device_type=device_type)
        if connection_factory is not None:
            self._protocol._managed_connection = True
        self._connected = False
        self._reconnect_lock = asyncio.Lock()
        self._heartbeat_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        # Liveness probe; the feeder does not need it to keep an idle link.
        self._heartbeat_interval: int = 20
        # Query codes -> when the feeder last left them unanswered.
        self._unanswered: Dict[tuple, float] = {}

    async def connect(self, ble_client: Optional[Any] = None) -> bool:
        """
        Connect to the feeder device.

        Args:
            ble_client: Optional already-connected BleakClient (e.g. from bleak_retry_connector).
                        When provided, the library uses it instead of creating a new connection
                        and makes a single attempt.  Otherwise the connection factory (or a
                        direct BleakClient) is used, with up to ``CONNECT_ATTEMPTS`` attempts.

        Returns:
            True if connection successful, False otherwise
        """
        _LOGGER.debug("Connecting to feeder %s", self.address)
        if ble_client is not None:
            ok = await self._open_session(ble_client)
        else:
            ok = await self._open_session_with_retries()
        if not ok:
            _LOGGER.warning(
                "Failed to connect to feeder %s: %s",
                self.address,
                self._protocol.last_error or "unknown error",
            )
            return False
        self._connected = True
        self._start_heartbeat()
        _LOGGER.info("Connected to feeder %s", self.address)
        return True

    async def reconnect(self, ble_client: Optional[Any] = None) -> bool:
        """
        Reconnect with an optional new BleakClient.

        Use this when the integration obtains a fresh BleakClient (e.g. via
        bleak_retry_connector) and needs to hand it to the library.
        """
        ok = await self._reconnect_once(ble_client)
        if not ok:
            _LOGGER.warning(
                "Failed to reconnect to feeder %s: %s",
                self.address,
                self._protocol.last_error or "unknown error",
            )
        return ok

    async def _reconnect_once(self, ble_client: Optional[Any] = None) -> bool:
        """One reconnect attempt; failures are logged at DEBUG only."""
        _LOGGER.debug("Reconnecting to feeder %s", self.address)
        # Set _connected=False while the reconnect is in progress so
        # is_connected returns False during the attempt.  Crucially we
        # restore it to True on failure: ensure_connected() guards on
        # `if not self._connected: return False`, so leaving it False
        # after a failed reconnect would permanently block all future
        # reconnection attempts until HA restarts.
        self._connected = False
        ok = await self._open_session(ble_client, replace=True)
        self._connected = True  # on failure: allow ensure_connected() to retry
        if not ok:
            _LOGGER.debug(
                "Reconnect attempt to feeder %s failed: %s",
                self.address,
                self._protocol.last_error,
            )
            return False
        self._start_heartbeat()
        _LOGGER.info("Reconnected to feeder %s", self.address)
        return True

    async def _open_session_with_retries(self) -> bool:
        """Obtain a client (factory or direct) and open a session, retrying."""
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            ble_client = None
            if self._connection_factory is not None:
                try:
                    ble_client = await self._connection_factory()
                except Exception as exc:
                    self._protocol.last_error = f"BLE connect failed: {exc}"
            if self._connection_factory is None or ble_client is not None:
                if await self._open_session(ble_client, replace=True):
                    return True
                if self._protocol.last_error == _VERIFICATION_REJECTED:
                    return False  # retrying the same code cannot help
            _LOGGER.debug(
                "Connect attempt %d/%d to feeder %s failed: %s",
                attempt,
                CONNECT_ATTEMPTS,
                self.address,
                self._protocol.last_error,
            )
            if attempt < CONNECT_ATTEMPTS:
                await self._release_stale_connection()
        return False

    async def _open_session(
        self, ble_client: Optional[Any], replace: bool = False
    ) -> bool:
        """Bring up GATT, notifications and authentication on one link.

        Order matches the Android app (Nordic BleManager): enable
        notifications first, then send the verification code, so the
        feeder's SET_FAMILY_ID verdict (EB 06 01 01) can be seen.
        """
        protocol = self._protocol
        if replace and ble_client is not None:
            ok_gatt = await protocol.replace_client(
                ble_client, enable_notifications=False
            )
        else:
            ok_gatt = await protocol.connect(
                ble_client=ble_client, enable_notifications=False
            )
        if not ok_gatt:
            return False
        if not await protocol.enable_notifications():
            return False
        verified = await protocol.send_verification_code(self.verification_code)
        if verified is False:
            protocol.last_error = _VERIFICATION_REJECTED
            await self._close_quietly()
            return False
        if verified is None:
            _LOGGER.debug(
                "No verification reply from feeder %s; continuing", self.address
            )
        # Make sure the link outlives the settle window (see LINK_SETTLE_S).
        if protocol.connected_at is not None:
            remaining = LINK_SETTLE_S - (time.monotonic() - protocol.connected_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        if protocol.client is None or not protocol.client.is_connected:
            after = protocol.dropped_after
            protocol.last_error = "feeder dropped the link during setup" + (
                f" ({after:.1f}s after connecting)" if after is not None else ""
            )
            return False
        protocol.session_ready = True
        return True

    async def _close_quietly(self) -> None:
        try:
            await self._protocol.disconnect()
        except Exception:
            pass

    def disconnected_callback(self, client: Any) -> None:
        """Bleak ``disconnected_callback`` that logs the exact drop time at INFO.

        Pass it to whatever creates the BleakClient, e.g.
        ``establish_connection(BleakClient, dev, name, disconnected_callback=feeder.disconnected_callback)``.
        Clients created by the library itself are wired up automatically.
        """
        self._protocol.handle_disconnect(client)

    async def disconnect(self):
        """Disconnect from the device"""
        _LOGGER.debug("Disconnecting from feeder %s", self.address)
        self._cancel_heartbeat()
        await self._protocol.disconnect()
        self._connected = False
        _LOGGER.info("Disconnected from feeder %s", self.address)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        """Start the background heartbeat task, cancelling any existing one."""
        self._cancel_heartbeat()
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        self._heartbeat_task = loop.create_task(self._heartbeat_loop())

    def _cancel_heartbeat(self) -> None:
        """Cancel any running heartbeat task."""
        task = self._heartbeat_task
        if task is not None and not task.done():
            task.cancel()
        self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        """Send CMD_HEARTBEAT every ``_heartbeat_interval`` seconds.

        The loop stops when the connection drops or ``disconnect()`` is called.
        The coordinator's poll (every 60 s) and ``ensure_connected()`` handle
        recovery; the heartbeat only verifies the link is still writable.
        """
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                if not self.is_connected:
                    _LOGGER.debug(
                        "Heartbeat loop stopped: feeder %s no longer connected",
                        self.address,
                    )
                    break
                ok = await self._protocol.send_heartbeat()
                if not ok:
                    _LOGGER.debug(
                        "Heartbeat failed for %s — connection likely dropped",
                        self.address,
                    )
                    break
        except asyncio.CancelledError:
            pass

    async def ensure_connected(self) -> bool:
        """Ensure the device is connected, attempting reconnection if needed.

        When a ``connection_factory`` was supplied at init time (e.g. from
        Home Assistant's ``establish_connection``), reconnection uses it to
        obtain a fresh, adapter-managed ``BleakClient``.  Otherwise falls
        back to a direct ``BleakClient`` connection (standalone mode).

        Returns ``False`` (without raising) when reconnection fails so the
        caller can decide how to handle it.
        """
        if self.is_connected:
            return True
        if not self._connected:
            return False

        async with self._reconnect_lock:
            if self.is_connected:
                return True
            _LOGGER.info("Feeder %s disconnected, attempting reconnect", self.address)
            # Cancel the heartbeat before touching the connection so no
            # writes race with the slot-release / new-connect flow.
            self._cancel_heartbeat()
            for attempt in range(1, CONNECT_ATTEMPTS + 1):
                try:
                    if self._connection_factory:
                        # Release the stale connection slot on the proxy BEFORE
                        # opening a new one.  Without this the ESP32 proxy leaks
                        # slots (max ~3) until it's completely stuck.
                        await self._release_stale_connection()
                        ble_client = await self._connection_factory()
                        ok = await self._reconnect_once(ble_client=ble_client)
                    else:
                        ok = await self._reconnect_once()
                except Exception as exc:
                    self._protocol.last_error = f"BLE connect failed: {exc}"
                    ok = False
                if ok:
                    return True
                if self._protocol.last_error == _VERIFICATION_REJECTED:
                    break  # retrying the same code cannot help
                _LOGGER.debug(
                    "Reconnect attempt %d/%d to feeder %s failed: %s",
                    attempt,
                    CONNECT_ATTEMPTS,
                    self.address,
                    self._protocol.last_error,
                )
            _LOGGER.warning(
                "Reconnection to feeder %s failed after %d attempt(s): %s",
                self.address,
                attempt,
                self._protocol.last_error or "unknown error",
            )
            return False

    async def _release_stale_connection(self) -> None:
        """Tell the BLE proxy to release any stale connection to this device.

        Even when the BLE link is dead (``is_connected`` is False), the
        proxy (e.g. ESP32 via ESPHome) may still hold the connection slot.
        Calling ``disconnect()`` on the old client sends an explicit
        disconnect request to the proxy so it can free the slot before we
        open a new one.
        """
        client = self._protocol.client
        if client is None:
            return
        self._protocol._expected_disconnect = True
        try:
            try:
                await client.disconnect()
            except Exception:
                pass
            _LOGGER.debug(
                "Released stale BLE connection for %s",
                self.address,
            )
        finally:
            self._protocol.client = None
        await asyncio.sleep(STALE_RELEASE_DELAY_S)

    async def feed(self, portions: int = 1, *, fast: bool = True) -> bool:
        """
        Trigger manual feed with specified number of portions.

        Args:
            portions: Number of portions to feed (1-15, typically 1-3)
            fast: If True (default), skip pre-queries (fault, child lock, feeding status)
                  for a quicker response. Set False to check device state before feeding.

        Returns:
            True if feed command was acknowledged, False otherwise

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected:
            raise RuntimeError("Not connected to device. Call connect() first.")

        if not await self.ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")

        _LOGGER.debug("Feeding %d portion(s) (fast=%s)", portions, fast)

        if not fast:
            await self._protocol.query_fault()
            await asyncio.sleep(0.5)
            await self._protocol.query_child_lock()
            await asyncio.sleep(0.5)
            await self._protocol.query_feeding_status()
            await asyncio.sleep(0.5)

        command = self._protocol.encode_command(
            CMD_FEEDING, length=1, action_hex=f"{portions:02X}"
        )

        self._protocol.clear_notifications()
        notification_count_before = len(self._protocol.received_data)

        try:
            await self._protocol.client.write_gatt_char(
                self._protocol.write_uuid, command, response=False
            )
            _LOGGER.debug("Feed command sent, waiting for response")

            feed_triggered = False
            for _ in range(40):  # Wait up to 10 seconds
                await asyncio.sleep(0.25)
                if len(self._protocol.received_data) > notification_count_before:
                    new_notifications = self._protocol.received_data[
                        notification_count_before:
                    ]
                    for data in new_notifications:
                        decoded = self._protocol.decode_notification(data)
                        cmd = decoded.get("command", "")

                        if cmd == "08":
                            feed_triggered = True
                            _LOGGER.debug("Feed acknowledged by device")
                        elif cmd == "0C":
                            _LOGGER.info("Feed completed (%d portions)", portions)
                            return True

            if feed_triggered:
                _LOGGER.info(
                    "Feed triggered but no completion result within 10s (%d portions)",
                    portions,
                )
            else:
                _LOGGER.warning(
                    "No feed response from device within 10s (%d portions)",
                    portions,
                )
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

        if not await self.ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")

        _LOGGER.debug("Setting schedule with %d slot(s)", len(schedules))

        schedule_data = bytearray()
        for schedule in schedules:
            schedule_data.extend(schedule.to_bytes())

        command = self._protocol.encode_command(
            CMD_SET_FEEDER_PLAN,
            length=len(schedule_data),
            action_hex=schedule_data.hex().upper(),
        )

        try:
            await self._protocol.client.write_gatt_char(
                self._protocol.write_uuid, command, response=False
            )
            await asyncio.sleep(1)
            _LOGGER.info("Schedule set (%d slots)", len(schedules))
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

        if not await self.ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")

        _LOGGER.debug("Setting child lock to %s", locked)

        value = "01" if locked else "00"
        command = self._protocol.encode_command(
            CMD_CHILD_LOCK, length=1, action_hex=value
        )

        try:
            await self._protocol.client.write_gatt_char(
                self._protocol.write_uuid, command, response=False
            )
            await asyncio.sleep(1)
            _LOGGER.info("Child lock set to %s", "locked" if locked else "unlocked")
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

        if not await self.ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")

        _LOGGER.debug("Setting sound to %s", enabled)

        value = "01" if enabled else "00"
        command = self._protocol.encode_command(
            CMD_REMINDER_TONE, length=1, action_hex=value
        )

        try:
            await self._protocol.client.write_gatt_char(
                self._protocol.write_uuid, command, response=False
            )
            await asyncio.sleep(1)
            _LOGGER.info("Sound set to %s", "on" if enabled else "off")
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
        _LOGGER.debug("Querying schedule")
        decoded = await self._query_state(
            self._protocol.query_feeder_plan,
            "11",
            timeout=4.0,
            max_age=0,
            skip_unanswered=False,
        )
        if decoded is None:
            _LOGGER.warning("No response to schedule query within 4s")
            return []
        slots = decoded.get("feed_plan_slots") or []
        _LOGGER.debug("Schedule received: %d slot(s)", len(slots))
        return slots

    async def get_device_info(self) -> Dict:
        """
        Query device name and firmware version.

        Returns:
            Dict with "device_name", "device_version" (or empty strings if not available).
        """
        _LOGGER.debug("Querying device info")
        decoded = await self._query_state(
            self._protocol.query_name_version,
            "00",
            timeout=2.0,
            max_age=float("inf"),
            skip_unanswered=False,
        )
        result: Dict = {"device_name": "", "device_version": ""}
        if decoded is None:
            _LOGGER.warning("No device info response received within 2s")
            return result
        result["device_name"] = decoded.get("device_name", "") or ""
        result["device_version"] = decoded.get("device_version", "") or ""
        _LOGGER.debug(
            "Device info: name=%s version=%s",
            result["device_name"],
            result["device_version"],
        )
        return result

    async def _query_state(
        self,
        query_func,
        command_code,
        timeout: float = 1.5,
        max_age: float = STATE_MAX_AGE_S,
        skip_unanswered: bool = True,
    ) -> Optional[Dict]:
        """Return state for ``command_code`` (str or tuple of codes), or None.

        Serves a value seen in this session within ``max_age`` seconds from
        cache; otherwise sends the query and returns as soon as the reply
        arrives (or None after ``timeout``).  With ``skip_unanswered``, a query
        that went unanswered earlier in this session returns None without
        being re-sent for ``UNANSWERED_RETRY_S``.
        """
        if not self._connected:
            raise RuntimeError("Not connected to device. Call connect() first.")
        if not await self.ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")
        codes = (command_code,) if isinstance(command_code, str) else tuple(command_code)
        protocol = self._protocol
        now = time.monotonic()
        session_start = protocol.connected_at or 0.0
        for code in codes:
            seen = protocol.last_seen.get(code)
            if seen is not None and seen >= session_start and now - seen <= max_age:
                return {**protocol.last_state, "command": code}
        missed_at = self._unanswered.get(codes)
        if (
            skip_unanswered
            and missed_at is not None
            and missed_at >= session_start
            and now - missed_at < UNANSWERED_RETRY_S
        ):
            return None
        protocol.clear_notifications()
        decoded = await protocol.request(query_func, codes, timeout)
        if decoded is None:
            self._unanswered[codes] = time.monotonic()
        else:
            self._unanswered.pop(codes, None)
        return decoded

    async def _set_feature(self, log_msg: str, coro) -> bool:
        """Connection-guard wrapper for protocol set commands."""
        if not self._connected:
            raise RuntimeError("Not connected to device. Call connect() first.")
        if not await self.ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")
        _LOGGER.debug(log_msg)
        await coro
        return True

    async def get_child_lock_status(self) -> Optional[bool]:
        """
        Query child lock status from the device.

        Returns:
            True if locked, False if unlocked, or None if query failed or no response.
        """
        decoded = await self._query_state(self._protocol.query_child_lock, "0D")
        if decoded is None or "child_lock" not in decoded:
            _LOGGER.debug("No child lock response received within 1.5s")
            return None
        locked = decoded["child_lock"] == 1
        _LOGGER.debug("Child lock status: %s", "locked" if locked else "unlocked")
        return locked

    async def get_prompt_sound_status(self) -> Optional[bool]:
        """
        Query prompt sound / reminder tone status from the device.

        Returns:
            True if sound is on, False if off, or None if query failed or no response.
        """
        decoded = await self._query_state(self._protocol.query_reminder_tone, "12")
        if decoded is None or "prompt_sound" not in decoded:
            _LOGGER.debug("No prompt sound response received within 1.5s")
            return None
        enabled = decoded["prompt_sound"] == 1
        _LOGGER.debug("Prompt sound status: %s", "on" if enabled else "off")
        return enabled

    async def sync_time(self, dt: Optional[datetime] = None) -> None:
        """
        Sync device clock to the given time (default: now).

        Args:
            dt: Time to set on the device; defaults to datetime.now().
        """
        if not self._connected:
            raise RuntimeError("Not connected to device. Call connect() first.")
        _LOGGER.debug("Syncing time to %s", dt or "now")
        await self._protocol.send_sync_time(dt)

    async def get_fault_status(self) -> Optional[int]:
        """Query device fault status. Returns fault code int (0 = no fault), or None."""
        decoded = await self._query_state(self._protocol.query_fault, "0A")
        if decoded is None or "fault_code" not in decoded:
            _LOGGER.debug("No fault status response received within 1.5s")
            return None
        _LOGGER.debug("Fault status: %s", decoded["fault_code"])
        return decoded["fault_code"]

    async def get_power_mode(self) -> Optional[str]:
        """Query power source. Returns "Battery", "DC Power", or None."""
        decoded = await self._query_state(self._protocol.query_power_mode, "0E")
        if decoded is None or "power_mode" not in decoded:
            _LOGGER.debug("No power source response received within 1.5s")
            return None
        return decoded["power_mode"]

    async def get_feeding_status(self) -> Optional[str]:
        """Query current feeding status. Returns 'idle', 'feeding', 'error', or None."""
        decoded = await self._query_state(self._protocol.query_feeding_status, "09")
        if decoded is None or "feeding_status_text" not in decoded:
            _LOGGER.debug("No feeding status response received within 1.5s")
            return None
        status = decoded["feeding_status_text"].lower()
        _LOGGER.debug("Feeding status: %s", status)
        return status

    async def set_led(self, enabled: bool) -> bool:
        """Set LED indicator on/off."""
        return await self._set_feature(
            f"Setting LED to {enabled}", self._protocol.set_led(enabled)
        )

    async def set_auto_lock(self, enabled: bool) -> bool:
        """Set auto-lock on/off."""
        return await self._set_feature(
            f"Setting auto-lock to {enabled}", self._protocol.set_auto_lock(enabled)
        )

    async def set_atmosphere_light(self, enabled: bool) -> bool:
        """Set atmosphere light on/off."""
        return await self._set_feature(
            f"Setting atmosphere light to {enabled}",
            self._protocol.set_atmosphere_light(enabled),
        )

    async def factory_reset(self) -> bool:
        """Send factory reset command."""
        _LOGGER.warning("Sending factory reset to feeder %s", self.address)
        return await self._set_feature("Factory reset", self._protocol.factory_reset())

    async def get_do_not_disturb(self) -> Optional[Dict]:
        """
        Query Do Not Disturb settings.

        Returns:
            Dict with keys: enabled (bool), start_time (str HH:MM), end_time (str HH:MM),
            or None if no response.
        """
        decoded = await self._query_state(
            self._protocol.query_do_not_disturb, ("17", "18")
        )
        if decoded is None or "do_not_disturb" not in decoded:
            # Some feeder firmwares don't implement DND (cmd 0x17/0x18) — log at
            # DEBUG only, since this fires every poll and is not actionable.
            _LOGGER.debug(
                "No DND response received within 1.5s (DND may not be supported by this firmware)"
            )
            return None
        result = {
            "enabled": decoded["do_not_disturb"],
            "start_time": decoded.get("dnd_start", "22:00"),
            "end_time": decoded.get("dnd_end", "08:00"),
        }
        _LOGGER.debug("DND settings: %s", result)
        return result

    async def set_do_not_disturb(
        self, enabled: bool, start_time: str = "22:00", end_time: str = "08:00"
    ) -> bool:
        """Set Do Not Disturb. start_time/end_time in HH:MM format."""
        return await self._set_feature(
            f"Setting DND to {enabled} ({start_time}–{end_time})",
            self._protocol.set_do_not_disturb(enabled, start_time, end_time),
        )

    async def set_long_ring(self, enabled: bool) -> bool:
        """Set long ring / extended alarm tone on/off."""
        return await self._set_feature(
            f"Setting long ring to {enabled}", self._protocol.set_long_ring(enabled)
        )

    def get_last_feed_result(self) -> Optional[Dict]:
        """Return last feed result from notifications, or None if no feed has completed."""
        return self._protocol.last_feed_result

    async def get_battery_level(self) -> Optional[int]:
        """Query battery level (0–100) via the feeding-status notification. Returns None if unsupported."""
        decoded = await self._query_state(self._protocol.query_feeding_status, "09")
        if decoded is None:
            return None
        return decoded.get("battery_level")

    # ------------------------------------------------------------------
    # CP01B (DU-CP01B interactive cat toy) accessors
    # ------------------------------------------------------------------

    async def get_cp01b_state(self) -> Dict:
        """Query all CP01B DP values. Returns a dict of {state_key: value}."""
        if not self._connected:
            raise RuntimeError("Not connected to device. Call connect() first.")
        if not await self.ensure_connected():
            raise RuntimeError("Connection lost. Please reconnect.")
        dp_map = [
            (CP01B_ROTATION_MODE,  "rotation_mode"),
            (CP01B_PLAYBACK_FREQ,  "playback_frequency"),
            (CP01B_SOUND_EFFECT,   "sound_effect"),
            (CP01B_OPERATION_MODE, "operation_mode"),
            (CP01B_VOLUME,         "volume"),
            (CP01B_AUTO_COUNTDOWN, "auto_mode_countdown"),
            (CP01B_PROMPT_SOUND,   "cp01b_prompt_sound"),
            (CP01B_FUN_MODE,       "fun_mode"),
        ]
        result: Dict = {}
        for cmd, key in dp_map:
            decoded = await self._query_state(
                lambda c=cmd: self._protocol.query_cp01b_dp(c), cmd
            )
            if decoded is not None and key in decoded:
                result[key] = decoded[key]
        return result

    async def set_cp01b_operation_mode(self, value: int) -> bool:
        """Set CP01B operation mode (0–5)."""
        return await self._set_feature(
            f"CP01B operation_mode={value}",
            self._protocol.set_cp01b_dp(CP01B_OPERATION_MODE, value),
        )

    async def set_cp01b_rotation_mode(self, value: int) -> bool:
        """Set CP01B rotation mode."""
        return await self._set_feature(
            f"CP01B rotation_mode={value}",
            self._protocol.set_cp01b_dp(CP01B_ROTATION_MODE, value),
        )

    async def set_cp01b_volume(self, value: int) -> bool:
        """Set CP01B volume (0–100)."""
        return await self._set_feature(
            f"CP01B volume={value}",
            self._protocol.set_cp01b_dp(CP01B_VOLUME, value),
        )

    async def set_cp01b_playback_frequency(self, value: int) -> bool:
        """Set CP01B playback frequency."""
        return await self._set_feature(
            f"CP01B playback_freq={value}",
            self._protocol.set_cp01b_dp(CP01B_PLAYBACK_FREQ, value),
        )

    async def set_cp01b_sound_effect(self, value: int) -> bool:
        """Set CP01B sound effect."""
        return await self._set_feature(
            f"CP01B sound_effect={value}",
            self._protocol.set_cp01b_dp(CP01B_SOUND_EFFECT, value),
        )

    async def set_cp01b_auto_mode_countdown(self, value: int) -> bool:
        """Set CP01B auto-mode countdown (minutes)."""
        return await self._set_feature(
            f"CP01B auto_countdown={value}",
            self._protocol.set_cp01b_dp(CP01B_AUTO_COUNTDOWN, value),
        )

    async def set_cp01b_prompt_sound(self, enabled: bool) -> bool:
        """Set CP01B prompt sound on/off."""
        return await self._set_feature(
            f"CP01B prompt_sound={enabled}",
            self._protocol.set_cp01b_dp(CP01B_PROMPT_SOUND, 1 if enabled else 0),
        )

    async def set_cp01b_fun_mode(self, value: int) -> bool:
        """Set CP01B fun mode."""
        return await self._set_feature(
            f"CP01B fun_mode={value}",
            self._protocol.set_cp01b_dp(CP01B_FUN_MODE, value),
        )

    # TC02 (Du-TC02 laser cat teaser) accessors
    # real_tags 50-59 per ble-device-type.json dpMappings

    async def get_tc02_state(self) -> Dict:
        """Query all TC02 DP values. Returns a dict of {state_key: value}."""
        result: Dict = {}
        dps = [
            (TC02_OPERATION_MODE,  "tc02_operation_mode"),
            (TC02_ROTATION_MODE,   "tc02_rotation_mode"),
            (TC02_COLOR_RGB,       "tc02_color_r"),  # also populates tc02_color_g/b
            (TC02_MOOD_LIGHT_MODE, "tc02_mood_light_mode"),
            (TC02_LED_COLOR,       "tc02_led_color"),
            (TC02_SOUND_EFFECT,    "tc02_sound_effect"),
            (TC02_PLAYBACK_FREQ,   "tc02_playback_frequency"),
            (TC02_VOLUME,          "tc02_volume"),
            (TC02_AUTO_COUNTDOWN,  "tc02_auto_countdown_total"),
        ]
        for cmd, key in dps:
            try:
                await self._query_state(
                    lambda c=cmd: self._protocol.query_tc02_dp(c), cmd
                )
                state = self._protocol.last_state or {}
                if key in state:
                    result[key] = state[key]
                    if cmd == TC02_COLOR_RGB:
                        for extra in ("tc02_color_g", "tc02_color_b"):
                            if extra in state:
                                result[extra] = state[extra]
                    if cmd == TC02_AUTO_COUNTDOWN and "tc02_auto_countdown_remaining" in state:
                        result["tc02_auto_countdown_remaining"] = state["tc02_auto_countdown_remaining"]
            except Exception:
                pass
        return result

    async def set_tc02_operation_mode(self, value: int) -> bool:
        """Set TC02 operation mode (0–5)."""
        return await self._set_feature(
            f"TC02 operation_mode={value}",
            self._protocol.set_tc02_dp(TC02_OPERATION_MODE, value),
        )

    async def set_tc02_rotation_mode(self, value: int) -> bool:
        """Set TC02 rotation mode (0–5)."""
        return await self._set_feature(
            f"TC02 rotation_mode={value}",
            self._protocol.set_tc02_dp(TC02_ROTATION_MODE, value),
        )

    async def set_tc02_color_rgb(self, r: int, g: int, b: int) -> bool:
        """Set TC02 RGB color (each channel 0–255)."""
        return await self._set_feature(
            f"TC02 color_rgb=({r},{g},{b})",
            self._protocol.set_tc02_color_rgb(r, g, b),
        )

    async def set_tc02_mood_light_mode(self, value: int) -> bool:
        """Set TC02 mood light mode."""
        return await self._set_feature(
            f"TC02 mood_light_mode={value}",
            self._protocol.set_tc02_dp(TC02_MOOD_LIGHT_MODE, value),
        )

    async def set_tc02_led_color(self, value: int) -> bool:
        """Set TC02 LED color preset index."""
        return await self._set_feature(
            f"TC02 led_color={value}",
            self._protocol.set_tc02_dp(TC02_LED_COLOR, value),
        )

    async def set_tc02_sound_effect(self, value: int) -> bool:
        """Set TC02 sound effect index."""
        return await self._set_feature(
            f"TC02 sound_effect={value}",
            self._protocol.set_tc02_dp(TC02_SOUND_EFFECT, value),
        )

    async def set_tc02_playback_frequency(self, value: int) -> bool:
        """Set TC02 playback frequency (0–10)."""
        return await self._set_feature(
            f"TC02 playback_frequency={value}",
            self._protocol.set_tc02_dp(TC02_PLAYBACK_FREQ, value),
        )

    async def set_tc02_volume(self, value: int) -> bool:
        """Set TC02 volume (0–100)."""
        return await self._set_feature(
            f"TC02 volume={value}",
            self._protocol.set_tc02_dp(TC02_VOLUME, value),
        )

    async def set_tc02_auto_mode_countdown(self, value: int) -> bool:
        """Set TC02 auto mode countdown in minutes (total duration, 0–60)."""
        return await self._set_feature(
            f"TC02 auto_mode_countdown={value}",
            self._protocol.set_tc02_dp(TC02_AUTO_COUNTDOWN, value),
        )

    @property
    def is_connected(self) -> bool:
        """Check if device is connected"""
        return self._connected and (
            self._protocol.client is not None and self._protocol.client.is_connected
        )
