import mysql.connector
from datetime import datetime


def get_tge_prices_data(date=None):
    conn = mysql.connector.connect(
        host="przewas.mysql.pythonanywhere-services.com",
        user="przewas",
        password="Ka$zanka77",
        database="przewas$scadaPv"
    )
    cursor = conn.cursor(dictionary=True)

    # --- jeśli podano datę → użyj jej ---
    if date:
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            selected_doba = dt.strftime("%Y-%m-%d")
        except Exception:
            selected_doba = None
    else:
        selected_doba = None

    # --- jeśli brak daty lub zła → pobierz najnowszą ---
    if not selected_doba:
        cursor.execute("SELECT MAX(doba) AS latest_doba FROM t_rdn")
        row = cursor.fetchone()

        if not row or not row["latest_doba"]:
            cursor.close()
            conn.close()
            return {
                "ceny_dla_dnia": datetime.now().strftime("%Y-%m-%d"),
                "godziny": []
            }

        selected_doba = row["latest_doba"]

    # --- sprawdź czy dane istnieją ---
    cursor.execute("SELECT COUNT(*) AS cnt FROM t_rdn WHERE doba = %s", (selected_doba,))
    count_row = cursor.fetchone()

    if not count_row or count_row["cnt"] == 0:
        cursor.close()
        conn.close()
        return {
            "ceny_dla_dnia": selected_doba,
            "godziny": []
        }

    # --- pobierz dane rzeczywiste z t_rdn ---
    cursor.execute("""
        SELECT czas, cena1, wolumen1, cena2, wolumen2
        FROM t_rdn
        WHERE doba = %s
        ORDER BY czas
    """, (selected_doba,))
    rows = cursor.fetchall()

    godziny = []

    for r in rows:
        czas = r["czas"]

        if isinstance(czas, str):
            godzina_str = datetime.strptime(czas, "%H:%M:%S").strftime("%H:%M")
        else:
            total_seconds = czas.total_seconds()
            hours = int(total_seconds // 3600)
            minutes = int((total_seconds % 3600) // 60)
            godzina_str = f"{hours:02d}:{minutes:02d}"

        cena1_str = f"{r['cena1'] / 100:.2f}".replace(".", ",")
        cena2_str = f"{r['cena2'] / 100:.2f}".replace(".", ",")
        vol1_str = str(r["wolumen1"])
        vol2_str = str(r["wolumen2"])

        obj = {
            "godzina": godzina_str,
            "fixing1": {"cena": cena1_str, "vol": vol1_str},
            "fixing2": {"cena": cena2_str, "vol": vol2_str}
        }

        godziny.append(obj)

    cursor.close()
    conn.close()

    return {
        "ceny_dla_dnia": str(selected_doba),
        "godziny": godziny
    }


# --- test lokalny ---
if __name__ == "__main__":
    import json
    data = get_tge_prices_data("2026-02-20")  # możesz testować dowolną datę
    print(json.dumps(data, indent=2, ensure_ascii=False))
