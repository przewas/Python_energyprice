#!/usr/bin/env python3
"""
Import cen RDN (96 × 15 min) z maila FIXING na Gmailu → t_rdn.

Prognozy: osobno tge_predict.py (API pradcast.pl).
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


def connect_db():
    import mysql.connector

    return mysql.connector.connect(**DB_CONFIG)


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


def check_if_data_exists(conn, date_str):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM t_rdn
        WHERE doba=%s
          AND MINUTE(czas) IN (0, 15, 30, 45)
        """,
        (date_str,),
    )
    count = cursor.fetchone()[0]
    cursor.close()
    return count >= 96


def insert_data(conn, records):
    cursor = conn.cursor()
    sql = """
        INSERT INTO t_rdn (czas_ceny, doba, czas, cena1, cena2)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            czas_ceny = VALUES(czas_ceny),
            cena1 = VALUES(cena1),
            cena2 = VALUES(cena2)
    """
    for record in records:
        cursor.execute(
            sql,
            (
                record["czas_ceny"],
                record["doba"],
                record["czas"],
                record["cena1"],
                record["cena2"],
            ),
        )

    conn.commit()
    cursor.close()
    print(f"Zapisano {len(records)} rekordow do t_rdn")


def main():
    log("Start import_rdn_mail.py (Gmail → t_rdn)")
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
            log(f"{row['czas']} - {row['czas_do']} | {row['cena1']} groszy")
        if len(parsed_data) > 8:
            log(f"... razem {len(parsed_data)} rekordów")

        conn = connect_db()
        try:
            if check_if_data_exists(conn, fixing_date):
                log(f"Dane 15-minutowe dla {fixing_date} już istnieją — pomijam zapis.")
            elif validate_parsed_data(parsed_data, fixing_date):
                insert_data(conn, parsed_data)
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