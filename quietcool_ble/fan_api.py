"""
QuietCool BLE Fan API

Protocol reverse-engineered from HCI snoop log captured from the QuietCool Android app.

BLE Details:
  - Service UUID: 000000ff-0000-1000-8000-00805f9b34fb
  - Characteristic UUID: 0000ff01-0000-1000-8000-00805f9b34fb (handle 0x002a)
  - CCCD handle: 0x002b (write 0x0100 to enable notifications)
  - Protocol: Plain JSON strings over BLE GATT writes + notifications

Connection Sequence:
  1. Connect to device
  2. Enable notifications (write 0x0100 to CCCD)
  3. Send Login: {"Api":"Login","PhoneID":"<hex_string>"}
  4. Issue commands as JSON, receive responses as JSON notifications

Discovered API Commands:
  Queries:  Login, GetFanInfo, GetVersion, GetWorkState, GetParameter, GetPresets, GetRemainTime
  Control:  SetMode (Idle/Timer/TH), SetTime, SetTempHumidity, SetSpeed (LOW/HIGH), SetGuideSetup

Notes:
  - SetSpeed sets manual/continuous run at the given speed. Disconnects BLE after.
  - SetTempHumidity with Index applies a preset profile (Summer=0, custom=1, Winter=2).
  - After SetTempHumidity, call SetMode TH to activate smart mode with new thresholds.
  - PhoneID must match a previously paired device. Extract from HCI snoop log.
  - Fan firmware: IT-BLT-ATTICFAN_V2.6 (2024.05.30), HW: A
  - MEDIUM speed in SetSpeed returns FALSE on this firmware version.
"""

import asyncio
import json
import uuid
import logging
from typing import Optional, Dict, Any, List, Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

logger = logging.getLogger(__name__)

# BLE UUIDs
SERVICE_UUID = "000000ff-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"

# ATT Handles (discovered from HCI snoop)
CHAR_HANDLE = 0x002A
CCCD_HANDLE = 0x002B

# BLE MTU for data (20 bytes default, can be larger after MTU exchange)
DEFAULT_CHUNK_SIZE = 20

# Fan modes
class FanMode:
    IDLE = "Idle"
    TIMER = "Timer"
    TH = "TH"  # Temperature/Humidity automatic mode

# Fan speed ranges
class FanRange:
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CLOSE = "CLOSE"  # Off/damper closed

# Fan types
class FanType:
    THREE = "THREE"  # 3-speed fan


# Preset tuple indices: [Name, Temp_H, Temp_M, Temp_L, Hum_H, Hum_L, Hum_Range]
PRESET_NAME = 0
PRESET_TEMP_H = 1
PRESET_TEMP_M = 2
PRESET_TEMP_L = 3
PRESET_HUM_H = 4
PRESET_HUM_L = 5
PRESET_HUM_RANGE = 6


class QuietCoolFan:
    """API wrapper for controlling a QuietCool fan over BLE.
    
    Usage:
        async with QuietCoolFan('XX:XX:XX:XX:XX:XX', phone_id='...') as fan:
            state = await fan.get_work_state()
            await fan.set_speed('HIGH')          # manual run
            await fan.apply_preset('Winter')     # switch profile + activate TH
            await fan.turn_off()                 # idle
    """
    
    def __init__(self, address: str, phone_id: Optional[str] = None,
                 auto_reconnect: bool = True):
        """
        Initialize fan controller.
        
        Args:
            address: BLE MAC address of the fan (e.g., "XX:XX:XX:XX:XX:XX")
            phone_id: Hex string used for login. Required — extract from HCI
                      snoop log using extract_phone_id() or the CLI tool.
            auto_reconnect: If True, automatically reconnect when BLE drops
                           (common after SetSpeed commands).
        """
        self.address = address
        if not phone_id:
            raise ValueError("phone_id is required. Generate one and pair, or extract from HCI snoop log.")
        self.phone_id = phone_id
        self.auto_reconnect = auto_reconnect
        self.client: Optional[BleakClient] = None
        self._response_buffer = bytearray()
        self._response_event = asyncio.Event()
        self._response_json: Optional[Dict] = None
        self._notification_callbacks: List[Callable] = []
        self._logged_in = False
        self._chunk_size = DEFAULT_CHUNK_SIZE
        self._presets_cache: Optional[List] = None
    
    async def connect(self) -> bool:
        """Connect to the fan and authenticate."""
        logger.info(f"Connecting to {self.address}...")
        self.client = BleakClient(self.address)
        await self.client.connect()
        
        if not self.client.is_connected:
            logger.error("Failed to connect")
            return False
        
        logger.info("Connected. Enabling notifications...")
        
        # Enable notifications via CCCD
        await self.client.start_notify(CHAR_UUID, self._notification_handler)
        
        # Login
        logger.info(f"Logging in with PhoneID={self.phone_id}...")
        response = await self.send_command("Login", PhoneID=self.phone_id)
        
        if response and response.get("Result") == "Success":
            self._logged_in = True
            logger.info(f"Login successful! PairState={response.get('PairState')}")
            return True
        else:
            logger.error(f"Login failed: {response}")
            return False
    
    async def disconnect(self):
        """Disconnect from the fan."""
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            logger.info("Disconnected")
        self._logged_in = False
    
    def _notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        """Handle incoming BLE notifications (response fragments)."""
        self._response_buffer.extend(data)
        
        # Check if we have a complete JSON response
        try:
            text = self._response_buffer.decode('ascii')
            if text.strip().endswith('}'):
                try:
                    parsed = json.loads(text)
                    self._response_json = parsed
                    self._response_event.set()
                    
                    # Call any registered notification callbacks
                    for cb in self._notification_callbacks:
                        try:
                            cb(parsed)
                        except Exception as e:
                            logger.error(f"Notification callback error: {e}")
                except json.JSONDecodeError:
                    pass  # Not complete yet, keep buffering
        except UnicodeDecodeError:
            pass
    
    @property
    def is_connected(self) -> bool:
        """Check if currently connected to fan."""
        return self.client is not None and self.client.is_connected

    async def _ensure_connected(self):
        """Reconnect if disconnected and auto_reconnect is enabled."""
        if not self.is_connected:
            if self.auto_reconnect:
                logger.info("Connection lost, reconnecting...")
                await asyncio.sleep(2)  # Brief delay for BLE stack
                await self.connect()
            else:
                raise ConnectionError("Not connected to fan")

    async def send_command(self, api: str, timeout: float = 5.0, **kwargs) -> Optional[Dict]:
        """
        Send a JSON API command to the fan and wait for the response.
        
        Args:
            api: API command name (e.g., "GetWorkState", "SetMode")
            timeout: Response timeout in seconds
            **kwargs: Additional JSON fields for the command
            
        Returns:
            Parsed JSON response dict, or None on timeout
        """
        await self._ensure_connected()
        
        # Build command JSON
        cmd = {"Api": api}
        cmd.update(kwargs)
        payload = json.dumps(cmd, separators=(',', ':'))
        
        logger.debug(f"Sending: {payload}")
        
        # Clear response state
        self._response_buffer.clear()
        self._response_json = None
        self._response_event.clear()
        
        # Send in chunks (BLE has limited write size)
        data = payload.encode('ascii')
        for i in range(0, len(data), self._chunk_size):
            chunk = data[i:i + self._chunk_size]
            await self.client.write_gatt_char(CHAR_UUID, chunk, response=True)
        
        # Wait for response notification
        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=timeout)
            return self._response_json
        except asyncio.TimeoutError:
            logger.warning(f"Timeout waiting for response to {api}")
            return None
    
    def on_notification(self, callback: Callable[[Dict], None]):
        """Register a callback for fan notifications."""
        self._notification_callbacks.append(callback)
    
    # ==================== Query Commands ====================
    
    async def get_fan_info(self) -> Optional[Dict]:
        """Get fan information (name, model, serial number)."""
        return await self.send_command("GetFanInfo")
    
    async def get_version(self) -> Optional[Dict]:
        """Get firmware version info."""
        return await self.send_command("GetVersion")
    
    async def get_work_state(self) -> Optional[Dict]:
        """
        Get current fan state.
        
        Returns dict with:
            - Mode: "TH" | "Timer" | "Idle"
            - Range: "LOW" | "MEDIUM" | "HIGH" | "CLOSE"
            - SensorState: "OK" | ...
            - Temp_Sample: int (temperature in tenths of °F)
            - Humidity_Sample: int (humidity %)
        """
        return await self.send_command("GetWorkState")
    
    async def get_parameter(self) -> Optional[Dict]:
        """
        Get full parameter state.
        
        Returns dict with mode, fan type, temp thresholds, humidity thresholds,
        timer settings, etc.
        """
        return await self.send_command("GetParameter")
    
    async def get_presets(self, fan_type: str = FanType.THREE) -> Optional[Dict]:
        """Get preset configurations.
        
        Returns dict with Presets list, each entry:
            [Name, Temp_H, Temp_M, Temp_L, Hum_H, Hum_L, Hum_Range]
            
        Example:
            {"Presets": [["Summer", 100, 80, 20, 70, 30, "LOW"],
                         ["Winter", 255, 255, 45, 75, 50, "LOW"]]}
        """
        result = await self.send_command("GetPresets", FanType=fan_type)
        if result and "Presets" in result:
            self._presets_cache = result["Presets"]
        return result
    
    async def get_preset_names(self) -> List[str]:
        """Get list of preset profile names (e.g., ['Summer', 'Winter'])."""
        if not self._presets_cache:
            await self.get_presets()
        if self._presets_cache:
            return [p[PRESET_NAME] for p in self._presets_cache]
        return []
    
    async def get_remain_time(self) -> Optional[Dict]:
        """
        Get remaining timer time.
        
        Returns dict with:
            - RemainHour: int
            - RemainMinute: int
            - RemainSecond: int
        """
        return await self.send_command("GetRemainTime")
    
    # ==================== Control Commands ====================
    
    async def set_mode(self, mode: str) -> Optional[Dict]:
        """
        Set fan operating mode.
        
        Args:
            mode: "Idle" (off), "Timer", or "TH" (temperature/humidity)
            
        Returns:
            Response with WorkMode and Flag fields
        """
        return await self.send_command("SetMode", Mode=mode)
    
    async def set_time(self, hours: int, minutes: int, speed: str = FanRange.HIGH) -> Optional[Dict]:
        """
        Set timer parameters.
        
        Args:
            hours: Timer hours (0+)
            minutes: Timer minutes (0-60)
            speed: Fan speed during timer ("LOW", "MEDIUM", "HIGH")
            
        Returns:
            Response with Flag field
        """
        return await self.send_command("SetTime",
            SetHour=hours,
            SetMinute=minutes,
            SetTime_Range=speed
        )
    
    async def set_temp_humidity(self,
                                temp_h: int = 255,
                                temp_m: int = 255,
                                temp_l: int = 45,
                                hum_h: int = 75,
                                hum_l: int = 50,
                                hum_range: str = FanRange.LOW,
                                index: int = 2) -> Optional[Dict]:
        """
        Set temperature/humidity thresholds for TH mode.
        
        Args:
            temp_h: High temp threshold (255 = disabled)
            temp_m: Medium temp threshold (255 = disabled)
            temp_l: Low temp threshold
            hum_h: High humidity threshold
            hum_l: Low humidity threshold
            hum_range: Humidity fan speed ("LOW", "MEDIUM", "HIGH")
            index: Preset index
            
        Returns:
            Response with Flag field
        """
        return await self.send_command("SetTempHumidity",
            SetTemp_H=temp_h,
            SetTemp_M=temp_m,
            SetTemp_L=temp_l,
            SetHum_H=hum_h,
            SetHum_L=hum_l,
            SetHum_Range=hum_range,
            Index=index
        )
    
    async def set_speed(self, speed: str) -> Optional[Dict]:
        """Set fan to manual/continuous run at the given speed.
        
        This is the equivalent of the app's "Run" mode. The fan runs
        continuously at the specified speed until another mode is set.
        
        NOTE: This command causes the fan to disconnect BLE. If
        auto_reconnect is enabled, subsequent commands will reconnect
        automatically.
        
        Args:
            speed: "LOW" or "HIGH" (MEDIUM not supported on V2.6 firmware)
            
        Returns:
            Response with Speed and Flag fields
        """
        return await self.send_command("SetSpeed", Speed=speed)
    
    async def set_guide_setup(self, done: bool = True) -> Optional[Dict]:
        """Mark the initial setup guide as complete."""
        return await self.send_command("SetGuideSetup",
            GuideSetup="No" if done else "Yes"
        )
    
    # ==================== Preset/Profile Methods ====================
    
    async def apply_preset(self, name: str, activate_th: bool = True) -> Optional[Dict]:
        """Apply a named preset profile (e.g., 'Summer', 'Winter').
        
        Loads the preset's temperature/humidity thresholds and optionally
        activates TH (smart) mode.
        
        Args:
            name: Preset name (case-insensitive match)
            activate_th: If True, also switch to TH mode after applying
            
        Returns:
            The SetTempHumidity response, or None if preset not found
        """
        if not self._presets_cache:
            await self.get_presets()
        
        if not self._presets_cache:
            logger.error("No presets available")
            return None
        
        # Find preset by name (case-insensitive)
        for idx, preset in enumerate(self._presets_cache):
            if preset[PRESET_NAME].lower() == name.lower():
                result = await self.set_temp_humidity(
                    temp_h=preset[PRESET_TEMP_H],
                    temp_m=preset[PRESET_TEMP_M],
                    temp_l=preset[PRESET_TEMP_L],
                    hum_h=preset[PRESET_HUM_H],
                    hum_l=preset[PRESET_HUM_L],
                    hum_range=preset[PRESET_HUM_RANGE],
                    index=idx
                )
                if activate_th and result and result.get("Flag") == "TRUE":
                    await self.set_mode(FanMode.TH)
                return result
        
        logger.error(f"Preset '{name}' not found. Available: {[p[0] for p in self._presets_cache]}")
        return None
    
    # ==================== Convenience Methods ====================
    
    async def turn_off(self) -> Optional[Dict]:
        """Turn the fan off (Idle mode)."""
        return await self.set_mode(FanMode.IDLE)
    
    async def turn_on_timer(self, hours: int = 1, minutes: int = 0,
                            speed: str = FanRange.HIGH) -> Optional[Dict]:
        """
        Turn on the fan in timer mode.
        
        Args:
            hours: Run duration hours
            minutes: Run duration minutes
            speed: Fan speed
        """
        # Set time first, then activate timer mode
        result = await self.set_time(hours, minutes, speed)
        if result and result.get("Flag") == "TRUE":
            return await self.set_mode(FanMode.TIMER)
        return result
    
    async def turn_on_auto(self) -> Optional[Dict]:
        """Turn on the fan in temperature/humidity auto mode."""
        return await self.set_mode(FanMode.TH)
    
    async def run_continuous(self, speed: str = FanRange.HIGH) -> Optional[Dict]:
        """Run fan continuously at a given speed (manual mode).
        
        Args:
            speed: "LOW" or "HIGH"
        """
        return await self.set_speed(speed)
    
    async def get_temperature_f(self) -> Optional[float]:
        """Get current temperature in Fahrenheit."""
        state = await self.get_work_state()
        if state and "Temp_Sample" in state:
            return state["Temp_Sample"] / 10.0
        return None
    
    async def get_humidity(self) -> Optional[int]:
        """Get current humidity percentage."""
        state = await self.get_work_state()
        if state and "Humidity_Sample" in state:
            return state["Humidity_Sample"]
        return None

    async def get_status(self) -> Dict[str, Any]:
        """Get a comprehensive status summary."""
        info = await self.get_fan_info() or {}
        state = await self.get_work_state() or {}
        params = await self.get_parameter() or {}
        version = await self.get_version() or {}
        presets = await self.get_presets() or {}
        
        status = {
            "connected": True,
            "name": info.get("Name"),
            "model": info.get("Model"),
            "serial": info.get("SerialNum"),
            "firmware": version.get("Version"),
            "hw_version": version.get("HW_Version"),
            "mode": state.get("Mode"),
            "range": state.get("Range"),
            "sensor_state": state.get("SensorState"),
            "temperature_f": state.get("Temp_Sample", 0) / 10.0 if state.get("Temp_Sample") else None,
            "humidity": state.get("Humidity_Sample"),
            "fan_type": params.get("FanType"),
            "presets": [p[PRESET_NAME] for p in presets.get("Presets", [])],
            "active_thresholds": {
                "temp_high": params.get("GetTemp_H"),
                "temp_med": params.get("GetTemp_M"),
                "temp_low": params.get("GetTemp_L"),
                "hum_high": params.get("GetHum_H"),
                "hum_low": params.get("GetHum_L"),
                "hum_range": params.get("GetHum_Range"),
            },
        }
        
        # Match active thresholds to a preset name
        for preset in presets.get("Presets", []):
            if (preset[PRESET_TEMP_H] == params.get("GetTemp_H") and
                preset[PRESET_TEMP_L] == params.get("GetTemp_L") and
                preset[PRESET_HUM_H] == params.get("GetHum_H")):
                status["active_preset"] = preset[PRESET_NAME]
                break
        
        # Add timer info if in timer mode
        if state.get("Mode") == "Timer":
            remain = await self.get_remain_time() or {}
            status["remain_hours"] = remain.get("RemainHour")
            status["remain_minutes"] = remain.get("RemainMinute")
            status["remain_seconds"] = remain.get("RemainSecond")
        
        return status

    async def __aenter__(self):
        await self.connect()
        return self
    
    async def __aexit__(self, *args):
        await self.disconnect()


# ==================== PhoneID Extraction ====================

def extract_phone_id(btsnoop_path: str) -> List[str]:
    """Extract PhoneID(s) from a btsnoop_hci.log file.
    
    This is the key piece needed for authentication. The PhoneID is sent
    by the QuietCool app during the Login handshake. To capture it:
    
    1. Enable Bluetooth HCI snoop log in Android Developer Options
    2. Open the QuietCool app and connect to the fan
    3. Generate a bug report: adb bugreport
    4. Extract btsnoop_hci.log from the ZIP under FS/data/log/bt/
    5. Run: quietcool-ble extract-phone-id <path_to_btsnoop_hci.log>
    
    Args:
        btsnoop_path: Path to btsnoop_hci.log file
        
    Returns:
        List of PhoneID strings found in the log
    """
    from .hci_parser import parse_btsnoop, extract_ble_traffic, filter_fan_traffic
    
    records = parse_btsnoop(btsnoop_path)
    att_ops = extract_ble_traffic(records)
    fan_ops = filter_fan_traffic(att_ops)
    
    # Reassemble write fragments and find Login commands
    phone_ids = []
    write_buf = b''
    
    for op in fan_ops:
        if op['type'] in ('ATT_WRITE_REQ', 'ATT_WRITE_CMD') and 'value_hex' in op:
            if op.get('att_handle') != '0x002a':
                continue
            write_buf += bytes.fromhex(op['value_hex'])
            try:
                text = write_buf.decode('ascii')
                if text.endswith('}'):
                    try:
                        parsed = json.loads(text)
                        if parsed.get('Api') == 'Login' and 'PhoneID' in parsed:
                            pid = parsed['PhoneID']
                            if pid not in phone_ids:
                                phone_ids.append(pid)
                        write_buf = b''
                    except json.JSONDecodeError:
                        pass
            except UnicodeDecodeError:
                write_buf = b''
    
    return phone_ids


# ==================== Standalone CLI ====================

async def main():
    """Interactive CLI for testing the QuietCool fan API."""
    import argparse
    
    parser = argparse.ArgumentParser(description='QuietCool Fan BLE Controller')
    parser.add_argument('--address', '-a', required=True,
                        help='Fan BLE address (e.g., XX:XX:XX:XX:XX:XX)')
    parser.add_argument('--phone-id', '-p', required=True,
                        help='PhoneID for authentication')
    parser.add_argument('--command', '-c', choices=[
        'status', 'info', 'version', 'state', 'params', 'presets',
        'off', 'timer', 'auto', 'temp', 'humidity',
        'run-low', 'run-high',
        'preset',
        'extract-phone-id',
        'interactive'
    ], default='interactive', help='Command to execute')
    parser.add_argument('--hours', type=int, default=1, help='Timer hours')
    parser.add_argument('--minutes', type=int, default=0, help='Timer minutes')
    parser.add_argument('--speed', choices=['LOW', 'MEDIUM', 'HIGH'], default='HIGH',
                        help='Fan speed')
    parser.add_argument('--preset-name', default=None, help='Preset name for preset command')
    parser.add_argument('--snoop-file', default=None,
                        help='Path to btsnoop_hci.log for extract-phone-id')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose logging')
    
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s'
    )
    
    # Handle extract-phone-id without fan connection
    if args.command == 'extract-phone-id':
        snoop = args.snoop_file or 'captures/btsnoop_hci.log'
        print(f"Extracting PhoneID from {snoop}...")
        ids = extract_phone_id(snoop)
        if ids:
            print(f"Found {len(ids)} PhoneID(s):")
            for pid in ids:
                print(f"  {pid}")
            print(f"\nUse with: quietcool-ble -p {ids[0]} -c status")
        else:
            print("No PhoneID found in log file.")
        return
    
    async with QuietCoolFan(args.address, phone_id=args.phone_id) as fan:
        if args.command == 'status':
            status = await fan.get_status()
            print(json.dumps(status, indent=2))
        
        elif args.command == 'info':
            print(json.dumps(await fan.get_fan_info(), indent=2))
        
        elif args.command == 'version':
            print(json.dumps(await fan.get_version(), indent=2))
        
        elif args.command == 'state':
            print(json.dumps(await fan.get_work_state(), indent=2))
        
        elif args.command == 'params':
            print(json.dumps(await fan.get_parameter(), indent=2))
        
        elif args.command == 'presets':
            result = await fan.get_presets()
            if result and 'Presets' in result:
                for i, p in enumerate(result['Presets']):
                    print(f"  [{i}] {p[0]:15s}  Temp: H={p[1]} M={p[2]} L={p[3]}  Hum: H={p[4]} L={p[5]} Range={p[6]}")
            else:
                print(json.dumps(result, indent=2))
        
        elif args.command == 'off':
            result = await fan.turn_off()
            print(f"Fan off: {json.dumps(result)}")
        
        elif args.command == 'timer':
            result = await fan.turn_on_timer(args.hours, args.minutes, args.speed)
            print(f"Timer set: {json.dumps(result)}")
        
        elif args.command == 'auto':
            result = await fan.turn_on_auto()
            print(f"Auto mode: {json.dumps(result)}")
        
        elif args.command == 'temp':
            temp = await fan.get_temperature_f()
            print(f"Temperature: {temp}°F")
        
        elif args.command == 'humidity':
            hum = await fan.get_humidity()
            print(f"Humidity: {hum}%")
        
        elif args.command == 'run-low':
            result = await fan.set_speed('LOW')
            print(f"Run LOW: {json.dumps(result)}")
        
        elif args.command == 'run-high':
            result = await fan.set_speed('HIGH')
            print(f"Run HIGH: {json.dumps(result)}")
        
        elif args.command == 'preset':
            name = args.preset_name
            if not name:
                names = await fan.get_preset_names()
                print(f"Available presets: {names}")
                print("Use --preset-name <name> to apply one.")
            else:
                result = await fan.apply_preset(name)
                print(f"Applied '{name}': {json.dumps(result)}")
        
        elif args.command == 'interactive':
            print("QuietCool Interactive Shell")
            print("Commands: status, info, version, state, params, presets,")
            print("          off, auto, timer [h] [m] [speed], run <LOW|HIGH>,")
            print("          preset <name>, temp, humidity, raw <json>, quit")
            print()
            
            while True:
                try:
                    line = input("qc> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                
                if not line:
                    continue
                
                parts = line.split()
                cmd = parts[0].lower()
                
                try:
                    if cmd in ('quit', 'exit', 'q'):
                        break
                    elif cmd == 'status':
                        print(json.dumps(await fan.get_status(), indent=2))
                    elif cmd == 'info':
                        print(json.dumps(await fan.get_fan_info(), indent=2))
                    elif cmd == 'version':
                        print(json.dumps(await fan.get_version(), indent=2))
                    elif cmd == 'state':
                        print(json.dumps(await fan.get_work_state(), indent=2))
                    elif cmd == 'params':
                        print(json.dumps(await fan.get_parameter(), indent=2))
                    elif cmd == 'presets':
                        result = await fan.get_presets()
                        if result and 'Presets' in result:
                            for i, p in enumerate(result['Presets']):
                                print(f"  [{i}] {p[0]:15s}  Temp: H={p[1]} M={p[2]} L={p[3]}  Hum: H={p[4]} L={p[5]} Range={p[6]}")
                    elif cmd == 'off':
                        print(json.dumps(await fan.turn_off(), indent=2))
                    elif cmd == 'auto' or cmd == 'smart':
                        print(json.dumps(await fan.turn_on_auto(), indent=2))
                    elif cmd == 'temp':
                        print(f"{await fan.get_temperature_f()}°F")
                    elif cmd == 'humidity':
                        print(f"{await fan.get_humidity()}%")
                    elif cmd == 'timer':
                        h = int(parts[1]) if len(parts) > 1 else 1
                        m = int(parts[2]) if len(parts) > 2 else 0
                        s = parts[3].upper() if len(parts) > 3 else 'HIGH'
                        print(json.dumps(await fan.turn_on_timer(h, m, s), indent=2))
                    elif cmd == 'run':
                        speed = parts[1].upper() if len(parts) > 1 else 'HIGH'
                        print(json.dumps(await fan.set_speed(speed), indent=2))
                    elif cmd == 'preset':
                        if len(parts) < 2:
                            names = await fan.get_preset_names()
                            print(f"Available: {names}. Usage: preset <name>")
                        else:
                            name = parts[1]
                            result = await fan.apply_preset(name)
                            print(json.dumps(result, indent=2))
                    elif cmd == 'raw':
                        raw = ' '.join(parts[1:])
                        cmd_json = json.loads(raw)
                        api = cmd_json.pop('Api')
                        result = await fan.send_command(api, **cmd_json)
                        print(json.dumps(result, indent=2))
                    elif cmd == 'help':
                        print("Commands: status, info, version, state, params, presets,")
                        print("          off, auto/smart, timer [h] [m] [speed], run <LOW|HIGH>,")
                        print("          preset <name>, temp, humidity, raw <json>, quit")
                    else:
                        print(f"Unknown command: {cmd}. Type 'help' for commands.")
                except Exception as e:
                    print(f"Error: {e}")


if __name__ == '__main__':
    asyncio.run(main())
