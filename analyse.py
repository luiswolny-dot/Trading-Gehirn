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

POSITIVE_WORDS = {"beat","beats","surge","soar","rally","upgrade","outperform","record",
                   "growth","strong","gain","gains","jump","rise","rises","bullish","buy",
                   "raises","raised","exceed","exceeds","soars","surges"}
NEGATIVE_WORDS = {"miss","misses","plunge","crash","downgrade","underperform","weak","loss",
                   "losses","fall","falls","drop","drops","bearish","sell","cuts","cut",
                   "lawsuit","fraud","investigation","recall","decline","plunges"}

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

    final_score = round(0.5 * candidate["buy_pct"] + 0.3 * fundamental_score + 0.2 * news_score)

    candidate["pe_ratio"] = pe
    candidate["fundamental_score"] = fundamental_score
    candidate["news_score"] = news_score
    candidate["news_label"] = news_label
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
                  f"KGV: {pick.get('pe_ratio') or '—'}, News: {pick.get('news_label') or '—'}. "
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

def main():
    os.makedirs("data", exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    results = []
    watchlist = load_watchlist()

    # Stufe 1: breiter, schneller Scan
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

    # Stufe 2: Tiefenanalyse der vielversprechendsten Kandidaten
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

    print(f"{len(results)} von {len(watchlist)} Aktien gescreent, {len(top_candidates)} vertieft analysiert, {len(top_picks)} Empfehlung(en).")

if __name__ == "__main__":
    main()
