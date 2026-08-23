#!/bin/bash
#
# Remote installer for dbus-shyion-switch on Venus OS
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/TechBlueprints/dbus-shyion-switch/main/install.sh | bash
#

set -e

REPO_URL="https://github.com/TechBlueprints/dbus-shyion-switch.git"
INSTALL_DIR="/data/apps/dbus-shyion-switch"
SERVICE_NAME="dbus-shyion-switch"

echo "========================================"
echo "Shyion BLE Relay Switch Installer"
echo "========================================"
echo ""

# Check if running on Venus OS
if [ ! -d "/data/apps" ]; then
    echo "Error: /data/apps not found. This script must run on Venus OS."
    exit 1
fi

# Step 1: Ensure git is installed
echo "Step 1: Checking for git..."
if ! command -v git >/dev/null 2>&1; then
    echo "Git not found. Installing git..."
    if ! opkg install git; then
        echo "Error: Failed to install git."
        exit 1
    fi
    echo "Git installed successfully"
else
    echo "Git already installed"
fi
echo ""

# Step 2: Clone or update repository
echo "Step 2: Setting up repository..."
cd /data/apps

NEEDS_RESTART=false

if [ -d "$INSTALL_DIR" ]; then
    echo "Directory exists: $INSTALL_DIR"
    cd "$INSTALL_DIR"

    git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true

    if [ -d .git ]; then
        CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
        if [ "$CURRENT_REMOTE" != "$REPO_URL" ]; then
            echo "Updating remote URL to $REPO_URL..."
            git remote set-url origin "$REPO_URL" 2>/dev/null || git remote add origin "$REPO_URL"
        fi

        echo "Fetching latest changes..."
        git fetch origin

        LOCAL=$(git rev-parse HEAD 2>/dev/null || echo "none")
        REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "none")

        if [ "$LOCAL" != "$REMOTE" ]; then
            echo "Updates available. Resetting to latest..."
            git checkout main 2>/dev/null || git checkout -b main origin/main
            git reset --hard origin/main
            NEEDS_RESTART=true
            echo "Repository updated to latest"
        else
            echo "Already up to date"
        fi
    else
        echo "Not a git repository. Converting..."
        git init
        git remote add origin "$REPO_URL"
        git fetch origin
        git checkout -b main origin/main 2>/dev/null || git checkout main
        git reset --hard origin/main
        git branch --set-upstream-to=origin/main main 2>/dev/null || true
        NEEDS_RESTART=true
        echo "Converted to git repository and updated to latest"
    fi
else
    echo "Directory does not exist. Cloning repository..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
    echo "Repository cloned"
fi
echo ""

# Step 3: Initialize submodules (service-specific deps only)
echo "Step 3: Setting up dependencies..."
cd "$INSTALL_DIR"

# Only Victron's D-Bus helpers are vendored here.  The BLE stack deliberately
# is not -- it comes from the shared checkout set up in step 3b.
SUBMODULES="
velib_python|https://github.com/victronenergy/velib_python.git
"

git submodule update --init --recursive 2>/dev/null && {
    echo "All submodules initialized"
} || {
    echo "Submodule init failed, cloning dependencies individually..."
    mkdir -p ext
    echo "$SUBMODULES" | while IFS='|' read -r name url; do
        [ -z "$name" ] && continue
        if [ ! -d "ext/$name/.git" ] && [ ! -f "ext/$name/.git" ]; then
            echo "  Cloning $name..."
            rm -rf "ext/$name" 2>/dev/null
            git clone "$url" "ext/$name" 2>/dev/null || echo "  WARNING: Failed to clone $name"
        fi
    done
}

# Verify critical dependencies
MISSING=""
for dep in velib_python/vedbus.py; do
    if [ ! -f "ext/$dep" ]; then
        MISSING="$MISSING  ext/$dep\n"
    fi
done
if [ -n "$MISSING" ]; then
    echo ""
    echo "WARNING: Some dependencies are missing:"
    printf "$MISSING"
    echo "The service may not start correctly."
    echo "Try: cd $INSTALL_DIR && git submodule update --init --recursive"
else
    echo "All dependencies verified"
fi
echo ""

# Step 3b: Converge the shared BLE stack (/data/bcm)
#
# This is where the BLE stack comes from -- bleak, bleak-retry-connector,
# bleak-connection-manager, dbus-fast and friends -- for this service and for
# every other BLE consumer on the box.  Nothing is vendored here, on purpose:
# BCM keys adapter claims by adapter MAC, so a consumer on a different build
# writes claims the rest of the fleet cannot read, and the coordination fails
# silently.  One checkout, one version.
#
# Idempotent and ff-only, so running it from a stale installer can never move
# the fleet backwards.  Rollback is `git -C /data/bcm checkout <hash> &&
# /data/bcm/install.sh`.
#
# Unlike step 3 this is fatal.  There is no fallback stack to degrade to, and
# a service that starts without BLE is worse than an install that stops and
# says why.
BCM_DIR="${BCM_DIR:-/data/bcm}"
BCM_URL=https://github.com/TechBlueprints/bleak-connection-manager
echo "Step 3b: Converging shared BLE stack at $BCM_DIR..."

if [ -d "$BCM_DIR/.git" ]; then
    git -C "$BCM_DIR" fetch -q origin && git -C "$BCM_DIR" merge -q --ff-only origin/main || {
        echo "ERROR: could not fast-forward $BCM_DIR to origin/main."
        echo "It may have local commits or be on a pinned hash. Resolve there, then re-run."
        exit 1
    }
else
    git clone -q "$BCM_URL" "$BCM_DIR" || {
        echo "ERROR: could not clone the shared BLE stack from $BCM_URL into $BCM_DIR."
        exit 1
    }
fi

# Run their installer unpiped: it fetches the pinned submodules, smoke-imports
# the stack and writes the shim, and it refuses to update the shim if that
# import fails.  Piping it would eat the exit code.
"$BCM_DIR/install.sh"
BCM_RC=$?
if [ "$BCM_RC" -ne 0 ]; then
    echo ""
    echo "ERROR: $BCM_DIR/install.sh failed (exit $BCM_RC)."
    echo "The BLE stack is not usable, so this service would not start."
    echo "Re-running is safe -- it is idempotent and resumes from partial state."
    exit 1
fi
if [ ! -x "$BCM_DIR/python3" ]; then
    echo "ERROR: $BCM_DIR/install.sh reported success but wrote no interpreter shim."
    exit 1
fi
echo "Shared BLE stack ready ($(git -C "$BCM_DIR" rev-parse --short HEAD))"
echo ""

# Step 4: Run enable script
echo "Step 4: Enabling service..."
bash "$INSTALL_DIR/enable.sh"
echo ""

# Step 5: Start or restart service
echo "Step 5: Starting service..."
if svstat "/service/$SERVICE_NAME" 2>/dev/null | grep -q ": up "; then
    if [ "$NEEDS_RESTART" = true ]; then
        echo "Restarting service to apply updates..."
        svc -t "/service/$SERVICE_NAME"
        sleep 2
        echo "Service restarted"
    else
        echo "Service already running, no updates needed"
    fi
else
    svc -u "/service/$SERVICE_NAME"
    sleep 2
    echo "Service started"
fi

echo ""
echo "========================================"
echo "Installation Complete!"
echo "========================================"
echo ""
echo "IMPORTANT: Run provision.py on your local machine to generate config.ini"
echo "  python3 provision.py --email your@email.com"
echo "  scp config.ini root@<cerbo>:/data/apps/dbus-shyion-switch/config.ini"
echo ""
echo "Then restart the service:"
echo "  svc -t /service/$SERVICE_NAME"
echo ""
echo "Service status:"
svstat "/service/$SERVICE_NAME"
echo ""
echo "View logs:"
echo "  tail -n 30 /var/log/$SERVICE_NAME/current | tai64nlocal"
echo ""
echo "Service management:"
echo "  svc -u /service/$SERVICE_NAME  # Start"
echo "  svc -d /service/$SERVICE_NAME  # Stop"
echo "  svc -t /service/$SERVICE_NAME  # Restart"
echo ""
