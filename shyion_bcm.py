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

BLE stack resolution: this repo vendors no BLE libraries at all.  The whole
stack -- bleak, bleak-retry-connector, bleak-connection-manager, dbus-fast,
bluetooth-adapters, aiooui -- comes from the shared BCM checkout that every
consumer on the box shares, which install.sh converges and whose own
installer builds the interpreter shim service/run execs.

Vendoring a second copy would defeat the point: BCM keys adapter claims by
adapter MAC, and a consumer running a different build can write claims the
rest of the fleet cannot read -- silently, with no error.  One checkout, one
version, or the coordination stops working.

_ensure_ble_stack() therefore prefers whatever the interpreter already
provides (the shim, normally) and otherwise resolves the shared checkout
directly, so hand-run CLI tools work under a plain python3 too.
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)


# The shared BCM checkout.  BCM_ROOT is the canary knob its own shim honours
# -- point one service at a second checkout to trial a version without moving
# the fleet -- so respect it here too, or a hand-run CLI would silently probe
# a different build than the service it is meant to be debugging.
BCM_ROOT = os.environ.get("BCM_ROOT", "/data/bcm")


def _bcm_lib_paths(root: str) -> list[str]:
    """The paths the BCM shim exports as PYTHONPATH, in its order.

    Mirrors `/data/bcm/python3`; keep in step with it if that layout moves.
    """
    return [
        os.path.join(root, "src"),
        os.path.join(root, "ext"),
        os.path.join(root, "ext", "upstream", "bleak"),
        os.path.join(root, "ext", "upstream", "bleak-retry-connector", "src"),
    ]


def _ensure_ble_stack() -> None:
    """Make the shared BLE stack importable, if it is not already.

    Normally a no-op: the service runs under the shim, which has already put
    the stack on PYTHONPATH.  The fallback exists for CLI tools run by hand
    under a plain python3 -- resolving the shared checkout ourselves rather
    than vendoring a copy, so those tools exercise the same build the service
    does.
    """
    if "bleak_connection_manager" in sys.modules:
        return
    try:
        import importlib.util
        if importlib.util.find_spec("bleak_connection_manager") is not None:
            return
    except (ImportError, ValueError):
        # A stub with __spec__ = None makes find_spec raise ValueError; any
        # odd interpreter state degrades to "add the shared paths", which is
        # the safe direction -- worst case a redundant sys.path entry.
        pass

    found = [p for p in _bcm_lib_paths(BCM_ROOT) if os.path.isdir(p)]
    if not found:
        raise ImportError(
            f"The shared BLE stack is not installed at {BCM_ROOT}. "
            f"Install it with:  git clone "
            f"https://github.com/TechBlueprints/bleak-connection-manager "
            f"{BCM_ROOT} && {BCM_ROOT}/install.sh   "
            f"(or re-run this project's install.sh, which does it for you). "
            f"Set BCM_ROOT to use a checkout elsewhere."
        )
    for path in reversed(found):
        if path not in sys.path:
            sys.path.insert(0, path)


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
