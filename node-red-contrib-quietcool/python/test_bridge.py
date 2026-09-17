import unittest
from unittest.mock import AsyncMock, call, patch

import bridge
from bleak.exc import BleakDeviceNotFoundError


class AdapterLeasePathTests(unittest.TestCase):
    def test_falls_back_when_xdg_runtime_directory_is_inaccessible(self):
        with (
            patch.dict(bridge.os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}, clear=False),
            patch.object(bridge.tempfile, "gettempdir", return_value="/tmp"),
            patch.object(bridge.os, "getuid", return_value=1001),
            patch.object(
                bridge.os,
                "makedirs",
                side_effect=[PermissionError("denied"), None],
            ) as makedirs,
        ):
            lease_path = bridge.adapter_lease_path("hci0")

        self.assertEqual(lease_path, "/tmp/bldgblocks-ble-1001/bldgblocks-ble-hci0.lease")
        self.assertEqual(
            makedirs.call_args_list,
            [
                call("/run/user/1000", mode=0o700, exist_ok=True),
                call("/tmp/bldgblocks-ble-1001", mode=0o700, exist_ok=True),
            ],
        )


class FanBridgeConnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_device_after_transient_disconnect(self):
        targets = []
        discovered_device = object()

        class FakeClient:
            def __init__(self, target, **kwargs):
                self.is_connected = False
                targets.append(target)

            async def connect(self):
                self.is_connected = True

            async def start_notify(self, characteristic, callback):
                if len(targets) == 1:
                    self.is_connected = False

            async def disconnect(self):
                self.is_connected = False

        fan = bridge.FanBridge("10:06:1C:42:52:F6", "phone-id", "hci0")

        async def send(api, **kwargs):
            if not fan.client or not fan.client.is_connected:
                raise ConnectionError("Not connected")
            return {"Result": "Success"}

        fan._send = send

        with (
            patch.object(bridge, "BleakClient", FakeClient),
            patch.object(
                bridge.BleakScanner,
                "find_device_by_address",
                AsyncMock(return_value=discovered_device),
            ) as find_device,
            patch.object(bridge.asyncio, "sleep", AsyncMock()),
            patch.object(bridge, "emit_status"),
        ):
            result = await fan.connect(max_retries=2)

        self.assertTrue(result["connected"])
        self.assertIs(targets[1], discovered_device)
        find_device.assert_awaited_once_with(
            fan.address,
            timeout=10.0,
            bluez={"adapter": "hci0"},
        )

    async def test_discovers_device_after_bluez_path_is_evicted(self):
        targets = []
        discovered_device = object()

        class FakeClient:
            def __init__(self, target, **kwargs):
                self.target = target
                self.is_connected = False
                targets.append(target)

            async def connect(self):
                if len(targets) == 1:
                    raise BleakDeviceNotFoundError(
                        "dev_10_06_1C_42_52_F6",
                        "device 'dev_10_06_1C_42_52_F6' not found",
                    )
                self.is_connected = True

            async def start_notify(self, characteristic, callback):
                return None

            async def disconnect(self):
                self.is_connected = False

        fan = bridge.FanBridge("10:06:1C:42:52:F6", "phone-id", "hci0")
        fan._send = AsyncMock(return_value={"Result": "Success"})

        with (
            patch.object(bridge, "BleakClient", FakeClient),
            patch.object(
                bridge.BleakScanner,
                "find_device_by_address",
                AsyncMock(return_value=discovered_device),
            ) as find_device,
            patch.object(bridge.asyncio, "sleep", AsyncMock()),
            patch.object(bridge, "emit_status"),
        ):
            result = await fan.connect(max_retries=2)

        self.assertTrue(result["connected"])
        self.assertIsNot(targets[0], discovered_device)
        self.assertIs(targets[1], discovered_device)
        find_device.assert_awaited_once_with(
            fan.address,
            timeout=10.0,
            bluez={"adapter": "hci0"},
        )


class FanBridgeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_raises_when_notification_times_out(self):
        client = AsyncMock()
        client.is_connected = True
        fan = bridge.FanBridge("10:06:1C:42:52:F6", "phone-id", "hci0")
        fan.client = client

        with self.assertRaisesRegex(TimeoutError, "GetWorkState response timed out"):
            await fan._send("GetWorkState", timeout=0)

    async def test_reconnects_and_retries_after_response_timeout(self):
        fan = bridge.FanBridge("10:06:1C:42:52:F6", "phone-id", "hci0")
        fan.ensure_connected = AsyncMock()
        fan._send = AsyncMock(
            side_effect=[TimeoutError("response timed out"), {"Mode": "Idle"}]
        )
        fan._cleanup_stale_connection = AsyncMock()
        fan.connect = AsyncMock()

        with patch.object(bridge.asyncio, "sleep", AsyncMock()):
            result = await fan.reconnect_and_send("GetWorkState")

        self.assertEqual(result, {"Mode": "Idle"})
        self.assertEqual(fan._send.await_count, 2)
        fan.connect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()