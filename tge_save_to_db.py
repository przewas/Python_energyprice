from bs4 import BeautifulSoup
import re
from datetime import datetime
import mysql.connector
import os

APP_DIR = "/home/przewas/pv"
HTML_FILE = os.path.join(APP_DIR, "tge.html")


def import_tge_to_db():
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    soup = BeautifulSoup(html, "html.parser")

    # --- data dostawy ---
    doba = None
    kontrakt_h4 = soup.find("h4", class_="kontrakt-date")
    if kontrakt_h4:
        small = kontrakt_h4.find("small")
        if small:
            m = re.search(r"(\d{2})-(\d{2})-(\d{4})", small.get_text())
            if m:
                doba = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    if not doba:
        doba = datetime.now().strftime("%Y-%m-%d")

    # --- parsowanie tabeli godzin ---
    godziny = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if not tds or len(tds) < 7:
            continue

        czas = tds[0].get_text(strip=True)
        if not re.match(r"\d{1,2}-\d{1,2}", czas):
            continue

        godzina_start = int(czas.split('-')[0])
        godzina_str = f"{godzina_start:02d}:00:00"

        fixing1_cena = tds[1].get_text(strip=True).replace(",", ".").replace(" ", "")
        fixing1_vol = tds[2].get_text(strip=True).replace(",", ".").replace(" ", "")
        fixing2_cena = tds[3].get_text(strip=True).replace(",", ".").replace(" ", "")
        fixing2_vol = tds[4].get_text(strip=True).replace(",", ".").replace(" ", "")


        godziny.append({
            "czas": godzina_str,
            "cena1": fixing1_cena,
            "wolumen1": fixing1_vol,
            "cena2": fixing2_cena,
            "wolumen2": fixing2_vol
        })

    # --- zapis do bazy ---
    conn = mysql.connector.connect(
        host="przewas.mysql.pythonanywhere-services.com",
        user="przewas",
        password="Ka$zanka77",  # <-- podmień
        database="przewas$scadaPv"
    )

    cursor = conn.cursor()

    sql = """
        INSERT INTO t_rdn (czas_ceny, doba, czas, cena1, wolumen1, cena2, wolumen2)
        VALUES (NOW(), %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            cena1 = VALUES(cena1),
            wolumen1 = VALUES(wolumen1),
            cena2 = VALUES(cena2),
            wolumen2 = VALUES(wolumen2),
            czas_ceny = VALUES(czas_ceny)
    """

    for g in godziny:
        cursor.execute(sql, (
            doba,
            g["czas"],
            int(float(g["cena1"]) * 100) if g["cena1"] else 0,   # grosze
            int(float(g["wolumen1"])) if g["wolumen1"] else 0,
            int(float(g["cena2"]) * 100) if g["cena2"] else 0,   # grosze
            int(float(g["wolumen2"])) if g["wolumen2"] else 0,
        ))


    conn.commit()
    cursor.close()
    conn.close()

    print(f"[INFO] Zaimportowano {len(godziny)} rekordów dla dnia {doba}")


if __name__ == "__main__":
    import_tge_to_db()
