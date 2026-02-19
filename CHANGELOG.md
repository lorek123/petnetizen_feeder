# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Nothing yet.

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

[Unreleased]: https://github.com/lorek123/petnetizen_feeder/compare/v0.3.2...HEAD
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
