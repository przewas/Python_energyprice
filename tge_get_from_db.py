"""Odczyt cen RDN z t_rdn dla API /api/tge."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import mysql.connector

DB_CONFIG = {
    "host": "przewas.mysql.pythonanywhere-services.com",
    "user": "przewas",
    "password": "Ka$zanka77",
    "database": "przewas$scadaPv",
}

FORECAST_TZ = ZoneInfo("Europe/Warsaw")
FORECAST_FLAG_COLUMN = "is_forecast"
DEFAULT_HORIZON_HOURS = 36


def _connect():
    return mysql.connector.connect(**DB_CONFIG)


def _czas_to_str(value: Any) -> str:
    if value is None:
        return "00:00:00"
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


def _has_forecast_column(cursor) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM t_rdn LIKE '{FORECAST_FLAG_COLUMN}'")
    return cursor.fetchone() is not None


def _format_price_grosze(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return f"{int(value) / 100:.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return None


def _slot_payload(
    *,
    doba: str,
    czas: str,
    cena1: Any,
    wolumen1: Any,
    cena2: Any,
    wolumen2: Any,
    is_forecast: bool | None,
) -> dict[str, Any]:
    if is_forecast is True:
        zrodlo = "prognoza"
    elif is_forecast is False:
        zrodlo = "realna"
    else:
        zrodlo = "brak"

    return {
        "doba": doba,
        "czas": czas,
        "czas_ceny": f"{doba} {czas}",
        "godzina": czas[:5],
        "fixing1": {
            "cena": _format_price_grosze(cena1),
            "vol": None if wolumen1 is None else str(wolumen1),
        },
        "fixing2": {
            "cena": _format_price_grosze(cena2) if cena2 is not None else "0,00",
            "vol": None if wolumen2 is None else str(wolumen2),
        },
        "is_forecast": is_forecast,
        "zrodlo": zrodlo,
    }


def get_tge_prices_rolling(
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    *,
    start_at: datetime | None = None,
) -> dict[str, Any]:
    """
    Rolling horyzont godzinowy z t_rdn (domyślnie 36h od bieżącej godziny PL).

    Bierze sloty :00 każdej godziny. Dla każdego slotu zwraca is_forecast / zrodlo.
    Brak danych w DB → is_forecast=null, zrodlo='brak', cena=null.
    """
    horizon_hours = max(1, min(int(horizon_hours), 72))
    now = start_at.astimezone(FORECAST_TZ) if start_at else datetime.now(FORECAST_TZ)
    start = now.replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=horizon_hours)

    # doba od startu do ostatniego slotu włącznie (end jest exclusive)
    last_slot = end - timedelta(hours=1)
    days = []
    day = start.date()
    while day <= last_slot.date():
        days.append(day.strftime("%Y-%m-%d"))
        day += timedelta(days=1)

    conn = _connect()
    try:
        cursor = conn.cursor(dictionary=True)
        has_flag = _has_forecast_column(cursor)

        flag_select = f", {FORECAST_FLAG_COLUMN} AS is_forecast" if has_flag else ""
        placeholders = ", ".join(["%s"] * len(days))
        cursor.execute(
            f"""
            SELECT doba, czas, cena1, wolumen1, cena2, wolumen2
                   {flag_select}
            FROM t_rdn
            WHERE doba IN ({placeholders})
              AND MINUTE(czas) = 0
              AND SECOND(czas) = 0
            ORDER BY doba, czas
            """,
            days,
        )
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        doba = str(row["doba"])
        if hasattr(row["doba"], "strftime"):
            doba = row["doba"].strftime("%Y-%m-%d")
        czas = _czas_to_str(row["czas"])
        flag = None
        if has_flag:
            raw_flag = row.get("is_forecast")
            flag = bool(int(raw_flag)) if raw_flag is not None else False
        by_key[(doba, czas)] = {
            "cena1": row.get("cena1"),
            "wolumen1": row.get("wolumen1"),
            "cena2": row.get("cena2"),
            "wolumen2": row.get("wolumen2"),
            "is_forecast": flag,
        }

    godziny: list[dict[str, Any]] = []
    for i in range(horizon_hours):
        slot_dt = start + timedelta(hours=i)
        doba = slot_dt.strftime("%Y-%m-%d")
        czas = slot_dt.strftime("%H:%M:%S")
        found = by_key.get((doba, czas))
        if found:
            godziny.append(
                _slot_payload(
                    doba=doba,
                    czas=czas,
                    cena1=found["cena1"],
                    wolumen1=found["wolumen1"],
                    cena2=found["cena2"],
                    wolumen2=found["wolumen2"],
                    is_forecast=found["is_forecast"],
                )
            )
        else:
            godziny.append(
                _slot_payload(
                    doba=doba,
                    czas=czas,
                    cena1=None,
                    wolumen1=None,
                    cena2=None,
                    wolumen2=None,
                    is_forecast=None,
                )
            )

    available = sum(1 for g in godziny if g["zrodlo"] != "brak")
    forecasts = sum(1 for g in godziny if g["is_forecast"] is True)
    reals = sum(1 for g in godziny if g["is_forecast"] is False)

    return {
        "horizon_hours": horizon_hours,
        "timezone": "Europe/Warsaw",
        "start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": end.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(godziny),
        "available": available,
        "real_count": reals,
        "forecast_count": forecasts,
        "godziny": godziny,
    }


def get_tge_prices_data(date=None):
    """Jedna doba (sloty 15-min) — tryb legacy mode=day. Nowy kontrakt: rolling."""
    conn = _connect()
    cursor = conn.cursor(dictionary=True)

    if date:
        try:
            selected_doba = datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d")
        except Exception:
            selected_doba = None
    else:
        selected_doba = None

    if not selected_doba:
        cursor.execute("SELECT MAX(doba) AS latest_doba FROM t_rdn")
        row = cursor.fetchone()
        if not row or not row["latest_doba"]:
            cursor.close()
            conn.close()
            return {
                "ceny_dla_dnia": datetime.now(FORECAST_TZ).strftime("%Y-%m-%d"),
                "godziny": [],
            }
        selected_doba = str(row["latest_doba"])

    has_flag = _has_forecast_column(cursor)
    flag_select = f", {FORECAST_FLAG_COLUMN} AS is_forecast" if has_flag else ""

    cursor.execute(
        f"""
        SELECT czas, cena1, wolumen1, cena2, wolumen2
               {flag_select}
        FROM t_rdn
        WHERE doba = %s
        ORDER BY czas
        """,
        (selected_doba,),
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    godziny = []
    for r in rows:
        czas = _czas_to_str(r["czas"])
        is_forecast = None
        if has_flag and r.get("is_forecast") is not None:
            is_forecast = bool(int(r["is_forecast"]))
        godziny.append(
            _slot_payload(
                doba=selected_doba,
                czas=czas,
                cena1=r.get("cena1"),
                wolumen1=r.get("wolumen1"),
                cena2=r.get("cena2"),
                wolumen2=r.get("wolumen2"),
                is_forecast=is_forecast,
            )
        )

    return {
        "ceny_dla_dnia": str(selected_doba),
        "godziny": godziny,
    }


if __name__ == "__main__":
    import json

    try:
        data = get_tge_prices_rolling(36)
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as exc:
        print(f"DB niedostępna lokalnie ({exc}) — smoke test formatu:")
        demo = {
            "horizon_hours": 36,
            "timezone": "Europe/Warsaw",
            "count": 36,
            "godziny": [
                _slot_payload(
                    doba="2026-07-19",
                    czas="00:00:00",
                    cena1=57646,
                    wolumen1=0,
                    cena2=0,
                    wolumen2=0,
                    is_forecast=True,
                )
            ],
        }
        print(json.dumps(demo, indent=2, ensure_ascii=False))
