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

from __future__ import annotations

"""
Standalone BLE probe for a single Shyion BBKS relay switch.

Use this script to test BLE connectivity and relay control from the command
line, independent of the D-Bus service.

Usage:
    python3 shyion_device.py --mac A4:C1:38:9E:01:FC [--adapter hci0] [--get|--on|--off]
    python3 shyion_device.py --scan [--adapter hci0]
"""

import argparse
import asyncio
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("shyion-device")

# BLE dependencies are on sys.path via dbus-shyion-switch.py entry point

from shyion_ble import ShyionSession, scan_for_relays  # noqa: E402


async def probe_device(mac: str, adapter: str, action: str):
    """Connect to a single BBKS device and run the requested action."""
    session = ShyionSession(address=mac, adapter=adapter)

    adapters = [adapter] if adapter else None
    logger.info("Connecting to %s...", mac)

    ok = await session.connect(adapters=adapters)
    if not ok:
        logger.error("Could not connect to %s", mac)
        return

    try:
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

    finally:
        await session.disconnect()
        logger.info("Done")


async def scan_only(adapter: str):
    """Just scan for BBKS devices."""
    adapters = [adapter] if adapter else None
    found = await scan_for_relays(adapters=adapters, timeout=12.0)
    if found:
        print("\nDiscovered %d BBKS device(s):" % len(found))
        for d in found:
            print("  %s  %s  RSSI=%d" % (d.mac, d.name, d.rssi))
    else:
        print("\nNo BBKS devices found")


def main():
    parser = argparse.ArgumentParser(description="Shyion BBKS relay probe")
    parser.add_argument("--mac", help="BLE MAC address of the device")
    parser.add_argument("--adapter", default="", help="BLE adapter (e.g. hci0)")
    parser.add_argument("--get", action="store_true", help="Read relay state")
    parser.add_argument("--on", action="store_true", help="Turn relay ON")
    parser.add_argument("--off", action="store_true", help="Turn relay OFF")
    parser.add_argument("--scan", action="store_true", help="Scan for BBKS devices only")
    args = parser.parse_args()

    if args.scan:
        asyncio.run(scan_only(args.adapter))
        return

    if not args.mac:
        parser.error("--mac is required (or use --scan)")

    if args.on:
        action = "on"
    elif args.off:
        action = "off"
    else:
        action = "get"

    asyncio.run(probe_device(args.mac, args.adapter, action))


if __name__ == "__main__":
    main()
