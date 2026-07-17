import os
import json
import csv
import time
from datetime import datetime, timezone

import requests

API_KEY = os.environ["FINNHUB_API_KEY"]
SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
REQUEST_DELAY = 1.05

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
    }
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

def send_notification(pick):
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=f"{pick['symbol']} bei ${pick['entry']} — Signalstärke {pick['buy_pct']}%. Stop: ${pick['stop_loss']}, Ziel 1: ${pick['target_1']}".encode("utf-8"),
            headers={
                "Title": f"Neues Kaufsignal: {pick['symbol']}",
                "Priority": "high",
                "Tags": "chart_with_upwards_trend"
            },
            timeout=10
        )
    except Exception as e:
        print(f"Notification fehlgeschlagen: {e}")

def main():
    os.makedirs("data", exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    results = []
    watchlist = load_watchlist()

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

    # Vollständige Liste (für Historie/spätere Auswertung)
    with open("data/latest.json", "w") as f:
        json.dump(results, f, indent=2)

    history_path = "data/history.csv"
    file_exists = os.path.exists(history_path)
    with open(history_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "symbol", "price", "change_pct", "buy_pct", "sell_pct",
            "entry", "stop_loss", "target_1", "target_2", "risk_reward"
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerows(results)

    # Nur die Top-3-Empfehlungen fürs Dashboard, mit Mindest-Schwelle
    top_picks = [r for r in results if r["buy_pct"] >= 60][:3]
    with open("data/recommendation.json", "w") as f:
        json.dump({"timestamp": timestamp, "picks": top_picks}, f, indent=2)

    print(f"{len(results)} von {len(watchlist)} Aktien analysiert. {len(top_picks)} Empfehlung(en) erstellt.")

if __name__ == "__main__":
    main()
