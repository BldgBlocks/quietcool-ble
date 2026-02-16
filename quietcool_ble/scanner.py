"""
BLE Scanner - Discover nearby BLE devices.

Use this to find your QuietCool fan's BLE address and advertisement data.
"""

import asyncio
import sys
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData


# Known QuietCool identifiers to look for in device names/manufacturer data
QUIETCOOL_HINTS = [
    "quietcool", "qc", "afg", "smt", "es-3", "es3",
    "fan", "whole house", "wholehouse",
]


def is_potential_quietcool(device: BLEDevice, adv: AdvertisementData) -> bool:
    """Check if a device might be a QuietCool fan based on name or adv data."""
    name = (device.name or "").lower()
    for hint in QUIETCOOL_HINTS:
        if hint in name:
            return True
    # Also check local_name from advertisement
    local_name = (adv.local_name or "").lower() if adv.local_name else ""
    for hint in QUIETCOOL_HINTS:
        if hint in local_name:
            return True
    return False


async def scan(duration: float = 10.0, show_all: bool = False):
    """
    Scan for BLE devices.

    Args:
        duration: How long to scan in seconds.
        show_all: If True, show all devices. If False, highlight likely QuietCool devices.
    """
    print(f"Scanning for BLE devices for {duration}s...")
    print("=" * 80)

    discovered = {}

    def callback(device: BLEDevice, adv: AdvertisementData):
        if device.address not in discovered:
            discovered[device.address] = (device, adv)
            is_qc = is_potential_quietcool(device, adv)
            marker = " <<<< POSSIBLE QUIETCOOL" if is_qc else ""

            if show_all or is_qc:
                print(f"\n{'*' * 40 if is_qc else '-' * 40}")
                print(f"  Address:    {device.address}")
                print(f"  Name:       {device.name or '(unknown)'}")
                print(f"  RSSI:       {adv.rssi} dBm")
                if adv.local_name:
                    print(f"  Local Name: {adv.local_name}")
                if adv.manufacturer_data:
                    print(f"  Mfr Data:   ", end="")
                    for company_id, data in adv.manufacturer_data.items():
                        print(f"Company 0x{company_id:04X} -> {data.hex()}")
                if adv.service_uuids:
                    print(f"  Services:   {adv.service_uuids}")
                if adv.service_data:
                    print(f"  Svc Data:   ", end="")
                    for uuid, data in adv.service_data.items():
                        print(f"{uuid} -> {data.hex()}")
                print(f"{marker}")

    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    await asyncio.sleep(duration)
    await scanner.stop()

    print("\n" + "=" * 80)
    print(f"Total devices found: {len(discovered)}")

    # Summary of potential matches
    matches = {addr: (dev, adv) for addr, (dev, adv) in discovered.items()
                if is_potential_quietcool(dev, adv)}
    if matches:
        print(f"\nPotential QuietCool devices: {len(matches)}")
        for addr, (dev, adv) in matches.items():
            print(f"  -> {addr}  {dev.name or '(unknown)'}  RSSI={adv.rssi}")
    else:
        print("\nNo obvious QuietCool devices found by name.")
        print("Try running with --all to see all devices, then identify yours by RSSI proximity.")
        print("Power cycle the fan or put it in pairing mode to help identify it.")

    return discovered


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Scan for BLE devices to find QuietCool fan")
    parser.add_argument("-d", "--duration", type=float, default=10.0,
                        help="Scan duration in seconds (default: 10)")
    parser.add_argument("-a", "--all", action="store_true",
                        help="Show all BLE devices (not just potential QuietCool matches)")
    args = parser.parse_args()

    asyncio.run(scan(duration=args.duration, show_all=args.all))


if __name__ == "__main__":
    main()
