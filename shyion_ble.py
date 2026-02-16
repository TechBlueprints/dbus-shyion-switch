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

Connection model: persistent connections maintained by ShyionConnection.
Uses bleak-connection-manager for all BLE operations.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass

from bleak import BleakClient

from bleak_connection_manager import (
    ConnectionWatchdog,
    EscalationConfig,
    EscalationPolicy,
    LockConfig,
    ScanLockConfig,
    establish_connection,
    managed_discover,
    managed_find_device,
    validate_gatt_services,
)

logger = logging.getLogger(__name__)

# ── Telink SPP UUIDs ──────────────────────────────────────────────────────────

SPP_SERVICE = "00010203-0405-0607-0809-0a0b0c0d1910"
SPP_NOTIFY  = "00010203-0405-0607-0809-0a0b0c0d2b10"
SPP_WRITE   = "00010203-0405-0607-0809-0a0b0c0d2b11"

# BLE name prefix for BBKS relay devices
BBKS_NAME_PREFIX = "BBKS"

# Watchdog timeout: if no notification arrives for 20 minutes, the
# connection is probably dead.  With 60-second keepalive, healthy connections
# will see activity well within this window.
WATCHDOG_TIMEOUT = 300.0  # 5 minutes

# Telink BLE chips disconnect idle connections after ~2 minutes.  Send a
# state query periodically to keep the connection alive.
KEEPALIVE_INTERVAL = 60.0  # seconds

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
    timeout: float = 12.0,
    scan_lock_config: ScanLockConfig | None = None,
) -> list[DiscoveredRelay]:
    """Scan BLE for BBKS relay devices.

    Uses bleak-connection-manager's managed_discover for automatic
    adapter rotation, scan locking, and InProgress retry.
    """
    if scan_lock_config is None:
        scan_lock_config = ScanLockConfig(enabled=True)

    devices = await managed_discover(
        timeout=timeout,
        scan_lock_config=scan_lock_config,
    )

    found: dict[str, DiscoveredRelay] = {}
    for device in devices:
        name = device.name or ""
        if not name.upper().startswith(BBKS_NAME_PREFIX):
            continue

        mac = device.address.upper()
        if mac not in found:
            found[mac] = DiscoveredRelay(
                mac=mac,
                name=name,
                rssi=getattr(device, "rssi", -100),
                model=name,
            )
            logger.info(
                "Discovered %s (%s) on scan", name, mac,
            )

    return list(found.values())


# ── Persistent BLE connection ────────────────────────────────────────────────

class ShyionConnection:
    """Persistent BLE connection to a Shyion BBKS relay device.

    Maintains a long-lived connection using bleak-connection-manager.
    Runs its own asyncio event loop in a daemon thread.
    Commands from the main (GLib) thread are dispatched via
    ``run_coroutine_threadsafe``.

    Uses ConnectionWatchdog to detect dead radio links — the watchdog
    is fed on every notification response from the device.
    """

    def __init__(
        self,
        address: str,
        reconnect_delay: float = 10.0,
        reconnect_max_delay: float = 120.0,
        lock_config: LockConfig | None = None,
        scan_lock_config: ScanLockConfig | None = None,
    ):
        self._address = address
        self._reconnect_delay = reconnect_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._lock_config = lock_config or LockConfig(enabled=True)
        self._scan_lock_config = scan_lock_config or ScanLockConfig(enabled=True)

        self._client: BleakClient | None = None
        self._connected = threading.Event()
        self._running = True

        # asyncio event loop reference (set by daemon thread)
        self._loop: asyncio.AbstractEventLoop | None = None

        # Command serialisation (one command at a time per device)
        self._command_lock: asyncio.Lock | None = None

        # Notification response handling
        self._response: bytes | None = None
        self._response_event: asyncio.Event | None = None

        # Connection watchdog
        self._watchdog: ConnectionWatchdog | None = None

        # Sleep task for interruptible delays
        self._sleep_task: asyncio.Task | None = None

        # Start daemon thread
        self._thread = threading.Thread(
            name="Shyion_BLE_%s" % address.replace(":", ""),
            target=self._run_loop,
            daemon=True,
        )
        self._thread.start()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def stop(self, timeout: float = 5.0):
        """Signal the BLE thread to stop and wait for clean disconnect."""
        if not self._running:
            return
        self._running = False

        if self._watchdog is not None:
            self._watchdog.stop()

        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._cancel_sleep)

        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                "[%s] BLE thread did not stop within %.1fs",
                self._address, timeout,
            )

    # ── Thread-safe command API (called from GLib main thread) ─────────

    def get_relay_state(self, timeout: float = 20.0) -> bool | None:
        """Query the current relay state (thread-safe, blocking).

        Returns True (ON), False (OFF), or None on failure.
        """
        return self._dispatch(self._get_relay_state(), timeout)

    def set_relay(self, on: bool, timeout: float = 20.0) -> bool | None:
        """Set relay state and verify (thread-safe, blocking).

        Returns the confirmed relay state (True/False) or None on failure.
        """
        return self._dispatch(self._set_relay(on), timeout)

    def _dispatch(self, coro, timeout: float):
        """Dispatch an async coroutine to the connection's event loop."""
        loop = self._loop
        if loop is None or not self._connected.is_set():
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=timeout)
        except Exception:
            logger.exception(
                "[%s] Command dispatch failed", self._address
            )
            return None

    # ── Daemon thread ─────────────────────────────────────────────────────

    def _cancel_sleep(self):
        if self._sleep_task is not None and not self._sleep_task.done():
            self._sleep_task.cancel()

    def _run_loop(self):
        """Daemon thread entry point."""
        while self._running:
            try:
                loop = asyncio.new_event_loop()
                self._loop = loop
                loop.run_until_complete(self._async_main())
                loop.close()
            except Exception:
                logger.exception(
                    "[%s] BLE daemon loop crashed, restarting...",
                    self._address,
                )
                time.sleep(self._reconnect_delay)
            finally:
                self._loop = None

    async def _async_main(self):
        """Connect and stay connected, auto-reconnecting on failure.

        Uses bleak-connection-manager for all BLE operations:
        1. managed_find_device — scan with lock + adapter rotation
        2. establish_connection — connect with all workarounds
        3. ConnectionWatchdog — detect dead radio links
        """
        self._command_lock = asyncio.Lock()
        delay = self._reconnect_delay
        escalation = EscalationPolicy([], config=EscalationConfig(reset_adapter=True))

        while self._running:
            client: BleakClient | None = None
            device = None

            try:
                # Step 1: Find the device
                logger.info("[%s] Scanning...", self._address)
                device = await managed_find_device(
                    self._address,
                    timeout=15.0,
                    max_attempts=3,
                    scan_lock_config=self._scan_lock_config,
                )

                if device is None:
                    logger.warning(
                        "[%s] Not found, retrying in %.0fs",
                        self._address, delay,
                    )
                    await self._interruptible_sleep(delay)
                    delay = min(delay * 1.5, self._reconnect_max_delay)
                    continue

                # Step 2: Connect via BCM
                logger.info("[%s] Connecting...", self._address)
                client = await establish_connection(
                    BleakClient,
                    device,
                    "Shyion %s" % self._address,
                    max_attempts=4,
                    close_inactive_connections=True,
                    try_direct_first=True,
                    validate_connection=validate_gatt_services,
                    lock_config=self._lock_config,
                    escalation_policy=escalation,
                )

                # Step 3: Set up notifications
                logger.info("[%s] Connected (MTU: %d)", self._address, client.mtu_size)
                self._client = client

                await asyncio.wait_for(
                    self._setup_notifications(), timeout=5.0
                )

                self._connected.set()
                delay = self._reconnect_delay  # reset backoff

                # Step 4: Start connection watchdog
                self._watchdog = ConnectionWatchdog(
                    timeout=WATCHDOG_TIMEOUT,
                    on_timeout=self._on_watchdog_timeout,
                    client=client,
                    device=device,
                )
                self._watchdog.start()

                # Step 5: Stay connected with periodic keepalive
                last_keepalive = asyncio.get_event_loop().time()
                while client.is_connected and self._running:
                    await self._interruptible_sleep(1.0)
                    now = asyncio.get_event_loop().time()
                    if now - last_keepalive >= KEEPALIVE_INTERVAL:
                        last_keepalive = now
                        try:
                            state = await self._get_relay_state()
                            if state is not None:
                                logger.debug(
                                    "[%s] Keepalive: relay %s",
                                    self._address,
                                    "ON" if state else "OFF",
                                )
                        except Exception:
                            logger.debug(
                                "[%s] Keepalive query failed",
                                self._address, exc_info=True,
                            )

                logger.info("[%s] Connection loop ended", self._address)

            except Exception:
                logger.exception(
                    "[%s] BLE error, retrying in %.0fs",
                    self._address, delay,
                )

            finally:
                self._connected.clear()
                self._client = None

                if self._watchdog is not None:
                    self._watchdog.stop()
                    self._watchdog = None

                if client is not None:
                    try:
                        await asyncio.wait_for(
                            client.disconnect(), timeout=5.0
                        )
                    except Exception:
                        pass

            if not self._running:
                break

            logger.warning("[%s] Disconnected, reconnecting in %.0fs",
                           self._address, delay)
            await self._interruptible_sleep(delay)
            delay = min(delay * 1.5, self._reconnect_max_delay)

    def _on_watchdog_timeout(self):
        """Called by ConnectionWatchdog when no activity for WATCHDOG_TIMEOUT."""
        logger.warning(
            "[%s] Watchdog timeout (%.0fs no activity), forcing reconnect",
            self._address, WATCHDOG_TIMEOUT,
        )
        self._connected.clear()

    async def _interruptible_sleep(self, seconds: float):
        """Sleep that can be cancelled by stop()."""
        try:
            self._sleep_task = asyncio.ensure_future(asyncio.sleep(seconds))
            await self._sleep_task
        except asyncio.CancelledError:
            pass
        finally:
            self._sleep_task = None

    # ── Notification handling ─────────────────────────────────────────────

    async def _setup_notifications(self):
        """Subscribe to SPP notify characteristic and handle CCCD."""
        if self._client is None:
            return

        svc_uuids = [str(s.uuid) for s in self._client.services]
        logger.debug("[%s] Services: %s", self._address, svc_uuids)

        await self._client.start_notify(SPP_NOTIFY, self._on_notify)

        for svc in self._client.services:
            if "1910" in str(svc.uuid):
                for ch in svc.characteristics:
                    if "2b10" in str(ch.uuid):
                        for desc in ch.descriptors:
                            if "2902" in str(desc.uuid):
                                cccd = await self._client.read_gatt_descriptor(
                                    desc.handle
                                )
                                if cccd == b'\x00\x00':
                                    logger.info(
                                        "[%s] Enabling CCCD", self._address
                                    )
                                    await self._client.write_gatt_descriptor(
                                        desc.handle, b'\x01\x00'
                                    )

    def _on_notify(self, _sender, data: bytearray):
        """Handle incoming BLE notification."""
        # Feed the connection watchdog
        if self._watchdog is not None:
            self._watchdog.notify_activity()

        payload = parse_frame(bytes(data))
        if payload is None:
            logger.debug(
                "[%s] Unparseable notification: %s",
                self._address, data.hex(),
            )
            return

        self._response = payload
        if self._response_event is not None:
            self._response_event.set()

    # ── Async command implementation ──────────────────────────────────────

    async def _send_command(self, frame: bytes, label: str) -> bytes | None:
        """Send a command frame and wait for the notification response.

        Serialised via _command_lock so only one command runs at a time.
        """
        if self._client is None or not self._client.is_connected:
            return None

        async with self._command_lock:
            self._response_event = asyncio.Event()
            self._response = None

            try:
                await self._client.write_gatt_char(
                    SPP_WRITE, frame, response=False
                )
            except Exception:
                logger.exception(
                    "[%s] Write failed for %s", self._address, label
                )
                return None

            try:
                await asyncio.wait_for(
                    self._response_event.wait(), timeout=5.0
                )
                return self._response
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] No response for %s (5s timeout)",
                    self._address, label,
                )
                return None

    async def _get_relay_state(self) -> bool | None:
        """Query the current relay state."""
        resp = await self._send_command(cmd_get_status(), "getDeviceStatus")
        if resp is None:
            return None

        if (len(resp) >= 7 and resp[0] == 0x20
                and resp[3] == 0x20 and resp[4] == 0x02):
            on = bool(resp[6])
            logger.info(
                "[%s] Relay state: %s", self._address, "ON" if on else "OFF"
            )
            return on

        logger.warning(
            "[%s] Unexpected getDeviceStatus response: %s",
            self._address, resp.hex(),
        )
        return None

    async def _set_relay(self, on: bool) -> bool | None:
        """Set the relay state and verify with a follow-up status read."""
        label = "ON" if on else "OFF"
        frame = cmd_set_status(on)
        resp = await self._send_command(frame, "setDeviceStatus(%s)" % label)

        if resp is None:
            logger.warning(
                "[%s] No ACK for set_relay(%s)", self._address, label
            )
            return None

        if len(resp) >= 7 and resp[0] == 0x20 and resp[4] == 0x01:
            result_code = resp[6]
            if result_code != 0x00:
                logger.warning(
                    "[%s] set_relay(%s) result code: 0x%02x",
                    self._address, label, result_code,
                )
                return None
        else:
            logger.warning(
                "[%s] Unexpected set response: %s",
                self._address, resp.hex(),
            )

        # Read back actual state to confirm
        await asyncio.sleep(0.3)
        confirmed = await self._get_relay_state()
        if confirmed is not None:
            logger.info(
                "[%s] set_relay(%s) confirmed: %s",
                self._address, label, "ON" if confirmed else "OFF",
            )
        return confirmed
