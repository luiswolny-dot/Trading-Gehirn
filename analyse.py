import os
import json
import csv
import time
from datetime import datetime, timezone, timedelta

import requests

API_KEY = os.environ["FINNHUB_API_KEY"]
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
REQUEST_DELAY = 1.05
DEEP_DIVE_COUNT = 25
EXPIRY_DAYS = 3
ATR_PERIOD = 10

STARTING_BALANCE = 100.0
MAX_CONCURRENT_POSITIONS = 3
TRADE_FEE = 1.0  # pro Order, also 2€ Rundtrip
MIN_TRADE_EUR = 5.0

POSITIVE_WORDS = {"beat","beats","surge","soar","rally","upgrade","outperform","record",
                   "growth","strong","gain","gains","jump","rise","rises","bullish","buy",
                   "raises","raised","exceed","exceeds","soars","surges"}
NEGATIVE_WORDS = {"miss","misses","plunge","crash","downgrade","underperform","weak","loss",
                   "losses","fall","falls","drop","drops","bearish","sell","cuts","cut",
                   "lawsuit","fraud","investigation","recall","decline","plunges"}

REC_LOG_PATH = "data/recommendations_log.csv"
REC_LOG_FIELDS = ["id", "symbol", "opened_at", "entry", "stop_loss", "target_1", "target_2",
                   "status", "resolved_at", "resolved_price", "pnl_pct", "invested_eur"]
PORTFOLIO_PATH = "data/portfolio.json"

def load_watchlist():
    try:
        r = requests.get(SP500_URL, timeout=15)
        r.raise_for_status()
        lines = r.text.splitlines()
        reader = csv.DictReader(lines)
        tickers = [row["Symbol"].strip().upper().replace(".", "-") for row in reader if row.get("Symbol")]
        if tickers:
            print(f"S&P-500-Liste geladen: {len(tickers)} Ticker")
            return tickers
    except Exception as e:
        print(f"Konnte S&P-500-Liste nicht laden ({e}), nutze lokale watchlist.txt als Fallback")

    with open("watchlist.txt") as f:
        return [line.strip().upper() for line in f if line.strip()]

def fetch_quote(symbol):
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={API_KEY}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()

def fetch_metric(symbol):
    url = f"https://finnhub.io/api/v1/stock/metric?symbol={symbol}&metric=all&token={API_KEY}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json().get("metric", {}) or {}

def fetch_company_news(symbol):
    to_date = datetime.now(timezone.utc).date()
    from_date = to_date - timedelta(days=3)
    url = f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={from_date}&to={to_date}&token={API_KEY}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json() or []

def fetch_candles(symbol, days=30):
    to_ts = int(datetime.now(timezone.utc).timestamp())
    from_ts = to_ts - days * 86400
    url = f"https://finnhub.io/api/v1/stock/candle?symbol={symbol}&resolution=D&from={from_ts}&to={to_ts}&token={API_KEY}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("s") != "ok" or not data.get("c"):
        return None
    return data

def analyze_symbol(symbol, quote):
    c, pc, h, l = quote.get("c"), quote.get("pc"), quote.get("h"), quote.get("l")
    if not c or not pc:
        return None

    change_pct = ((c - pc) / pc) * 100
    day_range = h - l if (h and l and h > l) else c * 0.01
    range_pos = 0.5 if day_range == 0 else (c - l) / day_range

    raw_score = 50 + (change_pct * 3) + ((range_pos - 0.5) * 40)
    buy_pct = max(2, min(98, round(raw_score)))

    entry = round(c, 2)
    stop_loss = round(c - day_range * 0.5, 2)
    target_1 = round(c + day_range * 0.75, 2)
    target_2 = round(c + day_range * 1.5, 2)

    risk = entry - stop_loss
    reward = target_1 - entry
    risk_reward = round(reward / risk, 2) if risk > 0 else None

    return {
        "symbol": symbol,
        "price": entry,
        "change_pct": round(change_pct, 2),
        "buy_pct": buy_pct,
        "sell_pct": 100 - buy_pct,
        "entry": entry,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "risk_reward": risk_reward,
        "final_score": buy_pct,
        "pe_ratio": None,
        "news_label": None,
        "rsi": None,
        "trend_label": None,
        "volume_label": None,
        "target_basis": "tagesspanne",
    }

def compute_fundamental_score(metric, price):
    pe = metric.get("peBasicExclExtraTTM")
    high52 = metric.get("52WeekHigh")
    low52 = metric.get("52WeekLow")

    if pe is not None and pe > 0:
        ideal = 20
        pe_component = max(0, 100 - abs(pe - ideal) * 2)
    else:
        pe_component = 50

    if high52 and low52 and high52 > low52:
        position = (price - low52) / (high52 - low52)
        position_component = max(0, 100 - abs(position - 0.6) * 150)
    else:
        position_component = 50

    return round((pe_component + position_component) / 2), pe

def analyze_news_sentiment(news_items):
    pos = neg = 0
    for item in news_items[:20]:
        text = (str(item.get("headline", "")) + " " + str(item.get("summary", ""))).lower()
        pos += sum(1 for w in POSITIVE_WORDS if w in text)
        neg += sum(1 for w in NEGATIVE_WORDS if w in text)
    if pos + neg == 0:
        return 50, "neutral"
    score = max(0, min(100, 50 + (pos - neg) * 8))
    label = "positiv" if score > 60 else "negativ" if score < 40 else "neutral"
    return score, label

def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def compute_atr(candles, period=ATR_PERIOD):
    if not candles:
        return None
    highs = candles.get("h", [])
    lows = candles.get("l", [])
    if len(highs) < period or len(lows) < period:
        return None
    ranges = [highs[i] - lows[i] for i in range(-period, 0) if highs[i] and lows[i] and highs[i] > lows[i]]
    if not ranges:
        return None
    return sum(ranges) / len(ranges)

def compute_momentum_quality(candles, current_price):
    trend_score, rsi_value, volume_score = 50, None, 50
    trend_label, volume_label = "unbekannt", "unbekannt"

    if candles:
        closes = candles.get("c", [])
        volumes = candles.get("v", [])

        if len(closes) >= 5:
            sma5 = sum(closes[-5:]) / 5
            deviation = (current_price - sma5) / sma5 if sma5 else 0
            trend_score = max(0, min(100, 50 + deviation * 500))
            trend_label = "aufwärts" if current_price > sma5 else "abwärts"

        rsi_value = compute_rsi(closes)
        if rsi_value is not None:
            rsi_component = max(0, 100 - abs(rsi_value - 55) * 2)
        else:
            rsi_component = 50

        if len(volumes) >= 6:
            today_vol = volumes[-1]
            avg_vol = sum(volumes[-6:-1]) / 5
            if avg_vol > 0:
                ratio = today_vol / avg_vol
                volume_score = max(0, min(100, 50 + (ratio - 1) * 50))
                volume_label = "hoch" if ratio > 1.2 else "niedrig" if ratio < 0.8 else "normal"

        momentum_quality = round((trend_score + rsi_component + volume_score) / 3)
    else:
        momentum_quality = 50

    return momentum_quality, rsi_value, trend_label, volume_label

def recompute_levels_with_atr(candidate, atr):
    entry = candidate["entry"]
    stop_loss = round(entry - atr * 1.0, 2)
    target_1 = round(entry + atr * 1.5, 2)
    target_2 = round(entry + atr * 2.5, 2)

    risk = entry - stop_loss
    reward = target_1 - entry
    risk_reward = round(reward / risk, 2) if risk > 0 else None

    candidate["stop_loss"] = stop_loss
    candidate["target_1"] = target_1
    candidate["target_2"] = target_2
    candidate["risk_reward"] = risk_reward
    candidate["target_basis"] = "10-tage-volatilitaet"

def deep_dive(candidate):
    symbol = candidate["symbol"]
    fundamental_score = 50
    news_score = 50
    news_label = "neutral"
    pe = None

    try:
        metric = fetch_metric(symbol)
        fundamental_score, pe = compute_fundamental_score(metric, candidate["price"])
    except Exception as e:
        print(f"Fundamentaldaten-Fehler bei {symbol}: {e}")
    time.sleep(REQUEST_DELAY)

    try:
        news = fetch_company_news(symbol)
        news_score, news_label = analyze_news_sentiment(news)
    except Exception as e:
        print(f"News-Fehler bei {symbol}: {e}")
    time.sleep(REQUEST_DELAY)

    momentum_quality = 50
    rsi_value = None
    trend_label = "unbekannt"
    volume_label = "unbekannt"
    candles = None
    try:
        candles = fetch_candles(symbol)
        momentum_quality, rsi_value, trend_label, volume_label = compute_momentum_quality(candles, candidate["price"])
        if candles is None:
            print(f"Keine Candle-Daten für {symbol} verfügbar (evtl. Free-Tier-Limit) — nutze neutrale Werte.")
    except Exception as e:
        print(f"Momentum-Fehler bei {symbol}: {e}")
    time.sleep(REQUEST_DELAY)

    atr = compute_atr(candles)
    if atr and atr > 0:
        recompute_levels_with_atr(candidate, atr)
    else:
        print(f"Kein ATR für {symbol} berechenbar — behalte tagesspannen-basierte Ziele.")

    final_score = round(
        0.35 * candidate["buy_pct"] +
        0.20 * momentum_quality +
        0.25 * fundamental_score +
        0.20 * news_score
    )

    candidate["pe_ratio"] = pe
    candidate["fundamental_score"] = fundamental_score
    candidate["news_score"] = news_score
    candidate["news_label"] = news_label
    candidate["momentum_quality"] = momentum_quality
    candidate["rsi"] = rsi_value
    candidate["trend_label"] = trend_label
    candidate["volume_label"] = volume_label
    candidate["final_score"] = final_score
    return candidate

def send_notification(pick):
    if not NTFY_TOPIC:
        print("Kein NTFY_TOPIC gesetzt, überspringe Benachrichtigung.")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=(f"{pick['symbol']} bei ${pick['entry']} — Gesamt-Score {pick['final_score']}%. "
                  f"KGV: {pick.get('pe_ratio') or '—'}, News: {pick.get('news_label') or '—'}, "
                  f"Trend: {pick.get('trend_label') or '—'}. "
                  f"Stop: ${pick['stop_loss']}, Ziel 1: ${pick['target_1']}").encode("utf-8"),
            headers={
                "Title": f"Neues Kaufsignal: {pick['symbol']}",
                "Priority": "high",
                "Tags": "chart_with_upwards_trend"
            },
            timeout=10
        )
        print(f"Benachrichtigung für {pick['symbol']} gesendet.")
    except Exception as e:
        print(f"Notification fehlgeschlagen: {e}")

def load_rec_log():
    if not os.path.exists(REC_LOG_PATH):
        return []
    with open(REC_LOG_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        if "pnl_pct" not in row:
            row["pnl_pct"] = ""
        if "invested_eur" not in row:
            row["invested_eur"] = ""
    return rows

def save_rec_log(rows):
    with open(REC_LOG_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REC_LOG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

def load_portfolio():
    default = {
        "cash": STARTING_BALANCE,
        "starting_balance": STARTING_BALANCE,
        "updated": None,
        "closed_trades": [],
    }
    if not os.path.exists(PORTFOLIO_PATH):
        return default
    with open(PORTFOLIO_PATH) as f:
        data = json.load(f)
    data.setdefault("closed_trades", [])
    data.setdefault("cash", STARTING_BALANCE)
    data.setdefault("starting_balance", STARTING_BALANCE)
    return data

def save_portfolio(portfolio):
    with open(PORTFOLIO_PATH, "w") as f:
        json.dump(portfolio, f, indent=2)

def resolve_open_recommendations(rows, now, portfolio):
    open_rows = [r for r in rows if r["status"] == "open"]
    for row in open_rows:
        try:
            quote = fetch_quote(row["symbol"])
        except Exception as e:
            print(f"Konnte {row['symbol']} für Tracking nicht abfragen: {e}")
            continue
        time.sleep(REQUEST_DELAY)

        price = quote.get("c")
        if not price:
            continue

        entry = float(row["entry"])
        stop_loss = float(row["stop_loss"])
        target_1 = float(row["target_1"])
        target_2 = float(row["target_2"])
        opened_at = datetime.fromisoformat(row["opened_at"])

        if price >= target_2:
            row["status"] = "ziel_2_erreicht"
        elif price >= target_1:
            row["status"] = "ziel_1_erreicht"
        elif price <= stop_loss:
            row["status"] = "stop_loss_ausgeloest"
        elif now - opened_at > timedelta(days=EXPIRY_DAYS):
            row["status"] = "abgelaufen_gewinn" if price > entry else "abgelaufen_verlust"
        else:
            continue

        row["resolved_at"] = now.isoformat()
        row["resolved_price"] = price
        row["pnl_pct"] = round((price - entry) / entry * 100, 2)
        print(f"{row['symbol']} abgeschlossen: {row['status']} bei ${price} ({row['pnl_pct']:+}%)")

        invested = float(row.get("invested_eur") or 0)
        if invested > 0:
            gross_pnl_eur = invested * (row["pnl_pct"] / 100)
            net_pnl_eur = round(gross_pnl_eur - 2 * TRADE_FEE, 2)
            portfolio["cash"] = round(portfolio["cash"] + invested + net_pnl_eur, 2)
            portfolio["closed_trades"].append({
                "symbol": row["symbol"],
                "opened_at": row["opened_at"],
                "resolved_at": row["resolved_at"],
                "invested_eur": invested,
                "net_pnl_eur": net_pnl_eur,
                "status": row["status"],
            })
            print(f"Portfolio: {row['symbol']} → {net_pnl_eur:+.2f}€ (netto, inkl. Gebühren)")

    return rows

def add_new_recommendations(rows, top_picks, timestamp, portfolio):
    open_symbols = {r["symbol"] for r in rows if r["status"] == "open"}
    open_count = len(open_symbols)
    slice_size = portfolio["starting_balance"] / MAX_CONCURRENT_POSITIONS

    for pick in top_picks:
        if pick["symbol"] in open_symbols:
            continue

        invest_amount = 0.0
        if open_count < MAX_CONCURRENT_POSITIONS:
            candidate_amount = min(portfolio["cash"], slice_size)
            if candidate_amount >= MIN_TRADE_EUR:
                invest_amount = round(candidate_amount, 2)
                portfolio["cash"] = round(portfolio["cash"] - invest_amount, 2)
                open_count += 1

        rows.append({
            "id": f"{pick['symbol']}_{timestamp}",
            "symbol": pick["symbol"],
            "opened_at": timestamp,
            "entry": pick["entry"],
            "stop_loss": pick["stop_loss"],
            "target_1": pick["target_1"],
            "target_2": pick["target_2"],
            "status": "open",
            "resolved_at": "",
            "resolved_price": "",
            "pnl_pct": "",
            "invested_eur": invest_amount if invest_amount > 0 else "",
        })
        if invest_amount > 0:
            print(f"Papier-Trade eröffnet: {pick['symbol']} mit {invest_amount}€")
    return rows

def compute_track_record(rows, timestamp):
    closed = [r for r in rows if r["status"] != "open"]
    wins = [r for r in closed if r["status"] in ("ziel_1_erreicht", "ziel_2_erreicht", "abgelaufen_gewinn")]
    losses = [r for r in closed if r["status"] in ("stop_loss_ausgeloest", "abgelaufen_verlust")]
    still_open = [r for r in rows if r["status"] == "open"]

    win_rate = round(len(wins) / len(closed) * 100) if closed else None

    def avg_pnl(items):
        vals = [float(r["pnl_pct"]) for r in items if r.get("pnl_pct") not in ("", None)]
        return round(sum(vals) / len(vals), 2) if vals else None

    avg_win = avg_pnl(wins)
    avg_loss = avg_pnl(losses)

    expectancy = None
    if win_rate is not None and avg_win is not None and avg_loss is not None:
        p_win = win_rate / 100
        expectancy = round(p_win * avg_win + (1 - p_win) * avg_loss, 2)
    total_pnl = None
    all_pnls = [float(r["pnl_pct"]) for r in closed if r.get("pnl_pct") not in ("", None)]
    if all_pnls:
        total_pnl = round(sum(all_pnls), 2)

    return {
        "updated": timestamp,
        "total_closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "total_pnl": total_pnl,
        "still_open": len(still_open),
        "recent": sorted(closed, key=lambda r: r["resolved_at"], reverse=True)[:10],
    }

def compute_portfolio_summary(portfolio, rows, timestamp):
    open_rows = [r for r in rows if r["status"] == "open" and float(r.get("invested_eur") or 0) > 0]
    invested_in_open = sum(float(r["invested_eur"]) for r in open_rows)
    total_value = round(portfolio["cash"] + invested_in_open, 2)
    return_pct = round((total_value - portfolio["starting_balance"]) / portfolio["starting_balance"] * 100, 2)

    return {
        "updated": timestamp,
        "starting_balance": portfolio["starting_balance"],
        "cash": round(portfolio["cash"], 2),
        "invested_in_open_positions": round(invested_in_open, 2),
        "total_value": total_value,
        "return_pct": return_pct,
        "open_position_count": len(open_rows),
        "recent_closed_trades": sorted(portfolio["closed_trades"], key=lambda t: t["resolved_at"], reverse=True)[:10],
    }

def main():
    os.makedirs("data", exist_ok=True)
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()
    results = []
    watchlist = load_watchlist()

    rec_rows = load_rec_log()
    portfolio = load_portfolio()

    rec_rows = resolve_open_recommendations(rec_rows, now, portfolio)

    for i, symbol in enumerate(watchlist):
        try:
            quote = fetch_quote(symbol)
            analyzed = analyze_symbol(symbol, quote)
            if analyzed:
                analyzed["timestamp"] = timestamp
                results.append(analyzed)
        except Exception as e:
            print(f"Fehler bei {symbol}: {e}")
        if i < len(watchlist) - 1:
            time.sleep(REQUEST_DELAY)

    results.sort(key=lambda r: r["buy_pct"], reverse=True)

    top_candidates = results[:DEEP_DIVE_COUNT]
    for candidate in top_candidates:
        deep_dive(candidate)

    results.sort(key=lambda r: r["final_score"], reverse=True)

    with open("data/latest.json", "w") as f:
        json.dump(results, f, indent=2)

    history_path = "data/history.csv"
    file_exists = os.path.exists(history_path)
    with open(history_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "symbol", "price", "change_pct", "buy_pct", "sell_pct",
            "entry", "stop_loss", "target_1", "target_2", "risk_reward",
            "final_score", "pe_ratio", "news_label"
        ], extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(results)

    top_picks = [r for r in results if r["final_score"] >= 60][:3]
    with open("data/recommendation.json", "w") as f:
        json.dump({"timestamp": timestamp, "picks": top_picks}, f, indent=2)

    rec_rows = add_new_recommendations(rec_rows, top_picks, timestamp, portfolio)
    save_rec_log(rec_rows)

    portfolio["updated"] = timestamp
    save_portfolio(portfolio)

    track_record = compute_track_record(rec_rows, timestamp)
    with open("data/track_record.json", "w") as f:
        json.dump(track_record, f, indent=2)

    portfolio_summary = compute_portfolio_summary(portfolio, rec_rows, timestamp)
    with open("data/portfolio_summary.json", "w") as f:
        json.dump(portfolio_summary, f, indent=2)

    state_path = "data/state.json"
    last_symbol = None
    if os.path.exists(state_path):
        with open(state_path) as f:
            last_symbol = json.load(f).get("last_top_symbol")

    if top_picks:
        current_top = top_picks[0]["symbol"]
        if current_top != last_symbol:
            send_notification(top_picks[0])
        with open(state_path, "w") as f:
            json.dump({"last_top_symbol": current_top}, f)

    print(f"{len(results)} von {len(watchlist)} Aktien gescreent, {len(top_candidates)} vertieft, {len(top_picks)} Empfehlung(en).")
    print(f"Trefferquote: {track_record['win_rate']}%, Erwartungswert: {track_record['expectancy']}%")
    print(f"Portfolio: {portfolio_summary['total_value']}€ ({portfolio_summary['return_pct']:+}%), Cash: {portfolio_summary['cash']}€")

if __name__ == "__main__":
    main()
