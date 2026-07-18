# Dokumentacja cen energii (RDN/TGE) dla agenta harmonogramu BESS

Kontekst dla agenta projektującego harmonogram magazynu energii (BESS): skąd bierze się cena RDN, jak czytać API i jak interpretować dane.

---

## 1. Cel systemu cen

System utrzymuje **jedną tabelę cen** `t_rdn` (MySQL), z której korzysta API. W tej samej tabeli są:

| Typ | Skąd | Flaga `is_forecast` |
|-----|------|---------------------|
| **Cena realna** (FIXING) | mail Gmail „FIXING” (`import_rdn_mail.py`) lub HTML TGE (`tge_save_to_db.py`) | `0` |
| **Prognoza** | API pradcast.pl, horyzont **D+1** (`fetch_pradcast_forecast.py`) | `1` |

**Reguła nadpisywania:**
- prognoza **nie nadpisuje** ceny realnej,
- cena realna (UPSERT) **nadpisuje** prognozę i ustawia `is_forecast=0`.

Dla BESS: po publikacji Fixingu sloty mają ceny potwierdzone; wcześniej w tych samych slotach może być prognoza modelu.

---

## 2. Źródła danych (pipeline)

```
pradcast.pl (prognoza D+1)
        │
        ▼
   t_rdn  �adcast.pl (prognoza D+1)
        │
        ▼
   t_rdn  ◄──── mail FIXING (Gmail) / tge.html  (ceny realne)
        │
        ▼
   GET /api/tge   →  klient / agent BESS
```

### 2.1. Prognoza (pradcast.pl)
- Endpoint zewnętrzny: `https://api.pradcast.pl` (klucz `X-API-Key`).
- Pobierana doba dostawy: **D+1** (jutro).
- Model zwraca **24 ceny godzinowe** w **PLN/MWh**.
- Przed zapisem: konwersja do **96 kwadransów** (każda godzina → 4 × 15 min z tą samą ceną).
- W bazie: `cena1` = liczba całkowita, **grosze za 1 MWh** (`PLN/MWh * 100`), np. `576,46` → `57646`.

### 2.2. Cena realna (FIXING)
- Mail z tematem zawierającym `FIXING` → parser 96 × 15 min.
- Cena w mailu w PLN/MWh → zapis jako **int grosze/MWh** (to samo co wyżej).
- UPSERT do `t_rdn` z `is_forecast=0`.

### 2.3. Strefa czasowa
- Rynek / doba dostawy: **Europe/Warsaw** (doba handlowa RDN).
- Slot `czas` to lokalny czas doby dostawy (`HH:MM:SS`), nie UTC.

---

## 3. API cen — `GET /api/tge`

### Request
```
GET /api/tge?date=YYYY-MM-DD
```

| Parametr | Wymagany | Opis |
|----------|----------|------|
| `date` | nie | Data doby dostawy `YYYY-MM-DD`. Brak/błąd → **jutro** (D+1). |

Źródło odpowiedzi: wyłącznie tabela **`t_rdn`** (nie wywołuje pradcast na żywo).

### Response (aktualny kształt)
```json
{
  "ceny_dla_dnia": "2026-07-19",
  "godziny": [
    {
      "godzina": "00:00",
      "fixing1": { "cena": "576,46", "vol": "1234" },
      "fixing2": { "cena": "0,00", "vol": "0" }
    },
    {
      "godzina": "00:15",
      "fixing1": { "cena": "576,46", "vol": "1234" },
      "fixing2": { "cena": "0,00", "vol": "0" }
    }
  ]
}
```

### Pola istotne dla BESS

| Pole | Znaczenie |
|------|-----------|
| `ceny_dla_dnia` | Doba dostawy (data) |
| `godziny[].godzina` | Początek slotu `HH:MM` (zwykle co 15 min) |
| `godziny[].fixing1.cena` | **Cena do użycia w harmonogramie** — Fixing I, string z przecinkiem, **PLN/MWh** |
| `godziny[].fixing1.vol` | Wolumen Fixing I (może być `"None"` / `"0"` przy prognozie) |
| `godziny[].fixing2.*` | Fixing II — zwykle mniej istotny dla BESS; przy prognozie często 0 |

**Uwaga:** API **nie zwraca dziś** pola `is_forecast`. Agent nie rozróżni w JSON prognozy od Fixingu — w bazie flaga jest, w odpowiedzi `/api/tge` jeszcze nie. Dla slotów przyszłych przed Fixingiem zakładaj, że to prognoza lub mieszanka; po Fixingu — cena realna.

### Konwersja ceny do float (Python)
```python
price_pln_mwh = float(item["fixing1"]["cena"].replace(",", "."))
# opcjonalnie do PLN/kWh:
price_pln_kwh = price_pln_mwh / 1000.0
```

### Agregacja do 24h (jeśli optymalizator chce godziny, nie kwadranse)
Dla każdej godziny `H` weź cenę ze slotu `H:00` (lub średnią z `H:00..H:45` — przy prognozie wszystkie 4 są równe; przy Fixingu 15-min mogą się różnić).

```python
# przykład: mapa godzina -> cena Fixing1
hourly = {}
for row in data["godziny"]:
    hh, mm = row["godzina"].split(":")
    if mm == "00":
        hourly[int(hh)] = float(row["fixing1"]["cena"].replace(",", "."))
# hourly[0]..hourly[23] w PLN/MWh
```

---

## 4. Jednostki — ważne

| Warstwa | Jednostka |
|---------|-----------|
| Baza `t_rdn.cena1` | **grosze / MWh** (int), np. `57646` |
| API `/api/tge` `fixing1.cena` | **PLN / MWh** (string z przecinkiem), np. `"576,46"` |
| `/api/optimize` `market_price` | tablica 24 × float — w praktyce **PLN/kWh** w logice taryfy (spread + opłata dystrybucyjna dodawane do tej skali) |

Przy budowie `market_price` do optymalizacji BESS z `/api/tge`:
```text
market_price[h] = (PLN/MWh z fixing1) / 1000   →  PLN/kWh
```
albo trzymaj cały optymalizator w PLN/MWh — ale **nie mieszaj skal**.

---

## 5. Endpoint optymalizacji BESS — `POST /api/optimize`

Osobny endpoint (nie pobiera sam cen z bazy — klient musi podać `market_price`).

### Request (JSON)
```json
{
  "battery_capacity": 100,
  "soc_0": 50,
  "charge_power": 50,
  "discharge_power": 50,
  "efficiency": 0.85,
  "pv": [ /* 24 float kWh lub kW·h slotu */ ],
  "load": [ /* 24 float */ ],
  "market_price": [ /* 24 float — ta sama skala co w modelu taryfy */ ],
  "tariff_type": "rdn",
  "fixed_price": 0.85,
  "distribution_fee": 0.25
}
```

| `tariff_type` | Zachowanie |
|---------------|------------|
| `"rdn"` (domyślnie) | `buy = market + 0.03 + distribution_fee`, `sell = max(market - 0.03, 0)`; sprzedaż do sieci możliwa |
| inne / `"fixed"` | stała cena zakupu, sprzedaż wyłączona |

Horyzont wewnętrzny: **dokładnie 24 sloty** (godziny 0..23).

### Response
- `schedule[]` — per godzina: pobór/oddanie sieci, ładowanie/rozładowanie BESS, SOC, ceny buy/sell,
- `summary` — koszty, arbitrage, uniknięte zakupy.

---

## 6. Jak agent BESS powinien używać cen (rekomendacja)

1. Wywołaj `GET /api/tge?date=<doba_dostawy>` (zwykle jutro).
2. Weź **`fixing1.cena`** jako cenę rynkową RDN (PLN/MWh).
3. Zbuduj wektor 24h (z kwadransów) w skali zgodnej z optymalizatorem.
4. Do `POST /api/optimize` przekaż `market_price` + progn. PV + load + parametry baterii.
5. Interpretuj `schedule`: ładuj przy niskiej cenie / nadmiarze PV, rozładowuj przy wysokiej cenie / deficycie.

**Ograniczenia dzisiejszego API cen:**
- zwraca **jedną dobę**, nie rolling 36h,
- nie oznacza w JSON, czy slot to prognoza czy Fixing,
- rozdzielczość w odpowiedzi to zwykle **15 min** (do 96 punktów na dobę).

**Kierunek docelowy (w toku):** to samo `/api/tge` ma zwracać **rolling 36 godzin** z `t_rdn` (mieszanka realnych cen + prognoz w jednej tablicy). Do czasu wdrożenia agent bierze jedną dobę D+1 (lub wskazaną `date`).

---

## 7. Przykład wywołania

```bash
# ceny na jutro (domyślnie też jutro bez parametru)
curl -s "https://<host>/api/tge?date=2026-07-19"
```

```python
import requests

r = requests.get("https://<host>/api/tge", params={"date": "2026-07-19"}, timeout=30)
data = r.json()
prices_mwh = [
    float(x["fixing1"]["cena"].replace(",", "."))
    for x in data["godziny"]
    if x["godzina"].endswith(":00")
]  # 24 wartości PLN/MWh
market_price_kwh = [p / 1000.0 for p in prices_mwh]
```

---

## 8. Szybka ściąga dla agenta

- **Cena sterująca BESS:** `fixing1.cena` z `/api/tge`.
- **Jednostka w API:** PLN/MWh (przecinek dziesiętny).
- **Skąd:** DB `t_rdn` = Fixing (mail/HTML) po publikacji, wcześniej prognoza pradcast D+1.
- **Nie wołaj pradcast bezpośrednio** z agenta BESS — korzystaj z `/api/tge`.
- **Optymalizacja:** `/api/optimize` wymaga 24-elementowego `market_price` + PV + load + parametry BESS.
- **Timezone:** Europe/Warsaw, doba dostawy RDN.
