import requests
import pandas as pd

latitude = 52.23      # Warszawa
longitude = 21.01
start_date = "2024-01-01"
end_date = "2025-06-14"

url = (
    f"https://api.open-meteo.com/v1/forecast?"
    f"latitude={latitude}&longitude={longitude}"
    f"&hourly=temperature_2m,shortwave_radiation,windspeed_10m"
    f"&start_date={start_date}&end_date={end_date}"
    f"&timezone=Europe%2FWarsaw"
)

response = requests.get(url)
data = response.json()

df = pd.DataFrame({
    "datetime": data["hourly"]["time"],
    "temperature_C": data["hourly"]["temperature_2m"],
    "ghi_Wm2": data["hourly"]["shortwave_radiation"],
    "wind_speed_mps": data["hourly"]["windspeed_10m"]
})
df["datetime"] = pd.to_datetime(df["datetime"])
df.to_csv("weather_forecast.csv", index=False)

print("✅ Zapisano plik weather_forecast.csv")
