"""Test: pobranie prognozy cen RDN z pradcast.pl dla D+1.

Działa ze starym pradcast_client.py (używa fetch_forecast_for_date).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

from pradcast_client import PradcastAPIError, fetch_forecast_for_date


def main() -> int:
    # D+1 = jutro
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

    print(f"as_of: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("horizon: D+1")
    print(f"date: {forecast.get('date')}  source={forecast.get('source')}")
    print(f"godzin: {len(prices)}  min={min(values):.2f}  max={max(values):.2f}  PLN/MWh")
    for entry in sorted(prices, key=lambda item: int(item["hour"])):
        hour = int(entry["hour"])
        print(f"  {hour:02d}:00  {float(entry['price']):8.2f}  {entry.get('level', '')}")

    if "--json" in sys.argv:
        print("\n--- JSON ---")
        print(json.dumps(forecast, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
