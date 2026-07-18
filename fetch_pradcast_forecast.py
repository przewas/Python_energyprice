"""Test: pobranie prognoz cen RDN z pradcast.pl dla D+1 i D+2."""

from __future__ import annotations

import json
import sys

from pradcast_client import PradcastAPIError, fetch_forecasts_d1_d2


def _summarize_horizon(label: str, payload: dict) -> None:
    prices = payload.get("prices") or []
    values = [float(p["price"]) for p in prices]
    print(f"\n=== {label}  data={payload.get('date')}  source={payload.get('source')} ===")
    print(f"godzin: {len(prices)}  min={min(values):.2f}  max={max(values):.2f}  PLN/MWh")
    for entry in sorted(prices, key=lambda item: int(item["hour"])):
        hour = int(entry["hour"])
        print(f"  {hour:02d}:00  {float(entry['price']):8.2f}  {entry.get('level', '')}")


def main() -> int:
    try:
        bundle = fetch_forecasts_d1_d2()
    except PradcastAPIError as exc:
        print(f"Błąd pradcast.pl: {exc}", file=sys.stderr)
        return 1

    print(f"as_of: {bundle['as_of']}")
    horizons = bundle["horizons"]
    _summarize_horizon("D+1", horizons["D+1"])
    _summarize_horizon("D+2", horizons["D+2"])

    if "--json" in sys.argv:
        print("\n--- JSON ---")
        print(json.dumps(bundle, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
