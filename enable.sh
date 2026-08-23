#!/bin/bash
#
# Enable script for dbus-shyion-switch
# This script is run on every boot via rc.local to ensure the service is properly set up
#

INSTALL_DIR="/data/apps/dbus-shyion-switch"
SERVICE_NAME="dbus-shyion-switch"

# Fix permissions
chmod +x "$INSTALL_DIR"/*.py
chmod +x "$INSTALL_DIR"/*.sh
chmod +x "$INSTALL_DIR/service/run"
chmod +x "$INSTALL_DIR/service/log/run"

# Verify critical submodules are present
DEPS_OK=true
for dep in velib_python/vedbus.py; do
    if [ ! -f "$INSTALL_DIR/ext/$dep" ]; then
        echo "WARNING: Missing dependency ext/$dep"
        DEPS_OK=false
    fi
done
if [ "$DEPS_OK" = false ]; then
    echo "Attempting to initialize submodules..."
    cd "$INSTALL_DIR" && git submodule update --init --recursive 2>/dev/null || true
fi

# The BLE stack is not vendored -- it comes from the shared checkout.  Warn
# rather than fail: enable.sh only wires up the service, and install.sh is
# what sets the stack up (and stops if it cannot).
BCM_DIR="${BCM_DIR:-/data/bcm}"
if [ ! -x "$BCM_DIR/python3" ]; then
    echo "WARNING: no shared BLE stack at $BCM_DIR (no interpreter shim)."
    echo "         The service will not start until it exists. Run:"
    echo "           bash $INSTALL_DIR/install.sh"
fi

# Create rc.local if it doesn't exist
if [ ! -f /data/rc.local ]; then
    echo "#!/bin/bash" > /data/rc.local
    chmod 755 /data/rc.local
fi

# Add enable script to rc.local (runs on every boot)
RC_ENTRY="bash $INSTALL_DIR/enable.sh"
grep -qxF "$RC_ENTRY" /data/rc.local || echo "$RC_ENTRY" >> /data/rc.local

# Create symlink to service directory
if [ ! -L "/service/$SERVICE_NAME" ]; then
    ln -s "$INSTALL_DIR/service" "/service/$SERVICE_NAME"
fi

echo "$SERVICE_NAME enabled"
