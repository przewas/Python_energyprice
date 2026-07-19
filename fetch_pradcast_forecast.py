"""Jeden skrypt: prognoza D+1 i D+2 z pradcast.pl → zapis do t_rdn.

- pobiera prognozę (24h PLN/MWh) dla D+1 oraz D+2
- każdą godzinę zapisuje jako 4 kwadranse (np. 20 PLN @ 08:00 →
  08:00, 08:15, 08:30, 08:45) = 96 rekordów / dobę
- UPSERT do t_rdn z is_forecast=1
- nie nadpisuje cen realnych (is_forecast=0)

Użycie (na serwerze, katalog z pradcast_client.py):
  python3 fetch_pradcast_forecast.py
  python3 fetch_pradcast_forecast.py --dry-run
  python3 fetch_pradcast_forecast.py --force   # nadpisz też wiersze is_forecast=0
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import mysql.connector

from pradcast_client import (
    PradcastAPIError,
    fetch_forecast_for_date,
    hourly_prices_to_rdn_records,
)

DB_CONFIG = {
    "host": "przewas.mysql.pythonanywhere-services.com",
    "user": "przewas",
    "password": "Ka$zanka77",
    "database": "przewas$scadaPv",
    "autocommit": False,
}

FORECAST_FLAG_COLUMN = "is_forecast"


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    skipped_real: int = 0
    total: int = 0


def _czas_to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, timedelta):
        total = int(value.total_seconds()) % (24 * 3600)
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M:%S")
    text = str(value)
    if "." in text:
        text = text.split(".", 1)[0]
    return text[:8] if len(text) >= 8 else text


def _table_columns(cursor) -> set[str]:
    cursor.execute("SHOW COLUMNS FROM t_rdn")
    cols: set[str] = set()
    for row in cursor.fetchall():
        cols.add(row["Field"] if isinstance(row, dict) else row[0])
    return cols


def _ensure_forecast_flag(cursor, columns: set[str]) -> bool:
    """
    Dodaje is_forecast. Istniejące wiersze dostają DEFAULT 0 (traktowane jak realne).
    To jest zamierzone: jeśli D+1 już ma FIXING z maila, prognoza ich nie nadpisze.
    """
    if FORECAST_FLAG_COLUMN in columns:
        return False
    cursor.execute(
        f"""
        ALTER TABLE t_rdn
        ADD COLUMN {FORECAST_FLAG_COLUMN} TINYINT(1) NOT NULL DEFAULT 0
        COMMENT '1=prognoza, 0=cena realna'
        """
    )
    columns.add(FORECAST_FLAG_COLUMN)
    return True


def _load_existing_flags(cursor, doba: str) -> dict[str, int]:
    cursor.execute(
        f"SELECT czas, {FORECAST_FLAG_COLUMN} AS is_forecast FROM t_rdn WHERE doba = %s",
        (doba,),
    )
    existing: dict[str, int] = {}
    for row in cursor.fetchall():
        if isinstance(row, dict):
            existing[_czas_to_str(row["czas"])] = int(row["is_forecast"] or 0)
        else:
            existing[_czas_to_str(row[0])] = int(row[1] or 0)
    return existing


def _row_values(rec: dict[str, Any], columns: set[str]) -> dict[str, Any]:
    values = {
        "czas_ceny": rec["czas_ceny"],
        "doba": rec["doba"],
        "czas": rec["czas"],
        "czas_do": rec.get("czas_do"),
        "interwal_minut": int(rec.get("interwal_minut", 15)),
        "cena1": int(rec["cena1"]),
        "cena2": int(rec.get("cena2", 0)),
        "wolumen1": int(rec.get("wolumen1", 0)),
        "wolumen2": int(rec.get("wolumen2", 0)),
        FORECAST_FLAG_COLUMN: 1,
    }
    return {key: val for key, val in values.items() if key in columns}


def upsert_forecast_records(
    records: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    force: bool = False,
) -> UpsertStats:
    """INSERT/UPDATE prognoz; domyślnie nie nadpisuje is_forecast=0 (chyba że force)."""
    if not records:
        return UpsertStats()

    doba = str(records[0]["doba"])
    stats = UpsertStats(total=len(records))
    conn = mysql.connector.connect(**DB_CONFIG)

    try:
        cursor = conn.cursor(dictionary=True)
        columns = _table_columns(cursor)
        if _ensure_forecast_flag(cursor, columns):
            print(f"Dodano kolumnę {FORECAST_FLAG_COLUMN} do t_rdn (DEFAULT 0 = realne)")

        required = {"doba", "czas", "cena1", FORECAST_FLAG_COLUMN}
        missing = required - columns
        if missing:
            raise RuntimeError(f"t_rdn: brak wymaganych kolumn: {sorted(missing)}")

        existing = _load_existing_flags(cursor, doba)

        for rec in records:
            czas = str(rec["czas"])
            flag = existing.get(czas)
            row = _row_values(rec, columns)

            if flag == 0 and not force:
                stats.skipped_real += 1
                continue

            if flag is None:
                cols = list(row.keys())
                placeholders = ", ".join(["%s"] * len(cols))
                sql = f"INSERT INTO t_rdn ({', '.join(cols)}) VALUES ({placeholders})"
                if not dry_run:
                    cursor.execute(sql, [row[c] for c in cols])
                stats.inserted += 1
            else:
                # flag == 1, albo force na realnych
                update_cols = [c for c in row.keys() if c not in ("doba", "czas")]
                set_sql = ", ".join(f"{c} = %s" for c in update_cols)
                if force:
                    sql = f"UPDATE t_rdn SET {set_sql} WHERE doba = %s AND czas = %s"
                else:
                    sql = (
                        f"UPDATE t_rdn SET {set_sql} "
                        f"WHERE doba = %s AND czas = %s AND {FORECAST_FLAG_COLUMN} = 1"
                    )
                if not dry_run:
                    cursor.execute(sql, [row[c] for c in update_cols] + [doba, czas])
                stats.updated += 1

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        cursor.close()
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _import_horizon(label: str, target_date: str, *, dry_run: bool, force: bool) -> int:
    try:
        forecast = fetch_forecast_for_date(target_date)
    except PradcastAPIError as exc:
        print(f"Błąd pradcast.pl ({label} {target_date}): {exc}", file=sys.stderr)
        return 1

    prices = forecast.get("prices") or []
    if not prices:
        print(f"Brak cen w odpowiedzi ({label}).", file=sys.stderr)
        return 1

    records = hourly_prices_to_rdn_records(forecast)
    values = [float(p["price"]) for p in prices]

    print(f"\n=== {label}  date={forecast.get('date')}  source={forecast.get('source')} ===")
    print(f"godzin: {len(prices)}  min={min(values):.2f}  max={max(values):.2f}  PLN/MWh")
    print(f"rekordów 15-min: {len(records)}  (każda godzina → 4 kwadranse)")

    # pokaż przykładowe rozbicie jednej godziny
    samples = [r for r in records if r["czas"].startswith("08:")][:4]
    if samples:
        print("przykład rozbicia godz. 08:")
        for rec in samples:
            print(
                f"  {rec['czas_ceny']}  {rec['cena1']} gr/MWh  "
                f"({rec['cena1'] / 100:.2f} PLN/MWh)"
            )

    for entry in sorted(prices, key=lambda item: int(item["hour"])):
        hour = int(entry["hour"])
        sample = next(r for r in records if r["czas"].startswith(f"{hour:02d}:00"))
        print(
            f"  {sample['czas_ceny']} (+ :15/:30/:45)  "
            f"{float(entry['price']):8.2f} PLN/MWh  "
            f"({int(sample['cena1'])} gr)  {entry.get('level', '')}"
        )

    try:
        stats = upsert_forecast_records(records, dry_run=dry_run, force=force)
    except Exception as exc:
        print(f"Błąd zapisu do t_rdn ({label}): {exc}", file=sys.stderr)
        return 1

    mode = "DRY-RUN" if dry_run else "ZAPIS"
    if force:
        mode += "+FORCE"
    print(
        f"[{mode}] t_rdn doba={forecast.get('date')} "
        f"inserted={stats.inserted} updated={stats.updated} "
        f"skipped_real={stats.skipped_real} total={stats.total}"
    )
    if stats.skipped_real and stats.inserted == 0 and stats.updated == 0:
        print(
            f"  → wszystkie sloty {forecast.get('date')} mają is_forecast=0 — pominięte.\n"
            "    Nadpisanie: python3 fetch_pradcast_forecast.py --force"
        )
    return 0


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    now = datetime.now()
    d1 = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    d2 = (now + timedelta(days=2)).strftime("%Y-%m-%d")

    print(f"as_of: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print("horyzonty: D+1, D+2  (zapis kwadransowy do t_rdn)")

    rc = 0
    rc |= _import_horizon("D+1", d1, dry_run=dry_run, force=force)
    rc |= _import_horizon("D+2", d2, dry_run=dry_run, force=force)
    return rc


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
