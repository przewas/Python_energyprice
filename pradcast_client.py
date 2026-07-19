"""Klient API pradcast.pl — ceny RDN i prognozy."""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta
from typing import Any

import requests

PRADCAST_BASE_URL = os.getenv("PRADCAST_BASE_URL", "https://api.pradcast.pl").rstrip("/")
PRADCAST_API_KEY = os.getenv("PRADCAST_API_KEY", "pcast_wD078XoBM1XJ9TiH21MFT6L4PqyrK8je")
REQUEST_TIMEOUT = float(os.getenv("PRADCAST_TIMEOUT", "30"))


class PradcastAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if PRADCAST_API_KEY:
        headers["X-API-Key"] = PRADCAST_API_KEY
    return headers


def _request_json(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{PRADCAST_BASE_URL}{path}"
    try:
        response = requests.get(
            url,
            headers=_headers(),
            params=params or None,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise PradcastAPIError(f"Błąd połączenia z pradcast.pl: {exc}") from exc

    if response.status_code == 404:
        raise PradcastAPIError(
            f"Brak danych pradcast.pl dla {path}",
            status_code=404,
        )
    if response.status_code == 401:
        raise PradcastAPIError(
            "Nieautoryzowany dostęp do pradcast.pl — ustaw PRADCAST_API_KEY "
            "(klucz z https://pradcast.pl/dokumentacja-api).",
            status_code=401,
        )
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "?")
        raise PradcastAPIError(
            f"Limit zapytań pradcast.pl (429). Retry-After: {retry_after}",
            status_code=429,
        )
    if not response.ok:
        detail = response.text[:300]
        raise PradcastAPIError(
            f"pradcast.pl HTTP {response.status_code}: {detail}",
            status_code=response.status_code,
        )

    payload = response.json()
    if not isinstance(payload, dict):
        raise PradcastAPIError("Nieoczekiwany format odpowiedzi pradcast.pl (oczekiwano obiektu JSON).")
    return payload


def fetch_prices_by_date(
    target_date: str,
    *,
    source: str | None = None,
    horizon: str | None = None,
) -> dict[str, Any]:
    params: dict[str, str] = {}
    if source:
        params["source"] = source
    if horizon:
        params["horizon"] = horizon
    return _request_json(f"/prices/date/{target_date}", params=params or None)


def fetch_actual_prices() -> dict[str, Any]:
    return _request_json("/prices/actual")


def fetch_all_forecasts(target_date: str) -> dict[str, Any]:
    return _request_json(f"/prices/forecasts/{target_date}")


def fetch_forecast_on_demand(target_date: str, *, horizon: str | None = None) -> dict[str, Any]:
    params = {"horizon": horizon} if horizon else None
    return _request_json(f"/prices/forecast/{target_date}", params=params)


def _is_confirmed_rdn(payload: dict[str, Any]) -> bool:
    source = str(payload.get("source", "")).lower()
    return "tge" in source or "fixing" in source or source in {"rdn", "actual", "confirmed"}


def fetch_confirmed_rdn(target_date: str) -> dict[str, Any] | None:
    """Potwierdzone ceny RDN (TGE Fixing I) dla daty lub None jeśli jeszcze nie opublikowane."""
    try:
        payload = fetch_prices_by_date(target_date, source="tge_fixing1")
        if _is_confirmed_rdn(payload):
            return payload
    except PradcastAPIError as exc:
        if exc.status_code == 404:
            return None
        raise

    actual = fetch_actual_prices()
    if str(actual.get("date")) == target_date and _is_confirmed_rdn(actual):
        return actual
    return None


def _extract_price_response(payload: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(payload.get("prices"), list):
        return payload

    forecasts = payload.get("forecasts")
    if isinstance(forecasts, dict):
        for key in ("D+1", "D+2", "D+3"):
            candidate = forecasts.get(key)
            if isinstance(candidate, dict) and isinstance(candidate.get("prices"), list):
                return candidate
        for candidate in forecasts.values():
            if isinstance(candidate, dict) and isinstance(candidate.get("prices"), list):
                return candidate

    for key in ("forecast", "data"):
        candidate = payload.get(key)
        if isinstance(candidate, dict) and isinstance(candidate.get("prices"), list):
            return candidate

    return None


def fetch_forecast_for_date(target_date: str) -> dict[str, Any]:
    """Prognoza modelu pradcast.pl dla wskazanej doby dostawy."""
    errors: list[str] = []

    try:
        bundle = fetch_all_forecasts(target_date)
        extracted = _extract_price_response(bundle)
        if extracted:
            return extracted
        errors.append("brak listy prices w /prices/forecasts")
    except PradcastAPIError as exc:
        if exc.status_code != 404:
            raise
        errors.append(str(exc))

    for horizon in (None, "D+1", "D+2", "D+3"):
        try:
            payload = fetch_prices_by_date(
                target_date,
                source="forecast_model",
                horizon=horizon,
            )
            if isinstance(payload.get("prices"), list):
                return payload
        except PradcastAPIError as exc:
            if exc.status_code == 404:
                errors.append(f"date/forecast_model horizon={horizon}: 404")
                continue
            raise

    try:
        payload = fetch_forecast_on_demand(target_date)
        if isinstance(payload.get("prices"), list):
            return payload
    except PradcastAPIError as exc:
        if exc.status_code != 404:
            raise
        errors.append(str(exc))

    raise PradcastAPIError(
        f"Nie udało się pobrać prognozy pradcast.pl dla {target_date}. "
        + "; ".join(errors)
    )


def _price_to_grosze_mwh(price_pln_mwh: float) -> int:
    return int(round(float(price_pln_mwh) * 100))


def hourly_prices_to_rdn_records(price_response: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Konwersja 24 cen godzinowych (PLN/MWh) na 96 rekordów 15-minutowych do t_rdn.
    Każdy kwadrans dziedziczy cenę swojej godziny (jak przy braku rozdzielczości 15 min w API).
    """
    date_str = str(price_response["date"])
    base_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    prices_by_hour: dict[int, dict[str, Any]] = {}

    for entry in price_response.get("prices", []):
        prices_by_hour[int(entry["hour"])] = entry

    if len(prices_by_hour) != 24:
        raise ValueError(
            f"Oczekiwano 24 cen godzinowych z pradcast.pl, otrzymano {len(prices_by_hour)}."
        )

    records: list[dict[str, Any]] = []
    for hour in range(24):
        price_grosze = _price_to_grosze_mwh(prices_by_hour[hour]["price"])
        for quarter in range(4):
            minute = quarter * 15
            start_time = time(hour, minute)
            end_minute_total = hour * 60 + minute + 15
            end_hour = (end_minute_total // 60) % 24
            end_minute = end_minute_total % 60
            end_time = time(end_hour, end_minute)
            start_dt = datetime.combine(base_date, start_time)

            records.append(
                {
                    "czas_ceny": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "doba": date_str,
                    "czas": start_time.strftime("%H:%M:%S"),
                    "czas_do": end_time.strftime("%H:%M:%S"),
                    "interwal_minut": 15,
                    "cena1": price_grosze,
                    "cena2": 0,
                }
            )

    return records


def hourly_prices_to_forecast_rows(price_response: dict[str, Any]) -> list[tuple[str, str, float]]:
    """Wiersze (forecast_date, hour, price_grosze) do t_tge_forecast."""
    date_str = str(price_response["date"])
    rows: list[tuple[str, str, float]] = []

    for entry in sorted(price_response.get("prices", []), key=lambda item: int(item["hour"])):
        hour = int(entry["hour"])
        rows.append((date_str, f"{hour:02d}:00:00", float(_price_to_grosze_mwh(entry["price"]))))

    if len(rows) != 24:
        raise ValueError(f"Oczekiwano 24 prognoz godzinowych, otrzymano {len(rows)}.")

    return rows


def default_rdn_import_date() -> str:
    """Domyślna doba importu RDN — jutro (rynek dnia następnego)."""
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def default_forecast_date() -> str:
    return default_rdn_import_date()


def delivery_date_d1(*, as_of: datetime | None = None) -> str:
    """Data dostawy D+1 względem as_of (domyślnie teraz, lokalnie)."""
    base = as_of or datetime.now()
    return (base + timedelta(days=1)).strftime("%Y-%m-%d")


def fetch_forecast_d1(*, as_of: datetime | None = None) -> dict[str, Any]:
    """
    Pobiera prognozę modelu pradcast.pl dla D+1.

    Zwraca:
      {
        "as_of": "...",
        "horizon": "D+1",
        "forecast": { date, source, currency, unit, prices[24] }
      }
    """
    d1 = delivery_date_d1(as_of=as_of)
    return {
        "as_of": (as_of or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        "horizon": "D+1",
        "forecast": fetch_forecast_for_date(d1),
    }