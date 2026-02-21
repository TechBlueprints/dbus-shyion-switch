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
Single-process Venus OS service for Shyion BLE relay switches.

Registers as a Venus OS switch device (com.victronenergy.switch.shyion)
with a discovery toggle and per-device relay switches.  When discovery is
enabled, periodically scans BLE for Shyion BBKS relay devices.

Each discovered (or pre-configured) device appears as a Type-1 (toggle)
switch that directly controls the physical relay:
  - State ON  = relay energised (circuit closed)
  - State OFF = relay de-energised (circuit open)

BLE connections are transient: each poll or toggle opens a fresh
connection, performs the operation, and disconnects.  State is polled
once per hour; toggles connect on-demand.

Devices from config.ini are loaded at startup.  The discovery scan finds
new devices that are not yet in the config.
"""

import asyncio
import configparser
import logging
import os
import signal
import sys
import threading
import xml.etree.ElementTree as ET

import dbus
from gi.repository import GLib
from dbus.mainloop.glib import DBusGMainLoop

# Add ext folders to sys.path
_ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext")
sys.path.insert(1, os.path.join(_ext_dir, "velib_python"))

# All BLE dependencies from local ext/ submodules (upstream repos)
for _sub in [
    os.path.join(_ext_dir, "bleak-connection-manager", "src"),  # bleak_connection_manager
    os.path.join(_ext_dir, "bleak-retry-connector", "src"),     # bleak_retry_connector
    os.path.join(_ext_dir, "bluetooth-adapters", "src"),        # bluetooth_adapters
    os.path.join(_ext_dir, "aiooui", "src"),                    # aiooui
    os.path.join(_ext_dir, "bleak"),                            # bleak (package at repo root)
]:
    if os.path.isdir(_sub) and _sub not in sys.path:
        sys.path.insert(0, _sub)

from vedbus import VeDbusService  # noqa: E402
from settingsdevice import SettingsDevice  # noqa: E402

from bleak_connection_manager import LockConfig, ScanLockConfig  # noqa: E402
from shyion_ble import (  # noqa: E402
    poll_relay_state,
    toggle_relay,
    scan_for_relays,
    DiscoveredRelay,
)

VERSION = "0.5.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dbus-shyion-switch")

# Defaults
DEFAULT_SCAN_INTERVAL = 60
DEFAULT_POLL_INTERVAL_MINUTES = 60

# Status codes for SwitchableOutput (per Venus OS switch API)
STATUS_OFF = 0x00          # Relay off
STATUS_ON = 0x09           # Relay on
STATUS_DISABLED = 0x20     # Disconnected / unknown


def get_bus():
    """Return the shared system bus connection (singleton provided by dbus-python)."""
    return dbus.SessionBus() if "DBUS_SESSION_BUS_ADDRESS" in os.environ else dbus.SystemBus()


def load_config() -> configparser.ConfigParser:
    """Load configuration from config.ini, falling back to config.default.ini."""
    config = configparser.ConfigParser()
    config_dir = os.path.dirname(os.path.abspath(__file__))

    config_file = os.path.join(config_dir, "config.ini")
    default_file = os.path.join(config_dir, "config.default.ini")

    if os.path.exists(config_file):
        config.read(config_file)
        logger.info("Loaded config from %s", config_file)
    elif os.path.exists(default_file):
        config.read(default_file)
        logger.info("Loaded default config from %s", default_file)
    else:
        logger.warning("No config file found, using defaults")

    return config


def get_device_configs(config: configparser.ConfigParser) -> dict[str, dict[str, str]]:
    """Extract per-device configurations from named sections.

    Returns a dict keyed by MAC address (upper-case) with sub-keys:
    name, model, shyion_id.
    """
    devices: dict[str, dict[str, str]] = {}
    for section in config.sections():
        mac = config.get(section, "mac", fallback="").strip().upper()
        if not mac or ":" not in mac:
            continue
        name = config.get(section, "name", fallback=section)
        devices[mac] = {
            "name": name,
            "model": config.get(section, "model", fallback=""),
            "shyion_id": config.get(section, "shyion_id", fallback=""),
        }
    return devices


# ── Discovery / Management Service ──────────────────────────────────────────

class ShyionSwitchService:
    """Venus OS switch device that manages Shyion BLE relay switches.

    Discovers devices via BLE scanning and/or config.ini.  Each relay
    appears as a toggleable switch in the Venus OS GUI.

    BLE connections are transient: each toggle or poll opens a fresh
    connection, sends the command, reads the state, and disconnects.
    State is polled once per hour.
    """

    def __init__(self):
        self._config = load_config()
        defaults = self._config["DEFAULT"] if "DEFAULT" in self._config else {}

        self._scan_interval = int(defaults.get(
            "scan_interval", str(DEFAULT_SCAN_INTERVAL)))
        self._poll_interval_minutes = float(defaults.get(
            "poll_interval_minutes", str(DEFAULT_POLL_INTERVAL_MINUTES)))

        # BLE name prefixes for scanning
        prefix_config = defaults.get("name_prefixes", "BBKS").strip()
        self._name_prefixes = [p.strip() for p in prefix_config.split(",") if p.strip()]

        # BCM lock configs for cross-process BLE coordination
        self._lock_config = LockConfig(enabled=True)
        self._scan_lock_config = ScanLockConfig(enabled=True)

        # Pre-configured devices from config.ini
        self._device_configs = get_device_configs(self._config)
        logger.info("Configured devices: %d", len(self._device_configs))
        logger.info("Scan interval: %ds", self._scan_interval)
        logger.info("State poll interval: %.0f min", self._poll_interval_minutes)

        # D-Bus
        self._bus = get_bus()

        # Per-device state
        self._device_info: dict[str, DiscoveredRelay] = {}
        self._last_known_state: dict[str, bool | None] = {}

        # Scanning state
        self._scan_timer_id = None
        self._scanning = False

        # State poll timer
        self._poll_timer_id = None

        # ── Register as Venus OS switch device ──────────────────────────
        self._dbusservice = VeDbusService(
            "com.victronenergy.switch.shyion", self._bus, register=False
        )

        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path("/Mgmt/ProcessVersion", VERSION)
        self._dbusservice.add_path("/Mgmt/Connection", "Shyion Relay Manager")
        self._dbusservice.add_path("/DeviceInstance", 140)
        self._dbusservice.add_path("/ProductId", 0xFFFF)
        self._dbusservice.add_path("/ProductName", "Shyion Relay Manager")
        self._dbusservice.add_path("/CustomName", "Shyion Relay Manager")
        self._dbusservice.add_path("/FirmwareVersion", VERSION)
        self._dbusservice.add_path("/HardwareVersion", None)
        self._dbusservice.add_path("/Connected", 1)
        self._dbusservice.add_path("/State", 0x100)

        # ── Discovery toggle ─────────────────────────────────────────────
        discovery_path = "/SwitchableOutput/relay_discovery"
        self._dbusservice.add_path(
            "%s/Name" % discovery_path, "* Shyion Relay Discovery"
        )
        self._dbusservice.add_path("%s/Type" % discovery_path, 1)
        self._dbusservice.add_path(
            "%s/State" % discovery_path, 0,
            writeable=True,
            onchangecallback=self._on_discovery_changed,
        )
        self._dbusservice.add_path("%s/Status" % discovery_path, 0x00)
        self._dbusservice.add_path("%s/Current" % discovery_path, 0)
        self._dbusservice.add_path(
            "%s/Settings/CustomName" % discovery_path, "", writeable=True)
        self._dbusservice.add_path(
            "%s/Settings/Type" % discovery_path, 1, writeable=True)
        self._dbusservice.add_path("%s/Settings/ValidTypes" % discovery_path, 2)
        self._dbusservice.add_path(
            "%s/Settings/Function" % discovery_path, 2, writeable=True)
        self._dbusservice.add_path(
            "%s/Settings/ValidFunctions" % discovery_path, 4)
        self._dbusservice.add_path(
            "%s/Settings/Group" % discovery_path, "", writeable=True)
        self._dbusservice.add_path(
            "%s/Settings/ShowUIControl" % discovery_path, 1, writeable=True)
        self._dbusservice.add_path(
            "%s/Settings/PowerOnState" % discovery_path, 1)

        # ── Persistent settings ──────────────────────────────────────────
        settings = {
            "ClassAndVrmInstance": [
                "/Settings/Devices/shyion/ClassAndVrmInstance",
                "switch:140", 0, 0,
            ],
            "DiscoveryEnabled": [
                "/Settings/Devices/shyion/DiscoveryEnabled",
                0, 0, 1,
            ],
        }
        self._settings = SettingsDevice(
            self._bus, settings,
            eventCallback=self._on_settings_changed,
            timeout=10,
        )

        # Register on D-Bus
        self._dbusservice.register()
        logger.info("Registered discovery service on D-Bus")

        # Load pre-configured devices and create switches
        self._load_configured_devices()

        # Restore previously discovered devices from settings
        self._restore_devices_from_settings()

        # Restore discovery state
        discovery_state = self._settings["DiscoveryEnabled"]
        self._dbusservice["/SwitchableOutput/relay_discovery/State"] = discovery_state
        if discovery_state:
            logger.info("Discovery enabled from saved settings")
            self._start_scanning()

        # Start the periodic state poll timer
        self._start_state_poll()

    # ── Config loading ──────────────────────────────────────────────────────

    def _load_configured_devices(self):
        """Create switches for all devices in config.ini."""
        for mac, conf in self._device_configs.items():
            mac_id = mac.replace(":", "").lower()
            if mac_id in self._device_info:
                continue

            self._device_info[mac_id] = DiscoveredRelay(
                mac=mac,
                name=conf["name"],
                model=conf.get("model", ""),
            )

            self._restore_relay_state(mac_id)
            self._create_device_switch(mac_id, conf["name"])
            logger.info("Loaded device from config: %s (%s)", conf["name"], mac)

    def _restore_relay_state(self, mac_id: str):
        """Try to read persisted relay state from com.victronenergy.settings."""
        try:
            path = "/Settings/Devices/shyion/Device_%s/RelayState" % mac_id
            rs = int(self._bus.get_object(
                "com.victronenergy.settings", path
            ).GetValue())
            if rs >= 0:
                self._last_known_state[mac_id] = bool(rs)
                logger.debug("Restored relay state for %s: %s",
                             mac_id, "ON" if rs else "OFF")
        except Exception:
            pass

    # ── Discovery callbacks ─────────────────────────────────────────────────

    def _on_discovery_changed(self, path, value):
        enabled = bool(int(value) if isinstance(value, str) else value)
        logger.info("Discovery %s", "enabled" if enabled else "disabled")
        self._settings["DiscoveryEnabled"] = 1 if enabled else 0

        if enabled:
            self._start_scanning()
            for mac_id in self._device_info:
                show_path = "/SwitchableOutput/relay_%s/Settings/ShowUIControl" % mac_id
                if show_path in self._dbusservice:
                    self._dbusservice[show_path] = 1
        else:
            self._stop_scanning()
        return True

    def _on_settings_changed(self, setting, old_value, new_value):
        logger.debug("Setting changed: %s = %s", setting, new_value)

    # ── BLE Scanning ────────────────────────────────────────────────────────

    def _start_scanning(self):
        if self._scan_timer_id is not None:
            return
        logger.info("Starting BLE discovery (interval: %ds)", self._scan_interval)
        GLib.idle_add(lambda: (self._trigger_scan(), False)[-1])
        self._scan_timer_id = GLib.timeout_add_seconds(
            self._scan_interval, self._trigger_scan
        )

    def _stop_scanning(self):
        if self._scan_timer_id is not None:
            GLib.source_remove(self._scan_timer_id)
            self._scan_timer_id = None
            logger.info("Stopped BLE discovery")

    def _trigger_scan(self) -> bool:
        if self._scanning:
            return True
        self._scanning = True
        thread = threading.Thread(
            target=self._run_scan_thread,
            name="Shyion_Discovery",
            daemon=True,
        )
        thread.start()
        return True

    def _run_scan_thread(self):
        try:
            found = asyncio.run(
                scan_for_relays(
                    timeout=15.0,
                    scan_lock_config=self._scan_lock_config,
                )
            )
            GLib.idle_add(self._process_scan_results, found)
        except Exception:
            logger.exception("BLE scan failed")
        finally:
            self._scanning = False

    def _process_scan_results(self, found: list[DiscoveredRelay]) -> bool:
        for device in found:
            mac_id = device.mac.replace(":", "").lower()
            if mac_id in self._device_info:
                continue

            logger.info(
                "New relay discovered: %s (%s) RSSI=%d",
                device.name, device.mac, device.rssi,
            )
            self._device_info[mac_id] = device
            self._create_device_switch(mac_id, device.name)
            self._save_device_to_settings(mac_id, device.name, device.mac)

        return False

    # ── Per-device switch management ────────────────────────────────────────

    def _create_device_switch(self, mac_id: str, name: str):
        """Create a D-Bus switchable output for a relay device."""
        output_path = "/SwitchableOutput/relay_%s" % mac_id

        if "%s/State" % output_path in self._dbusservice:
            return

        initial_state = self._last_known_state.get(mac_id)
        if initial_state is not None:
            initial_dbus_state = 1 if initial_state else 0
            initial_status = STATUS_ON if initial_state else STATUS_OFF
        else:
            initial_dbus_state = 0
            initial_status = STATUS_OFF

        with self._dbusservice as ctx:
            ctx.add_path("%s/Name" % output_path, name)
            ctx.add_path("%s/Type" % output_path, 1)
            ctx.add_path(
                "%s/State" % output_path,
                initial_dbus_state,
                writeable=True,
                onchangecallback=self._on_device_state_changed,
            )
            ctx.add_path("%s/Status" % output_path, initial_status)
            ctx.add_path("%s/Current" % output_path, 0)
            ctx.add_path(
                "%s/Settings/CustomName" % output_path, "", writeable=True)
            ctx.add_path(
                "%s/Settings/Type" % output_path, 1, writeable=True)
            ctx.add_path("%s/Settings/ValidTypes" % output_path, 2)
            ctx.add_path(
                "%s/Settings/Function" % output_path, 2, writeable=True)
            ctx.add_path("%s/Settings/ValidFunctions" % output_path, 4)
            ctx.add_path(
                "%s/Settings/Group" % output_path, "", writeable=True)
            ctx.add_path(
                "%s/Settings/ShowUIControl" % output_path, 1, writeable=True)
            ctx.add_path("%s/Settings/PowerOnState" % output_path, 1)

        logger.info("Created switch for %s (%s)", name, mac_id)

    def _on_device_state_changed(self, path, value):
        """Called when a relay toggle is changed from Venus OS GUI."""
        path_parts = path.split("/")
        if len(path_parts) < 3 or not path_parts[2].startswith("relay_"):
            return True

        mac_id = path_parts[2].replace("relay_", "")
        if mac_id == "discovery":
            return True

        on = bool(int(value) if isinstance(value, str) else value)
        name = self._get_device_name(mac_id)
        logger.info("Toggle '%s' (%s) -> %s", name, mac_id, "ON" if on else "OFF")

        thread = threading.Thread(
            target=self._run_toggle_relay,
            args=(mac_id, on),
            name="Shyion_Toggle_%s" % mac_id,
            daemon=True,
        )
        thread.start()

        return True

    def _get_device_name(self, mac_id: str) -> str:
        name_path = "/SwitchableOutput/relay_%s/Name" % mac_id
        if name_path in self._dbusservice:
            return self._dbusservice[name_path]
        if mac_id in self._device_info:
            return self._device_info[mac_id].name
        return mac_id

    # ── BLE relay toggle (runs in background thread) ─────────────────────

    def _run_toggle_relay(self, mac_id: str, on: bool):
        """Transient session: connect, set relay, read state, disconnect."""
        if mac_id not in self._device_info:
            logger.warning("[%s] Unknown device for toggle", mac_id)
            GLib.idle_add(self._apply_relay_result, mac_id, None)
            return

        address = self._device_info[mac_id].mac
        result = asyncio.run(
            toggle_relay(
                address, on,
                lock_config=self._lock_config,
                scan_lock_config=self._scan_lock_config,
            )
        )
        GLib.idle_add(self._apply_relay_result, mac_id, result)

    def _apply_relay_result(self, mac_id: str, state: bool | None) -> bool:
        """Apply BLE result to D-Bus (called on GLib thread)."""
        state_path = "/SwitchableOutput/relay_%s/State" % mac_id
        status_path = "/SwitchableOutput/relay_%s/Status" % mac_id

        if state is not None:
            self._last_known_state[mac_id] = state
            if state_path in self._dbusservice:
                self._dbusservice[state_path] = 1 if state else 0
            if status_path in self._dbusservice:
                self._dbusservice[status_path] = STATUS_ON if state else STATUS_OFF

            mac_address = self._device_info[mac_id].mac if mac_id in self._device_info else ""
            name = self._get_device_name(mac_id)
            self._save_device_to_settings(mac_id, name, mac_address)

            logger.info("Relay %s confirmed: %s",
                        self._get_device_name(mac_id), "ON" if state else "OFF")
        else:
            if status_path in self._dbusservice:
                self._dbusservice[status_path] = STATUS_DISABLED
            logger.warning("Relay %s: communication failed", self._get_device_name(mac_id))

        return False

    # ── Periodic state poll ──────────────────────────────────────────────

    def _start_state_poll(self):
        """Start the periodic state poll timer and schedule an initial poll."""
        interval_seconds = int(self._poll_interval_minutes * 60)

        # Initial poll 30s after startup to confirm actual state
        if self._device_info:
            logger.info("Initial state poll in 30s for %d device(s)",
                        len(self._device_info))
            GLib.timeout_add_seconds(30, self._trigger_initial_poll)

        if interval_seconds <= 0:
            logger.info("Periodic state polling disabled (poll_interval_minutes=0)")
            return

        logger.info("Periodic state poll: every %.0f minutes",
                     self._poll_interval_minutes)

        self._poll_timer_id = GLib.timeout_add_seconds(
            interval_seconds, self._trigger_state_poll
        )

    def _trigger_initial_poll(self) -> bool:
        """One-shot wrapper for the initial state poll at startup."""
        self._trigger_state_poll()
        return False

    def _trigger_state_poll(self) -> bool:
        """Fires periodically to poll all known devices."""
        logger.info("State poll: checking %d device(s)", len(self._device_info))

        thread = threading.Thread(
            target=self._run_poll_all_devices,
            name="Shyion_PollAll",
            daemon=True,
        )
        thread.start()

        return True

    def _run_poll_all_devices(self):
        """Background thread: poll each device sequentially (one at a time)."""
        for mac_id in list(self._device_info.keys()):
            self._run_poll_device(mac_id)

    def _run_poll_device(self, mac_id: str):
        """Transient session: connect, query state, disconnect."""
        if mac_id not in self._device_info:
            return

        address = self._device_info[mac_id].mac
        state = asyncio.run(
            poll_relay_state(
                address,
                lock_config=self._lock_config,
                scan_lock_config=self._scan_lock_config,
            )
        )
        GLib.idle_add(self._apply_poll_result, mac_id, state)

    def _apply_poll_result(self, mac_id: str, state: bool | None) -> bool:
        """Apply poll result to D-Bus (called on GLib thread)."""
        if state is None:
            return False

        state_path = "/SwitchableOutput/relay_%s/State" % mac_id
        status_path = "/SwitchableOutput/relay_%s/Status" % mac_id

        old_state = self._last_known_state.get(mac_id)
        self._last_known_state[mac_id] = state

        if state_path in self._dbusservice:
            self._dbusservice[state_path] = 1 if state else 0
        if status_path in self._dbusservice:
            self._dbusservice[status_path] = STATUS_ON if state else STATUS_OFF

        mac_address = self._device_info[mac_id].mac if mac_id in self._device_info else ""
        name = self._get_device_name(mac_id)
        self._save_device_to_settings(mac_id, name, mac_address)

        if old_state != state:
            logger.info("Poll: %s state changed to %s",
                        self._get_device_name(mac_id), "ON" if state else "OFF")
        else:
            logger.debug("Poll: %s state unchanged (%s)",
                         self._get_device_name(mac_id), "ON" if state else "OFF")

        return False

    # ── Settings persistence ────────────────────────────────────────────────

    def _save_device_to_settings(self, mac_id: str, name: str, mac_address: str):
        try:
            settings_proxy = self._bus.get_object(
                "com.victronenergy.settings", "/Settings"
            )
            settings_iface = dbus.Interface(
                settings_proxy, "com.victronenergy.Settings"
            )

            group = "Devices/shyion"
            prefix = "Device_%s" % mac_id

            settings_iface.AddSetting(group, "%s/Name" % prefix, name, "s", 0, 0)
            if mac_address:
                settings_iface.AddSetting(
                    group, "%s/MAC" % prefix, mac_address, "s", 0, 0)
            settings_iface.AddSetting(
                group, "%s/RelayState" % prefix, -1, "i", -1, 1)

            base = "/Settings/%s/%s" % (group, prefix)
            self._bus.get_object(
                "com.victronenergy.settings", base + "/Name"
            ).SetValue(name)
            if mac_address:
                self._bus.get_object(
                    "com.victronenergy.settings", base + "/MAC"
                ).SetValue(mac_address)

            relay_state = self._last_known_state.get(mac_id)
            if relay_state is not None:
                self._bus.get_object(
                    "com.victronenergy.settings", base + "/RelayState"
                ).SetValue(1 if relay_state else 0)

            logger.debug("Saved device %s to settings", mac_id)

        except Exception:
            logger.exception("Failed to save device %s to settings", mac_id)

    def _restore_devices_from_settings(self):
        """Restore previously discovered devices from com.victronenergy.settings."""
        try:
            settings_proxy = self._bus.get_object(
                "com.victronenergy.settings",
                "/Settings/Devices/shyion",
            )
            settings_iface = dbus.Interface(
                settings_proxy, "org.freedesktop.DBus.Introspectable"
            )
            xml_str = settings_iface.Introspect()
            root = ET.fromstring(xml_str)

            restored = 0
            for node in root.findall("node"):
                node_name = node.get("name", "")
                if not node_name.startswith("Device_"):
                    continue

                mac_id = node_name.replace("Device_", "")

                if mac_id in self._device_info:
                    continue

                try:
                    device_path = "/Settings/Devices/shyion/%s" % node_name
                    dev_proxy = self._bus.get_object(
                        "com.victronenergy.settings", device_path
                    )
                    dev_iface = dbus.Interface(
                        dev_proxy, "org.freedesktop.DBus.Introspectable"
                    )
                    dev_xml = dev_iface.Introspect()
                    dev_root = ET.fromstring(dev_xml)

                    sub_names = {n.get("name") for n in dev_root.findall("node")}

                    name = mac_id
                    if "Name" in sub_names:
                        name = str(self._bus.get_object(
                            "com.victronenergy.settings",
                            "%s/Name" % device_path,
                        ).GetValue())

                    mac_address = ""
                    if "MAC" in sub_names:
                        mac_address = str(self._bus.get_object(
                            "com.victronenergy.settings",
                            "%s/MAC" % device_path,
                        ).GetValue())

                    if "RelayState" in sub_names:
                        rs = int(self._bus.get_object(
                            "com.victronenergy.settings",
                            "%s/RelayState" % device_path,
                        ).GetValue())
                        if rs >= 0:
                            self._last_known_state[mac_id] = bool(rs)

                    if mac_address:
                        self._device_info[mac_id] = DiscoveredRelay(
                            mac=mac_address, name=name,
                        )

                    self._create_device_switch(mac_id, name)
                    restored += 1

                except Exception:
                    logger.exception("Failed to restore device %s", node_name)

            if restored > 0:
                logger.info("Restored %d device(s) from settings", restored)

        except dbus.exceptions.DBusException:
            logger.debug("No saved devices to restore (fresh install)")
        except Exception:
            logger.exception("Error restoring devices from settings")

    # ── Shutdown ────────────────────────────────────────────────────────────

    def stop(self):
        """Clean shutdown: stop scanning, cancel timers."""
        self._stop_scanning()

        if self._poll_timer_id is not None:
            GLib.source_remove(self._poll_timer_id)
            self._poll_timer_id = None

        logger.info("Service stopped")


def main():
    DBusGMainLoop(set_as_default=True)

    service = ShyionSwitchService()

    mainloop = GLib.MainLoop()

    def signal_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        service.stop()
        mainloop.quit()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("dbus-shyion-switch v%s started", VERSION)

    mainloop.run()


if __name__ == "__main__":
    main()
