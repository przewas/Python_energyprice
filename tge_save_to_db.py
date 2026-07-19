"""Import realnych cen TGE (z tge.html) do t_rdn.

- cena1/cena2 jako int w groszach / MWh
- rozbicie godzin na 96 kwadransów (jak prognoza / FIXING z maila)
- UPSERT z is_forecast=0 — nadpisuje prognozę
- czas_ceny = data+godzina slotu (nie NOW())
"""

from __future__ import annotations

import os
import re
from datetime import datetime, time, timedelta
from typing import Any

import mysql.connector
from bs4 import BeautifulSoup

APP_DIR = os.environ.get("PV_APP_DIR", os.path.dirname(os.path.abspath(__file__)))
HTML_FILE = os.path.join(APP_DIR, "tge.html")

DB_CONFIG = {
    "host": "przewas.mysql.pythonanywhere-services.com",
    "user": "przewas",
    "password": "Ka$zanka77",
    "database": "przewas$scadaPv",
    "autocommit": False,
}

FORECAST_FLAG_COLUMN = "is_forecast"


def _parse_number(raw: str) -> float | None:
    text = (raw or "").strip().replace("\xa0", " ").replace(" ", "").replace(",", ".")
    if not text or text in {"-", "–", "—", "n/a", "N/A"}:
        return None
    return float(text)


def _to_grosze(raw: str) -> int:
    value = _parse_number(raw)
    if value is None:
        return 0
    return int(round(value * 100))


def _to_int_volume(raw: str) -> int:
    value = _parse_number(raw)
    if value is None:
        return 0
    return int(round(value))


def _parse_delivery_date(soup: BeautifulSoup) -> str:
    kontrakt_h4 = soup.find("h4", class_="kontrakt-date")
    if kontrakt_h4:
        small = kontrakt_h4.find("small")
        if small:
            m = re.search(r"(\d{2})-(\d{2})-(\d{4})", small.get_text())
            if m:
                return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return datetime.now().strftime("%Y-%m-%d")


def _parse_hourly_rows(soup: BeautifulSoup) -> list[dict[str, Any]]:
    godziny: list[dict[str, Any]] = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if not tds or len(tds) < 5:
            continue

        czas = tds[0].get_text(strip=True)
        if not re.match(r"^\d{1,2}-\d{1,2}$", czas):
            continue

        godzina_start = int(czas.split("-")[0])
        if godzina_start < 0 or godzina_start > 23:
            continue

        godziny.append(
            {
                "hour": godzina_start,
                "cena1": _to_grosze(tds[1].get_text()),
                "wolumen1": _to_int_volume(tds[2].get_text()),
                "cena2": _to_grosze(tds[3].get_text()) if len(tds) > 3 else 0,
                "wolumen2": _to_int_volume(tds[4].get_text()) if len(tds) > 4 else 0,
            }
        )
    return godziny


def _expand_to_quarter_hours(doba: str, godziny: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """24 godziny → 96 rekordów 15-min (cena godziny kopiowana na każdy kwadrans)."""
    base_date = datetime.strptime(doba, "%Y-%m-%d").date()
    by_hour = {g["hour"]: g for g in godziny}
    records: list[dict[str, Any]] = []

    for hour in range(24):
        if hour not in by_hour:
            continue
        g = by_hour[hour]
        for quarter in range(4):
            minute = quarter * 15
            start_t = time(hour, minute)
            end_total = hour * 60 + minute + 15
            end_t = time((end_total // 60) % 24, end_total % 60)
            start_dt = datetime.combine(base_date, start_t)
            records.append(
                {
                    "czas_ceny": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "doba": doba,
                    "czas": start_t.strftime("%H:%M:%S"),
                    "czas_do": end_t.strftime("%H:%M:%S"),
                    "interwal_minut": 15,
                    "cena1": g["cena1"],
                    "wolumen1": g["wolumen1"],
                    "cena2": g["cena2"],
                    "wolumen2": g["wolumen2"],
                    FORECAST_FLAG_COLUMN: 0,
                }
            )
    return records


def _table_columns(cursor) -> set[str]:
    cursor.execute("SHOW COLUMNS FROM t_rdn")
    cols: set[str] = set()
    for row in cursor.fetchall():
        cols.add(row[0])
    return cols


def _ensure_forecast_flag(cursor, columns: set[str]) -> None:
    if FORECAST_FLAG_COLUMN in columns:
        return
    cursor.execute(
        f"""
        ALTER TABLE t_rdn
        ADD COLUMN {FORECAST_FLAG_COLUMN} TINYINT(1) NOT NULL DEFAULT 0
        COMMENT '1=prognoza, 0=cena realna'
        """
    )
    columns.add(FORECAST_FLAG_COLUMN)
    print(f"[INFO] Dodano kolumnę {FORECAST_FLAG_COLUMN} do t_rdn")


def _save_records(records: list[dict[str, Any]]) -> int:
    if not records:
        return 0

    conn = mysql.connector.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        columns = _table_columns(cursor)
        _ensure_forecast_flag(cursor, columns)

        # Kolumny wspólne z prognozą / starym importem
        preferred = [
            "czas_ceny",
            "doba",
            "czas",
            "czas_do",
            "interwal_minut",
            "cena1",
            "wolumen1",
            "cena2",
            "wolumen2",
            FORECAST_FLAG_COLUMN,
        ]
        cols = [c for c in preferred if c in columns]
        if "doba" not in cols or "czas" not in cols or "cena1" not in cols:
            raise RuntimeError("t_rdn: brak wymaganych kolumn doba/czas/cena1")

        col_sql = ", ".join(cols)
        placeholders = ", ".join(["%s"] * len(cols))
        update_cols = [c for c in cols if c not in ("doba", "czas")]
        update_sql = ", ".join(f"{c}=VALUES({c})" for c in update_cols)

        sql = f"""
            INSERT INTO t_rdn ({col_sql})
            VALUES ({placeholders})
            ON DUPLICATE KEY UPDATE
                {update_sql}
        """

        for rec in records:
            cursor.execute(sql, [rec.get(c) for c in cols])

        conn.commit()
        cursor.close()
        return len(records)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def import_tge_to_db(html_path: str | None = None) -> dict[str, Any]:
    path = html_path or HTML_FILE
    if not os.path.exists(path):
        raise FileNotFoundError(f"Brak pliku HTML: {path}")

    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    soup = BeautifulSoup(html, "html.parser")
    doba = _parse_delivery_date(soup)
    godziny = _parse_hourly_rows(soup)
    if not godziny:
        raise RuntimeError("Nie znaleziono wierszy godzinowych w tge.html")

    records = _expand_to_quarter_hours(doba, godziny)
    saved = _save_records(records)

    print(
        f"[INFO] Zaimportowano {len(godziny)} godzin → {saved} kwadransów "
        f"dla dnia {doba} (is_forecast=0, cena w groszach/MWh)"
    )
    return {
        "doba": doba,
        "hours": len(godziny),
        "quarters": saved,
        "html": path,
    }


if __name__ == "__main__":
    import_tge_to_db()
