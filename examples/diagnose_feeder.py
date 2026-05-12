#!/usr/bin/env python3
"""
Feeder connection diagnostic.

Usage:
    uv run python examples/diagnose_feeder.py                  # auto-discover
    uv run python examples/diagnose_feeder.py E6:C0:07:09:A3:D3
    uv run python examples/diagnose_feeder.py E6:C0:07:09:A3:D3 --feed

Runs through each connection stage and reports PASS/FAIL with timing.
All BLE notifications received are printed raw so nothing is hidden.

Intended to be run with a local Bluetooth adapter (not via ESP32 proxy).
If the feeder works here but not in HA, the issue is proxy-specific.
"""

import asyncio
import logging
import sys
import time
from datetime import datetime

from petnetizen_feeder import FeederDevice, discover_feeders
from petnetizen_feeder.protocol import (
    DEFAULT_VERIFICATION_CODE,
    FeederBLEProtocol,
    CMD_QUERY_NAME_VERSION,
    CMD_QUERY_FEEDER_PLAN,
    CMD_SET_FAMILY_ID,
    CMD_FEEDING_STATUS,
    CMD_FAULT,
    CMD_CHILD_LOCK,
    CMD_REMINDER_TONE,
)
from bleak import BleakClient

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
_LOGGER = logging.getLogger("diagnose")

# Silence noisy bleak internals a bit — keep protocol and feeder at DEBUG
logging.getLogger("bleak").setLevel(logging.INFO)
logging.getLogger("petnetizen_feeder").setLevel(logging.DEBUG)


def _fmt(data: bytearray) -> str:
    return " ".join(f"{b:02X}" for b in data)


# ── result tracker ────────────────────────────────────────────────────────────

class Stage:
    def __init__(self, name: str):
        self.name = name
        self._ok: bool | None = None
        self._detail = ""
        self._t0 = time.monotonic()

    def ok(self, detail: str = "") -> "Stage":
        self._ok = True
        self._detail = detail
        elapsed = time.monotonic() - self._t0
        print(f"  PASS  {self.name}  ({elapsed:.2f}s){f'  — {detail}' if detail else ''}")
        return self

    def fail(self, detail: str = "") -> "Stage":
        self._ok = False
        self._detail = detail
        elapsed = time.monotonic() - self._t0
        print(f"  FAIL  {self.name}  ({elapsed:.2f}s){f'  — {detail}' if detail else ''}")
        return self


stages: list[Stage] = []


def stage(name: str) -> Stage:
    print(f"\n── {name}")
    s = Stage(name)
    stages.append(s)
    return s


# ── raw notification logger ──────────────────────────────────────────────────

_raw_notifications: list[tuple[float, bytearray]] = []


def _make_raw_handler(proto: FeederBLEProtocol):
    orig = proto.notification_handler

    def _handler(sender, data: bytearray):
        _raw_notifications.append((time.monotonic(), bytearray(data)))
        print(f"         << {_fmt(data)}  (cmd=0x{data[1]:02X} len={len(data)})")
        orig(sender, data)

    return _handler


# ── main ─────────────────────────────────────────────────────────────────────

async def main() -> int:
    address: str | None = None
    do_feed = "--feed" in sys.argv

    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    if positional:
        raw = positional[0].strip().upper().replace("-", ":")
        if len(raw) == 12 and ":" not in raw:
            raw = ":".join(raw[i : i + 2] for i in range(0, 12, 2))
        address = raw

    # ── 1. Discovery ─────────────────────────────────────────────────────────
    s = stage("Discovery")
    if address:
        s.ok(f"address supplied: {address}")
    else:
        print("  scanning 10 s …")
        feeders = await discover_feeders(timeout=10.0)
        if not feeders:
            s.fail("no feeders found — pass MAC as argument")
            return 1
        address, name, dev_type = feeders[0]
        s.ok(f"found {address}  name={name!r}  type={dev_type}")

    # ── 2. Build protocol object ──────────────────────────────────────────────
    proto = FeederBLEProtocol(address)
    print(f"  device_type={proto.device_type}  service={proto.service_uuid}")

    # ── 3. GATT connect (no notifications yet) ───────────────────────────────
    s = stage("GATT connect (enable_notifications=False)")
    t0 = time.monotonic()
    ok = await proto.connect(enable_notifications=False)
    if not ok:
        s.fail("connect() returned False — device unreachable or wrong UUIDs")
        return 1
    s.ok(f"connected  mtu={getattr(proto.client, 'mtu_size', '?')}")

    # Swap in the raw notification logger
    proto.notification_handler = _make_raw_handler(proto)

    # ── 4. Send verification code ─────────────────────────────────────────────
    s = stage(f"Verification code ({DEFAULT_VERIFICATION_CODE})")
    t0 = time.monotonic()
    before = len(proto.received_data)
    await proto.send_verification_code(DEFAULT_VERIFICATION_CODE)
    after_count = len(proto.received_data) - before
    elapsed = time.monotonic() - t0
    # Look for CMD_SET_FAMILY_ID (06) response
    verif_ok = any(
        len(d) >= 2 and f"{d[1]:02X}" == "06"
        for d in proto.received_data[before:]
    )
    note = f"{after_count} notification(s) received"
    if verif_ok:
        s.ok(f"device ack'd verification code — {note}")
    else:
        # Not a hard failure — device may not ack at all
        s.ok(f"no explicit ack (may be normal) — {note}  elapsed={elapsed:.2f}s")

    # ── 5. Enable notifications ───────────────────────────────────────────────
    s = stage("start_notify (enable notifications)")
    before = len(proto.received_data)
    ok = await proto.enable_notifications()
    if not ok:
        s.fail("start_notify failed — this is the error seen in HA logs via ESP32 proxy")
        print("  → If direct BLE fails here too, the protocol/firmware is the issue.")
        print("  → If only HA/proxy fails, the proxy's NimBLE stack is causing HCI error 19.")
        await proto.disconnect()
        return 1
    after_count = len(proto.received_data) - before
    s.ok(f"notifications enabled  (got {after_count} unsolicited notifications)")

    # ── 6. Device info ────────────────────────────────────────────────────────
    s = stage("Device info (CMD 0x00)")
    before = len(proto.received_data)
    cmd = proto.encode_command(CMD_QUERY_NAME_VERSION, length=0)
    print(f"  >> {_fmt(cmd)}")
    await proto.client.write_gatt_char(proto.write_uuid, cmd, response=False)
    await asyncio.sleep(2.0)
    info_notifications = [
        proto.decode_notification(d)
        for d in proto.received_data[before:]
        if len(d) >= 2 and f"{d[1]:02X}" == "00"
    ]
    if info_notifications:
        n = info_notifications[0]
        s.ok(f"name={n.get('device_name', '?')!r}  version={n.get('device_version', '?')!r}")
    else:
        s.fail("no CMD_00 response within 2 s")

    # ── 7. Fault status ───────────────────────────────────────────────────────
    s = stage("Fault status (CMD 0x0A)")
    before = len(proto.received_data)
    await proto.query_fault()
    resp = next(
        (proto.decode_notification(d) for d in proto.received_data[before:] if len(d) >= 2 and f"{d[1]:02X}" == "0A"),
        None,
    )
    if resp:
        s.ok(f"fault_code={resp.get('fault_code')}")
    else:
        s.fail("no response")

    # ── 8. Feeding status ─────────────────────────────────────────────────────
    s = stage("Feeding status (CMD 0x09)")
    before = len(proto.received_data)
    await proto.query_feeding_status()
    resp = next(
        (proto.decode_notification(d) for d in proto.received_data[before:] if len(d) >= 2 and f"{d[1]:02X}" == "09"),
        None,
    )
    if resp:
        s.ok(f"status={resp.get('feeding_status_text')}")
    else:
        s.fail("no response")

    # ── 9. Child lock ─────────────────────────────────────────────────────────
    s = stage("Child lock (CMD 0x0D)")
    before = len(proto.received_data)
    await proto.query_child_lock()
    resp = next(
        (proto.decode_notification(d) for d in proto.received_data[before:] if len(d) >= 2 and f"{d[1]:02X}" == "0D"),
        None,
    )
    if resp:
        s.ok(f"child_lock={resp.get('child_lock_text')}")
    else:
        s.fail("no response")

    # ── 10. Prompt sound ─────────────────────────────────────────────────────
    s = stage("Prompt sound (CMD 0x12)")
    before = len(proto.received_data)
    await proto.query_reminder_tone()
    resp = next(
        (proto.decode_notification(d) for d in proto.received_data[before:] if len(d) >= 2 and f"{d[1]:02X}" == "12"),
        None,
    )
    if resp:
        s.ok(f"prompt_sound={resp.get('prompt_sound_text')}")
    else:
        s.fail("no response")

    # ── 11. Schedule query ────────────────────────────────────────────────────
    s = stage("Schedule query (CMD 0x11)")
    before = len(proto.received_data)
    cmd = proto.encode_command(CMD_QUERY_FEEDER_PLAN, length=0)
    print(f"  >> {_fmt(cmd)}")
    await proto.client.write_gatt_char(proto.write_uuid, cmd, response=False)
    await asyncio.sleep(4.0)
    plan_notifications = [
        proto.decode_notification(d)
        for d in proto.received_data[before:]
        if len(d) >= 2 and f"{d[1]:02X}" == "11"
    ]
    if plan_notifications:
        slots = plan_notifications[0].get("feed_plan_slots") or []
        if slots:
            s.ok(f"{len(slots)} slot(s)")
            for i, sl in enumerate(slots):
                print(f"    [{i}] {sl['time']}  days={sl['weekdays']}  portions={sl['portions']}  enabled={sl['enabled']}")
        else:
            s.ok("CMD 0x11 received but 0 parseable slots (check raw data above)")
    else:
        s.fail("no CMD_11 response within 4 s")

    # ── 12. Optional feed ────────────────────────────────────────────────────
    if do_feed:
        s = stage("Feed (1 portion)")
        feeder = FeederDevice(address)
        feeder._protocol = proto
        feeder._connected = True
        result = await feeder.feed(portions=1, fast=True)
        if result:
            s.ok("feed triggered / completed")
        else:
            s.fail("no feed ack within 10 s")

    # ── disconnect ────────────────────────────────────────────────────────────
    try:
        await proto.disconnect()
    except Exception:
        pass  # feeder may have already disconnected (normal after feed)

    # ── summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for s in stages if s._ok)
    failed = sum(1 for s in stages if s._ok is False)
    for s in stages:
        icon = "✓" if s._ok else "✗" if s._ok is False else "?"
        print(f"  {icon}  {s.name}")
    print(f"\n  {passed}/{len(stages)} passed   {failed} failed")
    print(f"  Total notifications received: {len(_raw_notifications)}")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
