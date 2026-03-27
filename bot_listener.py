#!/usr/bin/env python3
"""Telegram bot listener: receives messages and routes them to Claude for handling."""

import json
import logging
import os
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import urlencode

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(AGENT_DIR, "bot_listener.log")
POSITIONS_FILE = os.path.join(AGENT_DIR, "positions.json")
LAST_STATE_FILE = os.path.join(AGENT_DIR, "last_state.json")
OFFSET_FILE = os.path.join(AGENT_DIR, ".telegram_offset")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"
LONG_POLL_TIMEOUT = 30  # seconds for getUpdates long-poll


def load_env():
    """Load .env file into os.environ."""
    env_path = os.path.join(AGENT_DIR, ".env")
    if not os.path.exists(env_path):
        logger.warning("No .env file found")
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def get_config():
    """Return (token, chat_id) from environment, raising ValueError if missing."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id or token == "your_bot_token_here":
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
    return token, chat_id


def telegram_request(token, method, params=None):
    """Make a Telegram Bot API request. Returns parsed JSON or None on error."""
    url = TELEGRAM_API_BASE.format(token=token, method=method)
    if params:
        data = urlencode(params).encode("utf-8")
        req = Request(url, data=data, method="POST")
    else:
        req = Request(url)
    try:
        with urlopen(req, timeout=LONG_POLL_TIMEOUT + 5) as resp:
            return json.loads(resp.read())
    except (URLError, OSError) as e:
        logger.error(f"Telegram API error ({method}): {e}")
        return None


def get_updates(token, offset=None):
    """Long-poll for new Telegram updates."""
    params = {
        "timeout": LONG_POLL_TIMEOUT,
        "allowed_updates": '["message"]',
    }
    if offset is not None:
        params["offset"] = offset
    return telegram_request(token, "getUpdates", params)


def send_message(token, chat_id, text, parse_mode="HTML"):
    """Send a Telegram message, splitting if it exceeds the 4096-char limit."""
    for i in range(0, max(1, len(text)), 4096):
        chunk = text[i:i + 4096]
        params = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        telegram_request(token, "sendMessage", params)


def send_chat_action(token, chat_id, action="typing"):
    """Send a chat action (e.g. 'typing') to show the bot is working."""
    telegram_request(token, "sendChatAction", {"chat_id": chat_id, "action": action})


def load_offset():
    """Load the last processed update_id offset."""
    if os.path.exists(OFFSET_FILE):
        try:
            with open(OFFSET_FILE) as f:
                return int(f.read().strip())
        except (ValueError, OSError):
            pass
    return None


def save_offset(offset):
    """Persist the update_id offset so we don't reprocess messages on restart."""
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


def build_context():
    """Build a portfolio context string to include in Claude's prompt."""
    lines = [
        "You are a stock portfolio assistant. You help the user manage and understand their stock positions and watchlist.",
        "Answer questions about their portfolio, trading signals, P&L, options, and market data.",
        "Be concise and direct. Use plain text (no markdown) since this is displayed in Telegram.",
    ]

    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE) as f:
                portfolio = json.load(f)

            settings = portfolio.get("settings", {})
            positions = portfolio.get("positions", [])
            watchlist = portfolio.get("watchlist", [])

            lines.append(f"\nWatchlist ({len(watchlist)} tickers): {', '.join(watchlist)}")
            lines.append(
                f"Settings: profit lock at {settings.get('profit_lock_pct', 50)}%, "
                f"spread danger within ${settings.get('spread_danger_dollars', 5)}"
            )

            if positions:
                lines.append(f"\nOpen positions ({len(positions)}):")
                for p in positions:
                    ticker = p.get("ticker", "?")
                    pos_type = p.get("type", "?")
                    if pos_type == "stock":
                        lines.append(
                            f"  {ticker}: {p.get('shares')} shares @ ${p.get('avg_cost'):.2f} avg cost"
                        )
                    elif pos_type == "long_call":
                        lines.append(
                            f"  {ticker}: {p.get('contracts')}x ${p.get('strike')} call "
                            f"exp {p.get('expiry')}, premium paid ${p.get('premium_paid'):.2f}"
                        )
                    elif pos_type == "bull_put_spread":
                        lines.append(
                            f"  {ticker}: {p.get('contracts')}x "
                            f"${p.get('short_put_strike')}/{p.get('long_put_strike')} put spread "
                            f"exp {p.get('expiry')}, net credit ${p.get('net_credit'):.2f}"
                        )
            else:
                lines.append("\nNo open positions.")
        except Exception as e:
            logger.warning(f"Could not load positions: {e}")

    if os.path.exists(LAST_STATE_FILE):
        try:
            with open(LAST_STATE_FILE) as f:
                state = json.load(f)
            if state:
                lines.append("\nActive alerts from last hourly analysis:")
                for key in state:
                    lines.append(f"  - {key}")
            else:
                lines.append("\nNo active alerts from last analysis.")
        except Exception:
            pass

    return "\n".join(lines)


def ask_claude(user_message, context):
    """Send the user's message to Claude with portfolio context. Returns Claude's reply."""
    prompt = f"{context}\n\nUser: {user_message}"
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.error(f"Claude failed (rc={result.returncode}): {result.stderr[:200]}")
            return "Sorry, I couldn't process your request right now."

        outer = json.loads(result.stdout.strip())
        response = outer.get("result", "").strip()
        return response if response else "Sorry, I got an empty response."

    except subprocess.TimeoutExpired:
        logger.error("Claude timed out")
        return "Sorry, that request timed out. Try a simpler question."
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Claude response error: {e}")
        return "Sorry, there was an error processing your request."


def handle_message(token, chat_id, text):
    """Handle a single incoming message: route to Claude and send the reply."""
    text = text.strip()
    if not text:
        return

    # Built-in commands that don't need Claude
    if text.lower() in ("/start", "/help"):
        send_message(token, chat_id,
            "Stock Agent Bot\n\n"
            "Ask me anything about your portfolio, e.g.:\n"
            "  What are my current positions?\n"
            "  How is AAPL doing?\n"
            "  Explain my bull put spread on SPY\n"
            "  What does RSI oversold mean?\n\n"
            "I have full context of your portfolio and latest alert state."
        )
        return

    send_chat_action(token, chat_id, "typing")
    context = build_context()
    response = ask_claude(text, context)
    send_message(token, chat_id, response)


def main():
    load_env()

    try:
        token, authorized_chat_id = get_config()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    logger.info("Bot listener starting...")
    send_message(token, authorized_chat_id, "Stock agent bot is online. Send me a message or /help to get started.")

    offset = load_offset()

    while True:
        try:
            result = get_updates(token, offset)

            if result is None:
                # Network error — back off briefly
                time.sleep(5)
                continue

            if not result.get("ok"):
                logger.warning(f"getUpdates non-OK: {result}")
                time.sleep(5)
                continue

            for update in result.get("result", []):
                update_id = update.get("update_id")
                offset = update_id + 1
                save_offset(offset)

                message = update.get("message", {})
                if not message:
                    continue

                chat_id = str(message.get("chat", {}).get("id", ""))
                text = message.get("text", "")
                from_user = message.get("from", {}).get("username", "unknown")

                # Security: only respond to the configured chat
                if chat_id != str(authorized_chat_id):
                    logger.warning(f"Ignored message from unauthorized chat_id={chat_id}")
                    continue

                logger.info(f"Message from @{from_user}: {text[:100]}")
                handle_message(token, chat_id, text)
                logger.info(f"Replied to @{from_user}")

        except KeyboardInterrupt:
            logger.info("Bot listener stopped by user")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
