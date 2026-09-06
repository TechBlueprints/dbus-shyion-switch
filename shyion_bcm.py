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

The stack is sourced IN PROCESS, per the fleet consumer contract: the folder
is configuration, nothing about how the process was launched has any say, and
bleak_connection_manager is imported before anything imports bleak -- which is
also what stands the sitewide autowire hook down for us.  There is no
interpreter shim any more.
"""

from __future__ import annotations

import logging
import os
import re
import sys

logger = logging.getLogger(__name__)


# Where the shared bleak-connection-manager install lives.  One key, per the
# fleet consumer contract (CONSUMERS.md section 2): config.ini decides, an
# EMPTY value means never look (a deliberate standalone run), and nothing
# about how the process was launched has any say -- no interpreter shim, no
# PYTHONPATH, no environment contract.  Pointing this key at a second checkout
# is the canary knob; there is deliberately no environment override, because
# an env var would be exactly the launch-time influence the contract removes.
DEFAULT_BCM_DIR = "/data/bcm"

# StartNotify policy, also consumer-side and defaulting on.  AcquireNotify is
# the only path that creates the notify_io BlueZ 5.72 double-frees, and
# bleak's own default is moving toward AcquireNotify -- so this is the guard
# against a library flip, not a preference.
DEFAULT_FORCE_START_NOTIFY = True

_logged_once = set()


def _log_once(level, message):
    """The startup lines are contract text the fleet monitor greps for, and
    each must appear at most once per process life."""
    if message in _logged_once:
        return
    _logged_once.add(message)
    logger.log(level, message)


def _ensure_ble_stack(shared_dir):
    """Resolve the shared BLE stack and emit the contract's startup lines.

    Returns True when the connection manager is importable.  Delegates the
    mechanics to ble_stack.ensure_ble_stack(), which is lifted verbatim from
    the fleet reference so the pinned drift test against the installer's own
    layout covers this consumer too.  vendored_dir is None: this repo carries
    no BLE stack, so "no shared install" means plain bleak or nothing.
    """
    import ble_stack

    state = ble_stack.ensure_ble_stack(shared_dir or "", vendored_dir=None)

    if state == "shared" or state == "provided":
        try:
            import bleak_connection_manager
            served_from = os.path.dirname(
                os.path.abspath(bleak_connection_manager.__file__)
            )
        except Exception:
            served_from = shared_dir
        # The package's OWN directory, not the configured key: with a canary
        # checkout the two differ, and only the former is the truth.
        _log_once(
            logging.INFO,
            f"BLE coordination: bleak_connection_manager loaded from {served_from}",
        )
        return True

    if ble_stack.shared_failure is not None:
        _log_once(
            logging.ERROR,
            f"BLE coordination: shared install at {shared_dir} is present but "
            f"unusable, running uncoordinated: {ble_stack.shared_failure}",
        )
    else:
        _log_once(
            logging.WARNING,
            f"BLE coordination: no shared install at {shared_dir}; running "
            f"uncoordinated, no claims, no adapter routing, no card recovery",
        )
    return False


_HCI_RE = re.compile(r"^hci\d+$")


def _resolve_to_mac(name: str) -> str | None:
    """`hciN` -> its card's MAC in `AA:BB:CC:DD:EE:FF` form, or None.

    None means "leave this entry alone": the card is absent, dead (all-zeros),
    or unidentifiable on this kernel.  Guessing would be worse than keeping a
    stale name -- an hciN that resolves to nothing today may be a card that is
    merely unplugged.
    """
    if not _HCI_RE.match(name):
        return None
    try:
        _ensure_ble_stack(DEFAULT_BCM_DIR)
        from bleak_connection_manager import claims
    except Exception:
        logger.debug("claims module unavailable; cannot resolve %s", name,
                     exc_info=True)
        return None
    mac = claims.adapter_mac(name)
    if not mac or mac == claims.UNKNOWN_MAC:
        return None
    return mac.upper()


def normalize_adapter_entry(entry: str) -> str:
    """Rewrite one adapters entry so the adapter is named by MAC.

    Handles both forms the catcher accepts: a bare adapter (`hci3`) and a
    device pin (`AA:BB:..@hci3`), whose suffix is an adapter reference too.
    Entries already naming a MAC pass through untouched.
    """
    device, sep, adapter = entry.rpartition("@")
    target = adapter if sep else entry
    resolved = _resolve_to_mac(target)
    if resolved is None:
        return entry
    return f"{device}@{resolved}" if sep else resolved


def normalize_config_adapters(config_path: str) -> bool:
    """Rewrite `adapters` in config.ini so cards are named by MAC, not hciN.

    hciN numbering is not an identity: a USB reset, a replug or a reboot
    renumbers cards, and a numeric pin then silently points at a different
    radio.  On a box where adapters are deliberately allocated between
    services -- one card reserved for a BMS link, another for scanning --
    getting that backwards means connecting on the card we promised to leave
    alone.  So the config is corrected in place the first time we see it,
    rather than re-resolved forever at every use.

    Rewrites the line textually rather than via configparser, which would
    discard every comment in the file.  Returns whether anything changed.
    """
    try:
        with open(config_path) as handle:
            lines = handle.readlines()
    except OSError:
        logger.debug("cannot read %s to normalize adapters", config_path,
                     exc_info=True)
        return False

    changed = False
    for i, line in enumerate(lines):
        key, sep, value = line.partition("=")
        if not sep or key.strip() != "adapters" or line.lstrip().startswith("#"):
            continue
        entries = value.replace(",", " ").split()
        rewritten = [normalize_adapter_entry(e) for e in entries]
        if rewritten == entries:
            continue
        for before, after in zip(entries, rewritten):
            if before != after:
                logger.warning(
                    "config.ini: adapter %s is a name, not an identity -- "
                    "rewriting to %s (hciN numbering moves on reset/replug/reboot)",
                    before, after,
                )
        lines[i] = f"{key}= {' '.join(rewritten)}\n"
        changed = True

    if not changed:
        return False

    # Write via a temp file in the same directory then rename, so a crash or a
    # full disk cannot leave the service with a truncated config.
    tmp = f"{config_path}.tmp"
    try:
        with open(tmp, "w") as handle:
            handle.writelines(lines)
        os.replace(tmp, config_path)
    except OSError:
        logger.exception("could not rewrite %s; continuing with the values as read",
                         config_path)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False

    logger.info("config.ini: adapters normalized to MAC identities")
    return True


def _as_bool(raw: str, default: bool) -> bool:
    """Config booleans, permissive about spelling; blank means the default."""
    text = (raw or "").strip().lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    logger.warning(
        "Ignoring unrecognised force_start_notify value %r; using %s", raw, default
    )
    return default


def parse_adapters(raw: str) -> list[str]:
    """Split the adapters config value into catcher entries.

    Entries are handed to the catcher verbatim -- it understands both the
    plain ``hciX`` pool form and the ``MAC@hciX`` pin form, so this only
    splits and trims.  An empty value means "every adapter the kernel
    exposes is a candidate", which is the catcher's own default.
    """
    # Separated by whitespace or commas or both.  The other consumers on the
    # box write these space-separated, so comma-only splitting silently turns
    # "MAC MAC" into one bogus entry that matches no card -- and the catcher
    # then falls back to treating every adapter as a candidate, which is the
    # opposite of what an allowlist is for.
    return (raw or "").replace(",", " ").split()


def parse_link_caps(raw: str) -> dict[str, int]:
    """Split the link_caps config value into {adapter: capacity}.

    Entries have the form ``<adapter>:N`` with N the established-link capacity
    of that adapter, separated by whitespace or commas.  The adapter is named
    by MAC now, which itself contains colons, so the split is on the LAST one:
    ``00:01:95:CC:32:F7:5`` is that card capped at 5.  Malformed entries are
    logged and skipped rather than guessed at: a wrong cap silently gates
    connections.
    """
    caps: dict[str, int] = {}
    for entry in (raw or "").replace(",", " ").split():
        adapter, sep, cap = entry.rpartition(":")
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


def install_connection_manager(defaults, config_path: str | None = None) -> bool:
    """Install the bleak catcher for this process.

    ``defaults`` is the config's DEFAULT section (anything with .get).
    ``config_path``, when given, is normalized in place first so any hciN
    adapter names become MAC identities -- and the values used here are the
    corrected ones, not the stale names.

    Returns True when the catcher was installed.  A failed install is logged
    and swallowed: the catcher is coordination, and connecting uncoordinated
    beats not connecting at all.
    """
    raw_adapters = defaults.get("adapters", "")
    if config_path and normalize_config_adapters(config_path):
        # Re-resolve rather than re-reading the file: the section handed to us
        # is already parsed, and the same translation gives the same answer.
        raw_adapters = " ".join(
            normalize_adapter_entry(e) for e in raw_adapters.replace(",", " ").split()
        )
    adapters = parse_adapters(raw_adapters)
    link_caps = parse_link_caps(defaults.get("link_caps", ""))

    shared_dir = defaults.get(
        "bluetooth_connection_manager_dir", DEFAULT_BCM_DIR
    ).strip()
    force = _as_bool(
        defaults.get("force_start_notify", ""), DEFAULT_FORCE_START_NOTIFY
    )

    if not _ensure_ble_stack(shared_dir):
        return False

    try:
        import inspect

        from bleak_connection_manager import install_bleak_catcher

        kwargs = dict(
            adapters=adapters,
            link_caps=link_caps,
            # This driver scans for relays, so its scans must take the
            # exclusive per-card scan claim -- that is what steers the other
            # BLE services on this GX away from the radio we are scanning on.
            wrap_scanner=True,
        )
        if "force_start_notify" in inspect.signature(install_bleak_catcher).parameters:
            kwargs["force_start_notify"] = force
        else:
            # Pre-159536a install: the parameter does not exist yet, so the
            # policy has to go through the legacy environment path instead.
            os.environ["BCM_FORCE_START_NOTIFY"] = "true" if force else "false"
            _log_once(
                logging.WARNING,
                f"BLE coordination: shared install at {shared_dir} predates the "
                f"force_start_notify parameter; StartNotify policy passed "
                f"through the legacy BCM_FORCE_START_NOTIFY environment",
            )

        install_bleak_catcher("dbus-shyion-switch", **kwargs)
    except Exception as exc:
        # The import worked but the catcher refused: the operator action is to
        # fix the driver or the catcher, not the install, which is why this is
        # distinct from "present but unusable".  Uncoordinated still beats not
        # connecting at all, so this is reported, not raised.
        _log_once(
            logging.ERROR,
            f"BLE coordination: catcher would not install from {shared_dir}, "
            f"running uncoordinated: {exc!r}",
        )
        return False

    pinned = sum(1 for entry in adapters if "@" in entry)
    _log_once(
        logging.INFO,
        f"BLE coordination: catcher installed (force_start_notify={force}, "
        f"adapters={len(adapters)} configured, {pinned} pinned)",
    )
    return True
