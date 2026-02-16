"""
BLE Device Explorer - Connect to a device and enumerate all services/characteristics.

Use this after identifying your QuietCool fan's BLE address to map out
its GATT profile (services, characteristics, descriptors, properties).
"""

import asyncio
import sys
from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic


# Common BLE characteristic property flags explained
PROPERTY_DESCRIPTIONS = {
    "read": "Can read value",
    "write": "Can write value (with response)",
    "write-without-response": "Can write value (no response/ack)",
    "notify": "Can subscribe to notifications",
    "indicate": "Can subscribe to indications (ack'd notifications)",
    "broadcast": "Can broadcast value",
    "extended-properties": "Has extended properties",
    "authenticated-signed-writes": "Supports signed writes",
}


async def explore(address: str, read_values: bool = True, max_retries: int = 5):
    """
    Connect to a BLE device and enumerate its entire GATT profile.

    Args:
        address: BLE device address (e.g., "AA:BB:CC:DD:EE:FF")
        read_values: If True, attempt to read all readable characteristics.
        max_retries: Number of connection attempts.
    """
    for attempt in range(1, max_retries + 1):
        print(f"Connecting to {address} (attempt {attempt}/{max_retries})...")
        try:
            return await _explore_inner(address, read_values)
        except Exception as e:
            print(f"  Connection failed: {e}")
            if attempt < max_retries:
                wait = 2 * attempt
                print(f"  Retrying in {wait}s...")
                await asyncio.sleep(wait)
            else:
                print("  All attempts exhausted.")
                raise


async def _explore_inner(address: str, read_values: bool = True):
    async with BleakClient(address, timeout=30.0) as client:
        if not client.is_connected:
            print("Failed to connect!")
            return

        print(f"Connected: {client.is_connected}")
        print(f"MTU Size:  {client.mtu_size}")
        print()

        services = client.services
        service_list = list(services)
        print(f"Found {len(service_list)} services")
        print("=" * 80)

        result = {}

        for service in services:
            print(f"\nService: {service.uuid}")
            print(f"  Description: {service.description or '(unknown)'}")
            print(f"  Handle:      0x{service.handle:04X}")

            svc_data = {
                "uuid": service.uuid,
                "description": service.description,
                "handle": service.handle,
                "characteristics": [],
            }

            for char in service.characteristics:
                props = char.properties
                prop_str = ", ".join(props)

                print(f"\n  Characteristic: {char.uuid}")
                print(f"    Description:  {char.description or '(unknown)'}")
                print(f"    Handle:       0x{char.handle:04X}")
                print(f"    Properties:   [{prop_str}]")

                char_data = {
                    "uuid": char.uuid,
                    "description": char.description,
                    "handle": char.handle,
                    "properties": list(props),
                    "value": None,
                    "value_hex": None,
                    "descriptors": [],
                }

                # Try to read value if readable
                if read_values and "read" in props:
                    try:
                        value = await client.read_gatt_char(char)
                        char_data["value"] = value
                        char_data["value_hex"] = value.hex()

                        # Try multiple representations
                        print(f"    Value (hex):  {value.hex()}")
                        print(f"    Value (raw):  {value}")
                        try:
                            text = value.decode("utf-8", errors="replace")
                            print(f"    Value (utf8): {text}")
                            char_data["value_utf8"] = text
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"    Value:        <read error: {e}>")

                # List descriptors
                for desc in char.descriptors:
                    print(f"    Descriptor:   {desc.uuid} (handle 0x{desc.handle:04X})")
                    desc_data = {"uuid": desc.uuid, "handle": desc.handle}
                    if read_values:
                        try:
                            val = await client.read_gatt_descriptor(desc.handle)
                            desc_data["value"] = val.hex()
                            print(f"      Value:      {val.hex()}")
                        except Exception as e:
                            print(f"      Value:      <read error: {e}>")
                    char_data["descriptors"].append(desc_data)

                svc_data["characteristics"].append(char_data)

            result[service.uuid] = svc_data

        print("\n" + "=" * 80)
        print("Exploration complete.")
        print("\nKey characteristics to watch for (likely fan control):")
        print("  - Characteristics with 'write' or 'write-without-response' properties")
        print("  - Characteristics with 'notify' properties (status/feedback)")
        print("  - Non-standard (vendor-specific) UUIDs (not starting with 0000)")

        # Highlight interesting characteristics
        print("\n--- Writable Characteristics (potential commands) ---")
        for svc_uuid, svc in result.items():
            for ch in svc["characteristics"]:
                if "write" in ch["properties"] or "write-without-response" in ch["properties"]:
                    print(f"  {ch['uuid']}  props={ch['properties']}")

        print("\n--- Notifiable Characteristics (potential status/feedback) ---")
        for svc_uuid, svc in result.items():
            for ch in svc["characteristics"]:
                if "notify" in ch["properties"] or "indicate" in ch["properties"]:
                    print(f"  {ch['uuid']}  props={ch['properties']}")

        return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Explore BLE device GATT profile")
    parser.add_argument("address", help="BLE device address (e.g., AA:BB:CC:DD:EE:FF)")
    parser.add_argument("--no-read", action="store_true",
                        help="Don't attempt to read characteristic values")
    args = parser.parse_args()

    asyncio.run(explore(address=args.address, read_values=not args.no_read))


if __name__ == "__main__":
    main()
