#!/bin/bash
#
# Disable script for dbus-shyion-switch
# Cleanly stops and removes the service
#

INSTALL_DIR="/data/apps/dbus-shyion-switch"
SERVICE_NAME="dbus-shyion-switch"

# Stop the service
svc -d "/service/$SERVICE_NAME" 2>/dev/null || true
sleep 2

# Remove service symlink
rm -rf "/service/$SERVICE_NAME" 2>/dev/null || true

# Kill any remaining processes
pkill -f "supervise $SERVICE_NAME" 2>/dev/null || true
pkill -f "python.*dbus-shyion-switch" 2>/dev/null || true
pkill -f "python.*shyion_device" 2>/dev/null || true

# Remove enable script from rc.local
sed -i "/.*dbus-shyion-switch.*/d" /data/rc.local 2>/dev/null || true

echo "$SERVICE_NAME disabled"
