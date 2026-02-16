# node-red-contrib-quietcool

Node-RED nodes for controlling QuietCool whole house fans over BLE.

## Requirements

- Raspberry Pi (or Linux with BLE)
- Python 3.9+
- BlueZ (pre-installed on Raspberry Pi OS)
- A QuietCool fan with BLE (AFG SMT ES-3.0 or similar)

## Installation

```bash
cd ~/.node-red
npm install /path/to/quietcool-ble/node-red-contrib-quietcool
```

The `postinstall` script automatically creates a Python virtual environment and installs the `bleak` BLE library.

Restart Node-RED after installation:

```bash
node-red-restart
```

## Setup

### Quick Setup (recommended)

No phone app or HCI snoop needed — pair directly from Node-RED:

1. Drag a **fan control** or **fan sensor** node onto the canvas
2. Double-click and create a new fan configuration
3. Click **Scan** to find your fan — select it from the dropdown
4. Click **New** to generate a Phone ID
5. On the fan controller, hold the **Pair** button for ~5 seconds until the LED blinks
6. Click **Pair with Fan** in the editor
7. Save and deploy — you're done!

### Alternative: Extract Phone ID from existing app pairing

If you've already paired with the QuietCool mobile app, you can extract the Phone ID:

1. Enable *Bluetooth HCI snoop log* in Android Developer Options
2. Open the QuietCool app and connect to the fan
3. Pull the log: `adb bugreport bugreport.zip`
4. Extract and parse `btsnoop_hci.log` to find the `PhoneID` in a `Login` command

### Manual fan discovery

If the Scan button doesn't work, find the address manually:

```bash
bluetoothctl scan on
```

Look for a device named `ATTICFAN_*`. Note the MAC address (e.g., `XX:XX:XX:XX:XX:XX`).

## Nodes

### fan control (`quietcool-control`)

Send commands to the fan. Available actions:

| Action | Description |
|--------|-------------|
| Turn Off | Sets fan to Idle mode |
| Smart Mode (TH) | Temperature/humidity auto mode |
| Run High | Continuous run at high speed |
| Run Low | Continuous run at low speed |
| Timer | Run for a set duration |
| Apply Preset | Apply a named profile (Summer, Winter, etc.) |
| Set Thresholds | Set custom temp/humidity thresholds |
| Pair | Pair with fan (must be in pairing mode) |
| Raw | Send any raw API command |

Actions can be overridden via `msg.payload`:

```json
{
  "action": "preset",
  "args": { "name": "Summer" }
}
```

```json
{
  "action": "timer",
  "args": { "hours": 2, "minutes": 0, "speed": "HIGH" }
}
```

### fan sensor (`quietcool-sensor`)

Read data from the fan. Available queries:

| Query | Returns |
|-------|---------|
| State | Mode, speed, temperature (°F), humidity (%) |
| Full Status | Complete status with fan info, firmware, presets |
| Fan Info | Name, model, serial number |
| Firmware | Firmware and hardware version |
| Parameters | Current temp/humidity thresholds |
| Presets | List of preset profiles |
| Timer Remaining | Time left on active timer |

For State and Full Status queries, convenience fields are added:
- `msg.temperature` — Temperature in °F
- `msg.humidity` — Humidity %
- `msg.mode` — Current mode (Idle, Timer, TH)
- `msg.range` — Current speed (LOW, HIGH, CLOSE)

Optional **polling**: set a poll interval (seconds) to auto-query without input triggers.

## Architecture

The Node-RED nodes communicate with the fan through a Python bridge process:

```
Node-RED → stdin JSON → bridge.py → BLE/bleak → QuietCool Fan
Node-RED ← stdout JSON ← bridge.py ← BLE/bleak ← QuietCool Fan
```

The bridge maintains a persistent BLE connection, avoiding the ~3 second reconnect overhead for each command. It spawns automatically when nodes are deployed and shuts down when they're removed.

## Protocol

QuietCool fans use plain JSON over BLE GATT. All communication goes through a single characteristic (`0000ff01`) on service `000000ff`. The protocol requires a `Login` with a `PhoneID` before any commands are accepted.

Tested with firmware `IT-BLT-ATTICFAN_V2.6`.

## License

MIT
