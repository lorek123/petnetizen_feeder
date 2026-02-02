# Petnetizen Feeder BLE Library

A Python BLE library for controlling Petnetizen automatic pet feeders. Suitable for use with **uv** and as a dependency for **Home Assistant** custom integrations.

## Features

- ✅ **Manual Feed**: Trigger feeding with configurable portion count (1-15 portions)
- ✅ **Feed Schedule**: Set automated feeding schedules with time, weekdays, and portion count
- ✅ **Child Lock**: Enable/disable child lock to prevent accidental feeding
- ✅ **Sound Control**: Enable/disable reminder tone/sound notifications
- ✅ **Device Status**: Query device information, feeding status, and fault codes
- ✅ **Autodiscovery**: Scan for feeders via BLE (`discover_feeders()`)
- ✅ **Time sync**: Sync device clock to host time (`sync_time()`)

## Installation

**From PyPI** (after [publishing](https://pypi.org/project/petnetizen-feeder/)):

```bash
pip install petnetizen-feeder
# or
uv add petnetizen-feeder
```

**From source** (project root):

```bash
# Using uv (recommended)
uv sync
```

This creates a virtual environment (`.venv`), installs the package in editable mode, and pins dependencies (e.g. `bleak`). Then run scripts with:

```bash
uv run python your_script.py
```

To install elsewhere (e.g. for a Home Assistant integration), use `pip install -e .` in the project root or publish to PyPI and add `petnetizen-feeder` to your integration’s dependencies.

## Quick Start

```python
import asyncio
from petnetizen_feeder import FeederDevice, FeedSchedule, Weekday

async def main():
    # Connect to device
    feeder = FeederDevice("E6:C0:07:09:A3:D3")
    await feeder.connect()
    
    # Manual feed with 2 portions
    await feeder.feed(portions=2)
    
    # Set schedule: 8:00 AM every day, 1 portion
    schedules = [
        FeedSchedule(
            weekdays=Weekday.ALL_DAYS,
            time="08:00",
            portions=1,
            enabled=True
        )
    ]
    await feeder.set_schedule(schedules)
    
    # Toggle child lock
    await feeder.set_child_lock(False)  # Unlock
    
    # Toggle sound
    await feeder.set_sound(True)  # Enable
    
    await feeder.disconnect()

asyncio.run(main())
```

### Autodiscovery and reading settings

Discover feeders on BLE, connect to the first one, read device info and schedule, then sync time:

```bash
uv run python examples/read_settings_and_sync_time.py
```

```python
from petnetizen_feeder import discover_feeders, FeederDevice

async def main():
    feeders = await discover_feeders(timeout=10.0)  # [(address, name, device_type), ...]
    if not feeders:
        return
    address, name, device_type = feeders[0]
    feeder = FeederDevice(address, device_type=device_type)
    await feeder.connect()
    info = await feeder.get_device_info()   # {"device_name": "...", "device_version": "..."}
    schedules = await feeder.query_schedule()
    await feeder.sync_time()                # sync device clock to now
    await feeder.disconnect()
```

## API Reference

### `discover_feeders(timeout: float = 10.0) -> List[Tuple[str, str, str]]`

Scan for Petnetizen feeders via BLE. Uses an **unfiltered** BLE scan (no service-UUID filter), then recognizes feeders by **advertised name prefix** (like the Android app: `bleNames` / `getDeviceTypeByName`). Returns a list of `(address, name, device_type)` for each feeder found. `device_type` is `"standard"`, `"jk"`, or `"ali"`. Use `device_type` when constructing `FeederDevice` for correct service UUIDs. Name prefixes: `Du`, `JK`, `ALI`, `PET`, `FEED` (see `FEEDER_NAME_PREFIXES` in `protocol.py` to extend).

### `FeederDevice`

Main class for controlling feeder devices.

#### `__init__(address: str, verification_code: str = "00000000", device_type: Optional[str] = None)`

Initialize feeder device controller.

- `address`: BLE device MAC address (e.g., "E6:C0:07:09:A3:D3")
- `verification_code`: Verification code (default: "00000000")
- `device_type`: Optional `"standard"`, `"jk"`, or `"ali"` (auto-detected from name if not set; use when discovered via `discover_feeders()`)

#### `async connect() -> bool`

Connect to the feeder device. Returns `True` if successful.

#### `async disconnect()`

Disconnect from the device.

#### `async feed(portions: int = 1) -> bool`

Trigger manual feed with specified number of portions.

- `portions`: Number of portions to feed (1-15, typically 1-3)
- Returns: `True` if feed command was acknowledged

#### `async set_schedule(schedules: List[FeedSchedule]) -> bool`

Set feed schedule.

- `schedules`: List of `FeedSchedule` objects
- Returns: `True` if command was sent successfully

#### `async set_child_lock(locked: bool) -> bool`

Set child lock state.

- `locked`: `True` to lock, `False` to unlock
- Returns: `True` if command was sent successfully

#### `async set_sound(enabled: bool) -> bool`

Set reminder tone/sound state.

- `enabled`: `True` to enable sound, `False` to disable
- Returns: `True` if command was sent successfully

#### `async query_schedule() -> List[Dict]`

Query current feed schedule. Returns list of schedule dictionaries.

#### `async get_device_info() -> Dict`

Query device name and firmware version. Returns `{"device_name": "...", "device_version": "..."}`.

#### `async sync_time(dt: Optional[datetime] = None) -> None`

Sync device clock to the given time (default: now).

#### `is_connected: bool`

Property to check if device is connected.

### `FeedSchedule`

Represents a single feed schedule entry.

#### `__init__(weekdays: List[str], time: str, portions: int, enabled: bool = True)`

- `weekdays`: List of weekday names (e.g., `["mon", "wed", "fri"]` or `Weekday.ALL_DAYS`)
- `time`: Time in HH:MM format (e.g., "08:00")
- `portions`: Number of portions to feed (1-15)
- `enabled`: Whether this schedule is enabled

### `Weekday`

Weekday constants for schedules.

- `Weekday.SUNDAY`, `Weekday.MONDAY`, etc.
- `Weekday.ALL_DAYS`: All days of the week
- `Weekday.WEEKDAYS`: Monday through Friday
- `Weekday.WEEKEND`: Saturday and Sunday

## Examples

### Basic Manual Feed

```python
from petnetizen_feeder import FeederDevice

async def feed_pet():
    feeder = FeederDevice("E6:C0:07:09:A3:D3")
    await feeder.connect()
    await feeder.feed(portions=1)
    await feeder.disconnect()
```

### Set Multiple Schedules

```python
from petnetizen_feeder import FeederDevice, FeedSchedule, Weekday

async def setup_schedule():
    feeder = FeederDevice("E6:C0:07:09:A3:D3")
    await feeder.connect()
    
    schedules = [
        # Morning: 8:00 AM every day, 1 portion
        FeedSchedule(Weekday.ALL_DAYS, "08:00", 1, True),
        # Evening: 6:00 PM weekdays only, 2 portions
        FeedSchedule(Weekday.WEEKDAYS, "18:00", 2, True),
    ]
    
    await feeder.set_schedule(schedules)
    await feeder.disconnect()
```

### Control Child Lock and Sound

```python
async def configure_device():
    feeder = FeederDevice("E6:C0:07:09:A3:D3")
    await feeder.connect()
    
    # Unlock device (allow manual feeding)
    await feeder.set_child_lock(False)
    
    # Enable sound notifications
    await feeder.set_sound(True)
    
    await feeder.disconnect()
```

## Protocol Details

The library uses the Tuya BLE protocol format:
- Commands: `EA` header + Command + Length + Data + CRC(00) + `AE` footer
- Notifications: `EB` header + Command + Length + Data + CRC + `AE` footer

Based on reverse engineering of the official Petnetizen Android app.

## Requirements

- Python 3.12+ (aligned with current Home Assistant; see [HA version support](https://www.home-assistant.io/installation/))
- `bleak` library for BLE communication
- Linux: Bluetooth permissions (user in `bluetooth` group)
- macOS: Bluetooth access permissions
- Windows: Bluetooth adapter

## Troubleshooting

### Connection fails
- Ensure Bluetooth is enabled
- Make sure device is powered on
- Check device address is correct
- Try running with appropriate permissions (Linux may need `sudo` or user in `bluetooth` group)

### Feed doesn't occur
- Check child lock status (must be unlocked)
- Verify device is not in fault state
- Ensure device has food loaded
- Check feeding status before feeding

### Permission errors (Linux)
```bash
sudo usermod -aG bluetooth $USER
# Then log out and back in
```

## Development

From the project root:

```bash
uv sync --all-extras   # install with dev dependencies
uv run pytest tests/ -v
uv build               # build sdist + wheel in dist/
```

## Releasing (PyPI + GitHub)

1. **One-time setup**
   - In **pyproject.toml** and **CHANGELOG.md**, replace `your-username` with your GitHub username (or org) so URLs point to your repo.
   - On [PyPI](https://pypi.org), create an API token (Account → API tokens).
   - In your GitHub repo: **Settings → Secrets and variables → Actions** → add secret `PYPI_API_TOKEN` with the PyPI token.

2. **Cut a release**
   - Bump `version` in **pyproject.toml** and add an entry in **CHANGELOG.md** under `[Unreleased]` / new version.
   - Commit, push, then create and push a tag:
   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```
   - The **Release** workflow runs: builds the package, publishes to PyPI, and creates a GitHub Release with generated notes and `dist/` artifacts.

CI runs on every push/PR to `main` (or `master`) and tests Python 3.12–3.14 (aligned with current Home Assistant).

## License

This library is based on reverse engineering of the Petnetizen Android app for educational and personal use. See [LICENSE](LICENSE).
