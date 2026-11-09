# petnetizen-feeder

Python BLE library for controlling **Pet Netizen / CloudPets / Du** smart feeders and pet toys
without any cloud account or app pairing. Used by the
[netizen_ble](https://github.com/lorek123/netizen_ble) Home Assistant integration.

## Supported devices

| BLE name prefix | Device type | Notes |
|---|---|---|
| `Du-F*`, `Du-W*` | Standard feeder / water dispenser | All Du- prefix feeders |
| `DU-F14B` | F14B | V2 protocol variant |
| `DU-CP01B` | CP01B | Interactive ball feeder |
| `Du-TC02` | TC02 | Laser cat teaser |
| `JK-*` | JK feeder | Different service UUID |
| `ALI-*` | ALI feeder | Different service UUID |
| `PET-*`, `FEED-*` | Standard feeder | |

## Installation

```bash
pip install petnetizen-feeder
# or
uv add petnetizen-feeder
```

## Quick start

```python
import asyncio
from petnetizen_feeder import FeederDevice, FeedSchedule, Weekday

async def main():
    feeder = FeederDevice("E6:C0:07:09:A3:D3")
    await feeder.connect()

    await feeder.feed(portions=2)

    await feeder.set_schedule([
        FeedSchedule(weekdays=Weekday.ALL_DAYS, time="08:00", portions=1, enabled=True),
        FeedSchedule(weekdays=Weekday.WEEKDAYS, time="18:00", portions=2, enabled=True),
    ])

    await feeder.disconnect()

asyncio.run(main())
```

## Discovery

```python
from petnetizen_feeder import discover_feeders, FeederDevice

async def main():
    # Returns [(address, name, device_type), ...]
    devices = await discover_feeders(timeout=10.0)
    if not devices:
        print("No devices found")
        return

    address, name, device_type = devices[0]
    print(f"{name} @ {address} ({device_type})")

    feeder = FeederDevice(address, device_type=device_type)
    await feeder.connect()
    info = await feeder.get_device_info()
    print(info)  # {"device_name": "...", "device_version": "..."}
    await feeder.disconnect()
```

`device_type` values: `"standard"`, `"jk"`, `"ali"`, `"f14b"`, `"cp01b"`, `"tc02"`.

## API reference

### `discover_feeders(timeout=10.0)`

BLE scan returning `[(address, name, device_type)]`. Recognises devices by name prefix
(`Du`, `JK`, `ALI`, `PET`, `FEED`, `TC`) — same logic as the Android app.

### `FeederDevice(address, verification_code="00000000", device_type=None)`

Main device controller. `device_type` is auto-detected from the BLE name when using
`discover_feeders()`; pass it explicitly if you have the address only.

#### Connection

| Method | Returns | Notes |
|---|---|---|
| `connect()` | `bool` | Up to 3 attempts; returns only once the link has stayed up for ~1 s |
| `disconnect()` | — | |
| `ensure_connected()` | `bool` | Reconnects if dropped (up to 3 attempts, one WARNING if all fail) |
| `is_connected` | `bool` | Property |
| `disconnected_callback` | — | Pass to `establish_connection(..., disconnected_callback=...)` when supplying a `connection_factory`, so link drops are logged with their exact time |

**Connection behaviour (Du-W11B, fw 110):** session setup enables notifications, then
sends the verification code (same order as the Android app, so the `EB 06 01 01` verdict
is visible). The feeder drops links that never send the code within ~4–5 s; an
authenticated link stays up for minutes even when idle. Setup waits until ~1 s after
link-up before reporting success and retries if the link died; one test session saw about
half of fresh links dropped 2–3 s after link-up (not reproduced since).

**Polling:** queries return as soon as the feeder replies. State the feeder pushes after
verification (feeding status, fault, child lock, prompt sound, power, LED, auto-lock) is
served from `FeederBLEProtocol.last_state` for 30 s without a query, and a query the
firmware never answers is not re-sent for 10 minutes.

Tested through an ESPHome Bluetooth proxy running ESPHome 2026.10.0. Before that update,
the same proxy failed every connection while enabling notifications (error 19).

#### Feeder control

| Method | Returns | Notes |
|---|---|---|
| `feed(portions=1)` | `bool` | 1–15 portions |
| `set_schedule(schedules)` | `bool` | List of `FeedSchedule` |
| `query_schedule()` | `list[dict]` | Current schedule from device |
| `sync_time(dt=None)` | — | Syncs device clock (default: now) |
| `get_device_info()` | `dict` | `device_name`, `device_version` |
| `factory_reset()` | `bool` | |

#### State queries

| Method | Returns | Notes |
|---|---|---|
| `get_battery_level()` | `int \| None` | 0–100 % |
| `get_child_lock_status()` | `bool \| None` | |
| `get_prompt_sound_status()` | `bool \| None` | |
| `get_fault_status()` | `int \| None` | 0 = ok |
| `get_feeding_status()` | `str \| None` | |
| `get_do_not_disturb()` | `dict \| None` | `{enabled, start_time, end_time}` |
| `get_last_feed_result()` | `dict \| None` | Cached from last notification |

#### Switches

| Method | Notes |
|---|---|
| `set_child_lock(locked)` | |
| `set_sound(enabled)` | Prompt sound / reminder tone |
| `set_led(enabled)` | |
| `set_auto_lock(enabled)` | |
| `set_atmosphere_light(enabled)` | |
| `set_long_ring(enabled)` | |
| `set_do_not_disturb(enabled, start_time="22:00", end_time="08:00")` | |

#### CP01B (interactive ball feeder)

| Method | Notes |
|---|---|
| `get_cp01b_state()` | Returns dict of all CP01B DP values |
| `set_cp01b_operation_mode(value)` | 0–5 |
| `set_cp01b_rotation_mode(value)` | 0–5 |
| `set_cp01b_volume(value)` | 0–100 |
| `set_cp01b_playback_frequency(value)` | 0–10 |
| `set_cp01b_sound_effect(value)` | 0–10 |
| `set_cp01b_auto_mode_countdown(value)` | 0–60 minutes |
| `set_cp01b_fun_mode(value)` | 0–10 |
| `set_cp01b_prompt_sound(enabled)` | |

#### TC02 (laser cat teaser)

| Method | Notes |
|---|---|
| `get_tc02_state()` | Returns dict of all TC02 DP values |
| `set_tc02_operation_mode(value)` | 0–5 |
| `set_tc02_rotation_mode(value)` | 0–5 |
| `set_tc02_color_rgb(r, g, b)` | 0–255 each channel |
| `set_tc02_mood_light_mode(value)` | 0–10 |
| `set_tc02_led_color(value)` | 0–255 |
| `set_tc02_sound_effect(value)` | 0–10 |
| `set_tc02_playback_frequency(value)` | 0–10 |
| `set_tc02_volume(value)` | 0–100 |
| `set_tc02_auto_mode_countdown(value)` | 0–60 minutes |

### `FeedSchedule(weekdays, time, portions, enabled=True)`

| Param | Type | Notes |
|---|---|---|
| `weekdays` | `list[str]` or `Weekday` | `["mon", "wed"]` or `Weekday.ALL_DAYS` |
| `time` | `str` | `"HH:MM"` |
| `portions` | `int` | 1–15 |
| `enabled` | `bool` | |

### `Weekday`

Constants: `SUNDAY`, `MONDAY`, …, `SATURDAY`, `ALL_DAYS`, `WEEKDAYS`, `WEEKEND`.

## Model features

```python
from petnetizen_feeder import model_features, decode_fault_flags

f = model_features("Du-W11B_E6C00709A3D3")   # advertised BLE name
f.child_lock, f.do_not_disturb               # True, False
decode_fault_flags(0x05)                     # stuck_grain + out_of_food
```

`model_features()` mirrors the Pet Netizen app's per-model feature list and returns
`None` for models it doesn't know.

## Protocol

Tuya BLE, EA/AE framing:

```
TX: EA [cmd] [len] [data…] 00 AE
RX: EB [cmd] [len] [data…] [crc8] AE
```

Service UUID `0000ae30` (standard, CP01B, TC02), `0000fff0` (JK), `0000ffff` (ALI).
Default verification code `00000000` — no cloud pairing required. The feeder answers
`SET_FAMILY_ID` with `EB 06 01 01` (accepted) or `EB 06 01 00` (rejected); the reply is
only visible if notifications are enabled first. Right after verification the feeder
pushes its state unprompted (`05` time-sync request, `0E`, `09`, `0A`, `0D`, `0F`, `10`,
`12`, then the `0C`/`0B` feed records).

Reverse engineered from the Pet Netizen Android app (JADX).

## Requirements

- Python 3.12+
- `bleak` for BLE
- Linux: user in `bluetooth` group (`sudo usermod -aG bluetooth $USER`)

## Development

```bash
uv sync --all-extras
uv run pytest tests/ -v
```

## Releasing

Bump `version` in `pyproject.toml`, commit, then:

```bash
git tag v0.x.y && git push origin v0.x.y
```

CI publishes to PyPI automatically on tag push.

## License

MIT — based on reverse engineering of the Petnetizen Android app for educational and personal use.
