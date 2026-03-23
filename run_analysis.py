#!/usr/bin/env python3
"""Stock agent: hourly analysis of positions and watchlist with Telegram alerts."""

import csv
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

# Setup logging
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(AGENT_DIR, "agent.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

ANALYZER_PATH = os.path.join(AGENT_DIR, "skill", "scripts", "main.py")
POSITIONS_FILE = os.path.join(AGENT_DIR, "positions.json")
HISTORY_FILE = os.path.join(AGENT_DIR, "history.csv")
LAST_STATE_FILE = os.path.join(AGENT_DIR, "last_state.json")


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


def is_market_open():
    """Check if US stock market is currently open."""
    # Calculate Eastern Time
    now_utc = datetime.now(timezone.utc)
    year = now_utc.year

    # Determine if DST is active (2nd Sunday Mar - 1st Sunday Nov)
    # March: find 2nd Sunday
    mar1 = datetime(year, 3, 1, tzinfo=timezone.utc)
    mar_first_sunday = 7 - mar1.weekday() % 7
    if mar1.weekday() == 6:
        mar_first_sunday = 0
    dst_start_day = mar_first_sunday + 7 + 1  # 2nd Sunday, but we need day of month
    # Simpler approach: iterate
    dst_start = None
    count = 0
    for d in range(1, 32):
        dt = datetime(year, 3, d, 2, 0, tzinfo=timezone.utc)
        if dt.weekday() == 6:  # Sunday
            count += 1
            if count == 2:
                dst_start = datetime(year, 3, d, 7, 0, tzinfo=timezone.utc)  # 2am ET = 7am UTC
                break

    # November: find 1st Sunday
    count = 0
    dst_end = None
    for d in range(1, 8):
        dt = datetime(year, 11, d, 2, 0, tzinfo=timezone.utc)
        if dt.weekday() == 6:
            dst_end = datetime(year, 11, d, 6, 0, tzinfo=timezone.utc)  # 2am ET = 6am UTC
            break

    if dst_start and dst_end and dst_start <= now_utc < dst_end:
        et_offset = timedelta(hours=-4)  # EDT
    else:
        et_offset = timedelta(hours=-5)  # EST

    now_et = now_utc + et_offset

    # Check weekday
    if now_et.weekday() >= 5:  # Saturday or Sunday
        logger.info(f"Market closed: weekend ({now_et.strftime('%A')})")
        return False

    # Check market hours (9:30 AM - 4:00 PM ET)
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)

    if not (market_open <= now_et <= market_close):
        logger.info(f"Market closed: outside trading hours ({now_et.strftime('%H:%M')} ET)")
        return False

    # US market holidays for 2026 (update annually)
    holidays_2026 = [
        (1, 1),    # New Year's Day
        (1, 19),   # MLK Day
        (2, 16),   # Presidents' Day
        (4, 3),    # Good Friday
        (5, 25),   # Memorial Day
        (6, 19),   # Juneteenth
        (7, 3),    # Independence Day (observed)
        (9, 7),    # Labor Day
        (11, 26),  # Thanksgiving
        (12, 25),  # Christmas
    ]
    today = (now_et.month, now_et.day)
    if today in holidays_2026:
        logger.info(f"Market closed: holiday ({now_et.strftime('%B %d')})")
        return False

    return True


def call_analyzer(ticker):
    """Call the stock-analyzer skill for a ticker. Returns parsed JSON or None."""
    try:
        result = subprocess.run(
            [sys.executable, ANALYZER_PATH, "--ticker", ticker, "--technical", "--period", "1y", "--interval", "1d"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.error(f"Analyzer failed for {ticker}: {result.stderr}")
            return None

        data = json.loads(result.stdout)
        if "error" in data:
            logger.error(f"Analyzer error for {ticker}: {data['error']}")
            return None
        return data

    except subprocess.TimeoutExpired:
        logger.error(f"Analyzer timed out for {ticker}")
        return None
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Analyzer parse error for {ticker}: {e}")
        return None


def get_options_iv(ticker, current_price):
    """Get ATM implied volatility for a ticker. Returns IV as percentage or None."""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        expirations = stock.options
        if not expirations:
            return None

        # Pick expiry at least 14 days out for meaningful IV
        from datetime import date, timedelta
        min_expiry = date.today() + timedelta(days=14)
        target_expiry = None
        for exp in expirations:
            exp_date = date.fromisoformat(exp)
            if exp_date >= min_expiry:
                target_expiry = exp
                break

        if not target_expiry:
            target_expiry = expirations[-1]

        chain = stock.option_chain(target_expiry)
        puts = chain.puts

        # Find ATM put (strike closest to current price)
        atm_idx = (puts["strike"] - current_price).abs().idxmin()
        iv = puts.loc[atm_idx, "impliedVolatility"]

        return round(iv * 100, 1)  # as percentage
    except Exception as e:
        logger.debug(f"Could not get IV for {ticker}: {e}")
        return None


def get_earnings_date(ticker):
    """Get next earnings date for a ticker via yfinance. Returns date or None."""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        cal = stock.calendar
        if cal and "Earnings Date" in cal:
            dates = cal["Earnings Date"]
            if isinstance(dates, list) and dates:
                return dates[0]
            return dates
    except Exception as e:
        logger.debug(f"Could not get earnings date for {ticker}: {e}")
    return None


def check_earnings_warning(ticker, earnings_date):
    """Check if we should warn about upcoming earnings.
    Warn the day before earnings, or on Friday if earnings are on a weekend."""
    if not earnings_date:
        return False, None

    from datetime import date
    today = date.today()

    # Day before earnings
    day_before = earnings_date - timedelta(days=1)

    # If earnings on Saturday, warn on Friday (2 days before)
    # If earnings on Sunday, warn on Friday (3 days before)
    earnings_weekday = earnings_date.weekday()
    if earnings_weekday == 5:  # Saturday
        warn_date = earnings_date - timedelta(days=1)  # Friday
    elif earnings_weekday == 6:  # Sunday
        warn_date = earnings_date - timedelta(days=2)  # Friday
    else:
        warn_date = day_before
        # If day_before is a weekend, shift to Friday
        if warn_date.weekday() == 5:
            warn_date = warn_date - timedelta(days=1)
        elif warn_date.weekday() == 6:
            warn_date = warn_date - timedelta(days=2)

    if today == warn_date:
        return True, earnings_date

    return False, earnings_date


def compute_pnl(position, current_price):
    """Compute P&L for a position. Returns (pnl_dollars, pnl_percent)."""
    pos_type = position["type"]

    if pos_type == "stock":
        cost = position["avg_cost"]
        shares = position["shares"]
        pnl = (current_price - cost) * shares
        pct = ((current_price - cost) / cost) * 100
        return pnl, pct

    elif pos_type == "long_call":
        strike = position["strike"]
        premium = position["premium_paid"]
        contracts = position["contracts"]
        # Intrinsic value only (underestimates before expiry)
        intrinsic = max(0, current_price - strike)
        pnl_per = (intrinsic - premium) * 100
        pnl = pnl_per * contracts
        pct = ((intrinsic - premium) / premium) * 100 if premium > 0 else 0
        return pnl, pct

    elif pos_type == "bull_put_spread":
        # Max profit = net credit received. At expiry, if price > short put, full profit.
        # Approximate current P&L based on how far price is from short put
        short_strike = position["short_put_strike"]
        long_strike = position["long_put_strike"]
        credit = position["net_credit"]
        contracts = position["contracts"]
        spread_width = short_strike - long_strike

        if current_price >= short_strike:
            # Both puts OTM, approaching max profit
            pnl = credit * 100 * contracts
            pct = (credit / (spread_width - credit)) * 100
        elif current_price <= long_strike:
            # Max loss
            max_loss = (spread_width - credit) * 100 * contracts
            pnl = -max_loss
            pct = -100
        else:
            # Between strikes, partial loss
            loss_per = short_strike - current_price
            pnl = (credit - loss_per) * 100 * contracts
            pct = ((credit - loss_per) / (spread_width - credit)) * 100

        return pnl, pct

    return 0, 0


def check_spread_danger(position, current_price, danger_dollars):
    """Check if price is within danger zone of short put strike."""
    if position["type"] != "bull_put_spread":
        return False, 0
    short_strike = position["short_put_strike"]
    distance = current_price - short_strike
    # Alert if price is within danger_dollars above the strike (or below it)
    if distance <= danger_dollars:
        return True, distance
    return False, distance


def analyze_sentiment_with_claude(analyzed_data):
    """Send all headlines to Claude for proper financial sentiment analysis.
    Returns dict: ticker -> sentiment string."""
    # Collect headlines per ticker
    ticker_headlines = {}
    for ticker, data in analyzed_data.items():
        headlines = data.get("news_sentiment", {}).get("headlines", [])
        titles = [h.get("title", "") for h in headlines if h.get("title")]
        if titles:
            ticker_headlines[ticker] = titles

    if not ticker_headlines:
        return {}

    # Build prompt for Claude
    prompt_lines = [
        "Analyze the financial sentiment of these news headlines for each stock ticker.",
        "For each ticker, rate overall sentiment as: Bullish, Bearish, or Neutral.",
        "Add a one-sentence summary of the news mood.",
        "Reply ONLY in this exact JSON format, no other text:",
        '{"TICKER": {"sentiment": "Bullish|Bearish|Neutral", "summary": "one sentence"}, ...}',
        "",
    ]
    for ticker, titles in ticker_headlines.items():
        prompt_lines.append(f"{ticker}:")
        for t in titles:
            prompt_lines.append(f"  - {t}")
        prompt_lines.append("")

    prompt = "\n".join(prompt_lines)

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.error(f"Claude sentiment analysis failed: {result.stderr}")
            return {}

        # Parse Claude's response - extract JSON from result
        response = result.stdout.strip()
        # --output-format json wraps in {"result": "..."}
        outer = json.loads(response)
        inner = outer.get("result", response)

        # Find JSON object in the response
        start = inner.find("{")
        end = inner.rfind("}") + 1
        if start >= 0 and end > start:
            sentiments = json.loads(inner[start:end])
            return sentiments

    except subprocess.TimeoutExpired:
        logger.error("Claude sentiment analysis timed out")
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Claude sentiment parse error: {e}")

    return {}


def format_sentiment(ticker, claude_sentiments):
    """Format sentiment from Claude analysis. Returns string or None."""
    info = claude_sentiments.get(ticker)
    if not info:
        return None

    sentiment = info.get("sentiment", "Neutral")
    summary = info.get("summary", "")

    result = f"{sentiment}"
    if summary:
        result += f" — <i>{summary[:100]}</i>"
    return result


def find_bullish_setups(signals, indicators):
    """Check for bullish technical setups. RSI14 oversold is the primary buy signal."""
    reasons = []
    rsi = indicators.get("rsi_14")
    is_oversold = rsi is not None and rsi < 30

    if is_oversold:
        reasons.append(f"RSI14 OVERSOLD ({rsi:.1f})")

    if signals.get("macd_signal") == "bullish":
        reasons.append("MACD bullish")

    if signals.get("bb_signal") == "oversold":
        reasons.append("Bollinger oversold")

    rec = signals.get("recommendation", "").lower()
    if rec in ("strong buy", "buy"):
        reasons.append(f"Recommendation: {rec}")

    trend = signals.get("trend", "").lower()
    if "uptrend" in trend:
        reasons.append(f"Trend: {trend}")

    return reasons, is_oversold


def load_last_state():
    """Load previous run's alert state. Returns dict of alert keys."""
    if os.path.exists(LAST_STATE_FILE):
        try:
            with open(LAST_STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            pass
    return {}


def save_state(state):
    """Save current alert state for next run comparison."""
    with open(LAST_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_history(row):
    """Append a row to history.csv."""
    headers = ["timestamp", "ticker", "price", "rsi", "macd_signal",
               "recommendation", "position_type", "pnl_dollars", "pnl_pct", "alert"]

    file_exists = os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def main():
    load_env()

    # Import notifications after env is loaded
    from notifications import send_alerts, profit_alert, spread_danger_alert, watchlist_signal, earnings_warning

    if not is_market_open():
        return

    logger.info("=== Stock Agent run starting ===")

    # Load positions
    with open(POSITIONS_FILE) as f:
        portfolio = json.load(f)

    settings = portfolio.get("settings", {})
    positions = portfolio.get("positions", [])
    watchlist = portfolio.get("watchlist", [])
    profit_threshold = settings.get("profit_lock_pct", 50)
    danger_dollars = settings.get("spread_danger_dollars", 5)

    alerts = []
    current_state = {}  # tracks alert conclusions per ticker
    analyzed = {}  # cache: ticker -> analyzer data

    last_state = load_last_state()

    # --- Analyze position tickers ---
    position_tickers = set(p["ticker"] for p in positions)
    all_tickers = position_tickers | set(watchlist)

    logger.info(f"Analyzing {len(all_tickers)} tickers: {', '.join(sorted(all_tickers))}")

    for ticker in sorted(all_tickers):
        data = call_analyzer(ticker)
        if data:
            analyzed[ticker] = data
        time.sleep(1)  # rate limit

    # --- Analyze sentiment with Claude only for tickers with alerts (done after detection) ---
    # We'll collect which tickers need sentiment below, then batch-call Claude

    # --- Check positions (build state, defer alerts) ---
    position_alerts = []  # (alert_key, alert_builder_func)
    for pos in positions:
        ticker = pos["ticker"]
        data = analyzed.get(ticker)
        if not data:
            logger.warning(f"No data for position {ticker}, skipping")
            continue

        current_price = data["price"]["current"]
        pnl, pnl_pct = compute_pnl(pos, current_price)
        signals = data.get("technical", {}).get("signals", {})
        indicators = data.get("technical", {}).get("indicators", {})

        logger.info(f"Position {ticker} ({pos['type']}): ${current_price:.2f}, P&L: ${pnl:+,.2f} ({pnl_pct:+.1f}%)")

        # Profit lock check
        if pnl_pct >= profit_threshold:
            state_key = f"profit_lock:{ticker}:{pos['type']}"
            current_state[state_key] = True
            if state_key not in last_state:
                position_alerts.append((ticker, lambda t=ticker, p=pos, cp=current_price, pnl_d=pnl, pnl_p=pnl_pct, s=None:
                    profit_alert(p, cp, pnl_d, pnl_p, sentiment=s)))
                logger.info(f"  -> PROFIT ALERT triggered (NEW)")
            else:
                logger.info(f"  -> PROFIT ALERT (unchanged, skipping)")

        # Spread danger check
        if pos["type"] == "bull_put_spread":
            in_danger, distance = check_spread_danger(pos, current_price, danger_dollars)
            if in_danger:
                state_key = f"spread_danger:{ticker}"
                current_state[state_key] = True
                if state_key not in last_state:
                    alerts.append(spread_danger_alert(pos, current_price, distance))
                    logger.info(f"  -> SPREAD DANGER triggered (NEW, ${distance:.2f} from strike)")
                else:
                    logger.info(f"  -> SPREAD DANGER (unchanged, skipping)")

        # History
        append_history({
            "timestamp": datetime.now().isoformat(),
            "ticker": ticker,
            "price": current_price,
            "rsi": indicators.get("rsi_14", ""),
            "macd_signal": signals.get("macd_signal", ""),
            "recommendation": signals.get("recommendation", ""),
            "position_type": pos["type"],
            "pnl_dollars": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 1),
            "alert": "profit_lock" if pnl_pct >= profit_threshold else "",
        })

    # --- Check watchlist (build state, defer alerts) ---
    watchlist_alerts = []  # (ticker, recommendation, reasons, is_oversold)
    for ticker in watchlist:
        data = analyzed.get(ticker)
        if not data:
            continue

        current_price = data["price"]["current"]
        signals = data.get("technical", {}).get("signals", {})
        indicators = data.get("technical", {}).get("indicators", {})
        recommendation = signals.get("recommendation", "hold")

        reasons, is_oversold = find_bullish_setups(signals, indicators)

        # Build state key from the conclusion (signal + reasons)
        if is_oversold or len(reasons) >= 2:
            reason_keys = sorted(r.split("(")[0].strip() for r in reasons)  # normalize
            state_key = f"watchlist:{ticker}:{recommendation}:{','.join(reason_keys)}"
            current_state[state_key] = True

            if state_key not in last_state:
                watchlist_alerts.append((ticker, current_price, recommendation, reasons, is_oversold))
                logger.info(f"Watchlist {ticker}: BULLISH SETUP (NEW) - {', '.join(reasons)}")
            else:
                logger.info(f"Watchlist {ticker}: BULLISH SETUP (unchanged, skipping)")

        append_history({
            "timestamp": datetime.now().isoformat(),
            "ticker": ticker,
            "price": current_price,
            "rsi": indicators.get("rsi_14", ""),
            "macd_signal": signals.get("macd_signal", ""),
            "recommendation": recommendation,
            "position_type": "watchlist",
            "pnl_dollars": 0,
            "pnl_pct": 0,
            "alert": "bullish_setup" if (is_oversold or len(reasons) >= 2) else "",
        })

    # --- Check earnings dates ---
    for ticker in sorted(all_tickers):
        data = analyzed.get(ticker)
        if not data:
            continue
        earnings_date = get_earnings_date(ticker)
        warn, edate = check_earnings_warning(ticker, earnings_date)
        if warn:
            state_key = f"earnings:{ticker}:{edate}"
            current_state[state_key] = True
            if state_key not in last_state:
                current_price = data["price"]["current"]
                alerts.append(earnings_warning(ticker, edate, current_price))
                logger.info(f"EARNINGS WARNING: {ticker} reports on {edate} (NEW)")
            else:
                logger.info(f"EARNINGS WARNING: {ticker} (unchanged, skipping)")
        time.sleep(0.5)  # rate limit yfinance calendar calls

    # --- Only call Claude sentiment + IV for tickers that have new alerts ---
    tickers_needing_sentiment = set()
    for ticker, *_ in watchlist_alerts:
        tickers_needing_sentiment.add(ticker)
    for ticker, builder in position_alerts:
        tickers_needing_sentiment.add(ticker)

    claude_sentiments = {}
    if tickers_needing_sentiment:
        # Filter analyzed data to only tickers with new alerts
        filtered = {t: analyzed[t] for t in tickers_needing_sentiment if t in analyzed}
        logger.info(f"Analyzing sentiment with Claude for: {', '.join(sorted(tickers_needing_sentiment))}")
        claude_sentiments = analyze_sentiment_with_claude(filtered)

    # --- Build final alerts with sentiment ---
    for ticker, builder in position_alerts:
        sentiment = format_sentiment(ticker, claude_sentiments)
        # Rebuild the alert with sentiment (call the lambda with sentiment override)
        # Need to reconstruct — find the position data again
        for pos in positions:
            if pos["ticker"] == ticker:
                data = analyzed.get(ticker)
                if data:
                    cp = data["price"]["current"]
                    pnl, pnl_pct = compute_pnl(pos, cp)
                    alerts.append(profit_alert(pos, cp, pnl, pnl_pct, sentiment=sentiment))
                break

    for ticker, current_price, recommendation, reasons, is_oversold in watchlist_alerts:
        sentiment = format_sentiment(ticker, claude_sentiments)
        iv = None
        if recommendation in ("buy", "strong buy"):
            iv = get_options_iv(ticker, current_price)
            if iv:
                logger.info(f"  {ticker} ATM IV: {iv}%")
        alerts.append(watchlist_signal(ticker, current_price, recommendation, reasons, sentiment=sentiment, iv=iv))

    # --- Send only new/changed alerts ---
    if alerts:
        logger.info(f"Sending {len(alerts)} NEW alerts via Telegram")
        send_alerts(alerts)
    else:
        logger.info("No new alerts (nothing changed)")

    # --- Notify when alerts resolve ---
    resolved = []
    for key in last_state:
        if key not in current_state:
            parts = key.split(":")
            alert_type = parts[0]
            ticker = parts[1] if len(parts) > 1 else "?"
            if alert_type == "profit_lock":
                resolved.append(f"✅ {ticker}: profit lock alert cleared")
            elif alert_type == "spread_danger":
                resolved.append(f"✅ {ticker}: spread danger cleared")
            elif alert_type == "watchlist":
                resolved.append(f"⬜ {ticker}: bullish setup no longer active")
    if resolved:
        from notifications import send_message
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        msg = f"<b>Status Update - {now}</b>\n{'—' * 30}\n\n" + "\n".join(resolved)
        send_message(msg)
        logger.info(f"Sent {len(resolved)} resolution notifications")

    # --- Save state for next run ---
    save_state(current_state)

    logger.info("=== Stock Agent run complete ===")


if __name__ == "__main__":
    main()
