"""Odbiór maili FIXING (RDN) z Gmaila przez IMAP + parser 96 kwadransów."""

from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from email.header import decode_header

# --- KONFIGURACJA GMAIL (uzupełnij przed uruchomieniem) ---
IMAP_SERVER = "imap.gmail.com"
EMAIL_USER = "cenyrdn@gmail.com"
EMAIL_PASS = "elyv omgb kvrq vohp"  # hasło aplikacji Google (nie zwykłe hasło konta)
MAIL_SUBJECT = "FIXING"
PROCESSED_FOLDER = ""
INCLUDE_SEEN = False  # True = szukaj też przeczytanych (test)
IMAP_TIMEOUT = 30


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

PRICE_RE = r"-?\d+(?:[,.]\d+)?"
PERIOD_RE = re.compile(r"^(?P<start>\d{2}:\d{2})\s*-\s*(?P<end>(?:\d{2}|24):\d{2})$")
INLINE_ROW_RE = re.compile(
    rf"^\s*(?P<idx>\d{{1,3}})\s+"
    r"(?P<start>\d{2}:\d{2})\s*-\s*(?P<end>(?:\d{2}|24):\d{2})\s+"
    rf"(?P<price>{PRICE_RE})\s*$"
)


@dataclass(frozen=True)
class QuarterHourRecord:
    index: int
    start: str
    end: str
    price_grosze: int


def connect_imap():
    if not EMAIL_USER or not EMAIL_PASS:
        log("BŁĄD: brak loginu lub hasła.")
        log("Uzupełnij EMAIL_USER i EMAIL_PASS na górze pliku gmail_rdn.py")
        raise RuntimeError("Brak danych logowania Gmail.")
    log(f"Łączenie z {IMAP_SERVER} jako {EMAIL_USER} (timeout {IMAP_TIMEOUT}s)...")
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, timeout=IMAP_TIMEOUT)
    mail.login(EMAIL_USER, EMAIL_PASS)
    log(f"Połączono z {IMAP_SERVER}")
    return mail


def _search_fixing_ids(mail):
    criteria = f'(SUBJECT "{MAIL_SUBJECT}")'
    if not INCLUDE_SEEN:
        criteria = f'(UNSEEN SUBJECT "{MAIL_SUBJECT}")'
    log(f"Szukam maili: {criteria}")
    status, data = mail.search(None, criteria)
    if status != "OK" or not data[0]:
        return []

    ids = data[0].split()
    log(f"Znaleziono {len(ids)} wiadomości")
    return ids


def get_latest_fixing_email(mail):
    mail.select("INBOX")
    ids = _search_fixing_ids(mail)

    if not ids:
        if not INCLUDE_SEEN:
            log(f"Brak nieprzeczytanych maili z tematem '{MAIL_SUBJECT}'.")
            log("Wskazówka: ustaw INCLUDE_SEEN = True w gmail_rdn.py albo oznacz mail jako nieprzeczytany.")
        else:
            log(f"Brak maili z tematem '{MAIL_SUBJECT}'.")
        return None, None

    latest_id = ids[-1]
    log(f"Pobieram wiadomość id={latest_id.decode() if isinstance(latest_id, bytes) else latest_id}")
    status, msg_data = mail.fetch(latest_id, "(RFC822)")
    if status != "OK":
        log("Błąd podczas pobierania wiadomości")
        return None, None

    return email.message_from_bytes(msg_data[0][1]), latest_id


def decode_header_value(value):
    if not value:
        return ""
    decoded_parts = decode_header(value)
    return "".join(
        part.decode(charset or "utf-8", errors="ignore") if isinstance(part, bytes) else part
        for part, charset in decoded_parts
    )


def _html_to_text(html):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html
    return BeautifulSoup(html, "html.parser").get_text("\n")


def get_email_body(msg):
    plain_body = ""
    html_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition", "")).lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            content_type = part.get_content_type()
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="ignore")
            if content_type == "text/plain" and not plain_body:
                plain_body = decoded
            elif content_type == "text/html" and not html_body:
                html_body = _html_to_text(decoded)
        return plain_body or html_body

    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    decoded = payload.decode(charset, errors="ignore")
    if msg.get_content_type() == "text/html":
        return _html_to_text(decoded)
    return decoded


def convert_date_from_subject(subject):
    subject = (subject or "").strip()
    patterns = [
        r"(\d{4})[-.](\d{1,2})[-.](\d{1,2})",
        r"(\d{1,2})[-.](\d{1,2})[-.](\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, subject, re.IGNORECASE)
        if not match:
            continue
        try:
            if len(match.group(1)) == 4:
                year, month, day = match.group(1), match.group(2), match.group(3)
            else:
                day, month, year = match.group(1), match.group(2), match.group(3)
            return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def get_date_from_email_header(msg):
    try:
        raw_date = msg.get("Date")
        if not raw_date:
            return None
        dt = email.utils.parsedate_to_datetime(raw_date)
        if not dt:
            return None
        return (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        return None


def resolve_fixing_date(msg, subject):
    date_from_subject = convert_date_from_subject(subject)
    if date_from_subject:
        return date_from_subject

    print("Brak daty w temacie - probuje z naglowka maila")
    date_from_header = get_date_from_email_header(msg)
    if date_from_header:
        return date_from_header

    print("Brak daty w naglowku - uzywam jutra")
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def _normalize_text(text):
    return (
        (text or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\xa0", " ")
        .replace("\u202f", " ")
    )


def _parse_price_to_grosze(value):
    return int(round(float(value.replace(" ", "").replace(",", ".")) * 100))


def _parse_period_time(value, *, allow_24=False):
    if allow_24 and value == "24:00":
        return time(0, 0)
    hour, minute = [int(part) for part in value.split(":", 1)]
    return time(hour, minute)


def _record_to_db_row(record, date_str):
    start_time = _parse_period_time(record.start)
    end_time = _parse_period_time(record.end, allow_24=True)
    start_dt = datetime.combine(datetime.strptime(date_str, "%Y-%m-%d").date(), start_time)
    return {
        "czas_ceny": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "doba": date_str,
        "czas": start_time.strftime("%H:%M:%S"),
        "czas_do": end_time.strftime("%H:%M:%S"),
        "interwal_minut": 15,
        "cena1": record.price_grosze,
        "cena2": 0,
    }


def parse_quarter_hour_fixing(text, date_str):
    text = _normalize_text(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    records_by_start = {}

    for line in lines:
        match = INLINE_ROW_RE.match(line)
        if not match:
            continue
        record = QuarterHourRecord(
            index=int(match.group("idx")),
            start=match.group("start"),
            end=match.group("end"),
            price_grosze=_parse_price_to_grosze(match.group("price")),
        )
        records_by_start.setdefault(record.start, record)

    for idx in range(len(lines) - 2):
        if not re.fullmatch(r"\d{1,3}", lines[idx]):
            continue
        period_match = PERIOD_RE.match(lines[idx + 1])
        if not period_match or not re.fullmatch(PRICE_RE, lines[idx + 2]):
            continue
        record = QuarterHourRecord(
            index=int(lines[idx]),
            start=period_match.group("start"),
            end=period_match.group("end"),
            price_grosze=_parse_price_to_grosze(lines[idx + 2]),
        )
        records_by_start.setdefault(record.start, record)

    records = sorted(records_by_start.values(), key=lambda record: record.index)
    return [_record_to_db_row(record, date_str) for record in records]


def move_email_to_processed(mail, email_id):
    if PROCESSED_FOLDER:
        try:
            mail.copy(email_id, PROCESSED_FOLDER)
            mail.store(email_id, "+FLAGS", "\\Deleted")
            mail.expunge()
            log(f"Wiadomość przeniesiona do '{PROCESSED_FOLDER}'")
            return
        except Exception as exc:
            log(f"Nie udało się przenieść do folderu ({exc}) — oznaczam jako przeczytane")

    try:
        mail.store(email_id, "+FLAGS", "\\Seen")
        log("Wiadomość oznaczona jako przeczytana")
    except Exception as exc:
        log(f"Nie udało się oznaczyć wiadomości: {exc}")