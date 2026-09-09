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
  - Notify characteristic (2b10): device -> host responses
  - Write  characteristic (2b11): host -> device commands (write-without-response)

Framing:
  - Start marker:  0x6E
  - End marker:    0x6F
  - Escape byte:   0x6D  (followed by 0x4D->0x6D, 0x4E->0x6E, 0x4F->0x6F)
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

Connection model: transient sessions (connect, command, disconnect).

BLE coordination comes from the bleak-connection-manager catcher, which the
driver installs process-wide before importing this module: every BleakClient
and BleakScanner built here is already routed through claim-aware adapter
selection.  Nothing in this module configures adapters or locks -- it writes
plain bleak and bleak-retry-connector code, and the catcher does the rest.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak_retry_connector import (
    BleakAbortedError,
    BleakConnectionError,
    BleakNotFoundError,
    BleakOutOfConnectionSlotsError,
    establish_connection,
    get_device,
)

from bleak_connection_manager import tolerate_late_gatt, validate_char_exists

logger = logging.getLogger(__name__)

# Failures that mean "the relay is asleep, out of range, or every radio is
# busy" rather than "something is broken".  Each is worth one log line and a
# retry on the caller's backoff, never a traceback.
BLE_UNREACHABLE = (
    BleakNotFoundError,
    BleakOutOfConnectionSlotsError,
    BleakAbortedError,
    BleakConnectionError,
)

# ── Telink SPP UUIDs ──────────────────────────────────────────────────────────

SPP_SERVICE = "00010203-0405-0607-0809-0a0b0c0d1910"
SPP_NOTIFY  = "00010203-0405-0607-0809-0a0b0c0d2b10"
SPP_WRITE   = "00010203-0405-0607-0809-0a0b0c0d2b11"

# BLE name prefix for BBKS relay devices
BBKS_NAME_PREFIX = "BBKS"

# ── Connection tuning ─────────────────────────────────────────────────────────

# How long to scan for a relay that is not in the BlueZ cache.
SCAN_TIMEOUT = 15.0

# Connect attempts per session; bleak-retry-connector drives these, and the
# catcher routes each one -- a failed attempt walks to the next radio.
CONNECT_MAX_ATTEMPTS = 4

# Ceiling on the whole find-and-connect phase.  Sessions run on the shared
# asyncio loop with a worker thread blocked on the result, so a connect that
# never returns would wedge that thread for good.
CONNECT_OVERALL_TIMEOUT = 300.0

# Post-connect validation.  A connect that reports success is not always a
# usable link: these Telink parts announce ServicesResolved with only the
# Generic Attribute service present and register the vendor services seconds
# later, so a bare characteristic check would reject a link that was about to
# become fine.  tolerate_late_gatt re-reads the GATT table from BlueZ after
# 2s/4s/8s before giving up.  A rejection is a connection failure: the catcher
# tears the link down, charges the adapter a failure in its placement score,
# and bleak-retry-connector attempts again on the next radio.
VALIDATE_SPP_LINK = tolerate_late_gatt(validate_char_exists(SPP_NOTIFY))

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


async def scan_for_relays(timeout: float = 12.0) -> list[DiscoveredRelay]:
    """Scan BLE for BBKS relay devices.

    The catcher's wrapped scanner picks the least-busy adapter, holds that
    card's exclusive scan claim for the duration so other processes steer
    around it, and restarts the scan on another radio if this one goes quiet.
    """
    devices = await BleakScanner.discover(timeout=timeout)

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


# ── Transient BLE session ────────────────────────────────────────────────────

class ShyionSession:
    """Transient BLE session to a Shyion BBKS relay device.

    Connects to the device on entry, disconnects on exit.  All BLE
    operations happen within the session context.

    Usage::

        async with ShyionSession(address) as s:
            state = await s.get_relay_state()
            confirmed = await s.set_relay(True)
    """

    def __init__(self, address: str):
        self._address = address
        self._client: BleakClient | None = None
        self._response: bytes | None = None
        self._response_event: asyncio.Event | None = None

    async def _find_device(self):
        """Resolve the address to a BLEDevice, cache first.

        Cache-first is not just a speed optimisation here.  BBKS relays
        advertise sparsely, so scan-based discovery misses them often; and a
        cache-resolved BLEDevice carries its adapter in its D-Bus path, which
        the catcher treats as caller-explicit -- claims and connection-
        parameter tuning land on the radio the link will really use.
        """
        device = await get_device(self._address)
        if device is not None:
            return device

        logger.debug("[%s] Not in the BlueZ cache, scanning...", self._address)
        return await BleakScanner.find_device_by_address(
            self._address, timeout=SCAN_TIMEOUT
        )

    async def _connect(self) -> None:
        device = await self._find_device()
        if device is None:
            # Same condition bleak-retry-connector reports for a device that
            # vanishes between scan and connect -- raise the same type so
            # callers have one "not reachable" case to handle.
            raise BleakNotFoundError(
                f"[{self._address}] Device not found after scanning"
            )

        logger.debug("[%s] Connecting...", self._address)
        self._client = await establish_connection(
            BleakClient,
            device,
            "Shyion %s" % self._address,
            max_attempts=CONNECT_MAX_ATTEMPTS,
            validate_connection=VALIDATE_SPP_LINK,
        )

    async def __aenter__(self) -> ShyionSession:
        # Anything that fails past this point has to tear the link down
        # itself: __aexit__ only runs for a session that entered cleanly, and
        # a link left up holds its adapter claim and link slot until the
        # heartbeat reaps them.
        try:
            # Step 1: find and connect, under one overall budget.
            try:
                await asyncio.wait_for(
                    self._connect(), timeout=CONNECT_OVERALL_TIMEOUT
                )
            except asyncio.TimeoutError as exc:
                raise BleakNotFoundError(
                    f"[{self._address}] Gave up after "
                    f"{CONNECT_OVERALL_TIMEOUT:.0f}s trying to connect"
                ) from exc

            logger.debug(
                "[%s] Connected (MTU: %d)", self._address, self._client.mtu_size
            )

            # Step 2: Set up notifications
            await asyncio.wait_for(self._setup_notifications(), timeout=5.0)
        except BaseException:
            await self.__aexit__()
            raise

        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            try:
                if self._client.is_connected:
                    try:
                        await asyncio.wait_for(
                            self._client.stop_notify(SPP_NOTIFY), timeout=3.0
                        )
                        logger.debug("[%s] Notifications stopped", self._address)
                    except Exception:
                        logger.debug(
                            "[%s] stop_notify failed (harmless)",
                            self._address, exc_info=True,
                        )
                await asyncio.wait_for(
                    self._client.disconnect(), timeout=5.0
                )
                logger.debug("[%s] Disconnected", self._address)
            except Exception:
                logger.debug(
                    "[%s] Disconnect failed", self._address, exc_info=True
                )
            self._client = None

    # ── Notification handling ─────────────────────────────────────────

    async def _setup_notifications(self):
        """Subscribe to SPP notify characteristic and handle CCCD."""
        if self._client is None:
            return

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

    # ── Command implementation ────────────────────────────────────────

    async def _send_command(self, frame: bytes, label: str) -> bytes | None:
        """Send a command frame and wait for the notification response."""
        if self._client is None or not self._client.is_connected:
            return None

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

    async def get_relay_state(self) -> bool | None:
        """Query the current relay state.

        Returns True (ON), False (OFF), or None on failure.
        """
        resp = await self._send_command(cmd_get_status(), "getDeviceStatus")
        if resp is None:
            return None

        if (len(resp) >= 7 and resp[0] == 0x20
                and resp[3] == 0x20 and resp[4] == 0x02):
            on = bool(resp[6])
            # Per-poll state is DEBUG: the driver logs a state CHANGE at
            # INFO and a per-sweep availability summary, which is the signal
            # that mattered when diagnosing an outage -- 28 identical lines an
            # hour were not.
            logger.debug(
                "[%s] Relay state: %s", self._address, "ON" if on else "OFF"
            )
            return on

        logger.warning(
            "[%s] Unexpected getDeviceStatus response: %s",
            self._address, resp.hex(),
        )
        return None

    async def set_relay(self, on: bool) -> bool | None:
        """Set the relay state and verify with a follow-up status read.

        Returns the confirmed relay state (True/False) or None on failure.
        """
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
        confirmed = await self.get_relay_state()
        if confirmed is not None:
            logger.info(
                "[%s] set_relay(%s) confirmed: %s",
                self._address, label, "ON" if confirmed else "OFF",
            )
        return confirmed


# ── Convenience wrappers for background threads ──────────────────────────────

async def poll_relay_state(address: str) -> bool | None:
    """Connect, query relay state, disconnect.

    Returns True (ON), False (OFF), or None on failure.
    Runs on the driver's shared asyncio loop, submitted from a worker thread.
    """
    try:
        async with ShyionSession(address) as s:
            return await s.get_relay_state()
    except BLE_UNREACHABLE as exc:
        # Expected whenever the relay is asleep, out of range, or every radio
        # is busy -- log a single line rather than a traceback; the caller
        # retries with backoff.
        logger.warning("[%s] Not reachable, will retry later: %s", address, exc)
        return None
    except Exception:
        logger.exception("[%s] Poll failed", address)
        return None


async def toggle_relay(address: str, on: bool) -> bool | None:
    """Connect, set relay, read back state, disconnect.

    Returns the confirmed relay state (True/False) or None on failure.
    Runs on the driver's shared asyncio loop, submitted from a worker thread.
    """
    try:
        async with ShyionSession(address) as s:
            return await s.set_relay(on)
    except BLE_UNREACHABLE as exc:
        logger.warning("[%s] Not reachable, toggle not applied: %s", address, exc)
        return None
    except Exception:
        logger.exception("[%s] Toggle failed", address)
        return None
