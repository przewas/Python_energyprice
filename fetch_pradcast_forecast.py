"""Pobranie prognozy D+1 z pradcast.pl i zapis do t_rdn (z flagą is_forecast=1).

Nie nadpisuje rekordów z ceną realną (is_forecast=0).

Użycie:
  python3 fetch_pradcast_forecast.py
  python3 fetch_pradcast_forecast.py --dry-run
  python3 fetch_pradcast_forecast.py --json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

from pradcast_client import (
    PradcastAPIError,
    fetch_forecast_for_date,
    hourly_prices_to_rdn_records,
)
from rdn_store import upsert_forecast_records


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    d1 = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        forecast = fetch_forecast_for_date(d1)
    except PradcastAPIError as exc:
        print(f"Błąd pradcast.pl: {exc}", file=sys.stderr)
        return 1

    prices = forecast.get("prices") or []
    if not prices:
        print("Brak cen w odpowiedzi.", file=sys.stderr)
        return 1

    values = [float(p["price"]) for p in prices]
    records = hourly_prices_to_rdn_records(forecast)

    print(f"as_of: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("horizon: D+1")
    print(f"date: {forecast.get('date')}  source={forecast.get('source')}")
    print(f"godzin: {len(prices)}  min={min(values):.2f}  max={max(values):.2f}  PLN/MWh")
    print(f"rekordów 15-min do t_rdn: {len(records)}")

    for entry in sorted(prices, key=lambda item: int(item["hour"])):
        hour = int(entry["hour"])
        # pokaż też datetime pierwszego kwadransu godziny
        sample = next(r for r in records if r["czas"].startswith(f"{hour:02d}:"))
        print(
            f"  {sample['czas_ceny']}  "
            f"{float(entry['price']):8.2f} PLN/MWh  "
            f"({int(sample['cena1'])} gr)  {entry.get('level', '')}"
        )

    if "--json" in sys.argv:
        print("\n--- JSON (pierwsze 4 rekordy DB) ---")
        print(json.dumps(records[:4], indent=2, ensure_ascii=False))

    try:
        stats = upsert_forecast_records(records, dry_run=dry_run)
    except Exception as exc:
        print(f"Błąd zapisu do t_rdn: {exc}", file=sys.stderr)
        return 1

    mode = "DRY-RUN" if dry_run else "ZAPIS"
    print(
        f"\n[{mode}] t_rdn doba={forecast.get('date')} "
        f"inserted={stats.inserted} updated={stats.updated} "
        f"skipped_real={stats.skipped_real} total={stats.total}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
