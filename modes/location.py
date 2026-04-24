"""
location.py – gemensam positionsinmatning för SDR Mottagare

Stöder tre sätt att ange position:
  1. Sparat sedan tidigare  → visa och bekräfta
  2. Postnummer (5 siffror) → slå upp lat/lon i CSV-filen
  3. Koordinater manuellt   → latitud + longitud i decimalgrader
"""

import csv
import json
import re
from pathlib import Path

CONFIG_FILE = Path.home() / ".sdrmottagare.json"
CSV_FILE    = Path(__file__).parent.parent / "Pnr-Ort-Kommun-KnKod-LnNamn-Lat-Long-GM_202409.csv"

# Läs in CSV en gång vid import (lazy)
_pnr_cache: dict | None = None


def _load_pnr() -> dict:
    global _pnr_cache
    if _pnr_cache is not None:
        return _pnr_cache
    _pnr_cache = {}
    if not CSV_FILE.exists():
        return _pnr_cache
    with open(CSV_FILE, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                pnr = str(row["Postnummer"]).strip().zfill(5)
                lat = float(row["Latitude"])
                lon = float(row["Longitude"])
                ort = row["Ort"].strip().title()
                _pnr_cache[pnr] = {"lat": lat, "lon": lon, "ort": ort}
            except (ValueError, KeyError):
                pass
    return _pnr_cache


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def ask_position(cfg: dict) -> dict:
    """
    Fråga efter position om den inte redan är sparad.
    Accepterar postnummer (Sverige) eller koordinater (var som helst).
    """
    if "lat" in cfg and "lon" in cfg:
        lat  = cfg["lat"]
        lon  = cfg["lon"]
        elev = cfg.get("elevation", 0)
        ort  = cfg.get("ort", "")
        ort_str = f"  ({ort})" if ort else ""
        print(f"\n  Sparad position: {lat:.4f}°N  {lon:.4f}°E  {elev} m ö.h.{ort_str}")
        print("  Tryck Enter för att använda, eller 'c' för att ändra: ", end="", flush=True)
        if input().strip().lower() != "c":
            return cfg

    pnr_db = _load_pnr()
    har_csv = bool(pnr_db)

    print("\n  Ange din position (används för passprediktion):")
    if har_csv:
        print("  • Postnummer (5 siffror, t.ex. 11220 för Stockholm)")
        print("  • Eller koordinater: latitud longitud  (t.ex. 59.33 18.07)")
    else:
        print("  • Koordinater: latitud longitud  (t.ex. 59.33 18.07)")
    print()

    lat = lon = None
    ort = ""

    while lat is None:
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Avbruten.")
            raise

        # ── Postnummer: 5 siffror (ev. med mellanslag: "123 45") ──────────────
        pnr_clean = re.sub(r"\s", "", raw)
        if re.fullmatch(r"\d{5}", pnr_clean) and har_csv:
            entry = pnr_db.get(pnr_clean.zfill(5))
            if entry:
                lat, lon, ort = entry["lat"], entry["lon"], entry["ort"]
                print(f"  → {ort}  ({lat:.4f}°N  {lon:.4f}°E)")
            else:
                print(f"  ⚠️  Postnummer {pnr_clean} hittades inte i databasen.")
                print("     Ange koordinater istället (lat lon): ", end="", flush=True)
            continue

        # ── Koordinater: två tal separerade med mellanslag eller komma ────────
        parts = re.split(r"[\s,]+", raw)
        if len(parts) == 2:
            try:
                lat_try = float(parts[0].replace(",", "."))
                lon_try = float(parts[1].replace(",", "."))
                if -90 <= lat_try <= 90 and -180 <= lon_try <= 180:
                    lat, lon = lat_try, lon_try
                    continue
            except ValueError:
                pass

        print("  Förstod inte – ange postnummer (5 siffror) eller koordinater (t.ex. 59.33 18.07)")

    # ── Höjd ──────────────────────────────────────────────────────────────────
    try:
        elev_raw = input("  Höjd över havet i meter (tryck Enter för 0): ").strip()
        elev = int(elev_raw) if elev_raw else 0
    except ValueError:
        elev = 0

    cfg.update({"lat": lat, "lon": lon, "elevation": elev, "ort": ort})
    save_config(cfg)

    ort_str = f"  ({ort})" if ort else ""
    print(f"\n  ✅ Position sparad: {lat:.4f}°N  {lon:.4f}°E  {elev} m ö.h.{ort_str}")
    return cfg
