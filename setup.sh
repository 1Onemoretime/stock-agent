#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Setting up stock-agent in: $SCRIPT_DIR"

# --- Python venv ---
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$SCRIPT_DIR/venv"
fi

echo "Installing Python dependencies..."
"$SCRIPT_DIR/venv/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"

# --- Config files ---
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo ""
    echo ">>> Created .env — edit it with your Telegram bot token and chat ID:"
    echo "    $SCRIPT_DIR/.env"
fi

if [ ! -f "$SCRIPT_DIR/positions.json" ]; then
    cp "$SCRIPT_DIR/positions.example.json" "$SCRIPT_DIR/positions.json"
    echo ">>> Created positions.json from template"
fi

# --- Check claude CLI ---
if ! command -v claude &> /dev/null; then
    echo ""
    echo "WARNING: 'claude' CLI not found. Sentiment analysis requires it."
    echo "Install: npm install -g @anthropic-ai/claude-code"
fi

# --- Scheduler ---
OS="$(uname -s)"
PYTHON_PATH="$SCRIPT_DIR/venv/bin/python3"

if [ "$OS" = "Darwin" ]; then
    echo ""
    echo "Detected macOS — setting up launchd..."

    PLIST_SRC="$SCRIPT_DIR/com.stock-agent.plist"
    PLIST_DST="$HOME/Library/LaunchAgents/com.stock-agent.plist"

    # Generate plist with correct paths
    cat > "$PLIST_SRC" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.stock-agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_PATH</string>
        <string>$SCRIPT_DIR/run_analysis.py</string>
    </array>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/launchd-stderr.log</string>
</dict>
</plist>
PLIST

    # Unload if already loaded
    launchctl unload "$PLIST_DST" 2>/dev/null || true

    cp "$PLIST_SRC" "$PLIST_DST"
    launchctl load "$PLIST_DST"
    echo "Scheduler installed and started (every hour)"
    echo "  Stop:  launchctl unload ~/Library/LaunchAgents/com.stock-agent.plist"
    echo "  Start: launchctl load ~/Library/LaunchAgents/com.stock-agent.plist"

elif [ "$OS" = "Linux" ]; then
    echo ""
    echo "Detected Linux — setting up systemd timer..."

    SYSTEMD_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_DIR"

    # Service unit
    cat > "$SYSTEMD_DIR/stock-agent.service" <<EOF
[Unit]
Description=Stock Analysis Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON_PATH $SCRIPT_DIR/run_analysis.py
Environment=PATH=/usr/local/bin:/usr/bin:/bin
EOF

    # Timer unit (every hour)
    cat > "$SYSTEMD_DIR/stock-agent.timer" <<EOF
[Unit]
Description=Run stock agent every hour

[Timer]
OnCalendar=*:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable stock-agent.timer
    systemctl --user start stock-agent.timer

    echo "Scheduler installed and started (every hour)"
    echo "  Status:  systemctl --user status stock-agent.timer"
    echo "  Logs:    journalctl --user -u stock-agent.service"
    echo "  Stop:    systemctl --user stop stock-agent.timer"
    echo "  Start:   systemctl --user start stock-agent.timer"
    echo "  Run now: systemctl --user start stock-agent.service"
else
    echo "Unknown OS: $OS — set up a cron job manually:"
    echo "  0 * * * * $PYTHON_PATH $SCRIPT_DIR/run_analysis.py"
fi

echo ""
echo "Setup complete!"
echo ""
echo "Quick test:  $PYTHON_PATH $SCRIPT_DIR/run_analysis.py"
