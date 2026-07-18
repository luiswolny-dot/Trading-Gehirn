import os
import json
import csv
import re

def slugify(text):
    return re.sub(r'[^\w\-]', '_', text)

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)

def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

def main():
    os.makedirs("vault", exist_ok=True)

    latest = load_json("data/latest.json", [])
    rec_log = load_csv("data/recommendations_log.csv")
    track_record = load_json("data/track_record.json", {})

    top_analyzed = [r for r in latest if r.get("final_score") is not None][:30]

    index_lines = ["# Trading-Gehirn Übersicht", ""]
    index_lines.append(f"Trefferquote: {track_record.get('win_rate', '—')}% "
                        f"über {track_record.get('total_closed', 0)} abgeschlossene Empfehlungen.")
    index_lines.append("")
    index_lines.append("## Analysierte Aktien")

    for r in top_analyzed:
        symbol = r["symbol"]
        index_lines.append(f"- [[{symbol}]] — Score {r.get('final_score', '—')}%")

        trend = r.get("trend_label", "unbekannt")
        news = r.get("news_label", "unbekannt")
        pe = r.get("pe_ratio")

        note_lines = [
            f"# {symbol}",
            "",
            f"tags: #Aktie #S-P-500",
            "",
            f"- Preis: ${r.get('price', '—')}",
            f"- Tagesbewegung: {r.get('change_pct', '—')}%",
            f"- Gesamt-Score: {r.get('final_score', '—')}%",
            f"- KGV: {pe if pe else '—'}",
            f"- Einstieg: ${r.get('entry', '—')}",
            f"- Stop-Loss: ${r.get('stop_loss', '—')}",
            f"- Ziel 1: ${r.get('target_1', '—')}",
            f"- Ziel 2: ${r.get('target_2', '—')}",
            "",
            f"## Verknüpfungen",
            f"- Trend: [[Trend-{trend}]]",
            f"- News-Stimmung: [[News-{news}]]",
            f"- Gehört zu: [[S&P-500]]",
        ]

        history = [row for row in rec_log if row["symbol"] == symbol]
        if history:
            note_lines.append("")
            note_lines.append("## Bisherige Empfehlungen für diese Aktie")
            for h in history:
                status = h.get("status", "offen")
                pnl = h.get("pnl_pct", "")
                note_lines.append(f"- {h.get('opened_at', '')[:10]}: {status} ({pnl}%)" if pnl else f"- {h.get('opened_at', '')[:10]}: {status}")

        with open(f"vault/{slugify(symbol)}.md", "w") as f:
            f.write("\n".join(note_lines))

        # Hub-Notizen für Trend/News, damit sie als eigene Knoten im Graphen auftauchen
        for hub_name, hub_type in [(f"Trend-{trend}", "Trend"), (f"News-{news}", "News")]:
            hub_path = f"vault/{slugify(hub_name)}.md"
            if not os.path.exists(hub_path):
                with open(hub_path, "w") as f:
                    f.write(f"# {hub_name}\n\nSammlung aller Aktien mit {hub_type}-Kategorie \"{hub_name.split('-', 1)[1]}\".")

    if not os.path.exists("vault/S&P-500.md"):
        with open("vault/S&P-500.md", "w") as f:
            f.write("# S&P 500\n\nZentrale Notiz für alle analysierten S&P-500-Aktien.")

    with open("vault/index.md", "w") as f:
        f.write("\n".join(index_lines))

    print(f"{len(top_analyzed)} Aktien-Notizen im vault/-Ordner erzeugt.")

if __name__ == "__main__":
    main()
