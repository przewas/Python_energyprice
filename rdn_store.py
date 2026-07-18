"""Zapis cen/prognoz RDN do tabeli t_rdn (ta sama baza co ceny)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import mysql.connector

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

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped_real": self.skipped_real,
        }


def db_conn():
    return mysql.connector.connect(**DB_CONFIG)


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
    if len(text) >= 8:
        return text[:8]
    return text


def _table_columns(cursor) -> set[str]:
    cursor.execute("SHOW COLUMNS FROM t_rdn")
    cols: set[str] = set()
    for row in cursor.fetchall():
        if isinstance(row, dict):
            cols.add(row["Field"])
        else:
            cols.add(row[0])
    return cols


def ensure_forecast_flag_column(cursor, columns: set[str]) -> bool:
    """Dodaje kolumnę is_forecast jeśli jej brak. Zwraca True gdy właśnie dodano."""
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
            czas = _czas_to_str(row["czas"])
            flag = int(row["is_forecast"] or 0)
        else:
            czas = _czas_to_str(row[0])
            flag = int(row[1] or 0)
        existing[czas] = flag
    return existing


def _row_values(rec: dict[str, Any], columns: set[str]) -> dict[str, Any]:
    """Mapuje rekord prognozy na kolumny istniejące w t_rdn."""
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
) -> UpsertStats:
    """
    Zapisuje prognozy do t_rdn.

    - nowy wiersz → INSERT z is_forecast=1
    - istniejący z is_forecast=1 → UPDATE ceny/czasów
    - istniejący z is_forecast=0 (cena realna) → pomija (nie nadpisuje)
    """
    if not records:
        return UpsertStats()

    doba = str(records[0]["doba"])
    stats = UpsertStats(total=len(records))

    conn = db_conn()
    try:
        cursor = conn.cursor(dictionary=True)
        columns = _table_columns(cursor)
        created = ensure_forecast_flag_column(cursor, columns)
        if created:
            print(f"Dodano kolumnę {FORECAST_FLAG_COLUMN} do t_rdn")

        required = {"doba", "czas", "cena1", FORECAST_FLAG_COLUMN}
        missing = required - columns
        if missing:
            raise RuntimeError(f"t_rdn: brak wymaganych kolumn: {sorted(missing)}")

        existing = _load_existing_flags(cursor, doba)

        for rec in records:
            czas = str(rec["czas"])
            flag = existing.get(czas)
            row = _row_values(rec, columns)

            if flag == 0:
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
                # flag == 1 → wolno nadpisać prognozę świeższą prognozą
                update_cols = [c for c in row.keys() if c not in ("doba", "czas")]
                set_sql = ", ".join(f"{c} = %s" for c in update_cols)
                sql = (
                    f"UPDATE t_rdn SET {set_sql} "
                    f"WHERE doba = %s AND czas = %s AND {FORECAST_FLAG_COLUMN} = 1"
                )
                if not dry_run:
                    cursor.execute(
                        sql,
                        [row[c] for c in update_cols] + [doba, czas],
                    )
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
