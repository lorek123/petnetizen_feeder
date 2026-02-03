#!/usr/bin/env python3
"""
POC: Connect to a Petnetizen feeder and dump raw schedule query response.

Use this to see exactly what the device sends for QUERY_FEEDER_PLAN (0x11).
Run from project root:

    uv run python examples/get_schedule_poc.py E6:C0:07:09:A3:D3

Or discover first (no args) and use the first feeder found.

Note: Only one BLE connection to the feeder at a time. If Home Assistant (or the
Pet Netizen BLE integration) is using the feeder, disconnect or disable that
integration first, or this script will get "Connect failed."
"""

import asyncio
import sys


def format_hex(data: bytearray) -> str:
    return " ".join(f"{b:02X}" for b in data)


def decode_command_11_payload(data_section: bytearray) -> list[dict]:
    """Try to parse QUERY_FEEDER_PLAN payload into slots. 5 bytes per slot: week, hour, min, portions, enabled."""
    slots = []
    # Try length-prefixed: first byte = count
    offset = 0
    if len(data_section) >= 1 and 1 <= data_section[0] <= 15:
        n = data_section[0]
        if len(data_section) >= 1 + 5 * n:
            offset = 1
    # Parse 5-byte slots
    week_bits = [(1, "sun"), (2, "mon"), (4, "tue"), (8, "wed"), (16, "thu"), (32, "fri"), (64, "sat")]
    while offset + 5 <= len(data_section):
        week_val, hour, minute, portions, enabled = data_section[offset : offset + 5]
        weekdays = [d for bit, d in week_bits if week_val & bit]
        slots.append({
            "weekdays": weekdays,
            "time": f"{hour:02d}:{minute:02d}",
            "portions": portions,
            "enabled": bool(enabled),
        })
        offset += 5
    return slots


async def main() -> None:
    from petnetizen_feeder import discover_feeders
    from petnetizen_feeder.protocol import (
        FeederBLEProtocol,
        CMD_QUERY_FEEDER_PLAN,
        DEFAULT_VERIFICATION_CODE,
    )

    address: str | None = None
    device_type = "standard"

    if len(sys.argv) > 1:
        address = sys.argv[1].strip().upper().replace("-", ":")
        if len(address) == 12 and ":" not in address:
            address = ":".join(address[i : i + 2] for i in range(0, 12, 2))
        print(f"MAC: {address}")
    else:
        print("Scanning 10s...")
        feeders = await discover_feeders(timeout=10.0)
        if not feeders:
            print("No feeders found. Pass MAC:  uv run python examples/get_schedule_poc.py E6:C0:07:09:A3:D3")
            return
        address, name, device_type = feeders[0]
        print(f"Using: {address}  name={name!r}  type={device_type}")

    protocol = FeederBLEProtocol(address, device_type=device_type)
    if not await protocol.connect():
        print("Connect failed.")
        return

    try:
        # Verification
        await protocol.send_verification_code(DEFAULT_VERIFICATION_CODE)
        await asyncio.sleep(1.0)

        # Clear previous notifications, send query, wait
        before = len(protocol.received_data)
        cmd = protocol.encode_command(CMD_QUERY_FEEDER_PLAN, length=0)
        print(f"\n>>> Send QUERY_FEEDER_PLAN: {format_hex(cmd)}")
        await protocol.client.write_gatt_char(protocol.write_uuid, cmd, response=False)
        await asyncio.sleep(4.0)

        new_count = len(protocol.received_data) - before
        print(f"\n<<< Received {new_count} notification(s) after query\n")

        for i, data in enumerate(protocol.received_data[before:]):
            raw = format_hex(data)
            print(f"--- Notification #{i+1} (len={len(data)}) ---")
            print(f"  Raw: {raw}")

            if len(data) < 4:
                print("  (too short to decode)")
                continue

            cmd_byte = data[1]
            cmd_hex = f"{cmd_byte:02X}"
            length_byte = data[2]
            # Payload: data[3:-2] (between length and CRC)
            data_section = data[3:-2] if len(data) > 5 else data[3:]
            data_hex = format_hex(data_section)

            print(f"  Command: 0x{cmd_hex}  length_byte={length_byte}  payload_len={len(data_section)}")
            print(f"  Payload hex: {data_hex}")

            if cmd_hex == "11":
                print("  ^^^ QUERY_FEEDER_PLAN response ^^^")
                slots = decode_command_11_payload(data_section)
                if slots:
                    print(f"  Parsed {len(slots)} slot(s):")
                    for j, s in enumerate(slots):
                        print(f"    [{j}] {s}")
                else:
                    print("  Parsed 0 slots. Payload bytes:", list(data_section))
            print()
    finally:
        await protocol.disconnect()

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
