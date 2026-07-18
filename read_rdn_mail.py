import imaplib
import email
from bs4 import BeautifulSoup

from email.header import decode_header

# --- KONFIGURACJA ---
IMAP_SERVER = "serwer2518354.home.pl"
EMAIL_USER = "rdn@serwer2518354.home.pl"
EMAIL_PASS = "Ka$zanka77"  # 🔒 W produkcji użyj zmiennych środowiskowych

# --- POŁĄCZENIE ---
def connect_imap():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_USER, EMAIL_PASS)
        print("✅ Połączono z serwerem pocztowym")
        return mail
    except Exception as e:
        print("❌ Błąd logowania:", e)
        exit(1)

# --- SZUKANIE MAILI ---
def get_latest_fixing_email(mail):
    mail.select("INBOX")
    status, data = mail.search(None, '(SUBJECT "FIXING")')
    if status != "OK" or not data[0]:
        print("Brak wiadomości z tematem 'FIXING'")
        return None

    email_ids = data[0].split()
    latest_id = email_ids[-1]  # najnowszy e-mail
    status, msg_data = mail.fetch(latest_id, "(RFC822)")
    if status != "OK":
        print("Błąd podczas pobierania wiadomości")
        return None

    raw_email = msg_data[0][1]
    return email.message_from_bytes(raw_email)

# --- DEKODOWANIE NAGŁÓWKA ---
def decode_header_value(value):
    decoded_parts = decode_header(value)
    decoded_str = ""
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            decoded_str += part.decode(charset or "utf-8", errors="ignore")
        else:
            decoded_str += part
    return decoded_str

# --- ODCZYT TREŚCI ---
def get_email_body(msg):

    body = ""

    if msg.is_multipart():
        for part in msg.walk():

            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))

            if "attachment" in content_disposition:
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            payload = payload.decode(errors="ignore")

            if content_type == "text/plain":
                body = payload

            elif content_type == "text/html" and not body:
                soup = BeautifulSoup(payload, "html.parser")
                body = soup.get_text("\n")

    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(errors="ignore")

    return body


def get_email_body2(msg):
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))

            if content_type == "text/plain" and "attachment" not in content_disposition:
                return part.get_payload(decode=True).decode(errors="ignore")
            elif content_type == "text/html":
                html = part.get_payload(decode=True).decode(errors="ignore")
                # Można tu dodać stripowanie HTML -> plain text
                return html
    else:
        return msg.get_payload(decode=True).decode(errors="ignore")
    return ""

# --- GŁÓWNA FUNKCJA ---
def main():
    mail = connect_imap()
    msg = get_latest_fixing_email(mail)
    if not msg:
        mail.logout()
        return

    subject = decode_header_value(msg["Subject"])
    date = msg["Date"]
    sender = decode_header_value(msg["From"])

    print("------------------------------------------")
    print(f"📬 Temat: {subject}")
    print(f"📅 Data: {date}")
    print(f"👤 Nadawca: {sender}")
    print("------------------------------------------")

    body = get_email_body(msg)
    print(body)
    print("------------------------------------------")

    mail.logout()

if __name__ == "__main__":
    main()
