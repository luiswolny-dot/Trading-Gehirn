import os
import json
import csv
from datetime import datetime, timezone

import requests

API_KEY = os.environ["FINNHUB_API_KEY"]

def load_watchlist():
    with open("watchlist.txt") as f:
        return [line.strip().upper() for line in f if line.strip()]

def fetch_quote(symbol):
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={API_KEY}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()

def score(quote):
    c, pc, h, l = quote.get("c"), quote.get("pc"), quote.get("h"), quote.get("l")
    if not c or not pc:
        return None
    change_pct = ((c - pc) / pc) * 100
    range_pos = 0.5 if h == l else (c - l) / (h - l)
    raw = 50 + (change_pct * 3) + ((range_pos - 0.5) * 40)
    return max(2, min(98, round(raw)))

def main():
    os.makedirs("data", exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    results = []

    for symbol in load_watchlist():
        try:
            quote = fetch_quote(symbol)
            buy_pct = score(quote)
            if buy_pct is None:
                continue
            row = {
                "timestamp": timestamp,
                "symbol": symbol,
                "price": quote.get("c"),
                "change_pct": round(((quote["c"] - quote["pc"]) / quote["pc"]) * 100, 2),
                "buy_pct": buy_pct,
                "sell_pct": 100 - buy_pct,
            }
            results.append(row)
        except Exception as e:
            print(f"Fehler bei {symbol}: {e}")

    # Aktueller Snapshot
    with open("data/latest.json", "w") as f:
        json.dump(results, f, indent=2)

    # Historie anhängen
    history_path = "data/history.csv"
    file_exists = os.path.exists(history_path)
    with open(history_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "symbol", "price", "change_pct", "buy_pct", "sell_pct"])
        if not file_exists:
            writer.writeheader()
        writer.writerows(results)

    print(f"{len(results)} Analysen gespeichert.")

if __name__ == "__main__":
    main()
