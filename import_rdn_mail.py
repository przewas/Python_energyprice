#!/usr/bin/env python3
"""
Import cen RDN (96 × 15 min) z maila FIXING na Gmailu → t_rdn.

Zapisuje ceny realne (is_forecast=0) — UPSERT nadpisuje wcześniejszą prognozę.
Prognozy: osobno fetch_pradcast_forecast.py (API pradcast.pl).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

from gmail_rdn import (
    connect_imap,
    decode_header_value,
    get_email_body,
    get_latest_fixing_email,
    log,
    move_email_to_processed,
    parse_quarter_hour_fixing,
    resolve_fixing_date,
)

DB_CONFIG = {
    "host": "przewas.mysql.pythonanywhere-services.com",
    "user": "przewas",
    "password": "Ka$zanka77",
    "database": "przewas$scadaPv",
}

FORECAST_FLAG_COLUMN = "is_forecast"


def connect_db():
    import mysql.connector

    return mysql.connector.connect(**DB_CONFIG)


def ensure_forecast_flag_column(conn) -> None:
    cursor = conn.cursor()
    cursor.execute(f"SHOW COLUMNS FROM t_rdn LIKE '{FORECAST_FLAG_COLUMN}'")
    if cursor.fetchone():
        cursor.close()
        return
    cursor.execute(
        f"""
        ALTER TABLE t_rdn
        ADD COLUMN {FORECAST_FLAG_COLUMN} TINYINT(1) NOT NULL DEFAULT 0
        COMMENT '1=prognoza, 0=cena realna'
        """
    )
    conn.commit()
    cursor.close()
    log(f"Dodano kolumnę {FORECAST_FLAG_COLUMN} do t_rdn")


def validate_parsed_data(records, date_str):
    errors = []

    if len(records) != 96:
        errors.append(f"Nieprawidlowa liczba rekordow: {len(records)} (oczekiwano 96)")

    try:
        base_date = datetime.strptime(date_str, "%Y-%m-%d")
        if base_date < datetime(2025, 1, 1):
            errors.append(f"Data {date_str} jest niepoprawna - musi byc po 2025-01-01")
    except Exception:
        errors.append(f"Nie udalo sie sparsowac daty: {date_str}")
        base_date = None

    seen_times = set()
    for idx, record in enumerate(records, start=1):
        try:
            czas_ceny = datetime.strptime(record["czas_ceny"], "%Y-%m-%d %H:%M:%S")
            czas = datetime.strptime(record["czas"], "%H:%M:%S").time()
            czas_do = datetime.strptime(record["czas_do"], "%H:%M:%S").time()
        except Exception:
            errors.append(f"Rekord {idx}: nieprawidlowy format czasu")
            continue

        if base_date and czas_ceny.date() != base_date.date():
            errors.append(f"Rekord {idx}: czas_ceny nie nalezy do doby {date_str}")

        if record["czas"] in seen_times:
            errors.append(f"Rekord {idx}: zduplikowany poczatek okresu {record['czas']}")
        seen_times.add(record["czas"])

        expected_start = (base_date + timedelta(minutes=(idx - 1) * 15)).time() if base_date else None
        if expected_start and czas != expected_start:
            errors.append(
                f"Rekord {idx}: oczekiwano poczatku {expected_start.strftime('%H:%M:%S')}, "
                f"otrzymano {record['czas']}"
            )

        expected_end = (base_date + timedelta(minutes=idx * 15)).time() if base_date else None
        if expected_end and czas_do != expected_end:
            errors.append(
                f"Rekord {idx}: oczekiwano konca {expected_end.strftime('%H:%M:%S')}, "
                f"otrzymano {record['czas_do']}"
            )

        if record.get("interwal_minut") != 15:
            errors.append(f"Rekord {idx}: interwal_minut musi wynosic 15")

        cena1 = record.get("cena1")
        if not isinstance(cena1, int):
            errors.append(f"Rekord {idx}: cena nie jest liczba calkowita w groszach")
        elif abs(cena1 / 100.0) >= 4000:
            errors.append(f"Rekord {idx}: cena {cena1 / 100.0:.2f} zl poza zakresem (< 4000 zl)")

    if errors:
        print("Bledy walidacji danych RDN:\n\n" + "\n".join(errors))
        return False

    print("Walidacja danych zakonczona pomyslnie.")
    return True


def check_if_real_data_exists(conn, date_str) -> bool:
    """True gdy doba ma już pełne 96 slotów z ceną realną (is_forecast=0)."""
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT COUNT(*)
        FROM t_rdn
        WHERE doba=%s
          AND {FORECAST_FLAG_COLUMN}=0
          AND MINUTE(czas) IN (0, 15, 30, 45)
        """,
        (date_str,),
    )
    count = cursor.fetchone()[0]
    cursor.close()
    return count >= 96


def insert_data(conn, records):
    """UPSERT cen realnych — zawsze ustawia is_forecast=0 (nadpisuje prognozę)."""
    cursor = conn.cursor()

    # sprawdź opcjonalne kolumny
    cursor.execute("SHOW COLUMNS FROM t_rdn")
    columns = {row[0] for row in cursor.fetchall()}

    preferred = [
        "czas_ceny",
        "doba",
        "czas",
        "czas_do",
        "interwal_minut",
        "cena1",
        "cena2",
        FORECAST_FLAG_COLUMN,
    ]
    cols = [c for c in preferred if c in columns]
    if "doba" not in cols or "czas" not in cols or "cena1" not in cols:
        raise RuntimeError("t_rdn: brak wymaganych kolumn doba/czas/cena1")
    if FORECAST_FLAG_COLUMN not in cols:
        raise RuntimeError(f"t_rdn: brak kolumny {FORECAST_FLAG_COLUMN}")

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

    for record in records:
        row = {
            "czas_ceny": record["czas_ceny"],
            "doba": record["doba"],
            "czas": record["czas"],
            "czas_do": record.get("czas_do"),
            "interwal_minut": int(record.get("interwal_minut", 15)),
            "cena1": int(record["cena1"]),
            "cena2": int(record.get("cena2", 0)),
            FORECAST_FLAG_COLUMN: 0,
        }
        cursor.execute(sql, [row[c] for c in cols])

    conn.commit()
    cursor.close()
    print(f"Zapisano {len(records)} rekordow do t_rdn (is_forecast=0, grosze/MWh)")


def main():
    log("Start import_rdn_mail.py (Gmail → t_rdn, ceny realne)")
    mail = None
    try:
        mail = connect_imap()
        msg, email_id = get_latest_fixing_email(mail)
        if not msg:
            return

        subject = decode_header_value(msg.get("Subject"))
        log(f"Temat: {subject}")
        fixing_date = resolve_fixing_date(msg, subject)
        log(f"Data notowań: {fixing_date}")

        body = get_email_body(msg)
        parsed_data = parse_quarter_hour_fixing(body, fixing_date)

        if not parsed_data:
            log("Brak danych do zapisu w treści maila.")
            move_email_to_processed(mail, email_id)
            sys.exit(1)

        log("Podgląd danych:")
        for row in parsed_data[:8]:
            log(
                f"{row['czas_ceny']} | {row['czas']} - {row['czas_do']} | "
                f"{row['cena1']} groszy ({row['cena1'] / 100:.2f} PLN/MWh)"
            )
        if len(parsed_data) > 8:
            log(f"... razem {len(parsed_data)} rekordów")

        conn = connect_db()
        try:
            ensure_forecast_flag_column(conn)

            if check_if_real_data_exists(conn, fixing_date):
                log(
                    f"Realne ceny 15-min dla {fixing_date} już istnieją "
                    f"(is_forecast=0) — pomijam zapis."
                )
            elif validate_parsed_data(parsed_data, fixing_date):
                insert_data(conn, parsed_data)
                log(f"Nadpisano/uzupełniono t_rdn dla {fixing_date} jako ceny realne.")
            else:
                log("Walidacja nie powiodła się — zapis przerwany.")
                sys.exit(1)
        finally:
            conn.close()

        move_email_to_processed(mail, email_id)
    except Exception as exc:
        log(f"BŁĄD: {exc}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass
        log("Koniec.")


if __name__ == "__main__":
    main()
