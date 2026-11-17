# Shyion BBKS Relay Protocol Reference

## Overview

Shyion BBKS (Bluetooth Battery Kill Switch) devices are single-channel relay
switches built on the **Telink TLSR8266** BLE chipset. They communicate via a
proprietary serial-port-profile (SPP) protocol over BLE GATT.

No authentication or pairing is required. The device is controlled entirely
through GATT writes and notifications.

## Supported Models

| Model | Amperage | Device type code | Advertised name |
|-------|----------|------------------|-----------------|
| Bluetooth Battery Kill Switch-120A | 120A | 10 | `BBKS-2023-120A` |
| Bluetooth Battery Kill Switch-200A | 200A | 8 | `BBKS-2023-200A` |
| Bluetooth Battery Kill Switch-250A | 250A | 11 | `BBKS-2023-250A` |

All models use the same BLE protocol. Each model carries a numeric type code
(above) that determines its `BBKS-2023-NNNA` name. A device does not have to
be found by name: scanning unfiltered and identifying by type code also
works. Matching on a `BBKS` name prefix, as this driver does, is a
convenience that holds because the devices advertise a `BBKS-…` name; it is
not something the protocol requires. Only the `2023` model year has been
seen.

The type code is the first thing to check if a device with a different
advertised name does not respond as described here: the relay command header
(see Command Format) is specific to this device family, and other Shyion
products (for example the temperature/humidity sensors) use a different value
in header byte 3.

## BLE GATT Profile

### Telink SPP Service

| UUID | Type | Description |
|------|------|-------------|
| `00010203-0405-0607-0809-0a0b0c0d1910` | Service | Telink SPP service |
| `00010203-0405-0607-0809-0a0b0c0d2b10` | Characteristic | Notify (device → host) |
| `00010203-0405-0607-0809-0a0b0c0d2b11` | Characteristic | Write (host → device, no-response) |

### Telink OTA Service

Used only for emergency firmware restart (see below).

| UUID | Type | Properties |
|------|------|------------|
| `00010203-0405-0607-0809-0a0b0c0d1912` | Service | Telink OTA service |
| `00010203-0405-0607-0809-0a0b0c0d2b12` | Characteristic | Read, Write-without-response |

### CCCD (Notifications)

Before the device will send notification responses, the Client Characteristic
Configuration Descriptor (CCCD, UUID `0x2902`) on the Notify characteristic
(`2b10`) must be set to `0x0100` (notifications enabled).

Most BLE stacks handle this automatically when subscribing to notifications;
a single standard enable-notification write of `0x0100` is all the protocol
calls for.

*Troubleshooting note (field experience, not part of the protocol):* if
notifications are not received after subscribing, reading the CCCD and
writing `0x01 0x00` when it reads `0x00 0x00` has been used as a workaround
for a stack that did not apply the write.

## Frame Format

All commands and responses are wrapped in the Telink SPP framing protocol.

### Structure

```
[START] [escaped(payload + checksum)] [END]
```

| Field | Value | Description |
|-------|-------|-------------|
| START | `0x6E` | Frame start marker |
| END | `0x6F` | Frame end marker |
| ESC | `0x6D` | Escape byte |
| Checksum | XOR | XOR of all payload bytes |

### Byte Stuffing (Escaping)

Before transmission, the payload + checksum bytes are escaped. Any occurrence
of the three reserved bytes within the data is replaced with a two-byte
escape sequence:

| Original byte | Escaped as |
|---------------|------------|
| `0x6D` (ESC) | `0x6D 0x4D` |
| `0x6E` (START) | `0x6D 0x4E` |
| `0x6F` (END) | `0x6D 0x4F` |

### Encoding

1. Compute XOR checksum over all payload bytes
2. Append the checksum byte to the payload
3. Apply byte stuffing to the combined payload + checksum
4. Wrap with START and END markers

**Example** — encoding payload `[0x20, 0x00, 0x00, 0x20, 0x01, 0x01, 0x01]`:

1. XOR checksum: `0x20 ^ 0x00 ^ 0x00 ^ 0x20 ^ 0x01 ^ 0x01 ^ 0x01 = 0x01`
2. With checksum: `[0x20, 0x00, 0x00, 0x20, 0x01, 0x01, 0x01, 0x01]`
3. Escape (no reserved bytes present — unchanged)
4. Frame: `6E 20 00 00 20 01 01 01 01 6F`

### Decoding

1. Strip START (`0x6E`) and END (`0x6F`) markers
2. Reverse byte stuffing on the inner bytes
3. Split into payload (all but last byte) and checksum (last byte)
4. Verify: XOR of payload bytes must equal the checksum byte

## Command Format

All relay commands use the **Intelligent Switch** command type (`0x20`).

### Payload Structure

```
[0x20] [0x00] [0x00] [0x20] [CMD] [PARAM_COUNT] [PARAMS...]
```

| Byte | Name | Value |
|------|------|-------|
| 0 | Type | `0x20` |
| 1 | Reserved | `0x00` |
| 2 | Reserved | `0x00` |
| 3 | Type (repeated) | `0x20` |
| 4 | Command | See command table |
| 5 | Parameter count | Number of parameter bytes that follow |
| 6+ | Parameters | Command-specific |

### Commands

| CMD | Name | Full Payload |
|-----|------|--------------|
| `0x01` | Set relay state | `[0x20, 0x00, 0x00, 0x20, 0x01, 0x01, value]` |
| `0x02` | Get relay state | `[0x20, 0x00, 0x00, 0x20, 0x02, 0x01, 0x01]` |

### Relay State Values

The ON/OFF encoding differs between setting and reading:

| Context | ON | OFF |
|---------|-----|-----|
| Set command (byte 6) | `0x01` | `0x02` |
| Get response (byte 6) | `0x01` | `0x00` |

**Important:** To set the relay OFF, send `0x02`. The value `0x00` is not a
valid set value — the device will acknowledge it but not change state.

## Complete Command Reference

### Get Relay State

Query the current relay state.

**Request payload:**
```
[0x20, 0x00, 0x00, 0x20, 0x02, 0x01, 0x01]
```

**Request frame (hex):**
```
6E 20 00 00 20 02 01 01 02 6F
```

**Response payload (relay ON):**
```
[0x20, 0x00, 0x00, 0x20, 0x02, 0x01, 0x01]
                                       ^^^^
                                       0x01 = ON
```

**Response payload (relay OFF):**
```
[0x20, 0x00, 0x00, 0x20, 0x02, 0x01, 0x00]
                                       ^^^^
                                       0x00 = OFF
```

### Set Relay ON

Turn the relay ON (close circuit).

**Request payload:**
```
[0x20, 0x00, 0x00, 0x20, 0x01, 0x01, 0x01]
```

**Request frame (hex):**
```
6E 20 00 00 20 01 01 01 01 6F
```

**Response payload:**
```
[0x20, 0x00, 0x00, 0x20, 0x01, 0x01, 0x00]
                                       ^^^^
                                       0x00 = success
```

### Set Relay OFF

Turn the relay OFF (open circuit).

**Request payload:**
```
[0x20, 0x00, 0x00, 0x20, 0x01, 0x01, 0x02]
```

**Request frame (hex):**
```
6E 20 00 00 20 01 01 02 02 6F
```

**Response payload:**
```
[0x20, 0x00, 0x00, 0x20, 0x01, 0x01, 0x00]
                                       ^^^^
                                       0x00 = success
```

### Response Format

**Get relay state response** (7 bytes):
```
Byte 0-3: Header [0x20, 0x00, 0x00, 0x20]
Byte 4:   Command echo (0x02)
Byte 5:   Parameter count (0x01)
Byte 6:   Relay state — 0x00 = OFF, 0x01 = ON
```

**Set relay state response — success** (7 bytes):
```
Byte 0-3: Header [0x20, 0x00, 0x00, 0x20]
Byte 4:   Command echo (0x01)
Byte 5:   Value echo (the value that was sent)
Byte 6:   Result — 0x00 = success
```

### Timeouts

Responses arrive as BLE notifications within 1-2 seconds. A timeout of
5 seconds with no notification should be treated as a communication failure.

## Connection Sequence

1. **Scan** for BLE devices with names starting with `BBKS` (or identify them
   by type code, see Supported Models)
2. **Connect** via GATT (no pairing required)
3. **Subscribe to notifications** on the SPP Notify characteristic (`2b10`)
   — see CCCD note above if notifications are not received
4. **Get relay state** to read the current position
5. **Set relay state** with `0x01` (ON) or `0x02` (OFF) as needed
6. **Poll** get relay state periodically to track changes
7. **Disconnect** when done (or maintain connection for monitoring)

## Emergency Firmware Restart (OTA Watchdog Reboot)

If the device becomes unresponsive to SPP commands, it can be recovered
without a physical power cycle using the Telink OTA service.

**Status of this section:** the OTA PDU *format* below (layout, little-endian
index and CRC, CRC16 over the first 18 bytes) is the standard firmware-update
framing for these devices. The *recovery procedure* itself (three all-`0xFF`
PDUs, then disconnect, then a watchdog reboot) is an empirical technique, not
a documented device behavior: nothing in the normal update flow reboots the
device this way, and a real update sends as many PDUs as the firmware image
needs. Confirm it on the device in hand before depending on it.

### Symptoms of a stuck device

- BLE connection and GATT discovery succeed normally
- Writes to the SPP Write characteristic (`2b11`) succeed without error
- **No notification responses** are ever received on the Notify characteristic

### Recovery Procedure

The OTA characteristic (`2b12`) is handled by the Telink chip's ROM
bootloader, independent of the application firmware. Writing OTA PDUs puts
the device into DFU mode. Disconnecting while in DFU mode triggers a
watchdog timeout that reboots the chip, reloading the application firmware
from flash.

1. Connect to the device via BLE (GATT still works even when stuck)
2. Write 3 OTA PDUs to the OTA characteristic (`2b12`)
3. Disconnect immediately
4. Wait ~15 seconds for the watchdog to fire
5. Reconnect and verify with a get relay state command

### OTA PDU Format

Each PDU is 20 bytes:

```
[index_lo, index_hi, data (16 bytes), crc_lo, crc_hi]
```

| Field | Size | Description |
|-------|------|-------------|
| Index | 2 bytes, little-endian | Block index (0, 1, 2) |
| Data | 16 bytes | Use `0xFF` fill for reboot trigger |
| CRC | 2 bytes, little-endian | CRC16 over the 18 index + data bytes |

The CRC16 algorithm uses initial value `0xFFFF` and polynomial `0xA001`:

```
crc = 0xFFFF
for each byte b in (index + data):
    crc = crc XOR b
    repeat 8 times:
        if (crc AND 1):
            crc = (crc >> 1) XOR 0xA001
        else:
            crc = crc >> 1
```

## Safety Notes

- Only use command bytes `0x01` (set) and `0x02` (get) with type `0x20`
- **Do not** send other command bytes — undefined commands can crash the
  application firmware, requiring an OTA reboot or physical power cycle
- If the device becomes unresponsive, use the OTA watchdog reboot procedure
  above before resorting to a physical power cycle
- The value `0x00` in a set command is ignored by the device (it is not OFF)
- The relay controls a high-current DC circuit; ensure the load is safe to
  switch before toggling remotely
