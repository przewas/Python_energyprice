from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import re
import os
import json
from pvlib.location import Location
from pvlib.pvsystem import PVSystem
from pvlib.modelchain import ModelChain
from pvlib.irradiance import disc

app = Flask(__name__)

# Konfiguracja CORS: zezwól na wszystkie metody i nagłówki
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)
BIAS_FILE = os.path.join(os.path.dirname(__file__), "bias_store.json")
FORECAST_TZ = "Europe/Warsaw"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARS = (
    "temperature_2m,"
    "windspeed_10m,"
    "direct_normal_irradiance,"
    "diffuse_radiation,"
    "shortwave_radiation,"
    "weathercode"
)
FORWARD_HORIZON_HOURS = 25  # bieżąca godzina + 24h do przodu (rolling 24h)

print("BIAS PATH:", BIAS_FILE)


def load_bias_store():
    if not os.path.exists(BIAS_FILE):
        return {}
    try:
        with open(BIAS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_bias_store(store):
    tmp_file = BIAS_FILE + ".tmp"
    with open(tmp_file, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp_file, BIAS_FILE)


def _truthy(val):
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _want_forward_24h(payload, installation=None):
    """forward_24h: query ?forward_24h=1, pole top-level (single) lub per instalacja (batch)."""
    if _truthy(request.args.get("forward_24h")):
        return True
    if installation is not None and installation.get("forward_24h") is not None:
        return _truthy(installation.get("forward_24h"))
    if isinstance(payload, dict) and payload.get("forward_24h") is not None:
        return _truthy(payload.get("forward_24h"))
    return False


def _open_meteo_multi(lats, lons, **extra_params):
    params = {
        "latitude": ",".join(map(str, lats)),
        "longitude": ",".join(map(str, lons)),
        "hourly": HOURLY_VARS,
    }
    params.update(extra_params)
    resp = requests.get(OPEN_METEO_URL, params=params, timeout=90).json()
    if not isinstance(resp, list):
        resp = [resp]
    return resp


def _weather_dataframe(hourly_raw, from_utc=True):
    weather = pd.DataFrame(hourly_raw)
    if from_utc:
        weather["time"] = pd.to_datetime(weather["time"], utc=True)
        weather = weather.set_index("time").tz_convert(FORECAST_TZ)
    else:
        weather["time"] = pd.to_datetime(weather["time"])
        if weather["time"].dt.tz is None:
            weather["time"] = weather["time"].dt.tz_localize(FORECAST_TZ)
        else:
            weather["time"] = weather["time"].dt.tz_convert(FORECAST_TZ)
        weather = weather.set_index("time")

    weather = weather.rename(
        columns={
            "shortwave_radiation": "ghi",
            "direct_normal_irradiance": "dni",
            "diffuse_radiation": "dhi",
            "temperature_2m": "temp_air",
            "windspeed_10m": "wind_speed",
            "weathercode": "weather_code",
        }
    )
    return weather


def _resolve_bias(bias_store, id_instalacji, incoming_bias):
    if id_instalacji and incoming_bias:
        bias_store[str(id_instalacji)] = incoming_bias
        save_bias_store(bias_store)
        return incoming_bias
    if id_instalacji and str(id_instalacji) in bias_store:
        return bias_store[str(id_instalacji)]
    return {}


def _compute_pv_hourly(lat, lon, tilt, azimuth, power_dc, power_ac, weather, bias):
    """Model PV (pvlib) — zwraca listę słowników godzinowych."""
    weather_for_output = weather.copy()
    location = Location(lat, lon, tz=FORECAST_TZ)

    cs = location.get_clearsky(weather.index)
    low_sky = weather["ghi"] < 0.3 * cs["ghi"]
    weather = weather.copy()
    weather.loc[low_sky, "dni"] *= 0.15

    solpos = location.get_solarposition(weather.index)
    weather["dhi"] = weather["ghi"] - weather["dni"] * np.cos(np.radians(solpos["zenith"]))
    weather["dhi"] = weather["dhi"].clip(lower=0)

    system = PVSystem(
        surface_tilt=tilt,
        surface_azimuth=azimuth,
        module_parameters={"pdc0": power_dc * 1000, "gamma_pdc": -0.004},
        inverter_parameters={"pdc0": power_ac * 1000},
        racking_model="open_rack",
        module_type="glass_polymer",
    )

    mc = ModelChain(
        system,
        location,
        aoi_model="physical",
        spectral_model="no_loss",
        ac_model="pvwatts",
        losses_model="pvwatts",
        transposition_model="perez",
    )

    mc.run_model(weather)

    weather["ac_forecast"] = mc.results.ac / 1000
    weather["ac_forecast"] = np.minimum(weather["ac_forecast"], power_ac)

    solpos_cs = location.get_solarposition(cs.index)
    dni_clear = disc(cs["ghi"], solpos_cs["zenith"], cs.index)

    cs = cs.copy()
    cs["dni"] = dni_clear["dni"]
    cs["dhi"] = cs["ghi"] - cs["dni"] * np.cos(np.radians(solpos_cs["zenith"]))
    cs["dhi"] = cs["dhi"].clip(lower=0)
    cs["temp_air"] = weather["temp_air"]
    cs["wind_speed"] = weather["wind_speed"]

    mc.run_model(cs)

    weather["ac_clear"] = mc.results.ac / 1000
    weather["ac_clear"] = np.minimum(weather["ac_clear"], power_ac)

    ratio = weather["ghi"].sum() / cs["ghi"].sum() if cs["ghi"].sum() > 0 else 1

    if ratio > 0.75:
        bucket = "high"
    elif ratio > 0.4:
        bucket = "mid"
    else:
        bucket = "low"

    ratio_bias = bias.get("ratio", {}).get(bucket, 1.0)
    weather["ac_forecast"] *= ratio_bias

    hourly_result = []
    for t in weather.index:
        hourly_result.append(
            {
                "time": t.isoformat(),
                "forecast_kW": round(float(weather.loc[t, "ac_forecast"]), 3),
                "clear_sky_kW": round(float(weather.loc[t, "ac_clear"]), 3),
                "ratio_bias": round(float(ratio_bias), 4),
                "ghi": round(float(weather_for_output.loc[t, "ghi"]), 1),
                "temp_air": round(float(weather_for_output.loc[t, "temp_air"]), 1),
                "wind_speed": round(float(weather_for_output.loc[t, "wind_speed"]), 1),
                "weather_code": int(weather_for_output.loc[t, "weather_code"]),
            }
        )

    return hourly_result


def _slice_forward_24h(hourly_rows):
    """Z forecast_hours=25 zostaw 24 sloty rolling [start, start+24h)."""
    if len(hourly_rows) <= 24:
        return hourly_rows
    return hourly_rows[:24]


@app.after_request
def after_request(response):
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    return response


@app.route("/", methods=["POST", "OPTIONS"])
def forecast():
    if request.method == "OPTIONS":
        response = make_response()
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response, 200


@app.route("/api/forecast", methods=["POST"])
def forecast_pv():
    try:
        data = request.get_json(force=True)

        # ================= SINGLE / BATCH =================

        if isinstance(data, list):
            installations = data
            is_batch = True
            payload_root = None
        else:
            installations = [data]
            is_batch = False
            payload_root = data

        if len(installations) == 0:
            return jsonify({"error": "Empty input"}), 400

        date = installations[0].get("date", datetime.now().strftime("%Y-%m-%d"))

        lats = [float(inst.get("lat")) for inst in installations]
        lons = [float(inst.get("lon")) for inst in installations]

        # --- legacy: jedna doba (UTC) — bez zmian dla starych klientów ---
        legacy_resp = _open_meteo_multi(
            lats,
            lons,
            start_date=date,
            end_date=date,
            timezone="UTC",
        )

        need_forward = any(
            _want_forward_24h(payload_root, inst) for inst in installations
        )
        forward_resp = None
        if need_forward:
            forward_resp = _open_meteo_multi(
                lats,
                lons,
                forecast_hours=FORWARD_HORIZON_HOURS,
                timezone=FORECAST_TZ,
            )

        bias_store = load_bias_store()
        final_results = []

        for idx, inst in enumerate(installations):
            lat = float(inst.get("lat"))
            lon = float(inst.get("lon"))
            tilt = float(inst.get("tilt", 30))
            azimuth = float(inst.get("azimuth", 180))
            power_dc = float(inst.get("power_dc", 10))
            power_ac = float(inst.get("power_ac", 10))
            id_instalacji = inst.get("id_instalacji")
            incoming_bias = inst.get("bias")

            bias = _resolve_bias(bias_store, id_instalacji, incoming_bias)

            # Legacy prognoza (24h dla parametru date — jak dotychczas)
            weather_legacy = _weather_dataframe(legacy_resp[idx]["hourly"], from_utc=True)
            hourly_result = _compute_pv_hourly(
                lat, lon, tilt, azimuth, power_dc, power_ac, weather_legacy, bias
            )

            result_item = {
                "id_instalacji": id_instalacji,
                "forecast": hourly_result,
            }

            # Rolling 24h do przodu (opcjonalnie)
            if need_forward and forward_resp is not None:
                inst_wants_forward = _want_forward_24h(payload_root, inst)
                if inst_wants_forward:
                    weather_fwd = _weather_dataframe(
                        forward_resp[idx]["hourly"], from_utc=False
                    )
                    forward_rows = _compute_pv_hourly(
                        lat, lon, tilt, azimuth, power_dc, power_ac, weather_fwd, bias
                    )
                    forward_rows = _slice_forward_24h(forward_rows)
                    result_item["forecast_forward_24h"] = forward_rows
                    if forward_rows:
                        result_item["forecast_forward_meta"] = {
                            "hours": len(forward_rows),
                            "start": forward_rows[0]["time"],
                            "end": forward_rows[-1]["time"],
                            "timezone": FORECAST_TZ,
                        }

            final_results.append(result_item)

        # ================= RETURN =================

        if is_batch:
            return jsonify(final_results)

        single = final_results[0]
        if _want_forward_24h(payload_root, installations[0]):
            return jsonify(
                {
                    "forecast": single["forecast"],
                    "forecast_forward_24h": single.get("forecast_forward_24h", []),
                    "forecast_forward_meta": single.get("forecast_forward_meta"),
                }
            )

        # Kompatybilność wsteczna: single bez forward_24h → sama tablica godzin
        return jsonify(single["forecast"])

    except Exception as e:
        return jsonify({"error": str(e)}), 500


from tge_get_from_db import get_tge_prices_data, get_tge_prices_rolling


@app.route("/api/tge")
def get_tge_prices():
    """
    Domyślnie: rolling 36h godzinowych cen z t_rdn + is_forecast/zrodlo.

    Query:
      hours=36          — długość horyzontu (1..72), domyślnie 36
      mode=day&date=... — stary tryb: jedna doba (wszystkie sloty 15-min)
    """
    try:
        mode = (request.args.get("mode") or "rolling").strip().lower()

        if mode in ("day", "doba", "legacy"):
            raw_date = request.args.get("date")
            date = None
            if raw_date:
                m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw_date)
                if m:
                    try:
                        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                        date = dt.strftime("%Y-%m-%d")
                    except Exception:
                        pass
            if not date:
                date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            return jsonify(get_tge_prices_data(date))

        hours_raw = request.args.get("hours", "36")
        try:
            hours = int(hours_raw)
        except (TypeError, ValueError):
            hours = 36

        return jsonify(get_tge_prices_rolling(horizon_hours=hours))

    except Exception as e:
        return jsonify({"error": str(e)}), 500


import cvxpy as cp


@app.route("/api/optimize", methods=["POST"])
def optimize_energy_flow():
    try:
        data = request.get_json(force=True)

        capacity = float(data["battery_capacity"])
        soc_0 = float(data["soc_0"])
        p_charge = float(data["charge_power"])
        p_discharge = float(data["discharge_power"])
        eff = float(data.get("efficiency", 0.85))

        pv = np.array(data["pv"])
        load = np.array(data["load"])
        market_price = np.array(data["market_price"])

        tariff_type = data.get("tariff_type", "rdn")
        fixed_price = float(data.get("fixed_price", 0.85))
        dist_fee = float(data.get("distribution_fee", 0.25))

        if not (len(pv) == len(load) == len(market_price) == 24):
            return jsonify({"error": "Tablice pv, load i market_price muszą mieć po 24 elementy."}), 400

        if tariff_type == "rdn":
            buy_price = np.maximum(market_price + 0.03 + dist_fee, 0.0)
            sell_price = np.maximum(market_price - 0.03, 0.0)
        else:
            buy_price = np.full(24, fixed_price + dist_fee)
            sell_price = np.zeros(24)

        n = 24
        grid_buy = cp.Variable(n, nonneg=True)
        grid_sell = cp.Variable(n, nonneg=True)
        bat_charge = cp.Variable(n, nonneg=True)
        bat_discharge = cp.Variable(n, nonneg=True)
        curtail = cp.Variable(n, nonneg=True)
        soc = cp.Variable(n + 1)

        constraints = [soc[0] == soc_0]

        for i in range(n):
            pv_used = load[i] - bat_discharge[i] - grid_buy[i]
            constraints += [
                pv[i] == pv_used + bat_charge[i] + grid_sell[i] + curtail[i],
                soc[i + 1] == soc[i] + bat_charge[i] * eff - bat_discharge[i] / eff,
                soc[i + 1] >= 0,
                soc[i + 1] <= capacity,
                bat_charge[i] <= p_charge,
                bat_discharge[i] <= p_discharge,
            ]
            if tariff_type == "fixed" or sell_price[i] <= buy_price[i]:
                constraints += [grid_sell[i] == 0]

        total_cost = cp.sum(grid_buy * buy_price - grid_sell * sell_price)
        prob = cp.Problem(cp.Minimize(total_cost), constraints)
        prob.solve()

        if prob.status != cp.OPTIMAL:
            return jsonify({"error": "Optymalizacja nie powiodła się", "status": prob.status}), 400

        result = []
        for i in range(n):
            result.append(
                {
                    "godzina": int(i),
                    "pobrano_z_sieci": float(round(grid_buy.value[i], 4)),
                    "oddano_do_sieci": float(round(grid_sell.value[i], 4)),
                    "magazyn_ladowanie": float(round(bat_charge.value[i], 4)),
                    "magazyn_rozladowanie": float(round(bat_discharge.value[i], 4)),
                    "pv": float(round(pv[i], 4)),
                    "ograniczenie_pv": float(round(curtail.value[i], 4)),
                    "soc": float(round(soc.value[i + 1], 4)),
                    "cena_zakupu": float(round(buy_price[i], 4)),
                    "cena_sprzedzazy": float(round(sell_price[i], 4)),
                }
            )

        grid_buy_val = np.array(grid_buy.value)
        grid_sell_val = np.array(grid_sell.value)
        bat_charge_val = np.array(bat_charge.value)
        bat_discharge_val = np.array(bat_discharge.value)

        ref_cost_variable = float(np.sum(load * (market_price + 0.08)))
        ref_cost_fixed = float(np.sum(load * fixed_price))

        actual_cost = float(np.sum(grid_buy_val * buy_price))
        charge_from_grid = np.minimum(bat_charge_val, grid_buy_val)
        charge_cost = float(np.sum(charge_from_grid * buy_price))

        discharge_to_grid = np.minimum(grid_sell_val, bat_discharge_val)
        discharge_revenue = float(np.sum(discharge_to_grid * sell_price))
        arbitrage_profit = discharge_revenue - charge_cost

        discharge_used_locally = load - grid_buy_val - pv
        discharge_used_locally = np.clip(discharge_used_locally, 0, bat_discharge_val)

        avoided_purchase = discharge_used_locally * buy_price
        avoided_cost = float(np.sum(avoided_purchase))

        pv_used_direct = load - grid_buy_val - bat_discharge_val
        pv_used_direct = np.clip(pv_used_direct, 0, None)
        pv_saving = float(np.sum(pv_used_direct * buy_price))

        total_energy_charged = float(np.sum(bat_charge_val))
        total_energy_discharged = float(np.sum(bat_discharge_val))
        avg_charge_cost_per_kwh = charge_cost / total_energy_charged if total_energy_charged > 0 else 0
        avg_discharge_revenue_per_kwh = (
            discharge_revenue / total_energy_discharged if total_energy_discharged > 0 else 0
        )

        summary = {
            "stan_referecyjny": {
                "koszt_energii_taryfa_zmienna": round(ref_cost_variable, 2),
                "koszt_energii_taryfa_stala": round(ref_cost_fixed, 2),
            },
            "strategia": {
                "koszt_energia_zakupiona": round(actual_cost, 2),
                "przychod_energia_sprzedana": round(discharge_revenue, 2),
                "przychod_zakup_unikniety_pobor_z_magazynu": round(avoided_cost, 2),
                "przychod_zakup_unikniety_pobor_z_pv": round(pv_saving, 2),
            },
            "magazyn": {
                "ladowanie_kWh": round(total_energy_charged, 2),
                "koszt_ladowania": round(charge_cost, 2),
                "koszt_ladowania_kWh": round(avg_charge_cost_per_kwh, 4),
                "rozladowanie_kWh": round(total_energy_discharged, 2),
                "koszt_rozladowania": round(discharge_revenue, 2),
                "koszt_rozladowania_kWh": round(avg_discharge_revenue_per_kwh, 4),
                "zysk_arbitraz": round(arbitrage_profit, 2),
            },
        }

        return jsonify({"schedule": result, "summary": summary})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


import mysql.connector

DB_CONFIG = {
    "host": "przewas.mysql.pythonanywhere-services.com",
    "user": "przewas",
    "password": "Ka$zanka77",
    "database": "przewas$EnGrid",
    "autocommit": True,
}

API_TOKEN = "Yf9CXWStD3ZY0XpU"


def db_conn():
    return mysql.connector.connect(**DB_CONFIG)


def check_auth():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth.split(" ", 1)[1].strip()
    return token == API_TOKEN


@app.route("/api/liczniki", methods=["GET"])
def get_liczniki():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        conn = db_conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT id, device_id, channel_id,
                   total_cost, total_forward_active_energy, total_reverse_active_energy,
                   phase1_voltage, phase1_current, phase1_power_active,
                   phase2_voltage, phase2_current, phase2_power_active,
                   phase3_voltage, phase3_current, phase3_power_active
            FROM t_liczniki
            ORDER BY id ASC
            """
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/liczniki_history", methods=["GET"])
def get_liczniki_history():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    today_flag = request.args.get("today")
    date_from = request.args.get("from")
    date_to = request.args.get("to")

    if today_flag == "1":
        now_pl = datetime.utcnow() + timedelta(hours=2)
        date_from = now_pl.strftime("%Y-%m-%d 00:00:00")
        date_to = now_pl.strftime("%Y-%m-%d 23:59:59")
    else:
        if date_from and len(date_from) == 10:
            date_from = f"{date_from} 00:00:00"
        if date_to and len(date_to) == 10:
            date_to = f"{date_to} 23:59:59"

    sql = """
        SELECT id, device_id, channel_id,
               (reading_time + INTERVAL 2 HOUR) AS reading_time,
               total_cost, total_forward_active_energy, total_reverse_active_energy,
               phase1_voltage, phase1_current, phase1_power_active,
               phase2_voltage, phase2_current, phase2_power_active,
               phase3_voltage, phase3_current, phase3_power_active
        FROM t_liczniki_history
    """
    params = []
    if date_from and date_to:
        sql += " WHERE (reading_time + INTERVAL 2 HOUR) BETWEEN %s AND %s"
        params = [date_from, date_to]
    elif date_from:
        sql += " WHERE (reading_time + INTERVAL 2 HOUR) >= %s"
        params = [date_from]
    elif date_to:
        sql += " WHERE (reading_time + INTERVAL 2 HOUR) <= %s"
        params = [date_to]

    sql += " ORDER BY reading_time ASC"

    try:
        conn = db_conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        for row in rows:
            val = row.get("reading_time")
            if val:
                try:
                    if hasattr(val, "strftime"):
                        row["reading_time"] = val.strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        row["reading_time"] = str(val).split(".")[0].replace("T", " ")[:19]
                except Exception:
                    row["reading_time"] = None

        return jsonify(rows)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run()