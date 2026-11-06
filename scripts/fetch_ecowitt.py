#!/usr/bin/env python3
"""Fetch Ecowitt real-time data and publish a sanitised metric snapshot."""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

API_ENDPOINT = "https://api.ecowitt.net/api/v3/device/real_time"
CALLBACKS = "outdoor,wind,rainfall,rainfall_piezo,pressure,solar_and_uvi"
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
TRANSIENT_ECOWITT_CODES = {-1, 45001}
MAX_RESPONSE_BYTES = 1_000_000
MAX_OBSERVATION_AGE_SECONDS = 6 * 60 * 60
FUTURE_TOLERANCE_SECONDS = 10 * 60

class FeedError(RuntimeError):
    """A safe-to-log feed error which never includes request credentials."""

def utc_iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat().replace("+00:00", "Z")

def finite_number(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None

def normalise_unit(unit: Any) -> str:
    return (str(unit or "").strip().lower().replace("℃", "c").replace("°c", "c")
            .replace("℉", "f").replace("°f", "f").replace("²", "2")
            .replace("³", "3").replace(" ", ""))

def convert_temperature(value: float, unit: str) -> float:
    if unit in {"f", "fahrenheit"}:
        return (value - 32.0) * 5.0 / 9.0
    if unit in {"", "c", "celsius"}:
        return value
    raise FeedError("Ecowitt returned an unsupported temperature unit")

def convert_rainfall(value: float, unit: str) -> float:
    if unit in {"", "mm", "mm/h", "mm/hr", "mm/hour"}:
        return value
    if unit in {"in", "inch", "inches", "in/h", "in/hr", "in/hour"}:
        return value * 25.4
    raise FeedError("Ecowitt returned an unsupported rainfall unit")

def convert_wind(value: float, unit: str) -> float:
    if unit in {"", "km/h", "kmh", "kph"}:
        return value
    if unit in {"m/s", "mps"}:
        return value * 3.6
    if unit in {"mph", "mi/h"}:
        return value * 1.609344
    if unit in {"kt", "kts", "knot", "knots"}:
        return value * 1.852
    raise FeedError("Ecowitt returned an unsupported wind-speed unit")

def convert_pressure(value: float, unit: str) -> float:
    if unit in {"", "hpa", "mbar"}:
        return value
    if unit == "kpa":
        return value * 10.0
    if unit == "pa":
        return value / 100.0
    if unit in {"inhg", "in/hg"}:
        return value * 33.8638866667
    if unit == "mmhg":
        return value * 1.33322387415
    raise FeedError("Ecowitt returned an unsupported pressure unit")

def convert_identity(value: float, _unit: str) -> float:
    return value

def read_metric(group: Any, key: str,
                converter: Callable[[float, str], float] = convert_identity,
                digits: int = 1) -> tuple[float | int | None, int | None]:
    item = group.get(key) if isinstance(group, dict) else None
    if not isinstance(item, dict):
        return None, None
    value = finite_number(item.get("value"))
    converted = None if value is None else converter(value, normalise_unit(item.get("unit")))
    if converted is not None:
        converted = round(converted, digits)
        if digits == 0:
            converted = int(converted)
    timestamp = finite_number(item.get("time"))
    return converted, int(timestamp) if timestamp is not None else None

def newest_timestamp(group: Any) -> int:
    if not isinstance(group, dict):
        return 0
    times = []
    for item in group.values():
        if isinstance(item, dict):
            timestamp = finite_number(item.get("time"))
            if timestamp is not None:
                times.append(int(timestamp))
    return max(times, default=0)

def select_rain_group(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    candidates = []
    for key, label in (("rainfall", "tipping_bucket"), ("rainfall_piezo", "piezo")):
        group = data.get(key)
        if isinstance(group, dict) and group:
            populated = sum(finite_number(item.get("value")) is not None
                            for item in group.values() if isinstance(item, dict))
            candidates.append((newest_timestamp(group), populated, label, group))
    if not candidates:
        raise FeedError("Ecowitt returned no rainfall sensor data")
    _newest, _populated, label, group = max(candidates, key=lambda item: item[:3])
    return label, group

def build_snapshot(payload: Any, now: float | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise FeedError("Ecowitt returned an unsuccessful response")
    data = payload.get("data")
    if not isinstance(data, dict) or not data:
        raise FeedError("Ecowitt returned no weather data")
    rain_label, rain = select_rain_group(data)
    outdoor = data.get("outdoor") or {}
    wind = data.get("wind") or {}
    pressure = data.get("pressure") or {}
    solar = data.get("solar_and_uvi") or {}
    timestamps: list[int] = []

    def metric(group: Any, key: str,
               converter: Callable[[float, str], float] = convert_identity,
               digits: int = 1) -> float | int | None:
        value, timestamp = read_metric(group, key, converter, digits)
        if timestamp is not None:
            timestamps.append(timestamp)
        return value

    rainfall = {
        "rate_mm_per_hour": metric(rain, "rain_rate", convert_rainfall),
        "event_mm": metric(rain, "event", convert_rainfall),
        "hourly_mm": metric(rain, "hourly", convert_rainfall),
        "daily_mm": metric(rain, "daily", convert_rainfall),
        "weekly_mm": metric(rain, "weekly", convert_rainfall),
        "monthly_mm": metric(rain, "monthly", convert_rainfall),
        "yearly_mm": metric(rain, "yearly", convert_rainfall),
    }
    outdoor_values = {
        "temperature_c": metric(outdoor, "temperature", convert_temperature),
        "feels_like_c": metric(outdoor, "feels_like", convert_temperature),
        "apparent_temperature_c": metric(outdoor, "app_temp", convert_temperature),
        "dew_point_c": metric(outdoor, "dew_point", convert_temperature),
        "humidity_percent": metric(outdoor, "humidity", digits=0),
    }
    wind_values = {
        "speed_kmh": metric(wind, "wind_speed", convert_wind),
        "gust_kmh": metric(wind, "wind_gust", convert_wind),
        "direction_degrees": metric(wind, "wind_direction", digits=0),
    }
    pressure_values = {
        "relative_hpa": metric(pressure, "relative", convert_pressure),
        "absolute_hpa": metric(pressure, "absolute", convert_pressure),
    }
    solar_values = {
        "irradiance_w_m2": metric(solar, "solar"),
        "uv_index": metric(solar, "uvi", digits=0),
    }
    if not any(value is not None for value in rainfall.values()):
        raise FeedError("Ecowitt returned no usable rainfall readings")
    weather_groups = (outdoor_values, wind_values, pressure_values, solar_values)
    if not any(value is not None for group in weather_groups for value in group.values()):
        raise FeedError("Ecowitt returned no usable weather readings")
    server_timestamp = finite_number(payload.get("time"))
    if not timestamps and server_timestamp is not None:
        timestamps.append(int(server_timestamp))
    if not timestamps:
        raise FeedError("Ecowitt returned weather data without timestamps")
    current_time = time.time() if now is None else now
    observed_at = max(timestamps)
    data_age = int(current_time - observed_at)
    if data_age > MAX_OBSERVATION_AGE_SECONDS:
        raise FeedError("Ecowitt's latest observation is stale")
    if data_age < -FUTURE_TOLERANCE_SECONDS:
        raise FeedError("Ecowitt returned an invalid future timestamp")
    return {
        "schema_version": 1,
        "published_at_utc": utc_iso(current_time),
        "observed_at_utc": utc_iso(observed_at),
        "data_age_seconds": max(0, data_age),
        "rain_sensor": rain_label,
        "rainfall": rainfall,
        "outdoor": outdoor_values,
        "wind": wind_values,
        "pressure": pressure_values,
        "solar": solar_values,
    }

def fetch_payload(app_key: str, api_key: str, mac: str) -> dict[str, Any]:
    parameters = {
        "application_key": app_key,
        "api_key": api_key,
        "mac": mac,
        "call_back": CALLBACKS,
        "temp_unitid": "1",
        "pressure_unitid": "3",
        "wind_speed_unitid": "7",
        "rainfall_unitid": "12",
    }
    request = urllib.request.Request(
        f"{API_ENDPOINT}?{urllib.parse.urlencode(parameters)}",
        headers={"Accept": "application/json", "User-Agent": "altona-weather-feed/1"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise FeedError("Ecowitt returned an unexpectedly large response")
            payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code in TRANSIENT_HTTP_CODES and attempt < 2:
                time.sleep(2**attempt)
                continue
            raise FeedError(f"Ecowitt request failed with HTTP status {exc.code}") from None
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise FeedError("Ecowitt request failed due to a network error") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise FeedError("Ecowitt returned invalid JSON") from None
        if isinstance(payload, dict) and payload.get("code") in TRANSIENT_ECOWITT_CODES:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else "unknown"
            raise FeedError(f"Ecowitt API error code {code}")
        return payload
    raise FeedError("Ecowitt request failed after retries")

def required_environment() -> tuple[str, str, str]:
    names = ("ECOWITT_APP_KEY", "ECOWITT_API_KEY", "ECOWITT_MAC")
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        raise FeedError("Missing required GitHub Actions secret(s): " + ", ".join(missing))
    return tuple(os.environ[name].strip() for name in names)  # type: ignore[return-value]

def verify_sanitised(snapshot: dict[str, Any], secrets: tuple[str, str, str]) -> None:
    public_text = json.dumps(snapshot, sort_keys=True)
    if any(secret and secret in public_text for secret in secrets):
        raise FeedError("Refusing to publish a snapshot containing a secret")
    forbidden_keys = {"application_key", "api_key", "mac", "imei", "latitude",
                      "longitude", "location", "address"}
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in forbidden_keys:
                    raise FeedError("Refusing to publish a private field")
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(snapshot)

def write_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)

def main() -> int:
    output_path = Path(sys.argv[1] if len(sys.argv) > 1 else "weather.json")
    try:
        secrets = required_environment()
        payload = fetch_payload(*secrets)
        snapshot = build_snapshot(payload)
        verify_sanitised(snapshot, secrets)
        write_snapshot(output_path, snapshot)
    except FeedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote sanitised weather snapshot to {output_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
