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
import time
import xml.etree.ElementTree as ET

import dbus
from gi.repository import GLib
from dbus.mainloop.glib import DBusGMainLoop

# Victron's D-Bus helpers are service-specific, so they stay vendored here.
# The BLE stack does not: shyion_bcm sources it in process from the folder
# named in config.ini, per the fleet consumer contract.
_ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext")
sys.path.insert(1, os.path.join(_ext_dir, "velib_python"))

from vedbus import VeDbusService  # noqa: E402
from settingsdevice import SettingsDevice  # noqa: E402

import shyion_bcm  # noqa: E402  (must not import bleak at module scope)

VERSION = "0.7.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dbus-shyion-switch")

# Defaults
DEFAULT_SCAN_INTERVAL = 60
DEFAULT_POLL_INTERVAL_MINUTES = 60

# Reconnect backoff after a failed BLE operation: retry delays double
# from BASE up to MAX until the device answers again.
RECONNECT_BASE_DELAY = 30      # seconds
RECONNECT_MAX_DELAY = 900     # 15 minutes

# Pause between the first failed toggle attempt and its retry, giving
# the adapters a moment to settle before scanning again.
TOGGLE_RETRY_DELAY = 3.0

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


# ── BLE stack ───────────────────────────────────────────────────────────────

# The catcher rebinds bleak's client and scanner process-wide, and only
# reaches modules imported after it runs.  shyion_ble binds bleak at module
# scope, so the install has to happen here, between reading the config and
# importing it.
_CONFIG = load_config()
# Only ever normalize the live config.ini.  config.default.ini ships in git
# with empty values, and rewriting it would put this box's adapter MACs into
# a tracked file.
_CONFIG_INI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
# The connect allowlist starts as config.ini's devices.  Devices adopted by an
# earlier discovery live in /Settings and cannot be read here -- D-Bus is not
# up yet -- so they are added after the restore, which is also the only place
# the permitted set is allowed to grow.
_DEVICE_CONFIGS = get_device_configs(_CONFIG)
shyion_bcm.install_connection_manager(
    _CONFIG["DEFAULT"] if "DEFAULT" in _CONFIG else {},
    config_path=_CONFIG_INI if os.path.exists(_CONFIG_INI) else None,
    allowed_devices=list(_DEVICE_CONFIGS.keys()),
)

from shyion_ble import (  # noqa: E402
    poll_relay_state,
    toggle_relay,
    scan_for_relays,
    DiscoveredRelay,
)


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
        self._config = _CONFIG
        defaults = self._config["DEFAULT"] if "DEFAULT" in self._config else {}

        self._scan_interval = int(defaults.get(
            "scan_interval", str(DEFAULT_SCAN_INTERVAL)))
        self._poll_interval_minutes = float(defaults.get(
            "poll_interval_minutes", str(DEFAULT_POLL_INTERVAL_MINUTES)))

        # BLE name prefixes for scanning
        prefix_config = defaults.get("name_prefixes", "BBKS").strip()
        self._name_prefixes = [p.strip() for p in prefix_config.split(",") if p.strip()]

        # Pre-configured devices from config.ini
        self._device_configs = _DEVICE_CONFIGS
        logger.info("Configured devices: %d", len(self._device_configs))
        logger.info("Scan interval: %ds", self._scan_interval)
        logger.info("State poll interval: %.0f min", self._poll_interval_minutes)

        # D-Bus
        self._bus = get_bus()

        # Per-device state
        self._device_info: dict[str, DiscoveredRelay] = {}
        self._last_known_state: dict[str, bool | None] = {}

        # Per-device BLE session lock: ensures only one BLE session runs at a
        # time for each device, preventing "NotPermitted: Notify acquired" when
        # the GUI fires multiple rapid toggles before the first session closes.
        self._device_ble_lock: dict[str, threading.Lock] = {}

        # Per-device reconnect backoff after failed BLE operations.
        # Keyed by mac_id; each entry holds the attempt counter and the
        # pending GLib timer id (None when no retry is scheduled).
        self._reconnect_state: dict[str, dict] = {}

        # Per-device VeDbusItemImport handles for persistent settings.
        # Keyed by "{mac_id}/Name", "{mac_id}/MAC", "{mac_id}/RelayState".
        # Populated lazily by _register_device_settings().  Using stored
        # VeDbusItemImport objects avoids repeated get_object() calls which
        # accumulate and eventually exhaust the D-Bus connection limit.
        self._device_settings: dict[str, object] = {}

        # Scanning state
        self._scan_timer_id = None
        self._scanning = False

        # State poll timer
        self._poll_timer_id = None

        # ── Persistent asyncio event loop ──────────────────────────────────
        # All BLE work runs inside this single, long-lived loop.  Reason:
        # bleak keeps its BlueZManager (which owns the dbus_fast MessageBus
        # to BlueZ) in a WeakKeyDictionary keyed by event loop.  Using
        # ``asyncio.run()`` per operation creates a fresh loop and thus a
        # fresh manager + MessageBus every time, which dbus_fast doesn't
        # reliably tear down on loop close — leaking one system-bus
        # connection per scan/toggle/poll until UID 0 hits the dbus-daemon
        # connection cap and BLE breaks system-wide.  Routing every BLE
        # coroutine through one shared loop means one BlueZManager, one
        # MessageBus, no leak.  BLE connections themselves remain transient
        # (open per operation, closed at the end of ShyionSession).
        self._async_loop_ready = threading.Event()
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._async_thread = threading.Thread(
            target=self._run_async_loop,
            name="Shyion_AsyncLoop",
            daemon=True,
        )
        self._async_thread.start()
        # Block briefly so callers can submit immediately; 5 s is plenty
        # for a thread to enter run_forever() and avoids deadlocking
        # startup if the loop somehow fails to come up.
        if not self._async_loop_ready.wait(timeout=5.0):
            logger.error("Async event loop failed to start within 5 s")

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

    def _register_device_settings(self, mac_id: str, name: str, mac_address: str):
        """Register per-device settings with localsettings via addSetting().

        Returns VeDbusItemImport handles stored in self._device_settings so that
        subsequent reads and writes go through the already-opened items rather
        than creating new get_object() proxies each time.
        """
        group_path = "/Settings/Devices/shyion/Device_%s" % mac_id

        for key, path, default, _min, _max in [
            ("Name",       "%s/Name"       % group_path, name,        0, 0),
            ("MAC",        "%s/MAC"        % group_path, mac_address,  0, 0),
            ("RelayState", "%s/RelayState" % group_path, -1,          -1, 1),
        ]:
            item_key = "%s/%s" % (mac_id, key)
            if item_key not in self._device_settings:
                try:
                    item = self._settings.addSetting(path, default, _min, _max)
                    self._device_settings[item_key] = item
                except Exception:
                    logger.exception(
                        "Failed to register setting %s for device %s", key, mac_id
                    )

    def _restore_relay_state(self, mac_id: str):
        """Try to read persisted relay state from com.victronenergy.settings."""
        self._register_device_settings(mac_id, mac_id, "")
        try:
            item = self._device_settings.get("%s/RelayState" % mac_id)
            if item is not None:
                rs = item.get_value()
                if rs is not None and int(rs) >= 0:
                    self._last_known_state[mac_id] = bool(int(rs))
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
            found = self._submit_ble(
                scan_for_relays(timeout=15.0)
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

        # Create the per-device BLE session lock if not already present
        if mac_id not in self._device_ble_lock:
            self._device_ble_lock[mac_id] = threading.Lock()

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

        lock = self._device_ble_lock.get(mac_id)
        if lock is not None and not lock.acquire(blocking=False):
            logger.warning(
                "Toggle '%s' skipped: BLE session already in progress for this device",
                name,
            )
            # Reject the write so the slider snaps back to the current known state
            return False

        # Cancel any pending reconnect retry; this toggle supersedes it and
        # its outcome will either clear the failure or restart the backoff.
        self._cancel_reconnect(mac_id)

        # Gray out the control while the BLE operation is in flight so the
        # GUI shows "working" instead of an optimistic slider move that
        # would snap back.  The confirmed state replaces this when the
        # operation completes.
        status_path = "/SwitchableOutput/relay_%s/Status" % mac_id
        if status_path in self._dbusservice:
            self._dbusservice[status_path] = STATUS_DISABLED

        # Reject the GUI write immediately -- we will apply the confirmed state
        # after the BLE operation completes.  This keeps the slider at the old
        # value and leaves SettingSync in "busy" state (buttons disabled) while
        # the BLE operation is in flight (~20s).
        thread = threading.Thread(
            target=self._run_toggle_relay,
            args=(mac_id, on, lock),
            name="Shyion_Toggle_%s" % mac_id,
            daemon=True,
        )
        thread.start()

        return False

    def _get_device_name(self, mac_id: str) -> str:
        name_path = "/SwitchableOutput/relay_%s/Name" % mac_id
        if name_path in self._dbusservice:
            return self._dbusservice[name_path]
        if mac_id in self._device_info:
            return self._device_info[mac_id].name
        return mac_id

    # ── BLE relay toggle (runs in background thread) ─────────────────────

    def _run_toggle_relay(self, mac_id: str, on: bool, lock: threading.Lock | None = None):
        """Transient session: connect, set relay, read state, disconnect.

        ``lock`` should be the already-acquired per-device BLE lock.  It is
        released when the operation completes.
        """
        if mac_id not in self._device_info:
            logger.warning("[%s] Unknown device for toggle", mac_id)
            if lock is not None:
                lock.release()
            GLib.idle_add(self._apply_relay_result, mac_id, None)
            return

        # If no lock was passed (legacy call path), try to acquire one
        if lock is None:
            lock = self._device_ble_lock.get(mac_id)
            if lock is not None and not lock.acquire(blocking=False):
                logger.warning(
                    "Toggle '%s' skipped: BLE session already in progress for this device",
                    self._get_device_name(mac_id),
                )
                return
            elif lock is None:
                logger.warning("[%s] No BLE lock for device, skipping toggle", mac_id)
                GLib.idle_add(self._apply_relay_result, mac_id, None)
                return

        try:
            address = self._device_info[mac_id].mac
            result = self._submit_ble(
                toggle_relay(address, on)
            )
            if result is None:
                # Scan/connect failures are usually transient (adapter
                # contention); one more attempt resolves most of them.
                logger.warning(
                    "[%s] Toggle failed, retrying once", mac_id
                )
                time.sleep(TOGGLE_RETRY_DELAY)
                result = self._submit_ble(
                    toggle_relay(address, on)
                )
            GLib.idle_add(self._apply_relay_result, mac_id, result)
        finally:
            lock.release()

    def _apply_relay_result(self, mac_id: str, state: bool | None) -> bool:
        """Apply BLE result to D-Bus (called on GLib thread)."""
        state_path = "/SwitchableOutput/relay_%s/State" % mac_id
        status_path = "/SwitchableOutput/relay_%s/Status" % mac_id

        if state is not None:
            self._cancel_reconnect(mac_id)
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
            # Keep retrying in the background; a successful poll restores
            # the real state and clears the disabled status.
            self._schedule_reconnect(mac_id)

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
        """Background thread: poll each device sequentially (one at a time).

        Emits ONE summary line per sweep.  A healthy poll is otherwise silent
        (the per-session lifecycle is DEBUG), but the reachable/total series is
        kept at INFO deliberately: it is what lets an outage be reconstructed
        from the log afterwards, and a relay that stays OFF while unreachable
        is indistinguishable from a healthy one without it.
        """
        macs = list(self._device_info.keys())
        unreachable = []
        for mac_id in macs:
            if self._run_poll_device(mac_id) is None:
                unreachable.append(self._get_device_name(mac_id))

        reached = len(macs) - len(unreachable)
        if unreachable:
            logger.info(
                "Poll sweep: %d/%d reachable; no answer from %s",
                reached, len(macs), ", ".join(unreachable),
            )
        else:
            logger.info("Poll sweep: %d/%d reachable", reached, len(macs))

    def _run_poll_device(self, mac_id: str):
        """Transient session: connect, query state, disconnect.

        Returns the relay state, or None if it could not be read -- the sweep
        summary counts those rather than each poll logging its own success.
        """
        if mac_id not in self._device_info:
            return None

        lock = self._device_ble_lock.get(mac_id)
        if lock is not None and not lock.acquire(blocking=False):
            logger.debug(
                "Poll skipped for %s: BLE session already in progress",
                self._get_device_name(mac_id),
            )
            return None

        try:
            address = self._device_info[mac_id].mac
            state = self._submit_ble(
                poll_relay_state(address)
            )
            GLib.idle_add(self._apply_poll_result, mac_id, state)
            return state
        finally:
            if lock is not None:
                lock.release()

    def _apply_poll_result(self, mac_id: str, state: bool | None) -> bool:
        """Apply poll result to D-Bus (called on GLib thread)."""
        if state is None:
            return False

        state_path = "/SwitchableOutput/relay_%s/State" % mac_id
        status_path = "/SwitchableOutput/relay_%s/Status" % mac_id

        self._cancel_reconnect(mac_id)
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

    # ── Reconnect backoff after failed operations ───────────────────────────

    def _schedule_reconnect(self, mac_id: str) -> bool:
        """Schedule a reconnect poll with exponential backoff (GLib thread).

        Called after a failed BLE operation.  Retry delays double from
        RECONNECT_BASE_DELAY up to RECONNECT_MAX_DELAY until a poll or
        toggle succeeds, at which point _cancel_reconnect() resets the
        backoff and the disabled status is replaced by the real state.
        """
        state = self._reconnect_state.setdefault(
            mac_id, {"attempt": 0, "timer_id": None}
        )
        if state["timer_id"] is not None:
            return False  # Retry already scheduled

        delay = min(
            RECONNECT_BASE_DELAY * (2 ** state["attempt"]),
            RECONNECT_MAX_DELAY,
        )
        state["attempt"] += 1
        state["timer_id"] = GLib.timeout_add_seconds(
            delay, self._trigger_reconnect, mac_id
        )
        logger.info("Reconnect attempt %d for %s in %ds",
                    state["attempt"], self._get_device_name(mac_id), delay)
        return False

    def _trigger_reconnect(self, mac_id: str) -> bool:
        """One-shot timer callback: run the reconnect poll in a thread."""
        state = self._reconnect_state.get(mac_id)
        if state is not None:
            state["timer_id"] = None

        thread = threading.Thread(
            target=self._run_reconnect,
            args=(mac_id,),
            name="Shyion_Reconnect_%s" % mac_id,
            daemon=True,
        )
        thread.start()
        return False

    def _run_reconnect(self, mac_id: str):
        """Background thread: poll a device that previously failed."""
        if mac_id not in self._device_info:
            return

        lock = self._device_ble_lock.get(mac_id)
        if lock is None or not lock.acquire(blocking=False):
            # Another BLE session is running for this device; its result
            # will settle the status.  Try again later just in case.
            GLib.idle_add(self._schedule_reconnect, mac_id)
            return

        state: bool | None = None
        try:
            address = self._device_info[mac_id].mac
            state = self._submit_ble(
                poll_relay_state(address)
            )
        except Exception:
            logger.exception("[%s] Reconnect poll failed", mac_id)
        finally:
            lock.release()

        if state is None:
            GLib.idle_add(self._schedule_reconnect, mac_id)
        else:
            GLib.idle_add(self._apply_poll_result, mac_id, state)

    def _cancel_reconnect(self, mac_id: str):
        """Stop pending reconnect retries and reset the backoff (GLib thread)."""
        state = self._reconnect_state.pop(mac_id, None)
        if state is not None and state["timer_id"] is not None:
            GLib.source_remove(state["timer_id"])

    # ── Settings persistence ────────────────────────────────────────────────

    def _save_device_to_settings(self, mac_id: str, name: str, mac_address: str):
        """Persist device name, MAC, and relay state via pre-opened VeDbusItemImport handles.

        Uses self._device_settings populated by _register_device_settings() so
        that no new D-Bus proxies (get_object calls) are created on each save.
        """
        try:
            # Ensure settings items exist (idempotent after first call)
            self._register_device_settings(mac_id, name, mac_address)

            name_item = self._device_settings.get("%s/Name" % mac_id)
            mac_item  = self._device_settings.get("%s/MAC" % mac_id)
            rs_item   = self._device_settings.get("%s/RelayState" % mac_id)

            if name_item is not None:
                name_item.set_value(name)
            if mac_item is not None and mac_address:
                mac_item.set_value(mac_address)

            relay_state = self._last_known_state.get(mac_id)
            if rs_item is not None and relay_state is not None:
                rs_item.set_value(1 if relay_state else 0)

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
                    # Register settings items for this device (idempotent).
                    # Reads go through the stored VeDbusItemImport handles to
                    # avoid creating new get_object() proxies.
                    self._register_device_settings(mac_id, mac_id, "")

                    if "Name" in sub_names:
                        item = self._device_settings.get("%s/Name" % mac_id)
                        if item is not None:
                            v = item.get_value()
                            if v is not None:
                                name = str(v)

                    mac_address = ""
                    if "MAC" in sub_names:
                        item = self._device_settings.get("%s/MAC" % mac_id)
                        if item is not None:
                            v = item.get_value()
                            if v is not None:
                                mac_address = str(v)

                    if "RelayState" in sub_names:
                        item = self._device_settings.get("%s/RelayState" % mac_id)
                        if item is not None:
                            v = item.get_value()
                            if v is not None:
                                rs = int(v)
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

            # Devices restored from settings are stored configuration too, so
            # they belong in the connect allowlist -- but only these, and the
            # widening is logged.
            shyion_bcm.extend_allowed_devices(
                d.mac for d in self._device_info.values()
            )

        except dbus.exceptions.DBusException:
            logger.debug("No saved devices to restore (fresh install)")
        except Exception:
            logger.exception("Error restoring devices from settings")

    # ── Persistent asyncio loop ─────────────────────────────────────────────

    def _run_async_loop(self):
        """Daemon-thread entry point: run the shared asyncio loop forever.

        Bleak's per-loop BlueZManager singleton is initialised lazily on
        the first coroutine submitted; subsequent submissions reuse it.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._async_loop = loop
        try:
            self._async_loop_ready.set()
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                logger.exception("Error closing async event loop")

    def _submit_ble(self, coro):
        """Submit a coroutine to the persistent loop and block until done.

        Returns the coroutine's result, or re-raises its exception.
        Intended for use from background worker threads — never call
        from the GLib main thread (would block it).
        """
        if self._async_loop is None or not self._async_loop.is_running():
            raise RuntimeError("Async event loop not available")
        fut = asyncio.run_coroutine_threadsafe(coro, self._async_loop)
        return fut.result()

    # ── Shutdown ────────────────────────────────────────────────────────────

    def stop(self):
        """Clean shutdown: stop scanning, cancel timers, stop async loop."""
        self._stop_scanning()

        if self._poll_timer_id is not None:
            GLib.source_remove(self._poll_timer_id)
            self._poll_timer_id = None

        for mac_id in list(self._reconnect_state.keys()):
            self._cancel_reconnect(mac_id)

        # Stop the persistent asyncio loop.  The daemon thread will exit
        # naturally once run_forever() returns.
        loop = self._async_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

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
