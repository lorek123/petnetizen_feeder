# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Nothing yet.

## [0.1.0] - 2025-02-01

### Added

- Initial release.
- BLE control for Petnetizen/Tuya-style pet feeders via `bleak`.
- `FeederDevice`: connect, manual feed, set schedule, child lock, sound, query schedule.
- `FeedSchedule` and `Weekday` helpers for schedules.
- Support for standard, JK, and ALI device UUIDs.
- PyPI-ready packaging with `pyproject.toml` and uv.

[Unreleased]: https://github.com/your-username/petnetizen-feeder/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/your-username/petnetizen-feeder/releases/tag/v0.1.0
