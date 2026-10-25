# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Nothing yet.

## [0.6.0] - 2026-10-25

Connection stability fixes, based on measurements against a Du-W11B (fw 110) over BlueZ:
about half of all fresh links are dropped by the feeder 2.2–3.3 s after link-up regardless
of traffic, while a link that survives that window stays up for over two minutes with no
traffic at all. The earlier 60 s "idle timeout" theory was wrong, and the early drop explains
why the notify/verify order kept flipping between releases (0.3.5, 0.3.6, 0.5.3).

### Changed

- **Handshake order**: notifications are enabled *before* the verification code is sent,
  matching the Android app (Nordic `BleManager.initialize` → `onDeviceReady` → send code).
  `send_verification_code()` now waits for the feeder's `SET_FAMILY_ID` reply and returns
  `True` (accepted), `False` (rejected) or `None` (no reply). A rejected code fails the
  connection and is not retried.
- **Early-drop detection**: `connect()` and `ensure_connected()` wait until 4 s after link-up
  (`LINK_SETTLE_S`) and check the link is still up before reporting success, and retry up to
  3 times (`CONNECT_ATTEMPTS`). Previously a dropped link was reported as "Connected" and only
  noticed at the next heartbeat or poll.
- `connect()` without a `ble_client` now uses the `connection_factory` when one was given
  (previously it always created a raw `BleakClient`).
- Heartbeat interval lowered from 60 s to 20 s. It now serves as a liveness probe; the feeder
  does not need it to keep an idle link.
- **Logging**: connection-setup details (service discovery, `start_notify` retries,
  verification) are logged at DEBUG and summarised in a single WARNING per failed
  `connect()`/`ensure_connected()` call, which includes the reason.

### Added

- `FeederDevice.disconnected_callback` / `FeederBLEProtocol.handle_disconnect`: bleak
  `disconnected_callback` that logs, at INFO, the exact time an established link was dropped,
  how long after connect, and when the last notification arrived. Clients created by the
  library are wired up automatically; integrations pass it to `establish_connection`.
- `FeederBLEProtocol.last_error`: reason for the most recent connection failure.

### Fixed

- **Heartbeat ping-pong**: the feeder answers every heartbeat with `EB 03 00 00 AE`, which the
  0.5.6 echo logic treated as a device-initiated ping and echoed back, so each heartbeat started
  an endless exchange. A `03` arriving within 5 s of our own heartbeat is now treated as the
  reply and not echoed.

## [0.5.6] - 2026-05-12

### Added

- **Auto time-sync on device request**: the notification handler now responds automatically when the feeder firmware pushes `CMD_SYNC_TIME` ("05") as a device-initiated time-sync request. The Android app does this via `BleSyncTimeCodec.handlerController` → `BleDeviceController.syncTime`. Without this, the feeder clock stays wrong after a power cycle until a manual button press.
- **Heartbeat echo ACK**: when the *device* sends `CMD_HEARTBEAT` ("03"), the handler now echoes an ACK back (same command, length 0). The Android product-test controller does this; some firmware builds expect a bidirectional ping/pong rather than only central-initiated pulses.

## [0.5.5] - 2026-05-12

### Added

- **Heartbeat keep-alive** (`FeederBLEProtocol.send_heartbeat()`): sends `CMD_HEARTBEAT` (0x03, no payload) to the feeder, matching the Android app's behaviour. The feeder firmware treats the link as idle without periodic writes and may drop it.
- `FeederDevice._heartbeat_loop()`: background `asyncio.Task` that calls `send_heartbeat()` every 60 seconds (the same interval used by the official app). The loop is started automatically after a successful `connect()` or `reconnect()`, and cancelled on `disconnect()` or before a factory-managed reconnection attempt.

## [0.4.0] - 2026-02-19

### Added

- `FeederDevice(connection_factory=...)`: optional async callable returning a connected `BleakClient`. When provided (e.g. from Home Assistant's `establish_connection`), all reconnection goes through the factory instead of creating a raw `BleakClient`. Standalone scripts that omit this parameter keep the existing direct-connection behaviour.
- `FeederDevice.ensure_connected()`: public method that checks connectivity and reconnects via the factory (or directly) with proper verification. All library command methods now use this instead of the protocol-level `_ensure_connected`.
- `FeederBLEProtocol._managed_connection` flag: when set by the factory path, the protocol never creates raw `BleakClient` instances on its own — it returns `False` and lets the higher-level code handle reconnection.

### Changed

- Bump minimum `bleak` requirement from `>=2.0.0` to `>=2.1.0` (aligns with Home Assistant 2026.1+ which requires bleak 2.1.0 for the `start_notify` BlueZ regression fix).

## [0.3.2] - 2025-02-19

### Changed

- Replace blind 1-second sleep with explicit `get_services()` GATT round-trip as connection-readiness proof — faster when the device is ready, and fails fast when the link is broken instead of sleeping into a dead connection.
- Check `is_connected` between `start_notify` retries — bail immediately if the peripheral terminated the connection instead of retrying on a dead link.

## [0.3.1] - 2025-02-19

### Fixed

- Add 1-second GATT settling delay after BLE connect (both self-created and provided BleakClient paths) — prevents BlueZ from dropping the connection when `start_notify` fires too quickly.
- Retry `start_notify` up to 3 times with 2-second backoff on failure, instead of aborting the entire setup on first failure.

## [0.3.0] - 2025-02-19

### Fixed

- `_ensure_connected()`: removed notification stop/start cycle that ran on every command, which could destabilize the BLE link on Linux/BlueZ.

### Added

- `FeederDevice.reconnect(ble_client)`: accept a fresh BleakClient (e.g. from `bleak_retry_connector`) and re-establish the session (service discovery, verification code) without tearing down the whole device object. Enables proper HA-level reconnection.
- `FeederBLEProtocol.replace_client(ble_client)`: cleanly swap the underlying BleakClient.
- `FeederBLEProtocol.clear_notifications()`: clear accumulated notification data.
- All command methods now clear stale notifications before sending, preventing unbounded `received_data` growth over long-running sessions.

## [0.2.9] - 2025-02-19

### Added

- Comprehensive logging throughout `protocol.py` and `feeder.py` to diagnose unavailability issues.
- Protocol layer (`FeederBLEProtocol`): logs for BLE connect/disconnect, service discovery failures, characteristic lookup, notification received, verification code, all command writes, and `_ensure_connected` reconnection attempts.
- Device layer (`FeederDevice`): logs for connect/disconnect, feed command lifecycle (sent, acknowledged, completed, timed out), schedule/child-lock/sound set/query, device info query, and missing responses.
- Log levels: `DEBUG` for normal operations, `INFO` for connection state changes and successful actions, `WARNING` for failures and missing responses.

## [0.2.8] - 2025-02-01

### Changed

- `feed(portions=..., fast=True)`: default `fast=True` skips pre-queries (fault, child lock, feeding status) so feed responds in ~0.5–2 s instead of ~2–4 s. Use `fast=False` to check device state before feeding.
- Feed response polling: check every 0.25 s instead of 0.5 s so the call returns as soon as the device acknowledges.

## [0.2.7] - 2025-02-01

### Added

- `FeederDevice.get_child_lock_status()` – query child lock state from device (True/False/None).
- `FeederDevice.get_prompt_sound_status()` – query prompt sound/reminder tone state from device (True/False/None).
- Protocol: decode command 0x12 (REMINDER_TONE) response; `query_reminder_tone()`.

## [0.2.6] - 2025-02-01

### Fixed

- `query_schedule()`: wait 4s for QUERY_FEEDER_PLAN response (matches POC; improves reliability in Home Assistant). POC script docstring: note about single BLE connection.

## [0.2.5] - 2025-02-01

### Fixed

- `query_schedule()`: support length-prefixed QUERY_FEEDER_PLAN response (some firmwares send `[num_slots]` + slots). Longer wait (2.5s) for device response. Debug logging when parsing fails or notifications are received.

## [0.2.4] - 2025-02-01

### Fixed

- `query_schedule()` now parses QUERY_FEEDER_PLAN (0x11) response; returns list of slots (weekdays, time, portions, enabled) so Feed plan sensor and attributes show the actual schedule.

## [0.2.2] - 2025-02-01

### Fixed

- Relax `bleak` requirement to `>=2.0.0` for Home Assistant compatibility (HA pins bleak 2.0.0).

## [0.2.0] - 2025-02-01

### Added

- `discover_feeders(timeout)` – BLE scan for feeders by name prefix (Du, JK, ALI, PET, FEED); returns list of `(address, name, device_type)`.
- `FeederDevice.get_device_info()` – returns `device_name` and `device_version`.
- `FeederDevice.sync_time(dt)` – sync device clock with host (default: now).
- Optional `device_type` on `FeederDevice` for standard/jk/ali.
- Example script: `examples/read_settings_and_sync_time.py`.

## [0.1.0] - 2025-02-01

### Added

- Initial release.
- BLE control for Petnetizen/Tuya-style pet feeders via `bleak`.
- `FeederDevice`: connect, manual feed, set schedule, child lock, sound, query schedule.
- `FeedSchedule` and `Weekday` helpers for schedules.
- Support for standard, JK, and ALI device UUIDs.
- PyPI-ready packaging with `pyproject.toml` and uv.

[Unreleased]: https://github.com/lorek123/petnetizen_feeder/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/lorek123/petnetizen_feeder/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/lorek123/petnetizen_feeder/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/lorek123/petnetizen_feeder/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.9...v0.3.0
[0.2.9]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/lorek123/petnetizen_feeder/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lorek123/petnetizen_feeder/releases/tag/v0.1.0
