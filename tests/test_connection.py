"""Connection lifecycle tests against a fake BleakClient (no BLE hardware)."""

import asyncio
import logging

import pytest

from petnetizen_feeder import FeederDevice
from petnetizen_feeder import feeder as feeder_mod
from petnetizen_feeder.protocol import FEEDER_NOTIFY_UUID, FEEDER_WRITE_UUID

ADDRESS = "E6:C0:07:09:A3:D3"


class _Char:
    def __init__(self, uuid, properties):
        self.uuid = uuid
        self.properties = properties


class _Service:
    def get_characteristic(self, uuid):
        if uuid == FEEDER_WRITE_UUID:
            return _Char(uuid, ["write-without-response"])
        if uuid == FEEDER_NOTIFY_UUID:
            return _Char(uuid, ["notify"])
        return None


class _Services:
    def get_service(self, uuid):
        return _Service()


class FakeClient:
    """Mimics the feeder: answers verification and heartbeats, may drop early."""

    def __init__(
        self,
        *,
        drop_after=None,
        verify_reply=0x01,
        answer_heartbeat=True,
        push_after_verify=(),
        replies=None,
    ):
        self.drop_after = drop_after
        self.verify_reply = verify_reply
        self.answer_heartbeat = answer_heartbeat
        # Frames the feeder pushes unprompted after a successful verification.
        self.push_after_verify = list(push_after_verify)
        # Query replies: command byte -> reply frame.
        self.replies = dict(replies or {})
        self.disconnected_callback = None
        self.connected = True
        self.services = _Services()
        self.mtu_size = 23
        self.calls = []
        self.writes = []
        self._handler = None

    @property
    def is_connected(self):
        return self.connected

    def _notify(self, data):
        if self.connected and self._handler is not None:
            self._handler(None, bytearray(data))

    def drop(self):
        if not self.connected:
            return
        self.connected = False
        if self.disconnected_callback is not None:
            self.disconnected_callback(self)

    async def start_notify(self, char, handler):
        self.calls.append("start_notify")
        self._handler = handler
        if self.drop_after is not None:
            asyncio.get_running_loop().call_later(self.drop_after, self.drop)

    async def stop_notify(self, char):
        pass

    async def write_gatt_char(self, uuid, data, response=False):
        if not self.connected:
            raise RuntimeError("not connected")
        data = bytes(data)
        self.writes.append(data)
        cmd = data[1]
        if cmd == 0x06:
            self.calls.append("verify")
            if self.verify_reply is not None:
                reply = bytes([0xEB, 0x06, 0x01, self.verify_reply, 0x00, 0xAE])
                asyncio.get_running_loop().call_soon(self._notify, reply)
                for frame in self.push_after_verify:
                    asyncio.get_running_loop().call_soon(self._notify, frame)
        elif cmd in self.replies:
            asyncio.get_running_loop().call_soon(self._notify, self.replies[cmd])
        elif cmd == 0x03 and self.answer_heartbeat:
            asyncio.get_running_loop().call_soon(
                self._notify, bytes([0xEB, 0x03, 0x00, 0x00, 0xAE])
            )

    async def disconnect(self):
        self.connected = False


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch):
    monkeypatch.setattr(feeder_mod, "LINK_SETTLE_S", 0.2)
    monkeypatch.setattr(feeder_mod, "STALE_RELEASE_DELAY_S", 0.0)


def make_feeder(clients):
    """FeederDevice whose factory hands out the given FakeClients in order."""
    handed_out = []
    feeder = None

    async def factory():
        client = clients[len(handed_out)]
        client.disconnected_callback = feeder.disconnected_callback
        handed_out.append(client)
        return client

    feeder = FeederDevice(ADDRESS, device_type="standard", connection_factory=factory)
    return feeder, handed_out


def warnings_from(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_notifications_enabled_before_verification():
    client = FakeClient()
    feeder, _ = make_feeder([client])
    assert await feeder.connect()
    assert client.calls[:2] == ["start_notify", "verify"]
    await feeder.disconnect()


async def test_verification_reply_is_reported():
    feeder, _ = make_feeder([FakeClient(verify_reply=0x01)])
    proto = feeder._protocol
    assert await proto.connect(ble_client=FakeClient(), enable_notifications=True)
    assert await proto.send_verification_code("00000000") is True

    assert await proto.connect(
        ble_client=FakeClient(verify_reply=0x00), enable_notifications=True
    )
    assert await proto.send_verification_code("00000000") is False


async def test_missing_verification_reply_times_out(monkeypatch):
    from petnetizen_feeder import protocol as protocol_mod

    monkeypatch.setattr(protocol_mod, "VERIFICATION_REPLY_TIMEOUT_S", 0.05)
    feeder, _ = make_feeder([])
    proto = feeder._protocol
    assert await proto.connect(
        ble_client=FakeClient(verify_reply=None), enable_notifications=True
    )
    assert await proto.send_verification_code("00000000") is None


async def test_rejected_verification_fails_connect(caplog):
    feeder, handed_out = make_feeder([FakeClient(verify_reply=0x00) for _ in range(3)])
    assert not await feeder.connect()
    assert "verification code rejected" in caplog.text
    assert len(handed_out) == 1  # a rejected code is not retried
    assert feeder.verification_rejected
    assert feeder.last_error == "verification code rejected by feeder"


async def test_early_drop_is_retried_within_connect(caplog):
    caplog.set_level(logging.DEBUG, logger="petnetizen_feeder")
    first = FakeClient(drop_after=0.05)
    second = FakeClient()
    feeder, handed_out = make_feeder([first, second])

    assert await feeder.connect()
    assert handed_out == [first, second]
    assert feeder.is_connected
    assert not warnings_from(caplog)
    # The setup-time drop is logged with timing, but only at DEBUG.
    drops = [r for r in caplog.records if "dropped by feeder" in r.getMessage()]
    assert [r.levelno for r in drops] == [logging.DEBUG]
    await feeder.disconnect()


async def test_reconnect_failure_logs_one_warning(caplog):
    caplog.set_level(logging.DEBUG, logger="petnetizen_feeder")
    good = FakeClient()
    feeder, _ = make_feeder([good] + [FakeClient(drop_after=0.05) for _ in range(3)])
    assert await feeder.connect()

    good.drop()
    caplog.clear()
    assert not await feeder.ensure_connected()

    warnings = warnings_from(caplog)
    assert len(warnings) == 1
    assert "after 3 attempt(s)" in warnings[0].getMessage()
    assert "dropped the link during setup" in warnings[0].getMessage()


async def test_drop_of_established_link_logged_at_info(caplog):
    caplog.set_level(logging.DEBUG, logger="petnetizen_feeder")
    client = FakeClient()
    feeder, _ = make_feeder([client])
    assert await feeder.connect()

    client.drop()

    drops = [r for r in caplog.records if "dropped by feeder" in r.getMessage()]
    assert len(drops) == 1
    assert drops[0].levelno == logging.INFO
    assert "after connect" in drops[0].getMessage()


async def test_requested_disconnect_is_not_reported_as_drop(caplog):
    caplog.set_level(logging.DEBUG, logger="petnetizen_feeder")
    client = FakeClient()
    feeder, _ = make_feeder([client])
    assert await feeder.connect()
    await feeder.disconnect()
    client.drop()  # late callback from the backend
    assert "dropped by feeder" not in caplog.text


async def test_heartbeat_reply_is_not_echoed():
    client = FakeClient()
    feeder, _ = make_feeder([client])
    assert await feeder.connect()
    feeder._cancel_heartbeat()

    assert await feeder._protocol.send_heartbeat()
    await asyncio.sleep(0.05)
    heartbeats = [w for w in client.writes if w[1] == 0x03]
    assert len(heartbeats) == 1  # ours only, no echo of the feeder's reply
    await feeder.disconnect()


async def test_device_initiated_heartbeat_is_echoed():
    client = FakeClient(answer_heartbeat=False)
    feeder, _ = make_feeder([client])
    assert await feeder.connect()
    feeder._cancel_heartbeat()

    client._notify(bytes([0xEB, 0x03, 0x00, 0x00, 0xAE]))
    await asyncio.sleep(0.05)
    heartbeats = [w for w in client.writes if w[1] == 0x03]
    assert len(heartbeats) == 1
    await feeder.disconnect()


def frame(cmd, *data):
    """Feeder notification frame: EB cmd len data.. 00 AE."""
    return bytes([0xEB, cmd, len(data), *data, 0x00, 0xAE])


# What a Du-W11B pushes right after verification (captured 2026-10-25).
PUSHED_STATE = [
    frame(0x0E, 0x01),  # power: DC
    frame(0x09, 0x00),  # feeding status: idle
    frame(0x0A, 0x00),  # fault: none
    frame(0x0D, 0x01),  # child lock: on
    frame(0x0F, 0x01),  # LED on
    frame(0x10, 0x01),  # auto-lock on
    frame(0x12, 0x00),  # prompt sound off
]


async def test_pushed_state_is_served_without_queries():
    client = FakeClient(push_after_verify=PUSHED_STATE)
    feeder, _ = make_feeder([client])
    assert await feeder.connect()
    feeder._cancel_heartbeat()
    writes_after_connect = len(client.writes)

    assert await feeder.get_child_lock_status() is True
    assert await feeder.get_prompt_sound_status() is False
    assert await feeder.get_fault_status() == 0
    assert await feeder.get_feeding_status() == "idle"

    assert len(client.writes) == writes_after_connect  # nothing was queried
    await feeder.disconnect()


async def test_query_returns_as_soon_as_reply_arrives():
    client = FakeClient(replies={0x0D: frame(0x0D, 0x00)})
    feeder, _ = make_feeder([client])
    assert await feeder.connect()

    t = asyncio.get_running_loop().time()
    assert await feeder.get_child_lock_status() is False
    assert asyncio.get_running_loop().time() - t < 0.5  # not the 1.5 s timeout
    await feeder.disconnect()


async def test_setting_a_value_invalidates_its_cached_state():
    client = FakeClient(
        push_after_verify=PUSHED_STATE, replies={0x0D: frame(0x0D, 0x00)}
    )
    feeder, _ = make_feeder([client])
    assert await feeder.connect()
    assert await feeder.get_child_lock_status() is True  # pushed value

    await feeder.set_child_lock(False)
    # The set write made the pushed value stale, so this queries the feeder.
    assert await feeder.get_child_lock_status() is False
    await feeder.disconnect()


async def test_schedule_and_device_info_return_on_reply():
    schedule = frame(0x11, 0x7F, 8, 0, 2, 1)  # every day 08:00, 2 portions
    info = frame(0x00, *b"Du-W11B\0\0\0\0\0", *b"110")
    client = FakeClient(replies={0x11: schedule, 0x00: info})
    feeder, _ = make_feeder([client])
    assert await feeder.connect()

    t = asyncio.get_running_loop().time()
    slots = await feeder.query_schedule()
    details = await feeder.get_device_info()
    assert asyncio.get_running_loop().time() - t < 0.5  # not 4 s + 2 s

    assert slots == [
        {"weekdays": ["sun", "mon", "tue", "wed", "thu", "fri", "sat"],
         "time": "08:00", "portions": 2, "enabled": True}
    ]
    assert details == {"device_name": "Du-W11B", "device_version": "110"}
    await feeder.disconnect()


async def test_cached_state_is_not_reused_across_sessions():
    first = FakeClient(push_after_verify=PUSHED_STATE)
    second = FakeClient(replies={0x0D: frame(0x0D, 0x00)})
    feeder, _ = make_feeder([first, second])
    assert await feeder.connect()
    assert await feeder.get_child_lock_status() is True

    first.drop()
    assert await feeder.get_child_lock_status() is False  # reconnected, re-queried
    await feeder.disconnect()


async def test_unanswered_query_is_not_resent_in_the_same_session():
    client = FakeClient()  # never answers do-not-disturb (0x17)
    feeder, _ = make_feeder([client])
    assert await feeder.connect()

    assert await feeder.get_do_not_disturb() is None  # waits out the timeout once
    queries = [w for w in client.writes if w[1] == 0x17]
    t = asyncio.get_running_loop().time()
    assert await feeder.get_do_not_disturb() is None
    assert asyncio.get_running_loop().time() - t < 0.1
    assert [w for w in client.writes if w[1] == 0x17] == queries  # not re-sent
    await feeder.disconnect()


async def test_power_mode_is_served_from_pushed_state():
    client = FakeClient(push_after_verify=PUSHED_STATE)
    feeder, _ = make_feeder([client])
    assert await feeder.connect()
    writes = len(client.writes)
    assert await feeder.get_power_mode() == "DC Power"
    assert len(client.writes) == writes
    await feeder.disconnect()


# Real feed-record dumps from a Du-W11B (2026-10-25), newest record first.
MANUAL_DUMP = bytes.fromhex(
    "EB0C5A1A0A17110F230101001A0A151537170101001A0A1515371A0101001A050201261101"
    "01001A0502090E250101001A0502090E270101001A040A160A070101001A040A160A0E0101"
    "001A031909122B01010000AE"
)
PLAN_DUMP = bytes.fromhex(
    "EB0BB41A0A19090F020101001A0A04110F010101001A0A04150F050303001A0A05090F0201"
    "01001A0A050D0F030202001A0A05110F010101001A0A05150F050303001A0A06090F010101"
    "001A0A060D0F030202001A0A06110F010101001A0A06150F050303001A0717110F02010100"
    "1A0717150F050303001A0718090F020101001A07180D0F030202001A0718110F020101001A"
    "0718150F050303001A0719090F010101001A07190D0F030202001A0719110F0201010000AE"
)


async def test_last_feed_is_newest_record_across_manual_and_plan_reports():
    client = FakeClient(push_after_verify=[MANUAL_DUMP, PLAN_DUMP])
    feeder, _ = make_feeder([client])
    assert await feeder.connect()

    last = feeder.get_last_feed_result()
    # Not the oldest manual record (2026-03-25), but the newest of both lists.
    assert last == {
        "timestamp": "2026-10-25 09:15:02",
        "portions": 1,
        "requested": 1,
        "feed_type": "Plan",
        "status": "Success",
        "fault_code": 0,
    }

    # A later manual feed report replaces it; an older one does not.
    client._notify(frame(0x0C, 26, 10, 25, 22, 25, 42, 1, 1, 0))
    assert feeder.get_last_feed_result()["timestamp"] == "2026-10-25 22:25:42"
    client._notify(frame(0x0C, 26, 1, 1, 0, 0, 0, 1, 1, 0))
    assert feeder.get_last_feed_result()["timestamp"] == "2026-10-25 22:25:42"
    await feeder.disconnect()


async def test_record_portions_and_failure_follow_the_app_layout():
    feeder, _ = make_feeder([])
    decoded = feeder._protocol.decode_notification(
        frame(0x0B, 26, 10, 4, 21, 15, 5, 2, 3, 0x04)  # 2 of 3 dispensed, out of food
    )
    record = decoded["feed_records"][0]
    assert (record["portions"], record["requested"], record["status"]) == (2, 3, "Failed")
    assert record["fault_code"] == 0x04


async def test_feeding_status_codes_match_the_app():
    client = FakeClient(replies={0x09: frame(0x09, 0x02)})
    feeder, _ = make_feeder([client])
    assert await feeder.connect()
    assert await feeder.get_feeding_status() == "done"  # was misreported as "error"
    await feeder.disconnect()


async def test_led_and_auto_lock_come_from_pushed_state():
    client = FakeClient(push_after_verify=PUSHED_STATE)
    feeder, _ = make_feeder([client])
    assert await feeder.connect()
    writes = len(client.writes)
    assert await feeder.get_led_status() is True
    assert await feeder.get_auto_lock_status() is True
    assert len(client.writes) == writes
    await feeder.disconnect()


async def test_time_sync_uses_injected_clock():
    from datetime import datetime

    fixed = datetime(2026, 11, 9, 22, 40, 5)
    client = FakeClient()
    feeder, _ = make_feeder([client])
    feeder._protocol.clock = lambda: fixed  # what FeederDevice(clock=...) sets
    assert await feeder.connect()

    # Feeder-initiated request (EB 05 ...) is answered with the injected time.
    client._notify(frame(0x05, 26, 1, 1, 0, 0, 0))
    await asyncio.sleep(1.2)  # send_sync_time paces itself for 1 s after writing
    sync = [w for w in client.writes if w[1] == 0x05]
    assert sync and sync[-1][3:9] == bytes([26, 11, 9, 22, 40, 5])
    await feeder.disconnect()


def test_clock_is_passed_to_protocol():
    from datetime import datetime

    clock = lambda: datetime(2026, 1, 1)  # noqa: E731
    feeder = FeederDevice(ADDRESS, device_type="standard", clock=clock)
    assert feeder._protocol.clock is clock
