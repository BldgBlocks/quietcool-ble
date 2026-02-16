"""
BLE Monitor/Sniffer - Connect and listen to ALL notifications from a device.

This subscribes to every notifiable characteristic and logs all data received.
Use this while sending commands from the QuietCool app to capture the protocol.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic


class BLEMonitor:
    """Monitors all BLE notifications from a device and logs them."""

    def __init__(self, address: str, log_dir: str = "captures"):
        self.address = address
        self.log_dir = log_dir
        self.session_start = datetime.now()
        self.session_id = self.session_start.strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(log_dir, f"capture_{self.session_id}.jsonl")
        self.notifications = []
        self._running = True

    def _notification_handler(self, char: BleakGATTCharacteristic, data: bytearray):
        """Handle incoming BLE notifications."""
        timestamp = datetime.now().isoformat()
        entry = {
            "timestamp": timestamp,
            "direction": "notify",
            "characteristic_uuid": char.uuid,
            "characteristic_handle": f"0x{char.handle:04X}",
            "characteristic_desc": char.description or "(unknown)",
            "data_hex": data.hex(),
            "data_bytes": list(data),
            "data_len": len(data),
        }

        # Try UTF-8 decode
        try:
            entry["data_utf8"] = data.decode("utf-8", errors="replace")
        except Exception:
            pass

        self.notifications.append(entry)

        # Print live
        print(f"\n[{timestamp}] NOTIFY from {char.uuid}")
        print(f"  Handle: 0x{char.handle:04X}  Desc: {char.description}")
        print(f"  Hex:    {data.hex()}")
        print(f"  Bytes:  {list(data)}")
        print(f"  Len:    {len(data)}")
        if "data_utf8" in entry:
            printable = entry["data_utf8"]
            if any(c.isprintable() and c != '\x00' for c in printable):
                print(f"  UTF-8:  {printable}")

        # Append to log file
        self._append_log(entry)

    def _append_log(self, entry: dict):
        """Append a log entry to the JSONL capture file."""
        os.makedirs(self.log_dir, exist_ok=True)
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    async def monitor(self, duration: float = 0, also_read: bool = True):
        """
        Connect to device and monitor all notifications.

        Args:
            duration: How long to monitor (0 = until Ctrl+C).
            also_read: If True, do an initial read of all readable characteristics.
        """
        print(f"Connecting to {self.address}...")
        print(f"Capture log: {self.log_file}")
        print()

        async with BleakClient(self.address, timeout=20.0) as client:
            if not client.is_connected:
                print("Failed to connect!")
                return

            print(f"Connected! MTU: {client.mtu_size}")
            services = client.services

            # Initial read of all readable characteristics
            if also_read:
                print("\n--- Initial Characteristic Values ---")
                for service in services:
                    for char in service.characteristics:
                        if "read" in char.properties:
                            try:
                                value = await client.read_gatt_char(char)
                                entry = {
                                    "timestamp": datetime.now().isoformat(),
                                    "direction": "read",
                                    "characteristic_uuid": char.uuid,
                                    "characteristic_handle": f"0x{char.handle:04X}",
                                    "characteristic_desc": char.description or "(unknown)",
                                    "data_hex": value.hex(),
                                    "data_bytes": list(value),
                                    "data_len": len(value),
                                }
                                self._append_log(entry)
                                print(f"  {char.uuid}: {value.hex()}  ({list(value)})")
                            except Exception as e:
                                print(f"  {char.uuid}: <read error: {e}>")

            # Subscribe to ALL notifiable characteristics
            notify_chars = []
            for service in services:
                for char in service.characteristics:
                    if "notify" in char.properties or "indicate" in char.properties:
                        notify_chars.append(char)

            if not notify_chars:
                print("\nNo notifiable characteristics found!")
                print("The device may use a different communication pattern.")
                print("Try using the 'explore' command to examine the GATT profile.")
                return

            print(f"\nSubscribing to {len(notify_chars)} notification characteristic(s)...")
            for char in notify_chars:
                try:
                    await client.start_notify(char, self._notification_handler)
                    print(f"  Subscribed: {char.uuid} ({char.description})")
                except Exception as e:
                    print(f"  Failed:     {char.uuid} - {e}")

            print("\n" + "=" * 80)
            print("MONITORING - Now use QuietCool app to send commands!")
            print("Press Ctrl+C to stop.")
            print("=" * 80)

            try:
                if duration > 0:
                    await asyncio.sleep(duration)
                else:
                    while self._running:
                        await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                pass

            # Unsubscribe
            for char in notify_chars:
                try:
                    await client.stop_notify(char)
                except Exception:
                    pass

        print(f"\n\nSession complete. Captured {len(self.notifications)} notifications.")
        print(f"Log saved to: {self.log_file}")


async def monitor_and_poll(address: str, duration: float = 0):
    """
    Alternative monitoring mode: poll readable characteristics periodically.

    Some BLE devices don't use notifications - they expect the client to poll.
    This function reads all readable characteristics every second and logs changes.
    """
    print(f"Connecting to {address} (polling mode)...")

    async with BleakClient(address, timeout=20.0) as client:
        if not client.is_connected:
            print("Failed to connect!")
            return

        print(f"Connected! MTU: {client.mtu_size}")

        # Build list of readable characteristics
        readable = []
        for service in client.services:
            for char in service.characteristics:
                if "read" in char.properties:
                    readable.append(char)

        print(f"Found {len(readable)} readable characteristics. Polling...")
        print("Press Ctrl+C to stop.\n")

        last_values = {}
        log_dir = "captures"
        os.makedirs(log_dir, exist_ok=True)
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(log_dir, f"poll_{session_id}.jsonl")

        try:
            while True:
                for char in readable:
                    try:
                        value = await client.read_gatt_char(char)
                        hex_val = value.hex()
                        prev = last_values.get(char.uuid)

                        if prev != hex_val:
                            timestamp = datetime.now().isoformat()
                            change = "CHANGED" if prev is not None else "INITIAL"
                            print(f"[{timestamp}] {change} {char.uuid}: {hex_val}  ({list(value)})")
                            if prev is not None:
                                print(f"  Was: {prev}")

                            entry = {
                                "timestamp": timestamp,
                                "direction": "poll",
                                "change": change.lower(),
                                "characteristic_uuid": char.uuid,
                                "characteristic_handle": f"0x{char.handle:04X}",
                                "data_hex": hex_val,
                                "data_bytes": list(value),
                                "prev_hex": prev,
                            }
                            with open(log_file, "a") as f:
                                f.write(json.dumps(entry) + "\n")

                            last_values[char.uuid] = hex_val
                    except Exception:
                        pass

                await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            pass

        print(f"\nPoll log saved to: {log_file}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor BLE device notifications")
    parser.add_argument("address", help="BLE device address (e.g., AA:BB:CC:DD:EE:FF)")
    parser.add_argument("-d", "--duration", type=float, default=0,
                        help="Monitor duration in seconds (0 = until Ctrl+C)")
    parser.add_argument("--no-read", action="store_true",
                        help="Skip initial read of characteristics")
    parser.add_argument("--poll", action="store_true",
                        help="Use polling mode instead of notifications")
    args = parser.parse_args()

    if args.poll:
        asyncio.run(monitor_and_poll(address=args.address, duration=args.duration))
    else:
        mon = BLEMonitor(args.address)
        asyncio.run(mon.monitor(duration=args.duration, also_read=not args.no_read))


if __name__ == "__main__":
    main()
