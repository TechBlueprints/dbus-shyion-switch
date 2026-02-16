# dbus-shyion-switch

> **Experimental** -- This project is under active development and has not been
> widely tested. Use at your own risk. The BLE protocol, configuration format,
> and D-Bus interface may change without notice.

Venus OS driver for Shyion BLE relay switches (BBKS series) on Victron Cerbo GX
and similar GX devices.  Discovers Shyion battery kill switches over Bluetooth
Low Energy and exposes each relay in the Venus OS **Switches** pane for on/off
control.

## Features

- **BLE Discovery** -- Scans for BBKS relay devices and lists them in the Venus
  OS switches pane with a discovery toggle.
- **Relay Control** -- Toggle individual relays on/off from the Venus OS GUI,
  VRM portal, or any D-Bus client.
- **Robust BLE Handling** -- Connect-on-demand with cross-process locking,
  adapter rotation, phantom connection cleanup, and automatic recovery.
- **Persistent State** -- Remembers discovered devices and their enabled state
  across reboots.
- **No Cloud Dependency** -- All relay control happens over local BLE.  The
  Shyion cloud is only used once during provisioning to discover device MAC
  addresses.

## Supported Devices

- Bluetooth Battery Kill Switch-120A
- Bluetooth Battery Kill Switch-200A
- Bluetooth Battery Kill Switch-250A

These devices use the Telink TLSR8266 BLE chipset and advertise as `BBKS-XXXX`.

## Requirements

- **Venus OS** >= 3.x on a Cerbo GX (or compatible GX device with Bluetooth)
- **Python 3** (included with Venus OS)
- **bleak** -- BLE library (available from
  `/data/apps/dbus-serialbattery/ext` if dbus-serialbattery is installed)

## Quick Install

SSH into your Cerbo GX and run:

```bash
curl -fsSL https://raw.githubusercontent.com/TechBlueprints/dbus-shyion-switch/main/install.sh | bash
```

The installer will:
1. Install git if needed
2. Clone the repository to `/data/apps/dbus-shyion-switch`
3. Initialize the `velib_python` library
4. Create the service symlink and rc.local entry
5. Start the service

## Configuration

### Automatic Provisioning (recommended)

The included `provision.py` script logs into the Shyion cloud, fetches your
device list, and generates a ready-to-use `config.ini` with the correct BLE
MAC addresses.

**On your local machine** (requires `requests`):

```bash
pip3 install requests
python3 provision.py --email your@email.com
```

Copy the generated `config.ini` to your Cerbo:

```bash
scp config.ini root@<cerbo-ip>:/data/apps/dbus-shyion-switch/config.ini
svc -t /service/dbus-shyion-switch   # restart service
```

### Manual Configuration

Copy the default config and add your devices:

```bash
cd /data/apps/dbus-shyion-switch
cp config.default.ini config.ini
vi config.ini
```

Add a section for each relay.  The `mac` field must be the actual BLE
advertisement MAC address (visible in a BLE scanner app):

```ini
[DEFAULT]
scan_interval = 60
name_prefixes = BBKS

[Fridge_Switch]
name = Fridge Switch
mac = A4:C1:38:9E:01:FC
model = Bluetooth Battery Kill Switch-200A

[Running_Lights]
name = Running Lights
mac = A4:C1:38:19:62:00
model = Bluetooth Battery Kill Switch-120A
```

Restart the service after editing:

```bash
svc -t /service/dbus-shyion-switch
```

## Usage

### Discovery

1. Navigate to **Settings -> I/O -> Switches** on the Venus OS GUI
2. Find the **Shyion Relay Manager** device
3. Enable the **Shyion Relay Discovery** toggle
4. Nearby BBKS relays will appear as additional toggles

### Relay Control

Toggle a device switch **ON** to energise the relay (close the circuit).
Toggle **OFF** to de-energise (open the circuit).

The service connects to the device over BLE, sends the command, verifies
the result, and disconnects. State is polled periodically (default: every
24 hours).

### CLI Probe Tool

For testing, `shyion_device.py` can probe a single device from the command
line:

```bash
# Read relay state (read-only)
python3 shyion_device.py --mac A4:C1:38:9E:01:FC --get

# Scan for BBKS devices
python3 shyion_device.py --scan
```

## Service Management

```bash
# View service status
svstat /service/dbus-shyion-switch

# Start / Stop / Restart
svc -u /service/dbus-shyion-switch
svc -d /service/dbus-shyion-switch
svc -t /service/dbus-shyion-switch

# View logs
tail -n 50 /var/log/dbus-shyion-switch/current | tai64nlocal

# Disable (remove from boot)
bash /data/apps/dbus-shyion-switch/disable.sh

# Uninstall completely
bash /data/apps/dbus-shyion-switch/disable.sh
rm -rf /data/apps/dbus-shyion-switch
```

## Architecture

```
dbus-shyion-switch.py       Single-process service
  |  Registers: com.victronenergy.switch.shyion
  |  BLE scanning in background thread
  |  Per-device BLE connections in background threads
  |  Relay control via SwitchableOutput paths
  |
  +-- shyion_ble.py          BLE protocol module
  |     Telink SPP framing (XOR checksum, byte-stuffing)
  |     Connect-on-demand with cross-process locking & adapter rotation
  |     getDeviceStatus / setDeviceStatus commands
  |
  +-- shyion_device.py       Standalone CLI probe tool
  |
  +-- provision.py           Provisioning tool (generates config.ini)
```

### D-Bus Paths

**Service** (`com.victronenergy.switch.shyion`):

| Path | Description |
|------|-------------|
| `/SwitchableOutput/relay_discovery/State` | Discovery toggle (0/1) |
| `/SwitchableOutput/relay_<mac_id>/State` | Relay on/off for device |
| `/SwitchableOutput/relay_<mac_id>/Status` | 0x00=off, 0x09=on, 0x20=disconnected |
| `/Connected` | Service status |

## Troubleshooting

### Device not discovered

- Ensure the relay is powered on and in BLE range
- Check that the BLE adapter is working: `hciconfig hci0`
- Verify the device name starts with `BBKS`
- Check logs: `tail -n 50 /var/log/dbus-shyion-switch/current | tai64nlocal`

### Connection fails

- Verify the BLE MAC address is correct (use `shyion_device.py --scan`)
- Check if another process has a BLE connection to the device
- Try cycling the BLE adapter: `hciconfig hci0 down && sleep 2 && hciconfig hci0 up`

### Relay doesn't toggle

- Ensure BLE connection is established (Status != 0x20)
- Check that the device responds to `getDeviceStatus`:
  `python3 shyion_device.py --mac XX:XX:XX:XX:XX:XX --get`

## License

Apache License 2.0. See [LICENSE](LICENSE) for details.
