# Altona weather feed

This repository publishes a small, privacy-safe snapshot from a private Ecowitt weather station. GitHub Actions refreshes [`weather.json`](weather.json) every two hours and can also be run manually.

The public file contains rainfall totals and rate, outdoor temperature and humidity, wind, pressure, solar readings when available, and UTC timestamps.

It deliberately does **not** publish the Ecowitt application key, API key, station MAC address, coordinates, street address, or raw Ecowitt response.

## Automation

The workflow is defined in [`.github/workflows/ecowitt.yml`](.github/workflows/ecowitt.yml). It calls Ecowitt's `device/real_time` API with metric units, validates and normalises the response, and commits only the sanitised `weather.json` file. Invalid or stale data leaves the last good snapshot untouched.

Required encrypted GitHub Actions repository secrets:

- `ECOWITT_APP_KEY`
- `ECOWITT_API_KEY`
- `ECOWITT_MAC`

Secret values must never be committed to this repository or pasted into an issue, pull request, workflow log, or chat.

## Public data format

The generated JSON uses `schema_version: 1`. Measurements use Celsius, millimetres, kilometres per hour, hectopascals, degrees, percent, and watts per square metre. A missing optional sensor is represented by `null`.
