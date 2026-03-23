"""Telegram notification module for stock agent alerts."""

import os
import json
import logging
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LENGTH = 4096


def _get_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id or token == "your_bot_token_here":
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
    return token, chat_id


def send_message(text, parse_mode="HTML"):
    """Send a message via Telegram. Returns True on success."""
    try:
        token, chat_id = _get_config()
    except ValueError as e:
        logger.error(f"Telegram config error: {e}")
        return False

    url = TELEGRAM_API.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        data = urlencode(payload).encode("utf-8")
        req = Request(url, data=data, method="POST")
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                logger.info("Telegram message sent successfully")
                return True
            else:
                logger.error(f"Telegram API error: {result}")
                return False
    except (URLError, OSError) as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False


def send_alerts(alerts):
    """Send a list of alert strings as a Telegram message. Splits if too long."""
    if not alerts:
        return True

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"<b>Stock Agent Alert - {now}</b>\n{'—' * 30}\n\n"

    combined = header + "\n\n".join(alerts)

    # split into chunks if too long
    if len(combined) <= MAX_MESSAGE_LENGTH:
        return send_message(combined)

    chunks = []
    current = header
    for alert in alerts:
        if len(current) + len(alert) + 2 > MAX_MESSAGE_LENGTH:
            chunks.append(current)
            current = ""
        current += alert + "\n\n"
    if current.strip():
        chunks.append(current)

    success = True
    for chunk in chunks:
        if not send_message(chunk):
            success = False
    return success


# --- Alert formatters ---

def profit_alert(position, current_price, pnl_dollars, pnl_pct, sentiment=None):
    """Format a profit lock alert."""
    ticker = position["ticker"]
    pos_type = position["type"].replace("_", " ").title()

    if position["type"] == "stock":
        detail = f"{position['shares']} shares @ ${position['avg_cost']:.2f}"
    elif position["type"] == "long_call":
        detail = f"${position['strike']} call exp {position['expiry']}, {position['contracts']}x"
    elif position["type"] == "bull_put_spread":
        detail = (f"${position['short_put_strike']}/{position['long_put_strike']} "
                  f"put spread exp {position['expiry']}, {position['contracts']}x")
    else:
        detail = str(position)

    msg = (
        f"🔔 <b>LOCK PROFIT: {ticker}</b>\n"
        f"Type: {pos_type}\n"
        f"Position: {detail}\n"
        f"Current price: <b>${current_price:.2f}</b>\n"
        f"P&L: <b>${pnl_dollars:+,.2f} ({pnl_pct:+.1f}%)</b>"
    )
    if sentiment:
        msg += f"\nSentiment: {sentiment}"
    return msg


def spread_danger_alert(position, current_price, distance):
    """Format a bull put spread danger zone alert."""
    ticker = position["ticker"]
    short_strike = position["short_put_strike"]

    return (
        f"⚠️ <b>SPREAD DANGER: {ticker}</b>\n"
        f"Price: <b>${current_price:.2f}</b> — only "
        f"<b>${distance:.2f}</b> above short put strike ${short_strike:.2f}\n"
        f"Spread: ${short_strike}/{position['long_put_strike']} "
        f"exp {position['expiry']}"
    )


def watchlist_signal(ticker, current_price, recommendation, reasons, sentiment=None, iv=None):
    """Format a watchlist bullish signal alert."""
    reasons_text = ", ".join(reasons)
    msg = (
        f"📈 <b>BULLISH SETUP: {ticker}</b>\n"
        f"Price: ${current_price:.2f}\n"
        f"Signal: <b>{recommendation.upper()}</b>\n"
        f"Reasons: {reasons_text}"
    )
    if iv is not None:
        iv_note = "high IV, good for selling spreads" if iv >= 40 else "moderate IV" if iv >= 25 else "low IV"
        msg += f"\nOptions IV: <b>{iv}%</b> ({iv_note})"
    if sentiment:
        msg += f"\nSentiment: {sentiment}"
    return msg


def earnings_warning(ticker, earnings_date, current_price):
    """Format an earnings proximity warning."""
    return (
        f"📅 <b>EARNINGS TOMORROW: {ticker}</b>\n"
        f"Earnings date: <b>{earnings_date.strftime('%A, %B %d')}</b>\n"
        f"Current price: ${current_price:.2f}\n"
        f"Consider adjusting positions before earnings"
    )


def position_summary(ticker, current_price, pnl_dollars, pnl_pct, pos_type):
    """Format a position P&L line for summary messages."""
    sign = "🟢" if pnl_pct >= 0 else "🔴"
    return f"{sign} {ticker} ({pos_type}): ${current_price:.2f} | {pnl_pct:+.1f}% (${pnl_dollars:+,.2f})"
