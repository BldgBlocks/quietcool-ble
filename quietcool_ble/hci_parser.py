#!/usr/bin/env python3
"""
Parse btsnoop_hci.log to extract BLE GATT writes/reads/notifications
for the QuietCool fan (address XX:XX:XX:XX:XX:XX).

btsnoop format:
  File header: 16 bytes
    - "btsnoop\0" (8 bytes)
    - version (4 bytes, big-endian)
    - datalink type (4 bytes, big-endian) - 1002 for HCI

  Each record:
    - original length (4 bytes BE)
    - included length (4 bytes BE)
    - flags (4 bytes BE)
    - cumulative drops (4 bytes BE)
    - timestamp (8 bytes BE, microseconds since 0000-01-01)
    - packet data (included_length bytes)

  HCI packet types (from flags):
    bit 0: 0 = sent, 1 = received
    bit 1: 0 = data, 1 = command/event
"""

import struct
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path


# btsnoop epoch: January 1, 0000 AD
# Unix epoch offset in microseconds
BTSNOOP_EPOCH_DELTA = 0x00dcddb30f2f8000  # microseconds from 0AD to 1970


def parse_btsnoop(filepath):
    """Parse a btsnoop_hci.log file and return records."""
    with open(filepath, 'rb') as f:
        # Read file header
        magic = f.read(8)
        if magic != b'btsnoop\x00':
            raise ValueError(f"Not a btsnoop file: {magic!r}")
        
        version = struct.unpack('>I', f.read(4))[0]
        datalink = struct.unpack('>I', f.read(4))[0]
        print(f"btsnoop version: {version}, datalink: {datalink}")
        
        records = []
        record_num = 0
        
        while True:
            header = f.read(24)
            if len(header) < 24:
                break
            
            orig_len, incl_len, flags, drops, timestamp_us = struct.unpack(
                '>IIIIq', header
            )
            
            data = f.read(incl_len)
            if len(data) < incl_len:
                break
            
            # Convert timestamp
            unix_us = timestamp_us - BTSNOOP_EPOCH_DELTA
            ts = datetime.utcfromtimestamp(unix_us / 1_000_000)
            
            direction = 'recv' if (flags & 1) else 'sent'
            is_cmd_evt = bool(flags & 2)
            
            records.append({
                'num': record_num,
                'timestamp': ts,
                'direction': direction,
                'is_cmd_evt': is_cmd_evt,
                'data': data,
                'flags': flags,
            })
            record_num += 1
        
        print(f"Total records: {len(records)}")
        return records


def get_hci_type(data, is_cmd_evt, direction):
    """Determine HCI packet type from the first byte."""
    if len(data) == 0:
        return 'unknown', data
    
    pkt_type = data[0]
    types = {
        0x01: 'HCI_CMD',
        0x02: 'HCI_ACL',
        0x03: 'HCI_SCO', 
        0x04: 'HCI_EVT',
    }
    return types.get(pkt_type, f'UNKNOWN(0x{pkt_type:02x})'), data[1:]


def parse_acl_data(data):
    """Parse HCI ACL data packet.
    
    Format:
      Handle + flags (2 bytes LE)
      Total length (2 bytes LE)
      L2CAP data:
        Length (2 bytes LE)
        CID (2 bytes LE) - 0x0004 = ATT
        ATT PDU
    """
    if len(data) < 4:
        return None
    
    handle_flags = struct.unpack('<H', data[0:2])[0]
    handle = handle_flags & 0x0FFF
    pb_flag = (handle_flags >> 12) & 0x03
    bc_flag = (handle_flags >> 14) & 0x03
    total_len = struct.unpack('<H', data[2:4])[0]
    
    l2cap_data = data[4:]
    if len(l2cap_data) < 4:
        return None
    
    l2cap_len = struct.unpack('<H', l2cap_data[0:2])[0]
    cid = struct.unpack('<H', l2cap_data[2:4])[0]
    
    return {
        'handle': handle,
        'pb_flag': pb_flag,
        'bc_flag': bc_flag,
        'total_len': total_len,
        'l2cap_len': l2cap_len,
        'cid': cid,
        'payload': l2cap_data[4:],
    }


ATT_OPCODES = {
    0x01: 'ATT_ERROR_RSP',
    0x02: 'ATT_EXCHANGE_MTU_REQ',
    0x03: 'ATT_EXCHANGE_MTU_RSP',
    0x04: 'ATT_FIND_INFO_REQ',
    0x05: 'ATT_FIND_INFO_RSP',
    0x06: 'ATT_FIND_BY_TYPE_VALUE_REQ',
    0x07: 'ATT_FIND_BY_TYPE_VALUE_RSP',
    0x08: 'ATT_READ_BY_TYPE_REQ',
    0x09: 'ATT_READ_BY_TYPE_RSP',
    0x0A: 'ATT_READ_REQ',
    0x0B: 'ATT_READ_RSP',
    0x0C: 'ATT_READ_BLOB_REQ',
    0x0D: 'ATT_READ_BLOB_RSP',
    0x10: 'ATT_READ_BY_GROUP_TYPE_REQ',
    0x11: 'ATT_READ_BY_GROUP_TYPE_RSP',
    0x12: 'ATT_WRITE_REQ',
    0x13: 'ATT_WRITE_RSP',
    0x1B: 'ATT_HANDLE_VALUE_NTF',
    0x1D: 'ATT_HANDLE_VALUE_IND',
    0x1E: 'ATT_HANDLE_VALUE_CFM',
    0x52: 'ATT_WRITE_CMD',  # write without response
    0xD2: 'ATT_SIGNED_WRITE_CMD',
}


def parse_att(payload):
    """Parse ATT PDU."""
    if len(payload) < 1:
        return None
    
    opcode = payload[0]
    att_name = ATT_OPCODES.get(opcode, f'ATT_UNKNOWN(0x{opcode:02x})')
    
    result = {
        'opcode': opcode,
        'name': att_name,
        'raw': payload,
    }
    
    if opcode in (0x12, 0x52):  # Write Request / Write Command
        if len(payload) >= 3:
            att_handle = struct.unpack('<H', payload[1:3])[0]
            value = payload[3:]
            result['att_handle'] = att_handle
            result['value'] = value
    
    elif opcode == 0x0B:  # Read Response
        result['value'] = payload[1:]
    
    elif opcode == 0x0A:  # Read Request
        if len(payload) >= 3:
            att_handle = struct.unpack('<H', payload[1:3])[0]
            result['att_handle'] = att_handle
    
    elif opcode == 0x1B:  # Handle Value Notification
        if len(payload) >= 3:
            att_handle = struct.unpack('<H', payload[1:3])[0]
            value = payload[3:]
            result['att_handle'] = att_handle
            result['value'] = value
    
    elif opcode == 0x1D:  # Handle Value Indication
        if len(payload) >= 3:
            att_handle = struct.unpack('<H', payload[1:3])[0]
            value = payload[3:]
            result['att_handle'] = att_handle
            result['value'] = value
    
    elif opcode == 0x13:  # Write Response
        pass  # Empty payload
    
    elif opcode == 0x01:  # Error Response
        if len(payload) >= 5:
            req_opcode = payload[1]
            att_handle = struct.unpack('<H', payload[2:4])[0]
            error_code = payload[4]
            result['req_opcode'] = req_opcode
            result['att_handle'] = att_handle
            result['error_code'] = error_code
    
    elif opcode in (0x02, 0x03):  # Exchange MTU
        if len(payload) >= 3:
            mtu = struct.unpack('<H', payload[1:3])[0]
            result['mtu'] = mtu
    
    return result


def parse_hci_event(data):
    """Parse HCI event packet."""
    if len(data) < 2:
        return None
    
    event_code = data[0]
    param_len = data[1]
    params = data[2:]
    
    result = {
        'event_code': event_code,
        'param_len': param_len,
    }
    
    # LE Meta Event
    if event_code == 0x3E and len(params) >= 1:
        sub_event = params[0]
        result['sub_event'] = sub_event
        
        # LE Connection Complete (0x01) or Enhanced Connection Complete (0x0A)
        if sub_event in (0x01, 0x0A):
            if len(params) >= 12:
                status = params[1]
                conn_handle = struct.unpack('<H', params[2:4])[0]
                role = params[4]
                addr_type = params[5]
                peer_addr = ':'.join(f'{b:02X}' for b in reversed(params[6:12]))
                result['status'] = status
                result['conn_handle'] = conn_handle
                result['role'] = role
                result['peer_addr'] = peer_addr
    
    # Disconnection Complete
    elif event_code == 0x05:
        if len(params) >= 4:
            status = params[0]
            conn_handle = struct.unpack('<H', params[1:3])[0]
            reason = params[3]
            result['status'] = status
            result['conn_handle'] = conn_handle
            result['reason'] = reason
    
    return result


def extract_ble_traffic(records):
    """Extract all BLE ATT operations from HCI records."""
    
    # Track connection handles to peer addresses
    handle_to_addr = {}
    
    att_ops = []
    
    for rec in records:
        pkt_type, pkt_data = get_hci_type(rec['data'], rec['is_cmd_evt'], rec['direction'])
        
        if pkt_type == 'HCI_EVT':
            evt = parse_hci_event(pkt_data)
            if evt and 'peer_addr' in evt:
                handle_to_addr[evt['conn_handle']] = evt['peer_addr']
                att_ops.append({
                    'num': rec['num'],
                    'timestamp': rec['timestamp'].isoformat(),
                    'type': 'CONNECTION',
                    'direction': rec['direction'],
                    'peer_addr': evt['peer_addr'],
                    'conn_handle': evt['conn_handle'],
                })
            elif evt and evt['event_code'] == 0x05:
                addr = handle_to_addr.get(evt.get('conn_handle'), '?')
                att_ops.append({
                    'num': rec['num'],
                    'timestamp': rec['timestamp'].isoformat(),
                    'type': 'DISCONNECTION',
                    'direction': rec['direction'],
                    'peer_addr': addr,
                    'conn_handle': evt.get('conn_handle'),
                    'reason': evt.get('reason'),
                })
        
        elif pkt_type == 'HCI_ACL':
            acl = parse_acl_data(pkt_data)
            if acl is None:
                continue
            
            # Only ATT channel (CID 0x0004)
            if acl['cid'] != 0x0004:
                continue
            
            att = parse_att(acl['payload'])
            if att is None:
                continue
            
            peer_addr = handle_to_addr.get(acl['handle'], '?')
            
            entry = {
                'num': rec['num'],
                'timestamp': rec['timestamp'].isoformat(),
                'type': att['name'],
                'direction': rec['direction'],
                'peer_addr': peer_addr,
                'conn_handle': acl['handle'],
                'opcode': f"0x{att['opcode']:02x}",
            }
            
            if 'att_handle' in att:
                entry['att_handle'] = f"0x{att['att_handle']:04x}"
            if 'value' in att:
                entry['value_hex'] = att['value'].hex()
                entry['value_len'] = len(att['value'])
                # Try ASCII
                try:
                    ascii_val = att['value'].decode('ascii')
                    if all(32 <= ord(c) < 127 for c in ascii_val):
                        entry['value_ascii'] = ascii_val
                except (UnicodeDecodeError, ValueError):
                    pass
            if 'mtu' in att:
                entry['mtu'] = att['mtu']
            if 'error_code' in att:
                entry['error_code'] = f"0x{att['error_code']:02x}"
                entry['req_opcode'] = f"0x{att['req_opcode']:02x}"
            
            att_ops.append(entry)
    
    return att_ops


def filter_fan_traffic(att_ops, fan_addr='XX:XX:XX:XX:XX:XX'):
    """Filter ATT operations for the QuietCool fan."""
    return [op for op in att_ops if op.get('peer_addr') == fan_addr]


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Parse btsnoop_hci.log for QuietCool BLE traffic')
    parser.add_argument('logfile', help='Path to btsnoop_hci.log')
    parser.add_argument('--all', action='store_true', help='Show all BLE traffic, not just fan')
    parser.add_argument('--fan-addr', default='XX:XX:XX:XX:XX:XX', help='Fan BLE address')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--writes-only', action='store_true', help='Show only write ops')
    args = parser.parse_args()
    
    records = parse_btsnoop(args.logfile)
    att_ops = extract_ble_traffic(records)
    
    if not args.all:
        ops = filter_fan_traffic(att_ops, args.fan_addr)
        print(f"\nFan traffic ({args.fan_addr}): {len(ops)} operations")
    else:
        ops = att_ops
        print(f"\nAll BLE traffic: {len(ops)} operations")
    
    if args.writes_only:
        ops = [op for op in ops if 'WRITE' in op.get('type', '')]
    
    if args.json:
        print(json.dumps(ops, indent=2))
    else:
        for op in ops:
            ts = op['timestamp'].split('T')[1] if 'T' in op['timestamp'] else op['timestamp']
            line = f"[{ts}] {op['direction']:4s} {op['type']}"
            
            if 'att_handle' in op:
                line += f" handle={op['att_handle']}"
            if 'value_hex' in op:
                vhex = op['value_hex']
                line += f" value={vhex}"
                if 'value_ascii' in op:
                    line += f" ('{op['value_ascii']}')"
            if 'mtu' in op:
                line += f" mtu={op['mtu']}"
            if 'error_code' in op:
                line += f" error={op['error_code']} req={op['req_opcode']}"
            if 'conn_handle' in op:
                line += f" conn=0x{op['conn_handle']:04x}"
            if 'peer_addr' in op:
                line += f" [{op['peer_addr']}]"
            if 'reason' in op:
                line += f" reason=0x{op['reason']:02x}"
            
            print(line)
    
    # Summary
    if not args.json:
        print(f"\n--- Summary ---")
        # Unique addresses
        addrs = set(op.get('peer_addr', '?') for op in att_ops)
        print(f"Unique peer addresses: {addrs}")
        
        # Op types
        types = {}
        for op in (ops):
            t = op.get('type', '?')
            types[t] = types.get(t, 0) + 1
        print(f"Operation types: {types}")


if __name__ == '__main__':
    main()
