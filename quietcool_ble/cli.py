"""
QuietCool BLE CLI - Main entry point for all tools.

Usage:
    quietcool-ble scan [--all] [-d DURATION]
    quietcool-ble explore <ADDRESS> [--no-read]
    quietcool-ble monitor <ADDRESS> [--poll] [--no-read] [-d DURATION]
    quietcool-ble command <ADDRESS>
"""

import argparse
import asyncio
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="quietcool-ble",
        description="QuietCool BLE Reverse Engineering Toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Workflow:
  1. scan       - Find your QuietCool fan's BLE address
  2. explore    - Map out the GATT services and characteristics
  3. monitor    - Listen for notifications while using the QuietCool app
  4. command    - Send raw BLE commands interactively

Examples:
  %(prog)s scan --all                        # Find all BLE devices
  %(prog)s explore AA:BB:CC:DD:EE:FF         # Explore device GATT profile
  %(prog)s monitor AA:BB:CC:DD:EE:FF         # Monitor notifications
  %(prog)s monitor AA:BB:CC:DD:EE:FF --poll  # Poll for value changes
  %(prog)s command AA:BB:CC:DD:EE:FF         # Interactive command shell
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # scan
    scan_parser = subparsers.add_parser("scan", help="Scan for BLE devices")
    scan_parser.add_argument("-d", "--duration", type=float, default=10.0,
                             help="Scan duration in seconds (default: 10)")
    scan_parser.add_argument("-a", "--all", action="store_true",
                             help="Show all BLE devices")

    # explore
    explore_parser = subparsers.add_parser("explore", help="Explore device GATT profile")
    explore_parser.add_argument("address", help="BLE device address")
    explore_parser.add_argument("--no-read", action="store_true",
                                help="Don't read characteristic values")

    # monitor
    monitor_parser = subparsers.add_parser("monitor", help="Monitor device notifications")
    monitor_parser.add_argument("address", help="BLE device address")
    monitor_parser.add_argument("-d", "--duration", type=float, default=0,
                                help="Duration in seconds (0 = until Ctrl+C)")
    monitor_parser.add_argument("--no-read", action="store_true",
                                help="Skip initial read")
    monitor_parser.add_argument("--poll", action="store_true",
                                help="Use polling mode instead of notifications")

    # command
    cmd_parser = subparsers.add_parser("command", help="Interactive BLE command shell")
    cmd_parser.add_argument("address", help="BLE device address")

    # capture - protocol capture modes
    cap_parser = subparsers.add_parser("capture", help="Protocol capture (app-friendly)")
    cap_parser.add_argument("--address", "-a", default="XX:XX:XX:XX:XX:XX",
                            help="Fan BLE address")
    cap_sub = cap_parser.add_subparsers(dest="capture_mode")

    cap_passive = cap_sub.add_parser("passive", help="Connect and listen (blocks app)")
    cap_passive.add_argument("-d", "--duration", type=float, default=0)

    cap_alt = cap_sub.add_parser("alternate", help="Connect/disconnect cycles")
    cap_alt.add_argument("--window", type=float, default=5.0)
    cap_alt.add_argument("--gap", type=float, default=15.0)
    cap_alt.add_argument("--cycles", type=int, default=0)

    cap_write = cap_sub.add_parser("write", help="Send hex commands")
    cap_write.add_argument("commands", nargs="+", help="Hex strings to send")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "scan":
        from quietcool_ble.scanner import scan
        asyncio.run(scan(duration=args.duration, show_all=args.all))

    elif args.command == "explore":
        from quietcool_ble.explorer import explore
        asyncio.run(explore(address=args.address, read_values=not args.no_read))

    elif args.command == "monitor":
        if args.poll:
            from quietcool_ble.monitor import monitor_and_poll
            asyncio.run(monitor_and_poll(address=args.address, duration=args.duration))
        else:
            from quietcool_ble.monitor import BLEMonitor
            mon = BLEMonitor(args.address)
            asyncio.run(mon.monitor(duration=args.duration, also_read=not args.no_read))

    elif args.command == "command":
        from quietcool_ble.commander import interactive
        asyncio.run(interactive(address=args.address))

    elif args.command == "capture":
        if not args.capture_mode:
            cap_parser.print_help()
            sys.exit(1)
        if args.capture_mode == "passive":
            from quietcool_ble.protocol import passive_monitor
            asyncio.run(passive_monitor(args.address, args.duration))
        elif args.capture_mode == "alternate":
            from quietcool_ble.protocol import alternating_capture
            asyncio.run(alternating_capture(args.address, args.window, args.gap, args.cycles))
        elif args.capture_mode == "write":
            from quietcool_ble.protocol import write_and_observe
            asyncio.run(write_and_observe(args.address, args.commands))


if __name__ == "__main__":
    main()
