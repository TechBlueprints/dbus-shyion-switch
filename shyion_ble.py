#!/usr/bin/env python3
# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
BLE protocol module for Shyion BBKS relay switches (Telink SPP).

These devices use the Telink TLSR8266 chipset and communicate via a custom
serial-port-profile (SPP) protocol over BLE GATT.  The protocol uses:

  - Custom Telink service UUID:  00010203-0405-0607-0809-0a0b0c0d1910
  - Notify characteristic (2b10): device → host responses
  - Write  characteristic (2b11): host → device commands (write-without-response)

Framing:
  - Start marker:  0x6E
  - End marker:    0x6F
  - Escape byte:   0x6D  (followed by 0x4D→0x6D, 0x4E→0x6E, 0x4F→0x6F)
  - Checksum:      XOR of all payload bytes, appended before escaping
  - Frame:         [0x6E] [escaped(payload + xor)] [0x6F]

Intelligent Switch commands (TYPE=0x20):
  - getDeviceStatus:     [0x20, 0x00, 0x00, 0x20, 0x02, 0x01, 0x01]
  - setDeviceStatus(v):  [0x20, 0x00, 0x00, 0x20, 0x01, 0x01, v]
    where v = 0x01 (ON) or 0x02 (OFF)

Status values in getDeviceStatus response:
  - 0x01 = relay ON
  - 0x00 = relay OFF

Response to setDeviceStatus is 7 bytes on success; last byte 0x00 = OK.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import subprocess
import time as _time
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner

logger = logging.getLogger(__name__)

# Cross-process lock shared with dbus-serialbattery to prevent
# simultaneous BLE scans that trigger BlueZ "InProgress" errors.
_BLE_CONNECT_LOCK_PATH = "/tmp/dbus-serialbattery-ble-connect.lock"

# Module-level adapter reset tracking (shared across all sessions)
_consecutive_connect_failures = 0
_last_adapter_reset_time = 0.0
_ADAPTER_RESET_THRESHOLD = 7
_ADAPTER_RESET_COOLDOWN = 300.0  # 5 minutes

# ── Telink SPP UUIDs ──────────────────────────────────────────────────────────

SPP_SERVICE = "00010203-0405-0607-0809-0a0b0c0d1910"
SPP_NOTIFY  = "00010203-0405-0607-0809-0a0b0c0d2b10"
SPP_WRITE   = "00010203-0405-0607-0809-0a0b0c0d2b11"

# BLE name prefix for BBKS relay devices
BBKS_NAME_PREFIX = "BBKS"

# ── Framing constants ─────────────────────────────────────────────────────────

_START = 0x6E
_END   = 0x6F
_ESC   = 0x6D


# ── Frame encoding / decoding ────────────────────────────────────────────────

def _escape(data: bytes) -> bytes:
    """Byte-stuff frame markers and escape byte."""
    out = bytearray()
    for b in data:
        if b == _ESC:
            out.extend([_ESC, 0x4D])
        elif b == _START:
            out.extend([_ESC, 0x4E])
        elif b == _END:
            out.extend([_ESC, 0x4F])
        else:
            out.append(b)
    return bytes(out)


def _unescape(data: bytes) -> bytes:
    """Reverse byte-stuffing."""
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == _ESC and i + 1 < len(data):
            nb = data[i + 1]
            if nb == 0x4D:
                out.append(_ESC)
            elif nb == 0x4E:
                out.append(_START)
            elif nb == 0x4F:
                out.append(_END)
            else:
                out.extend([_ESC, nb])
            i += 2
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def build_frame(payload: list[int] | bytes) -> bytes:
    """Build a Telink SPP frame from a command payload."""
    payload = bytes(payload)
    xor = 0
    for b in payload:
        xor ^= b
    return bytes([_START]) + _escape(payload + bytes([xor])) + bytes([_END])


def parse_frame(data: bytes) -> bytes | None:
    """Parse a Telink SPP frame, returning the verified payload or None."""
    start = data.find(bytes([_START]))
    end = data.rfind(bytes([_END]))
    if start < 0 or end <= start:
        return None

    inner = _unescape(data[start + 1 : end])
    if len(inner) < 2:
        return None

    payload, chk = inner[:-1], inner[-1]
    xor = 0
    for b in payload:
        xor ^= b

    if xor != chk:
        logger.warning("Frame checksum mismatch: expected 0x%02x, got 0x%02x", chk, xor)
        return None

    return bytes(payload)


# ── Command builders ──────────────────────────────────────────────────────────

def cmd_get_status() -> bytes:
    """Build a getDeviceStatus command frame."""
    return build_frame([0x20, 0x00, 0x00, 0x20, 0x02, 0x01, 0x01])


def cmd_set_status(on: bool) -> bytes:
    """Build a setDeviceStatus command frame.

    Value 0x01 = relay ON, 0x02 = relay OFF.
    """
    return build_frame([0x20, 0x00, 0x00, 0x20, 0x01, 0x01, 0x01 if on else 0x02])


# ── Discovery ─────────────────────────────────────────────────────────────────

@dataclass
class DiscoveredRelay:
    """A BBKS relay device found during BLE scanning."""
    mac: str
    name: str
    rssi: int = -100
    model: str = ""


async def scan_for_relays(
    adapters: list[str] | None = None,
    timeout: float = 12.0,
) -> list[DiscoveredRelay]:
    """Scan BLE for BBKS relay devices on the given adapters.

    Acquires the cross-process BLE lock to avoid InProgress conflicts
    with dbus-serialbattery.
    """
    if adapters is None:
        adapters = [""]

    found: dict[str, DiscoveredRelay] = {}

    # Acquire cross-process lock
    lock_fd = None
    lock_acquired = False
    try:
        lock_fd = open(_BLE_CONNECT_LOCK_PATH, "w")
        lock_timeout = _time.time() + 15.0
        while _time.time() < lock_timeout:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_acquired = True
                break
            except (IOError, OSError):
                await asyncio.sleep(0.5)
    except Exception:
        pass

    try:
        for adapter in adapters:
            try:
                kwargs = {}
                if adapter:
                    kwargs["adapter"] = adapter

                scanner = BleakScanner(**kwargs)
                await scanner.start()
                await asyncio.sleep(timeout)
                await scanner.stop()

                for device, adv in scanner.discovered_devices_and_advertisement_data.values():
                    name = device.name or ""
                    if not name.upper().startswith(BBKS_NAME_PREFIX):
                        continue

                    mac = device.address.upper()
                    if mac not in found:
                        found[mac] = DiscoveredRelay(
                            mac=mac,
                            name=name,
                            rssi=adv.rssi,
                            model=name,
                        )
                        logger.info(
                            "Discovered %s (%s) RSSI=%d on %s",
                            name, mac, adv.rssi, adapter or "default",
                        )

            except Exception:
                logger.exception("BLE scan failed on adapter %s", adapter or "default")
    finally:
        if lock_fd is not None:
            try:
                if lock_acquired:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass

    return list(found.values())


# ── Session-based BLE client ──────────────────────────────────────────────────

class ShyionSession:
    """Transactional BLE session for a single Shyion BBKS relay device.

    Connects on demand, sends commands, and disconnects when no longer needed.
    All methods are async and must be called from an asyncio event loop.

    Usage::

        session = ShyionSession("A4:C1:38:9E:01:FC")
        await session.connect()
        state = await session.get_relay_state()
        await session.set_relay(True)
        await session.disconnect()
    """

    def __init__(self, address: str, adapter: str = ""):
        self._address = address
        self._adapter = adapter
        self._client: BleakClient | None = None
        self._response: bytes | None = None
        self._response_event = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # ── Connection management ─────────────────────────────────────────────

    async def connect(self, adapters: list[str] | None = None) -> bool:
        """Scan for the device, clean up stale BlueZ state, and connect.

        Incorporates lessons from dbus-serialbattery:
        - Cross-process file lock to avoid BlueZ InProgress errors
        - Phantom connection detection and cleanup
        - HCI LE_Create_Connection_Cancel for stuck pending connections
        - start_notify with timeout
        - GATT service resolution check
        - Adapter rotation on InProgress with escalating delays
        - Adapter reset after persistent failures

        Returns True on success, False on failure.
        """
        global _consecutive_connect_failures, _last_adapter_reset_time

        if self.is_connected:
            return True

        if adapters is None:
            if self._adapter:
                adapters = [self._adapter]
            else:
                adapters = ["hci0", "hci1"]

        # ── Acquire cross-process BLE lock ────────────────────────────
        lock_fd = None
        lock_acquired = False
        logger.debug("[%s] Acquiring BLE lock...", self._address)
        try:
            lock_fd = open(_BLE_CONNECT_LOCK_PATH, "w")
            lock_timeout = _time.time() + 15.0
            while _time.time() < lock_timeout:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_acquired = True
                    break
                except (IOError, OSError):
                    await asyncio.sleep(0.5)
            if not lock_acquired:
                logger.warning("[%s] BLE lock timeout after 15s, proceeding",
                               self._address)
            else:
                logger.debug("[%s] BLE lock acquired", self._address)
        except Exception as e:
            logger.warning("[%s] BLE lock unavailable: %s", self._address, e)

        try:
            return await self._connect_inner(adapters)
        finally:
            # Release lock
            if lock_fd is not None:
                try:
                    if lock_acquired:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception:
                    pass

    async def _connect_inner(self, adapters: list[str]) -> bool:
        """Core connection logic (called while holding the BLE lock).

        Strategy:
        1. Try direct connect (no scan) on each adapter — fast, works if
           BlueZ has the device cached from a prior scan.
        2. If direct connect fails, do phantom cleanup then try targeted
           scan + connect on each adapter.
        3. If all fail, escalate (BlueZ state clear, adapter reset).
        """
        global _consecutive_connect_failures, _last_adapter_reset_time

        inprogress_failures = 0

        # ── Phase 1: Direct connect (no scan overhead) ────────────────
        for adapter in adapters:
            adapter_name = adapter or "default"
            try:
                logger.info("[%s] Direct connect on %s...",
                            self._address, adapter_name)
                client_kwargs = {"timeout": 10.0}
                if adapter:
                    client_kwargs["adapter"] = adapter
                self._client = BleakClient(self._address, **client_kwargs)

                await asyncio.wait_for(
                    self._client.connect(), timeout=12.0
                )

                # Verify GATT services resolved
                if not self._client.services:
                    logger.warning("[%s] GATT services empty on %s",
                                   self._address, adapter_name)
                    await self._safe_disconnect()
                    continue

                logger.info("[%s] Connected on %s (direct)", self._address,
                            adapter_name)

                # Allow GATT service resolution to settle
                await asyncio.sleep(1.0)
                await asyncio.wait_for(
                    self._setup_notifications(), timeout=5.0
                )

                _consecutive_connect_failures = 0
                return True

            except Exception as e:
                error_str = repr(e)
                await self._safe_disconnect()

                if "InProgress" in error_str:
                    inprogress_failures += 1
                    logger.info("[%s] %s busy (InProgress, direct)",
                                self._address, adapter_name)
                    await asyncio.sleep(0.5)
                    continue

                logger.info("[%s] Direct connect on %s failed: %s",
                            self._address, adapter_name, error_str)

        # ── Phase 2: Phantom cleanup + scan-based connect ─────────────
        logger.info("[%s] Direct connect failed, cleaning up BlueZ...",
                    self._address)
        await self._cleanup_stale_bluez(adapters)

        for adapter in adapters:
            adapter_name = adapter or "default"

            # Targeted scan with hard timeout
            logger.info("[%s] Scanning on %s...", self._address, adapter_name)
            device = None
            try:
                kwargs = {"timeout": 10.0}
                if adapter:
                    kwargs["adapter"] = adapter
                device = await asyncio.wait_for(
                    BleakScanner.find_device_by_address(
                        self._address, **kwargs
                    ),
                    timeout=12.0,
                )
            except asyncio.TimeoutError:
                logger.warning("[%s] Scan on %s timed out",
                               self._address, adapter_name)
            except Exception as e:
                error_str = repr(e)
                if "InProgress" in error_str:
                    inprogress_failures += 1
                    logger.info("[%s] %s busy (InProgress, scan)",
                                self._address, adapter_name)
                    await asyncio.sleep(0.5)
                    continue
                logger.warning("[%s] Scan on %s failed: %s",
                               self._address, adapter_name, e)

            target = device if device is not None else self._address

            try:
                logger.info("[%s] Connecting on %s...",
                            self._address, adapter_name)
                client_kwargs = {"timeout": 15.0}
                if adapter:
                    client_kwargs["adapter"] = adapter
                self._client = BleakClient(target, **client_kwargs)

                await asyncio.wait_for(
                    self._client.connect(), timeout=15.0
                )

                if not self._client.services:
                    logger.warning("[%s] GATT services empty on %s",
                                   self._address, adapter_name)
                    await self._safe_disconnect()
                    continue

                logger.info("[%s] Connected on %s (scan)", self._address,
                            adapter_name)

                # Allow GATT service resolution to settle
                await asyncio.sleep(1.0)
                await asyncio.wait_for(
                    self._setup_notifications(), timeout=5.0
                )

                _consecutive_connect_failures = 0
                return True

            except Exception as e:
                error_str = repr(e)
                await self._safe_disconnect()

                if "InProgress" in error_str:
                    inprogress_failures += 1
                    logger.info("[%s] %s busy (InProgress, connect)",
                                self._address, adapter_name)
                    await asyncio.sleep(0.5)
                    continue

                logger.warning("[%s] Connect on %s failed: %s",
                               self._address, adapter_name, error_str)
                await asyncio.sleep(0.3)

        # ── All attempts exhausted ────────────────────────────────────

        if inprogress_failures >= len(adapters):
            logger.warning("[%s] InProgress dominated (%d), clearing BlueZ",
                           self._address, inprogress_failures)
            await self._cancel_pending_hci(adapters)
            self._run_cmd(["bluetoothctl", "remove", self._address])
            await asyncio.sleep(3.0)

        _consecutive_connect_failures += 1
        logger.warning("[%s] Connect failed (%d/%d before adapter reset)",
                       self._address, _consecutive_connect_failures,
                       _ADAPTER_RESET_THRESHOLD)

        if (
            _consecutive_connect_failures >= _ADAPTER_RESET_THRESHOLD
            and (_time.time() - _last_adapter_reset_time) > _ADAPTER_RESET_COOLDOWN
        ):
            await self._reset_adapters(adapters)
            _last_adapter_reset_time = _time.time()
            _consecutive_connect_failures = 0

        return False

    async def _safe_disconnect(self):
        """Disconnect with timeout, swallowing errors."""
        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.disconnect(), timeout=3.0)
            except Exception:
                pass
            self._client = None

    async def disconnect(self):
        """Cleanly disconnect from the device."""
        if self._client is not None:
            try:
                if self._client.is_connected:
                    await self._client.disconnect()
                    logger.info("[%s] Disconnected", self._address)
            except Exception:
                logger.exception("[%s] Error during disconnect", self._address)
            finally:
                self._client = None

    # ── Relay commands ────────────────────────────────────────────────────

    async def get_relay_state(self) -> bool | None:
        """Query the current relay state.

        Returns True (ON), False (OFF), or None on communication failure.
        """
        if not self.is_connected:
            return None

        resp = await self._send_command(cmd_get_status(), "getDeviceStatus")
        if resp is None:
            return None

        if len(resp) >= 7 and resp[0] == 0x20 and resp[3] == 0x20 and resp[4] == 0x02:
            on = bool(resp[6])
            logger.info("[%s] Relay state: %s", self._address, "ON" if on else "OFF")
            return on

        logger.warning("[%s] Unexpected getDeviceStatus response: %s",
                       self._address, resp.hex())
        return None

    async def set_relay(self, on: bool) -> bool | None:
        """Set the relay state and verify with a follow-up status read.

        Returns the confirmed relay state (True/False) or None on failure.
        """
        if not self.is_connected:
            return None

        label = "ON" if on else "OFF"
        frame = cmd_set_status(on)
        resp = await self._send_command(frame, "setDeviceStatus(%s)" % label)

        if resp is None:
            logger.warning("[%s] No ACK for set_relay(%s)", self._address, label)
            return None

        # Verify: set ACK byte 6 is result code (0x00 = success), not state.
        if len(resp) >= 7 and resp[0] == 0x20 and resp[4] == 0x01:
            result_code = resp[6]
            if result_code != 0x00:
                logger.warning("[%s] set_relay(%s) result code: 0x%02x",
                               self._address, label, result_code)
                return None
        else:
            logger.warning("[%s] Unexpected set response: %s",
                           self._address, resp.hex())

        # Read back actual state to confirm
        await asyncio.sleep(0.3)
        confirmed = await self.get_relay_state()
        if confirmed is not None:
            logger.info("[%s] set_relay(%s) confirmed: %s",
                        self._address, label, "ON" if confirmed else "OFF")
        return confirmed

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _run_cmd(cmd: list[str], timeout: int = 5) -> str:
        """Run a subprocess command, returning stdout or empty string."""
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            return result.stdout or ""
        except Exception:
            return ""

    async def _cleanup_stale_bluez(self, adapters: list[str]):
        """Detect and clean up phantom/stale BlueZ connections.

        Phantom = BlueZ reports Connected=yes but no HCI handle exists.
        Fix: bluetoothctl remove to forget the stale state.

        Also cancels pending LE Create Connection on each adapter and
        drops any stale HCI handles for this device.
        """
        # Check if BlueZ thinks the device is connected
        info_out = self._run_cmd(["bluetoothctl", "info", self._address], timeout=3)
        bluez_connected = "Connected: yes" in info_out

        if bluez_connected:
            # Check for real HCI handle
            has_handle = False
            for adapter in adapters:
                if not adapter:
                    continue
                con_out = self._run_cmd(
                    ["hcitool", "-i", adapter, "con"], timeout=3
                )
                if self._address.upper() in con_out.upper():
                    has_handle = True
                    break

            if not has_handle:
                # Phantom: Connected=yes but no HCI handle
                logger.warning("[%s] Phantom detected (Connected=yes, no HCI handle) "
                               "— removing from BlueZ", self._address)
                self._run_cmd(["bluetoothctl", "remove", self._address])
                await asyncio.sleep(3.0)
            else:
                # Real connection exists — disconnect it
                logger.info("[%s] Stale connection found, disconnecting",
                            self._address)
                self._run_cmd(["bluetoothctl", "disconnect", self._address])
                await asyncio.sleep(1.0)

        # Cancel pending LE connections and drop stale handles
        await self._cancel_pending_hci(adapters)

    async def _cancel_pending_hci(self, adapters: list[str]):
        """Send HCI LE_Create_Connection_Cancel and drop stale handles."""
        for adapter in adapters:
            if not adapter:
                continue
            # Cancel any pending LE Create Connection
            self._run_cmd(
                ["hcitool", "-i", adapter, "cmd", "0x08", "0x000E"], timeout=3
            )
            # Drop stale HCI handles for this address
            con_out = self._run_cmd(
                ["hcitool", "-i", adapter, "con"], timeout=3
            )
            for line in con_out.splitlines():
                if self._address.upper() in line.upper():
                    parts = line.split("handle ")
                    if len(parts) > 1:
                        handle = parts[1].split()[0]
                        self._run_cmd(
                            ["hcitool", "-i", adapter, "ledc", handle],
                            timeout=3,
                        )
                        logger.info("[%s] Dropped stale handle %s on %s",
                                    self._address, handle, adapter)
                        await asyncio.sleep(0.5)

    async def _reset_adapters(self, adapters: list[str]):
        """Reset BLE adapters via hciconfig down/up as a last resort."""
        for adapter in adapters:
            if not adapter:
                continue
            logger.warning("[%s] Resetting adapter %s", self._address, adapter)
            self._run_cmd(["hciconfig", adapter, "down"], timeout=5)
            await asyncio.sleep(1.0)
            self._run_cmd(["hciconfig", adapter, "up"], timeout=5)

        # Check bluetoothd is still running
        ps_out = self._run_cmd(["pidof", "bluetoothd"], timeout=3)
        if not ps_out.strip():
            logger.warning("[%s] bluetoothd not running, restarting", self._address)
            self._run_cmd(["/etc/init.d/bluetooth", "start"], timeout=10)

        await asyncio.sleep(3.0)
        logger.info("[%s] Adapter reset complete", self._address)

    async def _setup_notifications(self):
        """Subscribe to SPP notify characteristic and handle CCCD."""
        if self._client is None:
            return

        # Log available services for diagnostics
        svc_uuids = [str(s.uuid) for s in self._client.services]
        logger.debug("[%s] Services: %s", self._address, svc_uuids)

        await self._client.start_notify(SPP_NOTIFY, self._on_notify)

        for svc in self._client.services:
            if "1910" in str(svc.uuid):
                for ch in svc.characteristics:
                    if "2b10" in str(ch.uuid):
                        for desc in ch.descriptors:
                            if "2902" in str(desc.uuid):
                                cccd = await self._client.read_gatt_descriptor(desc.handle)
                                if cccd == b'\x00\x00':
                                    logger.info("[%s] Enabling CCCD", self._address)
                                    await self._client.write_gatt_descriptor(
                                        desc.handle, b'\x01\x00'
                                    )

    def _on_notify(self, _sender, data: bytearray):
        """Handle incoming BLE notification."""
        payload = parse_frame(bytes(data))
        if payload is None:
            logger.debug("[%s] Unparseable notification: %s", self._address, data.hex())
            return

        self._response = payload
        self._response_event.set()

    async def _send_command(self, frame: bytes, label: str) -> bytes | None:
        """Send a command frame and wait for the notification response."""
        if self._client is None or not self._client.is_connected:
            return None

        # Recreate the event on the current running loop to avoid
        # cross-loop issues when sessions are reused across threads.
        self._response_event = asyncio.Event()
        self._response = None

        try:
            await self._client.write_gatt_char(SPP_WRITE, frame, response=False)
        except Exception:
            logger.exception("[%s] Write failed for %s", self._address, label)
            return None

        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=5.0)
            return self._response
        except asyncio.TimeoutError:
            logger.warning("[%s] No response for %s (5s timeout)", self._address, label)
            return None
