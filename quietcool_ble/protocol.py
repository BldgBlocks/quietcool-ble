"""
BLE MITM Proxy - Act as a relay between the QuietCool app and the fan.

The Pi advertises as a clone of the fan. When the app connects to the Pi,
the Pi connects to the real fan and relays all traffic, logging everything.

Architecture:
  Phone App  <--BLE-->  Pi (GATT Server)  <--BLE-->  Real Fan (GATT Client)

This lets us capture the full bidirectional protocol.
"""

import asyncio
import json
import os
import sys
from datetime import datetime

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.characteristic import BleakGATTCharacteristic
except ImportError:
    print("bleak is required: pip install bleak")
    sys.exit(1)

# The fan's single vendor-specific characteristic
FAN_SERVICE_UUID = "000000ff-0000-1000-8000-00805f9b34fb"
FAN_CHAR_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
FAN_ADDRESS = "XX:XX:XX:XX:XX:XX"


class ProtocolLogger:
    """Logs all BLE protocol exchanges to a JSONL file."""

    def __init__(self, log_dir="captures"):
        self.log_dir = log_dir
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(log_dir, f"protocol_{self.session_id}.jsonl")
        os.makedirs(log_dir, exist_ok=True)
        self.entries = []

    def log(self, direction: str, data: bytes, char_uuid: str = FAN_CHAR_UUID,
            label: str = "", context: str = ""):
        """Log a protocol exchange."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "direction": direction,
            "characteristic": char_uuid,
            "data_hex": data.hex(),
            "data_bytes": list(data),
            "data_len": len(data),
            "label": label,
            "context": context,
        }
        # Try ASCII decode
        try:
            text = data.decode("ascii", errors="replace")
            if any(c.isprintable() for c in text):
                entry["data_ascii"] = text
        except Exception:
            pass

        self.entries.append(entry)
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

        # Pretty print
        arrow = "->" if "write" in direction or "cmd" in direction else "<-"
        print(f"[{entry['timestamp']}] {arrow} {direction:20s} {data.hex():<40s} len={len(data)}")
        if entry.get("data_ascii"):
            printable = "".join(c if c.isprintable() else "." for c in entry["data_ascii"])
            print(f"{'':>25s} ASCII: {printable}")

        return entry


async def passive_monitor(address: str = FAN_ADDRESS, duration: float = 0):
    """
    Connect to fan and passively monitor notifications.

    NOTE: While we're connected, the phone app CANNOT connect.
    Use this for initial exploration or when you don't need the app.

    For simultaneous app + monitoring, we need the MITM proxy approach.
    """
    logger = ProtocolLogger()
    print(f"Passive monitor - connecting to {address}")
    print(f"Log file: {logger.log_file}")
    print(f"WARNING: Phone app cannot connect while Pi is connected!")
    print()

    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            print(f"Connection attempt {attempt}/{max_retries}...")
            async with BleakClient(address, timeout=30.0) as client:
                print(f"Connected! MTU: {client.mtu_size}")

                # Read initial value
                try:
                    value = await client.read_gatt_char(FAN_CHAR_UUID)
                    logger.log("initial_read", bytes(value))
                except Exception as e:
                    print(f"  Initial read failed: {e}")

                # Subscribe to notifications
                def on_notify(char: BleakGATTCharacteristic, data: bytearray):
                    logger.log("fan_notify", bytes(data), char.uuid)

                await client.start_notify(FAN_CHAR_UUID, on_notify)
                print(f"Subscribed to notifications on {FAN_CHAR_UUID}")
                print("=" * 80)
                print("LISTENING - Ctrl+C to stop")
                print("=" * 80)

                try:
                    if duration > 0:
                        await asyncio.sleep(duration)
                    else:
                        while True:
                            await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    pass

                return  # Clean exit

        except Exception as e:
            print(f"  Failed: {e}")
            if attempt < max_retries:
                await asyncio.sleep(2 * attempt)
            else:
                raise


async def alternating_capture(address: str = FAN_ADDRESS, listen_window: float = 5.0,
                               gap: float = 15.0, cycles: int = 0):
    """
    Alternating capture mode: connect briefly, read, disconnect, repeat.

    This gives the phone app windows to connect and send commands.
    Between our connection windows, the app can communicate.
    We capture state changes by reading before and after.

    Args:
        address: Fan BLE address
        listen_window: How long to stay connected each cycle (seconds)
        gap: How long to stay disconnected (let app connect)
        cycles: Number of cycles (0 = infinite)
    """
    logger = ProtocolLogger()
    print(f"Alternating capture mode")
    print(f"  Connect for {listen_window}s, disconnect for {gap}s")
    print(f"  Log file: {logger.log_file}")
    print()
    print("Instructions:")
    print(f"  1. Wait for 'DISCONNECTED' message")
    print(f"  2. Use the QuietCool app to send a command")
    print(f"  3. Wait for 'CONNECTED' - we'll read the new state")
    print(f"  4. Repeat!")
    print()

    cycle = 0
    last_value = None

    try:
        while cycles == 0 or cycle < cycles:
            cycle += 1
            print(f"\n--- Cycle {cycle} ---")

            # Connect and read
            try:
                async with BleakClient(address, timeout=15.0) as client:
                    print(f"CONNECTED")

                    # Read current value
                    try:
                        value = await client.read_gatt_char(FAN_CHAR_UUID)
                        hex_val = value.hex()

                        if last_value is None:
                            logger.log("state_read", bytes(value), label="initial")
                        elif hex_val != last_value:
                            logger.log("state_changed", bytes(value),
                                       label="changed",
                                       context=f"was: {last_value}")
                            print(f"  *** STATE CHANGED! Was: {last_value} Now: {hex_val}")
                        else:
                            logger.log("state_read", bytes(value), label="unchanged")

                        last_value = hex_val
                    except Exception as e:
                        print(f"  Read failed: {e}")

                    # Listen for notifications briefly
                    notifications = []

                    def on_notify(char, data):
                        notifications.append(data)
                        logger.log("fan_notify", bytes(data), char.uuid)

                    try:
                        await client.start_notify(FAN_CHAR_UUID, on_notify)
                        await asyncio.sleep(listen_window)
                        await client.stop_notify(FAN_CHAR_UUID)
                    except Exception:
                        await asyncio.sleep(listen_window)

                    if notifications:
                        print(f"  Got {len(notifications)} notifications during window")

            except Exception as e:
                print(f"  Connection failed: {e}")

            # Disconnect and wait
            print(f"DISCONNECTED - Use QuietCool app now! ({gap}s window)")
            await asyncio.sleep(gap)

    except asyncio.CancelledError:
        pass

    print(f"\nCapture complete. Log: {logger.log_file}")


async def write_and_observe(address: str, hex_commands: list[str]):
    """
    Send a list of hex commands and observe responses.

    Args:
        address: Fan BLE address
        hex_commands: List of hex strings to send
    """
    logger = ProtocolLogger()
    print(f"Write-and-observe mode")
    print(f"  Commands to send: {len(hex_commands)}")
    print(f"  Log file: {logger.log_file}")

    async with BleakClient(address, timeout=30.0) as client:
        print(f"Connected! MTU: {client.mtu_size}")

        def on_notify(char, data):
            logger.log("fan_response", bytes(data), char.uuid)

        await client.start_notify(FAN_CHAR_UUID, on_notify)

        for hex_cmd in hex_commands:
            data = bytes.fromhex(hex_cmd.replace(" ", ""))
            logger.log("send_command", data)

            try:
                await client.write_gatt_char(FAN_CHAR_UUID, data, response=True)
            except Exception:
                try:
                    await client.write_gatt_char(FAN_CHAR_UUID, data, response=False)
                except Exception as e:
                    print(f"  Write failed: {e}")

            # Wait for response
            await asyncio.sleep(2.0)

            # Also read current value
            try:
                value = await client.read_gatt_char(FAN_CHAR_UUID)
                logger.log("read_after_cmd", bytes(value))
            except Exception:
                pass

        await client.stop_notify(FAN_CHAR_UUID)

    print(f"\nDone. Log: {logger.log_file}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="QuietCool BLE Protocol Capture")
    parser.add_argument("--address", "-a", default=FAN_ADDRESS,
                        help=f"Fan BLE address (default: {FAN_ADDRESS})")

    sub = parser.add_subparsers(dest="mode", help="Capture mode")

    # Passive monitor
    p = sub.add_parser("passive", help="Connect and listen (blocks app)")
    p.add_argument("-d", "--duration", type=float, default=0)

    # Alternating capture
    a = sub.add_parser("alternate", help="Connect/disconnect cycles (app-friendly)")
    a.add_argument("--window", type=float, default=5.0,
                   help="Connected window duration (default: 5s)")
    a.add_argument("--gap", type=float, default=15.0,
                   help="Disconnected gap for app (default: 15s)")
    a.add_argument("--cycles", type=int, default=0,
                   help="Number of cycles (0 = infinite)")

    # Write and observe
    w = sub.add_parser("write", help="Send hex commands and observe responses")
    w.add_argument("commands", nargs="+", help="Hex strings to send")

    args = parser.parse_args()

    if not args.mode:
        parser.print_help()
        return

    if args.mode == "passive":
        asyncio.run(passive_monitor(args.address, args.duration))
    elif args.mode == "alternate":
        asyncio.run(alternating_capture(args.address, args.window, args.gap, args.cycles))
    elif args.mode == "write":
        asyncio.run(write_and_observe(args.address, args.commands))


if __name__ == "__main__":
    main()
