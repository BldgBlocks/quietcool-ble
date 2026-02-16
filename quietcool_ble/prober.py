"""
QuietCool BLE Protocol Discovery - Systematically probe the fan's command interface.

Strategy:
  1. Connect and read initial state
  2. Subscribe to notifications
  3. Try common BLE fan command patterns
  4. Log everything
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from bleak import BleakClient

ADDR = "XX:XX:XX:XX:XX:XX"
CHAR = "0000ff01-0000-1000-8000-00805f9b34fb"


class ProtocolProber:
    def __init__(self, address: str):
        self.address = address
        self.client = None
        self.log_file = f"captures/probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        self.notifications = []
        os.makedirs("captures", exist_ok=True)

    def _log(self, entry):
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
        self.notifications.append(entry)

    def _on_notify(self, char, data):
        ts = datetime.now().isoformat()
        entry = {
            "ts": ts,
            "dir": "NOTIFY",
            "hex": data.hex(),
            "bytes": list(data),
            "len": len(data),
        }
        self._log(entry)
        print(f"    <<< NOTIFY: {data.hex()}  bytes={list(data)}  len={len(data)}")
        try:
            ascii_str = data.decode("ascii", errors="replace")
            if any(c.isalnum() for c in ascii_str):
                print(f"    <<< ASCII:  {ascii_str}")
        except:
            pass

    async def connect(self):
        print(f"Connecting to {self.address}...")
        self.client = BleakClient(self.address, timeout=15.0)
        await self.client.connect()
        print(f"Connected! MTU={self.client.mtu_size}")
        await self.client.start_notify(CHAR, self._on_notify)
        print("Subscribed to notifications.\n")

    async def disconnect(self):
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(CHAR)
            except:
                pass
            await self.client.disconnect()
        print("Disconnected.")

    async def read(self, label=""):
        val = await self.client.read_gatt_char(CHAR)
        entry = {"ts": datetime.now().isoformat(), "dir": "READ", "hex": val.hex(), "bytes": list(val), "label": label}
        self._log(entry)
        print(f"  READ ({label}): {val.hex()}  bytes={list(val)}")
        return val

    async def write(self, data: bytes, label: str = "", with_response: bool = True):
        """Write and wait for response."""
        self.notifications.clear()
        entry = {
            "ts": datetime.now().isoformat(),
            "dir": "WRITE",
            "hex": data.hex(),
            "bytes": list(data),
            "label": label,
            "with_response": with_response,
        }
        self._log(entry)
        print(f"\n  >>> WRITE ({label}): {data.hex()}  bytes={list(data)}  resp={with_response}")

        try:
            await self.client.write_gatt_char(CHAR, data, response=with_response)
            print(f"  >>> Write OK")
        except Exception as e:
            print(f"  >>> Write ERROR: {e}")
            entry["error"] = str(e)
            self._log(entry)

        # Wait for notifications
        await asyncio.sleep(1.0)

        # Also read the characteristic after writing
        try:
            await self.read(label=f"after_{label}")
        except Exception as e:
            print(f"  Read after write error: {e}")

    async def probe(self):
        """Run systematic command probes."""
        await self.connect()

        print("=" * 70)
        print("PHASE 1: Initial state read")
        print("=" * 70)
        await self.read("initial")
        await asyncio.sleep(0.5)

        print("\n" + "=" * 70)
        print("PHASE 2: Try writing the idle value back (echo test)")
        print("=" * 70)
        await self.write(bytes([0xDE, 0xED, 0xBE, 0xEF]), "echo_deedbeef")

        print("\n" + "=" * 70)
        print("PHASE 3: Common single-byte commands")
        print("=" * 70)
        # Try common on/off/status bytes
        for val, label in [
            (b'\x00', "zero"),
            (b'\x01', "one_on?"),
            (b'\x02', "two"),
            (b'\x03', "three"),
            (b'\xff', "ff"),
            (b'\x0a', "0a_newline"),
            (b'\x10', "0x10"),
        ]:
            await self.write(val, label)
            await asyncio.sleep(0.3)

        print("\n" + "=" * 70)
        print("PHASE 4: Common multi-byte patterns")
        print("=" * 70)
        patterns = [
            (b'\x01\x01', "on_cmd_1"),
            (b'\x01\x00', "off_cmd_1"),
            (b'\x00\x01', "status_query?"),
            (b'\x00\x00', "zeros"),
            (b'\x01\x01\x01', "on_3byte"),
            (b'\x01\x00\x00', "off_3byte"),
            (b'\xaa', "0xAA"),
            (b'\x55', "0x55"),
            (b'\xaa\x55', "aa55_sync"),
            (b'\x55\xaa', "55aa_sync"),
        ]
        for data, label in patterns:
            await self.write(data, label)
            await asyncio.sleep(0.3)

        print("\n" + "=" * 70)
        print("PHASE 5: ASCII text commands")
        print("=" * 70)
        text_cmds = [
            ("ON\r\n", "ascii_ON"),
            ("OFF\r\n", "ascii_OFF"),
            ("STATUS\r\n", "ascii_STATUS"),
            ("AT\r\n", "ascii_AT"),
            ("?\r\n", "ascii_query"),
            ("on", "ascii_on_lower"),
            ("status", "ascii_status_lower"),
        ]
        for text, label in text_cmds:
            await self.write(text.encode(), label)
            await asyncio.sleep(0.3)

        print("\n" + "=" * 70)
        print("PHASE 6: Try write-without-response mode")
        print("=" * 70)
        for val, label in [
            (b'\x01', "wr_no_resp_01"),
            (b'\x01\x01', "wr_no_resp_0101"),
            (b'\x00', "wr_no_resp_00"),
        ]:
            await self.write(val, label, with_response=False)
            await asyncio.sleep(0.3)

        print("\n" + "=" * 70)
        print("PHASE 7: Final state read")
        print("=" * 70)
        await self.read("final")

        await self.disconnect()

        print(f"\n\nProbe complete! Log: {self.log_file}")
        print(f"Total notifications received: {sum(1 for e in self.notifications if e.get('dir') == 'NOTIFY')}")


async def main():
    prober = ProtocolProber(ADDR)
    try:
        await prober.probe()
    except KeyboardInterrupt:
        await prober.disconnect()
    except Exception as e:
        print(f"\nError: {e}")
        await prober.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
