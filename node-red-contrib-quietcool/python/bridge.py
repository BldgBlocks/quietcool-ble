#!/usr/bin/env python3
"""
QuietCool BLE Bridge for Node-RED

Long-running daemon that maintains a BLE connection to a QuietCool fan
and accepts JSON commands on stdin, returning JSON responses on stdout.

Protocol (one JSON object per line):
  stdin  -> {"id":"<msg_id>","cmd":"<command>","args":{...}}
  stdout <- {"id":"<msg_id>","ok":true,"data":{...}}
  stdout <- {"id":"<msg_id>","ok":false,"error":"<message>"}
  stdout <- {"type":"status","connected":true/false,"address":"..."}

Commands:
  connect        - Connect and login to fan
  disconnect     - Disconnect from fan
  get_status     - Full status (info, state, params, version, presets)
  get_state      - Current work state (mode, range, temp, humidity)
  get_info       - Fan info (name, model, serial)
  get_version    - Firmware version
  get_params     - Current parameters/thresholds
  get_presets    - List preset profiles
  get_remain     - Timer remaining time
  set_mode       - Set mode: args.mode = "Idle"|"Timer"|"TH"
  set_speed      - Manual run: args.speed = "LOW"|"HIGH"
  set_timer      - Timer mode: args.hours, args.minutes, args.speed
  set_preset     - Apply preset: args.name = "Summer"|"Winter"|...
  set_thresholds - Set temp/humidity thresholds
  pair           - Pair with fan (fan must be in pairing mode)
  raw            - Send raw API command: args.api, args.params
  scan           - Scan for QuietCool fans on BLE
  generate_id    - Generate a new random Phone ID
"""

import asyncio
import json
import sys
import os
import signal
import logging
import secrets
from typing import Optional, Dict, Any

# Add parent paths for imports
sys.path.insert(0, os.path.dirname(__file__))

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

logger = logging.getLogger("quietcool-bridge")

# BLE constants
SERVICE_UUID = "000000ff-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
DEFAULT_CHUNK_SIZE = 20


class FanBridge:
    """BLE bridge for a single QuietCool fan."""

    def __init__(self, address: str, phone_id: str):
        self.address = address
        self.phone_id = phone_id
        self.client: Optional[BleakClient] = None
        self._response_buffer = bytearray()
        self._response_event = asyncio.Event()
        self._response_json: Optional[Dict] = None
        self._logged_in = False
        self._presets_cache = None
        self._chunk_size = DEFAULT_CHUNK_SIZE

    @property
    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    def _on_disconnect(self, client: BleakClient):
        self._logged_in = False
        emit_status(False, self.address, "disconnected")

    def _notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        self._response_buffer.extend(data)
        try:
            text = self._response_buffer.decode("ascii")
            if text.strip().endswith("}"):
                try:
                    parsed = json.loads(text)
                    self._response_json = parsed
                    self._response_event.set()
                except json.JSONDecodeError:
                    pass
        except UnicodeDecodeError:
            pass

    async def connect(self) -> Dict:
        if self.is_connected:
            return {"already_connected": True}

        self.client = BleakClient(
            self.address,
            timeout=15.0,
            disconnected_callback=self._on_disconnect,
        )
        await self.client.connect()

        if not self.client.is_connected:
            raise ConnectionError("Failed to connect")

        await self.client.start_notify(CHAR_UUID, self._notification_handler)

        response = await self._send("Login", PhoneID=self.phone_id)
        if not response or response.get("Result") != "Success":
            await self.client.disconnect()
            raise ConnectionError(
                f"Login failed: {response}. Check PhoneID or pair the device."
            )

        self._logged_in = True
        emit_status(True, self.address, "connected")
        return {
            "connected": True,
            "pair_state": response.get("PairState"),
        }

    async def disconnect(self) -> Dict:
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self._logged_in = False
        return {"disconnected": True}

    async def ensure_connected(self):
        if not self.is_connected:
            await self.connect()

    async def _send(self, api: str, timeout: float = 5.0, **kwargs) -> Optional[Dict]:
        if not self.client or not self.client.is_connected:
            raise ConnectionError("Not connected")

        cmd = {"Api": api}
        cmd.update(kwargs)
        payload = json.dumps(cmd, separators=(",", ":"))

        self._response_buffer.clear()
        self._response_json = None
        self._response_event.clear()

        data = payload.encode("ascii")
        for i in range(0, len(data), self._chunk_size):
            chunk = data[i : i + self._chunk_size]
            await self.client.write_gatt_char(CHAR_UUID, chunk, response=True)

        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=timeout)
            return self._response_json
        except asyncio.TimeoutError:
            return None

    async def reconnect_and_send(self, api: str, timeout: float = 5.0, **kwargs):
        """Send command with auto-reconnect on BLE drop."""
        try:
            await self.ensure_connected()
            return await self._send(api, timeout, **kwargs)
        except (ConnectionError, Exception) as e:
            logger.warning(f"Connection lost, reconnecting: {e}")
            await asyncio.sleep(3)
            await self.connect()
            return await self._send(api, timeout, **kwargs)

    # ---- High-level commands ----

    async def get_state(self) -> Dict:
        r = await self.reconnect_and_send("GetWorkState")
        if r:
            r["temperature_f"] = r.get("Temp_Sample", 0) / 10.0
            r["humidity_pct"] = r.get("Humidity_Sample", 0)
        return r or {}

    async def get_info(self) -> Dict:
        return await self.reconnect_and_send("GetFanInfo") or {}

    async def get_version(self) -> Dict:
        return await self.reconnect_and_send("GetVersion") or {}

    async def get_params(self) -> Dict:
        return await self.reconnect_and_send("GetParameter") or {}

    async def get_presets(self) -> Dict:
        r = await self.reconnect_and_send("GetPresets", FanType="THREE")
        if r and "Presets" in r:
            self._presets_cache = r["Presets"]
            # Convert to named dicts for clarity
            r["presets_named"] = []
            for p in r["Presets"]:
                r["presets_named"].append({
                    "name": p[0],
                    "temp_high": p[1],
                    "temp_med": p[2],
                    "temp_low": p[3],
                    "hum_high": p[4],
                    "hum_low": p[5],
                    "hum_range": p[6],
                })
        return r or {}

    async def get_remain(self) -> Dict:
        return await self.reconnect_and_send("GetRemainTime") or {}

    async def get_status(self) -> Dict:
        info = await self.get_info()
        state = await self.get_state()
        params = await self.get_params()
        version = await self.get_version()
        presets = await self.get_presets()

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
            "temperature_f": state.get("temperature_f"),
            "humidity": state.get("humidity_pct"),
            "fan_type": params.get("FanType"),
            "presets": [p[0] for p in (self._presets_cache or [])],
            "active_thresholds": {
                "temp_high": params.get("GetTemp_H"),
                "temp_med": params.get("GetTemp_M"),
                "temp_low": params.get("GetTemp_L"),
                "hum_high": params.get("GetHum_H"),
                "hum_low": params.get("GetHum_L"),
                "hum_range": params.get("GetHum_Range"),
            },
        }

        # Match active preset
        for p in self._presets_cache or []:
            if (
                p[1] == params.get("GetTemp_H")
                and p[3] == params.get("GetTemp_L")
                and p[4] == params.get("GetHum_H")
            ):
                status["active_preset"] = p[0]
                break

        if state.get("Mode") == "Timer":
            remain = await self.get_remain()
            status["remain_hours"] = remain.get("RemainHour")
            status["remain_minutes"] = remain.get("RemainMinute")
            status["remain_seconds"] = remain.get("RemainSecond")

        return status

    async def set_mode(self, mode: str) -> Dict:
        return await self.reconnect_and_send("SetMode", Mode=mode) or {}

    async def set_speed(self, speed: str) -> Dict:
        """Manual/continuous run. BLE disconnects after this."""
        r = await self.reconnect_and_send("SetSpeed", Speed=speed)
        return r or {}

    async def set_timer(self, hours: int, minutes: int, speed: str = "HIGH") -> Dict:
        r = await self.reconnect_and_send(
            "SetTime", SetHour=hours, SetMinute=minutes, SetTime_Range=speed
        )
        if r and r.get("Flag") == "TRUE":
            await asyncio.sleep(0.5)
            r2 = await self.reconnect_and_send("SetMode", Mode="Timer")
            return r2 or r
        return r or {}

    async def set_preset(self, name: str) -> Dict:
        if not self._presets_cache:
            await self.get_presets()
        if not self._presets_cache:
            return {"error": "No presets available"}

        for idx, p in enumerate(self._presets_cache):
            if p[0].lower() == name.lower():
                r = await self.reconnect_and_send(
                    "SetTempHumidity",
                    SetTemp_H=p[1],
                    SetTemp_M=p[2],
                    SetTemp_L=p[3],
                    SetHum_H=p[4],
                    SetHum_L=p[5],
                    SetHum_Range=p[6],
                    Index=idx,
                )
                if r and r.get("Flag") == "TRUE":
                    await asyncio.sleep(0.5)
                    await self.reconnect_and_send("SetMode", Mode="TH")
                return r or {}

        return {
            "error": f"Preset '{name}' not found",
            "available": [p[0] for p in self._presets_cache],
        }

    async def set_thresholds(self, args: Dict) -> Dict:
        return (
            await self.reconnect_and_send(
                "SetTempHumidity",
                SetTemp_H=args.get("temp_high", 255),
                SetTemp_M=args.get("temp_med", 255),
                SetTemp_L=args.get("temp_low", 45),
                SetHum_H=args.get("hum_high", 75),
                SetHum_L=args.get("hum_low", 50),
                SetHum_Range=args.get("hum_range", "LOW"),
                Index=args.get("index", 0),
            )
            or {}
        )

    async def pair(self) -> Dict:
        """Pair with fan. Fan must be in pairing mode (hold pair button).

        Flow (from emerose/quietcool):
        1. Send Login with our PhoneID
        2. If Result=Success → already paired
        3. If Result=Fail and PairState indicates pairing mode → send Pair command
        4. If Result=Fail and not in pairing mode → tell user to press button
        """
        if not self.client or not self.client.is_connected:
            raise ConnectionError("Not connected (call connect_for_pairing first)")

        # Step 1: Try login
        r = await self._send("Login", PhoneID=self.phone_id)
        if not r:
            return {"paired": False, "error": "No response from fan"}

        if r.get("Result") == "Success":
            self._logged_in = True
            return {"paired": True, "phone_id": self.phone_id, "message": "Already paired with this ID"}

        # Result is Fail — check if fan is in pairing mode
        pair_state = r.get("PairState", "")
        if pair_state.lower() in ("no", "nopaired", ""):
            return {
                "paired": False,
                "message": "Fan is not in pairing mode. Hold the Pair button on the controller for ~5 seconds until the LED blinks, then try again.",
            }

        # Fan is in pairing mode — send Pair command
        pair_r = await self._send("Pair", PhoneID=self.phone_id)
        if pair_r and pair_r.get("Result") == "Success":
            self._logged_in = True
            emit_status(True, self.address, "paired")
            return {"paired": True, "phone_id": self.phone_id, "message": "Pairing successful! Save this Phone ID."}

        return {"paired": False, "error": f"Pair command failed: {pair_r}"}

    async def connect_for_pairing(self) -> Dict:
        """Connect to fan without login (for pairing flow)."""
        if self.is_connected:
            return {"already_connected": True}

        self.client = BleakClient(
            self.address,
            timeout=15.0,
            disconnected_callback=self._on_disconnect,
        )
        await self.client.connect()

        if not self.client.is_connected:
            raise ConnectionError("Failed to connect")

        await self.client.start_notify(CHAR_UUID, self._notification_handler)
        return {"connected": True, "ready_to_pair": True}

    async def raw(self, api: str, params: Dict) -> Dict:
        return await self.reconnect_and_send(api, **params) or {}


# ---- Globals ----
fan: Optional[FanBridge] = None


def emit(msg_id: str, ok: bool, data: Any = None, error: str = None):
    """Write a JSON response to stdout."""
    resp = {"id": msg_id, "ok": ok}
    if data is not None:
        resp["data"] = data
    if error is not None:
        resp["error"] = error
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()


def emit_status(connected: bool, address: str = "", detail: str = ""):
    """Write a status update to stdout."""
    msg = {"type": "status", "connected": connected, "address": address, "detail": detail}
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


async def handle_command(line: str):
    global fan

    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        emit("?", False, error=f"Invalid JSON: {line}")
        return

    msg_id = msg.get("id", "?")
    cmd = msg.get("cmd", "")
    args = msg.get("args", {})

    try:
        if cmd == "connect":
            address = args.get("address", "")
            phone_id = args.get("phone_id", "")
            if not address or not phone_id:
                emit(msg_id, False, error="address and phone_id are required")
                return
            fan = FanBridge(address, phone_id)
            result = await fan.connect()
            emit(msg_id, True, result)

        elif cmd == "disconnect":
            if fan:
                result = await fan.disconnect()
                emit(msg_id, True, result)
            else:
                emit(msg_id, True, {"disconnected": True})

        elif cmd == "get_status":
            await _ensure_fan(msg_id)
            result = await fan.get_status()
            emit(msg_id, True, result)

        elif cmd == "get_state":
            await _ensure_fan(msg_id)
            result = await fan.get_state()
            emit(msg_id, True, result)

        elif cmd == "get_info":
            await _ensure_fan(msg_id)
            result = await fan.get_info()
            emit(msg_id, True, result)

        elif cmd == "get_version":
            await _ensure_fan(msg_id)
            result = await fan.get_version()
            emit(msg_id, True, result)

        elif cmd == "get_params":
            await _ensure_fan(msg_id)
            result = await fan.get_params()
            emit(msg_id, True, result)

        elif cmd == "get_presets":
            await _ensure_fan(msg_id)
            result = await fan.get_presets()
            emit(msg_id, True, result)

        elif cmd == "get_remain":
            await _ensure_fan(msg_id)
            result = await fan.get_remain()
            emit(msg_id, True, result)

        elif cmd == "set_mode":
            await _ensure_fan(msg_id)
            mode = args.get("mode", "Idle")
            result = await fan.set_mode(mode)
            emit(msg_id, True, result)

        elif cmd == "set_speed":
            await _ensure_fan(msg_id)
            speed = args.get("speed", "HIGH")
            result = await fan.set_speed(speed)
            emit(msg_id, True, result)

        elif cmd == "set_timer":
            await _ensure_fan(msg_id)
            result = await fan.set_timer(
                hours=args.get("hours", 1),
                minutes=args.get("minutes", 0),
                speed=args.get("speed", "HIGH"),
            )
            emit(msg_id, True, result)

        elif cmd == "set_preset":
            await _ensure_fan(msg_id)
            name = args.get("name", "")
            if not name:
                emit(msg_id, False, error="preset name required")
                return
            result = await fan.set_preset(name)
            if "error" in result:
                emit(msg_id, False, error=result["error"], data=result)
            else:
                emit(msg_id, True, result)

        elif cmd == "set_thresholds":
            await _ensure_fan(msg_id)
            result = await fan.set_thresholds(args)
            emit(msg_id, True, result)

        elif cmd == "pair":
            # Pairing flow: connect without login, then attempt pair
            address = args.get("address", "")
            phone_id = args.get("phone_id", "")
            if not address:
                emit(msg_id, False, error="address is required")
                return
            if not phone_id:
                phone_id = secrets.token_hex(8)  # Auto-generate
            fan = FanBridge(address, phone_id)
            await fan.connect_for_pairing()
            result = await fan.pair()
            emit(msg_id, True, result)

        elif cmd == "scan":
            # Scan for QuietCool fans (bleak 2.x returns (device, adv_data) tuples)
            timeout = args.get("timeout", 8)
            discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
            fans = []
            for address, (device, adv_data) in discovered.items():
                name = device.name or adv_data.local_name or ""
                mfr_data = adv_data.manufacturer_data or {}
                # QuietCool fans advertise as ATTICFAN_* with manufacturer ID 0x4133 (16691)
                is_quietcool = name.startswith("ATTICFAN") or (16691 in mfr_data)
                if is_quietcool:
                    fans.append({
                        "address": device.address,
                        "name": name,
                        "rssi": adv_data.rssi,
                    })
            emit(msg_id, True, {"fans": fans})

        elif cmd == "generate_id":
            new_id = secrets.token_hex(8)
            emit(msg_id, True, {"phone_id": new_id})

        elif cmd == "raw":
            await _ensure_fan(msg_id)
            api = args.get("api", "")
            params = args.get("params", {})
            if not api:
                emit(msg_id, False, error="api name required")
                return
            result = await fan.raw(api, params)
            emit(msg_id, True, result)

        elif cmd == "ping":
            emit(msg_id, True, {"pong": True, "connected": fan.is_connected if fan else False})

        else:
            emit(msg_id, False, error=f"Unknown command: {cmd}")

    except Exception as e:
        logger.exception(f"Error handling {cmd}")
        emit(msg_id, False, error=str(e))


async def _ensure_fan(msg_id: str):
    global fan
    if not fan:
        raise ConnectionError("Not connected. Send 'connect' first.")
    await fan.ensure_connected()


async def stdin_reader():
    """Read lines from stdin asynchronously."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    emit_status(False, "", "bridge_ready")

    while True:
        line = await reader.readline()
        if not line:
            break  # EOF - Node-RED process ended
        try:
            await handle_command(line.decode("utf-8").strip())
        except Exception as e:
            logger.exception(f"Unhandled error: {e}")
            emit("?", False, error=f"Internal error: {e}")


def main():
    log_level = os.environ.get("QUIETCOOL_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # Logs go to stderr, JSON protocol goes to stdout
    )

    logger.info("QuietCool BLE Bridge starting...")

    try:
        asyncio.run(stdin_reader())
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Bridge shutting down")


if __name__ == "__main__":
    main()
