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

"""Wiring for the bleak-connection-manager catcher.

Kept in its own module on purpose: the catcher rebinds bleak.BleakClient and
bleak.BleakScanner process-wide, and a module only picks the wrappers up
through its own ``from bleak import ...`` if the install already happened when
it was imported.  shyion_ble imports bleak at module scope, so the driver
calls install_connection_manager() before importing it -- which is why this
module must not import bleak or shyion_ble at module scope itself.

BLE stack resolution: on a fleet install the service/run script execs the
shared BCM checkout's interpreter shim (/data/bcm/python3), which serves
bleak, bleak-retry-connector, bleak-connection-manager, dbus-fast and friends
to every consumer on the box.  A standalone checkout (bare clone, plain
python3) gets the same stack from this repo's vendored ext/ submodules
instead.  _ensure_ble_stack() prefers whatever the interpreter already
provides and only falls back to ext/.
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)


def _ensure_ble_stack() -> None:
    """Put the vendored ext/ BLE stack on sys.path unless already provided
    (by the shared /data/bcm shim, or by test stubs).

    The vendored bleak-connection-manager checkout carries the whole stack:
    its own src/, the libraries it vendors (dbus_fast, bluetooth_adapters,
    aiooui) under ext/, and bleak plus bleak-retry-connector as submodules
    under ext/upstream/.  This repo therefore vendors one BLE submodule, not
    six.
    """
    if "bleak_connection_manager" in sys.modules:
        return
    try:
        import importlib.util
        if importlib.util.find_spec("bleak_connection_manager") is not None:
            return
    except (ImportError, ValueError):
        # A stub with __spec__ = None makes find_spec raise ValueError; any
        # odd interpreter state degrades to "insert the ext paths", which is
        # the safe direction -- worst case the vendored copy shadows the
        # shim, never running with no stack at all.
        pass
    _bcm = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ext", "bleak-connection-manager"
    )
    for _sub in [
        os.path.join(_bcm, "src"),
        os.path.join(_bcm, "ext"),
        os.path.join(_bcm, "ext", "upstream", "bleak"),
        os.path.join(_bcm, "ext", "upstream", "bleak-retry-connector", "src"),
    ]:
        if os.path.isdir(_sub) and _sub not in sys.path:
            sys.path.insert(0, _sub)


def parse_adapters(raw: str) -> list[str]:
    """Split the adapters config value into catcher entries.

    Entries are handed to the catcher verbatim -- it understands both the
    plain ``hciX`` pool form and the ``MAC@hciX`` pin form, so this only
    splits and trims.  An empty value means "every adapter the kernel
    exposes is a candidate", which is the catcher's own default.
    """
    return [entry.strip() for entry in (raw or "").split(",") if entry.strip()]


def parse_link_caps(raw: str) -> dict[str, int]:
    """Split the link_caps config value into {adapter: capacity}.

    Entries have the form ``hciX:N`` with N the established-link capacity of
    that adapter.  Malformed entries are logged and skipped rather than
    guessed at: a wrong cap silently gates connections.
    """
    caps: dict[str, int] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        adapter, sep, cap = entry.partition(":")
        adapter = adapter.strip()
        try:
            cap_value = int(cap.strip()) if sep else 0
        except ValueError:
            cap_value = 0
        if not adapter or cap_value <= 0:
            logger.warning(
                "Ignoring malformed link_caps entry '%s'", entry
            )
            continue
        caps[adapter] = cap_value
    return caps


def install_connection_manager(defaults) -> bool:
    """Install the bleak catcher for this process.

    ``defaults`` is the config's DEFAULT section (anything with .get).

    Returns True when the catcher was installed.  A failed install is logged
    and swallowed: the catcher is coordination, and connecting uncoordinated
    beats not connecting at all.
    """
    adapters = parse_adapters(defaults.get("adapters", ""))
    link_caps = parse_link_caps(defaults.get("link_caps", ""))

    try:
        _ensure_ble_stack()
        from bleak_connection_manager import install_bleak_catcher

        install_bleak_catcher(
            "dbus-shyion-switch",
            adapters=adapters,
            link_caps=link_caps,
            # This driver scans for relays, so its scans must take the
            # exclusive per-card scan claim -- that is what steers the other
            # BLE services on this GX away from the radio we are scanning on.
            wrap_scanner=True,
        )
    except Exception:
        logger.exception(
            "Failed to install the BLE connection manager, "
            "continuing without adapter coordination"
        )
        return False

    logger.info(
        "BLE connection manager installed (adapters=%s, link_caps=%s)",
        adapters or "auto", link_caps or "uncapped",
    )
    return True
