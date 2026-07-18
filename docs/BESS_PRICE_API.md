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
pradcast.pl (prognoza D+1) ──► t_rdn (is_forecast=1)
                                      ▲
mail FIXING / tge.html (realne) ──────┘  (is_forecast=0, nadpisuje prognozę)
                                      │
                                      ▼
                               GET /api/tge
                                      │
                                      ▼
                               agent / harmonogram BESS
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

### Request (domyślny — rolling 36h)
```
GET /api/tge
GET /api/tge?hours=36
```

| Parametr | Wymagany | Opis |
|----------|----------|------|
| `hours` | nie | Liczba godzin horyzontu (1..72). Domyślnie **36**. |
| `mode=day&date=YYYY-MM-DD` | nie | Stary tryb: jedna doba, wszystkie sloty 15-min. |

Źródło odpowiedzi: wyłącznie tabela **`t_rdn`** (nie wywołuje pradcast na żywo).
Start horyzontu: **bieżąca godzina** w `Europe/Warsaw` (zaokrąglenie w dół do pełnej godziny).
Rozdzielczość: **1 godzina** (slot `:00` z bazy; baza trzyma też kwadranse).

### Response (rolling 36)
```json
{
  "horizon_hours": 36,
  "timezone": "Europe/Warsaw",
  "start": "2026-07-18 15:00:00",
  "end": "2026-07-20 03:00:00",
  "count": 36,
  "available": 34,
  "real_count": 10,
  "forecast_count": 24,
  "godziny": [
    {
      "doba": "2026-07-18",
      "czas": "15:00:00",
      "czas_ceny": "2026-07-18 15:00:00",
      "godzina": "15:00",
      "fixing1": { "cena": "420,00", "vol": "100" },
      "fixing2": { "cena": "0,00", "vol": "0" },
      "is_forecast": false,
      "zrodlo": "realna"
    },
    {
      "doba": "2026-07-19",
      "czas": "10:00:00",
      "czas_ceny": "2026-07-19 10:00:00",
      "godzina": "10:00",
      "fixing1": { "cena": "80,56", "vol": "0" },
      "fixing2": { "cena": "0,00", "vol": "0" },
      "is_forecast": true,
      "zrodlo": "prognoza"
    }
  ]
}
```

### Pola istotne dla BESS

| Pole | Znaczenie |
|------|-----------|
| `godziny` | Zawsze **36** elementów (lub `hours`) — po jednej cenie na godzinę |
| `godziny[].czas_ceny` | Data+czas slotu (PL) |
| `godziny[].fixing1.cena` | **Cena do harmonogramu** — PLN/MWh, string z przecinkiem; `null` jeśli brak w DB |
| `godziny[].is_forecast` | `false` = cena realna (FIXING), `true` = prognoza, `null` = brak danych |
| `godziny[].zrodlo` | `"realna"` \| `"prognoza"` \| `"brak"` — wygodna etykieta dla agenta |

### Konwersja ceny do float (Python)
```python
raw = item["fixing1"]["cena"]
if raw is None:
    price_pln_mwh = None
else:
    price_pln_mwh = float(raw.replace(",", "."))
    price_pln_kwh = price_pln_mwh / 1000.0
```

### Wektor pod optymalizator (pierwsze 24h z rolling 36)
```python
market_price_kwh = []
for row in data["godziny"][:24]:
    raw = row["fixing1"]["cena"]
    if raw is None:
        raise ValueError(f"Brak ceny dla {row['czas_ceny']}")
    market_price_kwh.append(float(raw.replace(",", ".")) / 1000.0)
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

1. Wywołaj `GET /api/tge` (rolling **36** godzin od teraz).
2. Dla każdego slotu weź **`fixing1.cena`** (PLN/MWh) oraz **`is_forecast` / `zrodlo`**.
3. Do `POST /api/optimize` (24h) użyj pierwszych 24 slotów; reszta 12h = kontekst / plan dalszy.
4. Slotów z `zrodlo="brak"` nie używaj w optymalizacji bez fallbacku.
5. `zrodlo="prognoza"` — cena niepewna; `zrodlo="realna"` — Fixing, wyższy priorytet pewności.

---

## 7. Przykład wywołania

```bash
curl -s "https://<host>/api/tge"
curl -s "https://<host>/api/tge?hours=36"
```

```python
import requests

r = requests.get("https://<host>/api/tge", timeout=30)
data = r.json()
assert data["count"] == 36

for row in data["godziny"]:
    print(row["czas_ceny"], row["fixing1"]["cena"], row["zrodlo"], row["is_forecast"])

market_price_kwh = [
    float(x["fixing1"]["cena"].replace(",", ".")) / 1000.0
    for x in data["godziny"][:24]
    if x["fixing1"]["cena"] is not None
]
```

---

## 8. Szybka ściąga dla agenta

- **Endpoint:** `GET /api/tge` → **36 godzin** + `is_forecast` / `zrodlo`.
- **Cena:** `fixing1.cena` w PLN/MWh (przecinek).
- **Skąd:** DB `t_rdn` = Fixing albo prognoza pradcast D+1.
- **Nie wołaj pradcast bezpośrednio** — tylko `/api/tge`.
- **Optymalizacja:** `/api/optimize` bierze 24× `market_price` (zwykle z pierwszych 24 slotów).
- **Timezone:** Europe/Warsaw.
