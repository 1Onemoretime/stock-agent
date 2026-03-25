# Stock Agent

Automated stock analysis agent that monitors your portfolio and watchlist, sending Telegram alerts when something needs your attention.

## What it does

- Analyzes your positions and watchlist tickers every hour during market hours
- Sends Telegram alerts only when the analysis conclusion changes (no spam)
- Notifies when alerts resolve (signal no longer active)

### Alerts

| Alert | Trigger |
|-------|---------|
| **Bullish setup** | RSI14 oversold (<30) or 2+ bullish signals (MACD, Bollinger, trend) |
| **Profit lock** | Any position reaches 50% profit |
| **Spread danger** | Price within $5 of bull put spread short put strike |
| **Earnings warning** | Trading day before earnings date |

### Analysis

- **Technical indicators**: RSI14 (Wilder's smoothing), MACD, Bollinger Bands, SMA 20/50/200, Stochastic, ADX, ATR, OBV
- **News sentiment**: Headlines analyzed by Claude for financial context (not keyword matching)
- **Options IV**: ATM implied volatility shown on buy signals for bull put spread assessment
- **Daily chart**: 1 year of daily candles, interval explicitly set to 1d

### Strategy

Bullish bias — long stock, long calls, or bull put spreads. Primary buy signal is RSI14 oversold.

## Setup

```bash
git clone git@github.com:1Onemoretime/stock-agent.git
cd stock-agent
./setup.sh
```

The setup script:
1. Creates a Python virtual environment and installs dependencies
2. Copies config templates (`.env.example` -> `.env`, `positions.example.json` -> `positions.json`)
3. Detects your OS and installs the scheduler:
   - **macOS**: launchd (runs every hour)
   - **Linux**: systemd timer (runs every hour)

### Configure

Edit `.env` with your Telegram bot credentials:
```
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

To get these:
1. Message `@BotFather` on Telegram, send `/newbot`
2. Copy the token to `.env`
3. Message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to get your chat ID

### Requirements

- Python 3.9+
- [Claude Code CLI](https://github.com/anthropics/claude-code) (for sentiment analysis)

## Position tracking

Edit `positions.json` to add your holdings (or tell Claude about your position changes):

```json
{
  "positions": [
    {"type": "stock", "ticker": "AAPL", "shares": 50, "avg_cost": 220.00},
    {"type": "long_call", "ticker": "AAPL", "contracts": 1, "strike": 200, "expiry": "2026-05-04", "premium_paid": 15.00},
    {"type": "bull_put_spread", "ticker": "TSLA", "contracts": 1, "short_put_strike": 200, "long_put_strike": 180, "expiry": "2026-05-04", "net_credit": 5.50}
  ],
  "watchlist": ["AAPL", "SPY", "NVDA"],
  "settings": {
    "profit_lock_pct": 50,
    "spread_danger_dollars": 5
  }
}
```

## Scheduler control

**macOS:**
```bash
launchctl load ~/Library/LaunchAgents/com.stock-agent.plist    # start
launchctl unload ~/Library/LaunchAgents/com.stock-agent.plist  # stop
```

**Linux:**
```bash
systemctl --user start stock-agent.timer    # start
systemctl --user stop stock-agent.timer     # stop
systemctl --user status stock-agent.timer   # status
systemctl --user start stock-agent.service  # run now
journalctl --user -u stock-agent.service    # logs
```

## Manual test run

```bash
./venv/bin/python3 run_analysis.py
```

Note: market hours check will skip execution on weekends and outside 9:30 AM - 4:00 PM ET.

## ⚠️ Risk Tips

**Important: This framework is for research and educational purposes only and does not constitute investment advice.**

- 📊 Trading performance may vary depending on a number of factors
- 🤖 AI model predictions are uncertain
- 💰 Investing is risky, so decisions should be made with caution
- 👨‍💼 It is recommended to consult a professional financial advisor
