# QuietCool BLE Reverse Engineering Toolkit

Reverse-engineer the Bluetooth Low Energy (BLE) protocol used by QuietCool AFG SMT ES-3.0 whole house fans for integration with Node-RED.

## Setup

```bash
cd ~/quietcool-ble
pip install -e .
```

Or without installing:
```bash
cd ~/quietcool-ble
python -m quietcool_ble.cli <command>
```

## Workflow

### Step 1: Scan for the fan
```bash
quietcool-ble scan --all
```
Find your fan's BLE address (e.g., `AA:BB:CC:DD:EE:FF`). Tips:
- Power cycle the fan to see it appear/disappear
- Look for the strongest RSSI (closest device)
- Put the fan in pairing mode if available

### Step 2: Explore the GATT profile
```bash
quietcool-ble explore AA:BB:CC:DD:EE:FF
```
This maps out all services and characteristics. Note which ones are writable (commands) and notifiable (status/feedback).

### Step 3: Monitor while using the app
Open **two terminals**:

**Terminal 1** - Start the monitor:
```bash
quietcool-ble monitor AA:BB:CC:DD:EE:FF
```

**Terminal 2** (or use your phone) - Use the QuietCool app to:
- Turn fan ON/OFF
- Change speed
- Set timer
- Check status

The monitor will capture all BLE notifications. If the device doesn't use notifications, try polling mode:
```bash
quietcool-ble monitor AA:BB:CC:DD:EE:FF --poll
```

### Step 4: Send commands
Once you've identified command patterns, replay them:
```bash
quietcool-ble command AA:BB:CC:DD:EE:FF
```
Interactive shell:
```
qc> list                                    # see writable characteristics
qc> send <char_uuid> <hex_data>             # send a command
qc> read <char_uuid>                        # read a value
qc> quit
```

## Captures

All captured data is saved to `captures/` as JSONL files:
- `capture_YYYYMMDD_HHMMSS.jsonl` - notification captures
- `poll_YYYYMMDD_HHMMSS.jsonl` - polling captures
- `commands_YYYYMMDD_HHMMSS.jsonl` - command send/response logs

## Project Structure

```
quietcool-ble/
├── pyproject.toml
├── README.md
├── captures/             # Captured BLE data (gitignored)
└── quietcool_ble/
    ├── __init__.py
    ├── cli.py            # Main CLI entry point
    ├── scanner.py        # BLE device scanner
    ├── explorer.py       # GATT profile explorer
    ├── monitor.py        # Notification monitor / poller
    └── commander.py      # Interactive command sender
```

## Roadmap

- [ ] Scan and identify fan BLE address
- [ ] Map GATT profile (services/characteristics)
- [ ] Capture protocol commands from official app
- [ ] Build command dictionary (on/off, speed, timer, etc.)
- [ ] Create Python API wrapper
- [ ] Build Node-RED custom node (`node-red-contrib-quietcool`)

## Requirements

- Raspberry Pi 4 with Bluetooth
- Python 3.9+
- `bleak` BLE library
- QuietCool AFG SMT ES-3.0 fan within BLE range
