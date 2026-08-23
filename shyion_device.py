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
Standalone BLE probe for a single Shyion BBKS relay switch.

Use this script to test BLE connectivity and relay control from the command
line, independent of the D-Bus service.  It installs the same BLE catcher the
service does, so its connects and scans take the same /run/bt-claims claims and
coordinate with the running service rather than fighting it for a radio.

Usage:
    python3 shyion_device.py --mac AA:BB:CC:DD:EE:FF [--adapter hci0] [--get|--on|--off]
    python3 shyion_device.py --scan [--adapter hci0]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("shyion-device")


def _install_catcher(adapter: str) -> None:
    """Install the BLE catcher before shyion_ble binds bleak.

    ``--adapter hciX`` becomes the whole adapter pool, which is how this tool
    honours the flag: with one entry the routed client and scanner have only
    that radio to choose from.  Without it, every adapter is a candidate.
    """
    import shyion_bcm

    shyion_bcm.install_connection_manager({"adapters": adapter or ""})


async def probe_device(mac: str, action: str) -> None:
    """Connect to a single BBKS device and run the requested action."""
    from shyion_ble import BLE_UNREACHABLE, ShyionSession

    logger.info("Connecting to %s...", mac)
    try:
        async with ShyionSession(mac) as session:
            if action == "get":
                state = await session.get_relay_state()
                if state is not None:
                    print("Relay state: %s" % ("ON" if state else "OFF"))
                else:
                    print("No response received")

            elif action == "on":
                result = await session.set_relay(True)
                if result is not None:
                    print("Relay set to ON (confirmed: %s)" % ("ON" if result else "OFF"))
                else:
                    print("Set relay ON: no confirmation received")

            elif action == "off":
                result = await session.set_relay(False)
                if result is not None:
                    print("Relay set to OFF (confirmed: %s)" % ("ON" if result else "OFF"))
                else:
                    print("Set relay OFF: no confirmation received")
    except BLE_UNREACHABLE as exc:
        logger.error("Could not connect to %s: %s", mac, exc)
        return

    logger.info("Done")


async def scan_only() -> None:
    """Just scan for BBKS devices."""
    from shyion_ble import scan_for_relays

    found = await scan_for_relays(timeout=12.0)
    if found:
        print("\nDiscovered %d BBKS device(s):" % len(found))
        for d in found:
            print("  %s  %s  RSSI=%d" % (d.mac, d.name, d.rssi))
    else:
        print("\nNo BBKS devices found")


def main():
    parser = argparse.ArgumentParser(description="Shyion BBKS relay probe")
    parser.add_argument("--mac", help="BLE MAC address of the device")
    parser.add_argument("--adapter", default="", help="BLE adapter to use (e.g. hci0)")
    parser.add_argument("--get", action="store_true", help="Read relay state")
    parser.add_argument("--on", action="store_true", help="Turn relay ON")
    parser.add_argument("--off", action="store_true", help="Turn relay OFF")
    parser.add_argument("--scan", action="store_true", help="Scan for BBKS devices only")
    args = parser.parse_args()

    if not args.scan and not args.mac:
        parser.error("--mac is required (or use --scan)")

    _install_catcher(args.adapter)

    if args.scan:
        asyncio.run(scan_only())
        return

    if args.on:
        action = "on"
    elif args.off:
        action = "off"
    else:
        action = "get"

    asyncio.run(probe_device(args.mac, action))


if __name__ == "__main__":
    main()
