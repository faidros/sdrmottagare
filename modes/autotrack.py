"""
Autotrack – Automatisk satellitspaning (läge 12)
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import ephem
except ImportError:
    ephem = None

CONFIG_FILE = Path.home() / ".sdrmottagare.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def qual_symbol(elev: float) -> str:
    if elev >= 40:
        return "Bra "
    elif elev >= 20:
        return "OK  "
    else:
        return "Lag "


def sat_label(sat: str) -> str:
    return {
        "iss":     "ISS",
        "meteor":  "Meteor-M2-3",
        "noaa_15": "NOAA 15",
        "noaa_18": "NOAA 18",
        "noaa_19": "NOAA 19",
    }.get(sat, sat.upper())


def build_schedule(lat: float, lon: float, elev: int,
                   min_el: float, hours: int = 24) -> list:
    from modes.satellite import fetch_tle as met_fetch, find_passes as met_passes, METEOR_NAME
    from modes.iss import fetch_tle as iss_fetch, find_passes as iss_passes
    from modes.noaa import fetch_all_tles as noaa_fetch, find_passes as noaa_passes

    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours)
    results = []

    met_tle = met_fetch(METEOR_NAME)
    if met_tle:
        for p in met_passes(lat, lon, elev, met_tle, count=20):
            if p["aos"] > cutoff:
                break
            if p["max_el"] >= min_el and p["aos"] >= now - timedelta(minutes=1):
                results.append({**p, "sat": "meteor", "tle": met_tle})
    else:
        print("  Kunde inte hamta Meteor TLE.")

    iss_tle = iss_fetch()
    if iss_tle:
        for p in iss_passes(lat, lon, elev, iss_tle, count=30):
            if p["aos"] > cutoff:
                break
            if p["max_el"] >= min_el and p["aos"] >= now - timedelta(minutes=1):
                results.append({**p, "sat": "iss", "tle": iss_tle})
    else:
        print("  Kunde inte hamta ISS TLE.")

    noaa_tles = noaa_fetch()
    for noaa_name, noaa_tle in noaa_tles.items():
        sat_key = noaa_name.lower().replace(" ", "_")
        for p in noaa_passes(lat, lon, elev, noaa_tle, noaa_name, count=8):
            if p["aos"] > cutoff:
                break
            if p["max_el"] >= min_el and p["aos"] >= now - timedelta(minutes=1):
                results.append({**p, "sat": sat_key, "tle": noaa_tle})

    results.sort(key=lambda x: x["aos"])
    return results


def print_schedule(schedule: list):
    now = datetime.now(timezone.utc)
    print(f"\n  {'#':<3}  {'Satellit':<14}  {'Datum':<7}  {'AOS-LOS':<11}  "
          f"{'Dur':<7}  {'Max el':<7}  {'Kval':<5}  Vantan")
    print("  " + "-" * 72)
    for i, p in enumerate(schedule, 1):
        date  = p["aos"].astimezone().strftime("%d %b")
        aos_s = p["aos"].astimezone().strftime("%H:%M")
        los_s = p["los"].astimezone().strftime("%H:%M")
        dur   = f"{p['dur_s']//60}m{p['dur_s']%60:02d}s"
        qual  = qual_symbol(p["max_el"])
        ds    = (p["aos"] - now).total_seconds()
        if ds < 0:
            wait = "pagar"
        elif ds < 3600:
            wait = f"om {int(ds//60)}m{int(ds%60):02d}s"
        else:
            wait = f"om {int(ds//3600)}h{int((ds%3600)//60):02d}m"
        print(f"  {i:<3}  {sat_label(p['sat']):<14}  {date:<7}  {aos_s}-{los_s:<5}  "
              f"{dur:<7}  {p['max_el']:4.0f} deg  {qual}  {wait}")
    print("  " + "-" * 72)


def run_autotrack(settings=None):
    settings = settings or {}

    print("\n" + "=" * 60)
    print(" Autotrack - automatisk satellitspaning (lage 12)")
    print(" ISS (145 MHz) + Meteor-M2-3 (137.9 MHz) + NOAA 15/18/19")
    print("=" * 60)

    if ephem is None:
        print("\n  Paketet 'ephem' saknas.  pip install ephem")
        return

    print("\n  Minimikvalitet att spela in?")
    print("  1. Bra  (>40 grader)")
    print("  2. OK   (>20 grader) [standard]")
    print("  3. Alla (>10 grader)")
    print("\n  Val [2]: ", end="", flush=True)
    try:
        val = input().strip() or "2"
    except (EOFError, KeyboardInterrupt):
        print("\n  Avbruten.")
        return
    min_el    = {"1": 40.0, "2": 20.0, "3": 10.0}.get(val, 20.0)
    qual_name = {"1": "Bra (>40)", "2": "OK (>20)", "3": "Alla (>10)"}.get(val, "OK (>20)")
    print(f"\n  Filtrerar: {qual_name}\n")

    cfg = load_config()
    if "lat" not in cfg:
        print("  Ingen position sparad. Kor lage 10, 11 eller 13 forst.")
        return
    lat  = cfg["lat"]
    lon  = cfg["lon"]
    elev = cfg.get("elevation", 0)
    print(f"  Position: {lat:.4f} N  {lon:.4f} E  ({elev} m)")

    print("\n  Hamtar TLE och beraknar pass for narmaste 24 h...")
    schedule = build_schedule(lat, lon, elev, min_el, hours=24)

    if not schedule:
        print(f"\n  Inga pass med {qual_name} de narmaste 24 timmarna.")
        return

    print(f"\n  Hittade {len(schedule)} pass:\n")
    print_schedule(schedule)

    print("\n  Tryck Enter for att starta autotrack, Ctrl+C for att avbryta: ", end="", flush=True)
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print("\n  Avbruten.")
        return

    from modes.iss import receive_pass as iss_receive
    from modes.satellite import countdown_and_record as met_receive
    from modes.noaa import record_pass as noaa_receive

    completed = 0
    for idx, p in enumerate(schedule):
        now = datetime.now(timezone.utc)

        if p["los"] < now:
            print(f"\n  {sat_label(p['sat'])} har redan passerat, hoppar over.")
            continue

        print(f"\n{'='*60}")
        print(f"  Pass {idx+1}/{len(schedule)}  --  {sat_label(p['sat'])}")
        print(f"{'='*60}")

        try:
            if p["sat"] == "iss":
                iss_receive(p, settings)
            elif p["sat"] == "meteor":
                met_receive(p, settings)
            else:
                noaa_receive(p, settings)
            completed += 1
        except KeyboardInterrupt:
            print(f"\n\n  Autotrack avbruten efter {completed} avklarade pass.")
            return

        if idx < len(schedule) - 1:
            next_p   = schedule[idx + 1]
            next_aos = next_p["aos"].astimezone().strftime("%H:%M")
            gap_m    = int((next_p["aos"] - datetime.now(timezone.utc)).total_seconds() // 60)
            print(f"\n  Klart. Nasta: {sat_label(next_p['sat'])} kl {next_aos} (om {gap_m} min)")

    print(f"\n{'='*60}")
    print(f"  Autotrack klar!  {completed} av {len(schedule)} pass genomforda.")
    print(f"  ISS-data:     ~/sdr_data/iss/")
    print(f"  Meteorbilder: ~/sdr_bilder/meteor/")
    print(f"  NOAA-bilder:  ~/sdr_bilder/noaa/")
    print(f"{'='*60}\n")
