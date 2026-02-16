"""
BLE Command Sender - Write raw bytes to a BLE characteristic.

Use this to replay captured commands or test new ones against the fan.
"""

import asyncio
import json
import os
from datetime import datetime
from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic


class BLECommander:
    """Send commands to a BLE device and capture responses."""

    def __init__(self, address: str, log_dir: str = "captures"):
        self.address = address
        self.log_dir = log_dir
        self.client = None
        self.responses = []
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(log_dir, f"commands_{self.session_id}.jsonl")

    def _notification_handler(self, char: BleakGATTCharacteristic, data: bytearray):
        """Capture response notifications."""
        timestamp = datetime.now().isoformat()
        entry = {
            "timestamp": timestamp,
            "direction": "response",
            "characteristic_uuid": char.uuid,
            "data_hex": data.hex(),
            "data_bytes": list(data),
        }
        self.responses.append(entry)
        self._log(entry)
        print(f"  <- RESPONSE on {char.uuid}: {data.hex()}  ({list(data)})")

    def _log(self, entry: dict):
        """Append to command log."""
        os.makedirs(self.log_dir, exist_ok=True)
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    async def connect(self):
        """Connect and subscribe to all notifications."""
        self.client = BleakClient(self.address, timeout=20.0)
        await self.client.connect()
        print(f"Connected to {self.address}")

        # Subscribe to all notifiable characteristics
        for service in self.client.services:
            for char in service.characteristics:
                if "notify" in char.properties or "indicate" in char.properties:
                    try:
                        await self.client.start_notify(char, self._notification_handler)
                    except Exception:
                        pass

    async def disconnect(self):
        """Disconnect from device."""
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            print("Disconnected.")

    async def send_hex(self, char_uuid: str, hex_data: str, with_response: bool = True):
        """
        Send hex data to a characteristic.

        Args:
            char_uuid: Target characteristic UUID.
            hex_data: Hex string of data to send (e.g., "01ff03").
            with_response: Whether to use write-with-response.
        """
        data = bytes.fromhex(hex_data.replace(" ", ""))
        return await self.send_bytes(char_uuid, data, with_response)

    async def send_bytes(self, char_uuid: str, data: bytes, with_response: bool = True):
        """
        Send raw bytes to a characteristic.

        Args:
            char_uuid: Target characteristic UUID.
            data: Bytes to send.
            with_response: Whether to use write-with-response.
        """
        timestamp = datetime.now().isoformat()
        entry = {
            "timestamp": timestamp,
            "direction": "command",
            "characteristic_uuid": char_uuid,
            "data_hex": data.hex(),
            "data_bytes": list(data),
            "with_response": with_response,
        }

        print(f"\n[{timestamp}] SEND to {char_uuid}")
        print(f"  -> {data.hex()}  ({list(data)})  response={with_response}")

        self.responses.clear()
        self._log(entry)

        try:
            await self.client.write_gatt_char(char_uuid, data, response=with_response)
            # Wait a moment for notifications to arrive
            await asyncio.sleep(1.0)
            if not self.responses:
                print("  (no notification response received)")
        except Exception as e:
            print(f"  ERROR: {e}")
            entry["error"] = str(e)
            self._log(entry)

    async def read_char(self, char_uuid: str):
        """Read and display a characteristic value."""
        try:
            value = await self.client.read_gatt_char(char_uuid)
            print(f"  READ {char_uuid}: {value.hex()}  ({list(value)})")
            return value
        except Exception as e:
            print(f"  READ ERROR: {e}")
            return None


async def interactive(address: str):
    """
    Interactive command shell for sending BLE commands.

    Commands:
        send <char_uuid> <hex_data>     - Write hex data to characteristic
        sendn <char_uuid> <hex_data>    - Write without response
        read <char_uuid>                - Read a characteristic
        list                            - List writable characteristics
        help                            - Show commands
        quit                            - Exit
    """
    cmd = BLECommander(address)
    await cmd.connect()

    print("\n" + "=" * 60)
    print("Interactive BLE Commander")
    print("Type 'help' for commands, 'quit' to exit")
    print("=" * 60)

    # Build a list of writable chars for reference
    writable = []
    notifiable = []
    for service in cmd.client.services:
        for char in service.characteristics:
            if "write" in char.properties or "write-without-response" in char.properties:
                writable.append(char)
            if "notify" in char.properties or "indicate" in char.properties:
                notifiable.append(char)

    try:
        while True:
            try:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("\nqc> ")
                )
            except EOFError:
                break

            line = line.strip()
            if not line:
                continue

            parts = line.split()
            command = parts[0].lower()

            if command == "quit" or command == "exit":
                break
            elif command == "help":
                print(interactive.__doc__)
            elif command == "list":
                print("\nWritable characteristics:")
                for ch in writable:
                    print(f"  {ch.uuid}  props={ch.properties}  desc={ch.description}")
                print(f"\nNotifiable characteristics:")
                for ch in notifiable:
                    print(f"  {ch.uuid}  props={ch.properties}  desc={ch.description}")
            elif command == "send" and len(parts) >= 3:
                char_uuid = parts[1]
                hex_data = "".join(parts[2:])
                await cmd.send_hex(char_uuid, hex_data, with_response=True)
            elif command == "sendn" and len(parts) >= 3:
                char_uuid = parts[1]
                hex_data = "".join(parts[2:])
                await cmd.send_hex(char_uuid, hex_data, with_response=False)
            elif command == "read" and len(parts) >= 2:
                await cmd.read_char(parts[1])
            else:
                print("Unknown command. Type 'help' for available commands.")

    finally:
        await cmd.disconnect()

    print(f"\nCommand log saved to: {cmd.log_file}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Send BLE commands to device")
    parser.add_argument("address", help="BLE device address")
    args = parser.parse_args()

    asyncio.run(interactive(address=args.address))


if __name__ == "__main__":
    main()
