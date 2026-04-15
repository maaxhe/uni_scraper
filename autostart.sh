#!/bin/bash
# ─────────────────────────────────────────────────────────────────
# Stud.IP Dashboard — autostart manager (macOS only)
#
# Usage:
#   ./autostart.sh install    → start in background + auto-start on login
#   ./autostart.sh uninstall  → stop and remove autostart
#   ./autostart.sh status     → show if running
#   ./autostart.sh logs       → tail live log output
# ─────────────────────────────────────────────────────────────────

LABEL="com.studip.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/dashboard.log"
PYTHON="$(command -v python3 || command -v python)"

if [ -z "$PYTHON" ]; then
  echo "❌  Python not found. Make sure Python is installed and in your PATH."
  exit 1
fi

case "$1" in

  # ── INSTALL ───────────────────────────────────────────────────
  install)
    if [ ! -f "$DIR/.env" ]; then
      echo "❌  .env file not found in $DIR"
      echo "   Copy .env.example to .env and fill in your credentials first."
      exit 1
    fi

    # Build environment variables block from .env
    ENV_ENTRIES=""
    while IFS= read -r line || [ -n "$line" ]; do
      # Skip comments and empty lines
      [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
      KEY="${line%%=*}"
      VAL="${line#*=}"
      # Strip surrounding quotes from value
      VAL="${VAL%\"}"
      VAL="${VAL#\"}"
      VAL="${VAL%\'}"
      VAL="${VAL#\'}"
      ENV_ENTRIES="$ENV_ENTRIES
        <key>$KEY</key>
        <string>$VAL</string>"
    done < "$DIR/.env"

    # Write the plist
    cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$DIR/dashboard.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$DIR</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>$ENV_ENTRIES
    </dict>

    <!-- Start automatically when you log in -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Restart automatically if it crashes -->
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
EOF

    # Unload first in case it's already registered
    launchctl unload "$PLIST" 2>/dev/null

    # Load and start
    launchctl load "$PLIST"

    echo "✅  Dashboard is now running in the background."
    echo "   It will also start automatically every time you log in."
    echo ""
    echo "   Open in browser: http://localhost:5001"
    echo "   View logs:       ./autostart.sh logs"
    echo "   Stop:            ./autostart.sh uninstall"
    ;;

  # ── UNINSTALL ─────────────────────────────────────────────────
  uninstall)
    if [ ! -f "$PLIST" ]; then
      echo "ℹ️   Autostart is not installed."
      exit 0
    fi
    launchctl unload "$PLIST"
    rm "$PLIST"
    echo "✅  Dashboard stopped and removed from autostart."
    ;;

  # ── STATUS ────────────────────────────────────────────────────
  status)
    if launchctl list | grep -q "$LABEL"; then
      PID=$(launchctl list | grep "$LABEL" | awk '{print $1}')
      if [ "$PID" != "-" ]; then
        echo "✅  Running (PID $PID) → http://localhost:5001"
      else
        echo "⚠️   Registered but not currently running (may have crashed — check logs)"
      fi
    else
      echo "⛔  Not running and not set up for autostart."
      echo "   Run: ./autostart.sh install"
    fi
    ;;

  # ── LOGS ─────────────────────────────────────────────────────
  logs)
    if [ ! -f "$LOG" ]; then
      echo "No log file yet."
      exit 0
    fi
    echo "── Showing live log (Ctrl+C to stop) ──"
    tail -f "$LOG"
    ;;

  *)
    echo "Usage: $0 {install|uninstall|status|logs}"
    echo ""
    echo "  install    Start dashboard in background + auto-start on login"
    echo "  uninstall  Stop dashboard and remove autostart"
    echo "  status     Check if dashboard is running"
    echo "  logs       Show live log output"
    ;;
esac
