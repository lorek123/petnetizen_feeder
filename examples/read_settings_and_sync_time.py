#!/usr/bin/env python3
"""
Example: discover a Petnetizen feeder, read its settings, and sync time.

Run from project root:
    uv run python examples/read_settings_and_sync_time.py
    uv run python examples/read_settings_and_sync_time.py E6:C0:07:09:A3:D3

With a MAC argument, discovery is skipped and the script connects directly
(useful when the feeder does not advertise service UUIDs).
"""

import asyncio
import sys
from petnetizen_feeder import discover_feeders, FeederDevice


async def main() -> None:
    address: str | None = None
    name = ""
    device_type = "standard"

    if len(sys.argv) > 1:
        address = sys.argv[1].strip().upper().replace("-", ":")
        if len(address) == 12 and ":" not in address:
            address = ":".join(address[i : i + 2] for i in range(0, 12, 2))
        print(f"Using MAC: {address!r} (no discovery)")
    else:
        print("Scanning for feeders (10s)...")
        feeders = await discover_feeders(timeout=10.0)
        if not feeders:
            print("No feeders found. Ensure Bluetooth is on and a feeder is in range.")
            print("Or pass a MAC:  uv run python examples/read_settings_and_sync_time.py E6:C0:07:09:A3:D3")
            return
        address, name, device_type = feeders[0]
        print(f"Using feeder: {address!r}  name={name!r}  type={device_type}")

    assert address

    feeder = FeederDevice(address, device_type=device_type)
    if not await feeder.connect():
        print("Failed to connect.")
        return

    try:
        # Read device info (name + firmware version)
        info = await feeder.get_device_info()
        print(f"Device: {info.get('device_name', '')!r}  firmware={info.get('device_version', '')!r}")

        # Read schedule (may be empty if not yet parsed from response)
        schedules = await feeder.query_schedule()
        print(f"Schedule entries: {len(schedules)}")
        for i, s in enumerate(schedules):
            print(f"  [{i}] {s}")

        # Sync device time to host
        await feeder.sync_time()
        print("Time synced to current host time.")
    finally:
        await feeder.disconnect()

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
