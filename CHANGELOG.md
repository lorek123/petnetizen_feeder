# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Nothing yet.

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

[Unreleased]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/lorek123/petnetizen_feeder/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/lorek123/petnetizen_feeder/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lorek123/petnetizen_feeder/releases/tag/v0.1.0
