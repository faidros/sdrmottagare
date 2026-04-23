"""
Autotrack – Automatisk satellitspaning (läge 12)

Hämdef build_schedule(lat: float, lon: float, elev: int,
                   min_el: float, hours: int = 24) -> list[dict]:
    """
    Hämta TLE för ISS, Meteor och NOAA 15/18/19 och returnera en
    tidsordnad lista med pass med maxelevation >= min_el inom `hours` timmar.
    Varje post: {sat, aos, los, max_el, dur_s, tle}
    """
    from modes.satellite import fetch_tle as met_fetch, find_passes as met_passes, METEOR_NAME
    from modes.iss import fetch_tle as iss_fetch, find_passes as iss_passes
    from modes.noaa import fetch_all_tles as noaa_fetch, find_passes as noaa_passesör ISS och Meteor-M2-3, beräknar alla kommande pass de
närmaste 24 timmarna, filtrerar på valbar minimielevation och kör
sedan automatiskt rätt mottagare vid varje AOS:

  ISS       → rtl_fm | multimon-ng (APRS) + audio.raw sparas
  Meteor    → satdump live meteor_m2-x_lrpt (PNG-bilder sparas)

Programmet loopar tills användaren trycker Ctrl+C.
"""

import json
import ssl
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import ephem
except ImportError:
    ephem = None


# ── Beroenden (återanvänds från de enskilda lägena) ────────────────────────────
# Vi importerar sent (inside run_autotrack) för att undvika cirkulära importer.

CONFIG_FILE = Path.home() / ".sdrmottagare.json"

# Minimielevationer för respektive kvalitetsmärke
QUAL_THRESHOLDS = {
    "bra":  40,   # 🟢
    "ok":   20,   # 🟡
    "alla": 10,   # 🔴 + 🟡 + 🟢
}

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
        return "🟢 Bra "
    elif elev >= 20:
        return "🟡 OK  "
    else:
        return "🔴 Låg "


def build_schedule(lat: float, lon: float, elev: int,
                   min_el: float, hours: int = 24) -> list[dict]:
    """
    Hämta TLE för båda satelliterna och returnera en tidsordnad lista
    med pass med maxelevation >= min_el inom de närmaste `hours` timmarna.
    Varje post: {sat, aos, los, max_el, dur_s, tle}
    """
    from modes.satellite import fetch_tle as met_fetch, find_passes as met_passes, METEOR_NAME
    from modes.iss import fetch_tle as iss_fetch, find_passes as iss_passes

    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours)

    results = []

    # ── Meteor-M2-3 ────────────────────────────────────────────────
    met_tle = met_fetch(METEOR_NAME)
    if met_tle:
        for p in met_passes(lat, lon, elev, met_tle, count=20):
            if p["aos"] > cutoff:
                break
            if p["max_el"] >= min_el and p["aos"] >= now - timedelta(minutes=1):
                results.append({**p, "sat": "meteor", "tle": met_tle})
    else:
        print("  ⚠️  Kunde inte hämta Meteor TLE.")

    # ── ISS ────────────────────────────────────────────────────────
    iss_tle = iss_fetch()
    if iss_tle:
        for p in iss_passes(lat, lon, elev, iss_tle, count=30):
            if p["aos"] > cutoff:
                break
            if p["max_el"] >= min_el and p["aos"] >= now - timedelta(minutes=1):
                results.append({**p, "sat": "iss", "tle": iss_tle})
    else:
        print("  ⚠️  Kunde inte hämta ISS TLE.")

    # ── NOAA 15 / 18 / 19 ─────────────────────────────────────────
    noaa_tles = noaa_fetch()
    for noaa_name, noaa_tle in noaa_tles.items():
        sat_key = noaa_name.lower().replace(" ", "_")  # "noaa_15" etc
        for p in noaa_passes(lat, lon, elev, noaa_tle, noaa_name, count=8):
            if p["aos"] > cutoff:
                break
            if p["max_el"] >= min_el and p["aos"] >= now - timedelta(minutes=1):
                results.append({**p, "sat": sat_key, "tle": noaa_tle})

    results.sort(key=lambda x: x["aos"])
    return results


def print_schedule(schedule: list[dict]):
    now = datetime.now(timezone.utc)
    print(f"\n  {'#':<3}  {'Satellit':<14}  {'Datum':<7}  {'AOS–LOS':<13}  "
          f"{'Dur':<8}  {'Max el':<7}  {'Kvalitet':<8}  Väntan")
    print("  " + "─" * 72)
    for i, p in enumerate(schedule, 1):
        if p["sat"] == "iss":
            name = "🛸 ISS"
        elif p["sat"] == "meteor":
            name = "🛰️  Meteor"
        elif p["sat"] == "noaa_15":
            name = "📡 NOAA 15"
        elif p["sat"] == "noaa_18":
            name = "📡 NOAA 18"
        elif p["sat"] == "noaa_19":
            name = "� NOAA 19"
        else:
            name = p["sat"]
        date    = p["aos"].astimezone().strftime("%d %b")
        aos_s   = p["aos"].astimezone().strftime("%H:%M")
        los_s   = p["los"].astimezone().strftime("%H:%M")
        dur     = f"{p['dur_s']//60}m{p['dur_s']%60:02d}s"
        qual    = qual_symbol(p["max_el"])
        delta_s = (p["aos"] - now).total_seconds()
        if delta_s < 0:
            wait = "  pågår  "
        elif delta_s < 3600:
            wait = f"  om {int(delta_s//60)}m{int(delta_s%60):02d}s"
        else:
            h2 = int(delta_s // 3600)
            m2 = int((delta_s % 3600) // 60)
            wait = f"  om {h2}h{m2:02d}m  "
        print(f"  {i:<3}  {name:<14}  {date:<7}  {aos_s}–{los_s:<5}  "
              f"{dur:<8}  {p['max_el']:4.0f}°   {qual}  {wait}")
    print("  " + "─" * 72)


def run_autotrack(settings: dict | None = None):
    settings = settings or {}

    print("\n" + "=" * 60)
    print(" 🔭 Autotrack – automatisk satellitspaning")
    print(" ISS (145 MHz) + Meteor-M2-3 (137.9 MHz) + NOAA 15/18/19 (137 MHz)")
    print("=" * 60)

    if ephem is None:
        print("\n  ❌ Python-paketet 'ephem' saknas.  pip install ephem")
        return

    print("""
  Programmet beräknar alla kommande pass och startar
  automatiskt rätt mottagare vid varje AOS:

    🛸 ISS          → APRS-paket + audio.raw (~/sdr_data/iss/)
    🛰️  Meteor-M2-3  → PNG-bilder via SatDump (~/sdr_bilder/meteor/)
    📡 NOAA 15/18/19 → APT-bilder via SatDump (~/sdr_bilder/noaa/)

  Kör tills du trycker Ctrl+C.
""")

    # ── Kvalitetströskel ───────────────────────────────────────────
    print("  Minimikvalitet att spela in?")
    print("  1. 🟢 Bra  (>40° – högsta chans att lyckas, färre pass)")
    print("  2. 🟡 OK   (>20° – fler pass, något sämre kvalitet)  [standard]")
    print("  3. 🔴 Alla (>10° – maximalt antal pass)")
    print("\n  Val [2]: ", end="")
    try:
        val = input().strip() or "2"
    except (EOFError, KeyboardInterrupt):
        print("\n  Avbruten.")
        return
    min_el = {"1": 40, "2": 20, "3": 10}.get(val, 20)
    qual_name = {"1": "🟢 Bra (>40°)", "2": "🟡 OK (>20°)", "3": "🔴 Alla (>10°)"}.get(val, "🟡 OK (>20°)")
    print(f"\n  Filtrerar: {qual_name}\n")

    # ── Position ────────────────────────────────────────────────────
    cfg = load_config()
    if "lat" not in cfg:
        print("  ❌ Ingen position sparad. Kör läge 10 eller 11 först för att spara position.")
        return
    lat  = cfg["lat"]
    lon  = cfg["lon"]
    elev = cfg.get("elevation", 0)
    print(f"  Position: {lat:.4f}°N  {lon:.4f}°E  ({elev} m ö.h.)")

    # ── Hämta schema ────────────────────────────────────────────────
    print("\n  Hämtar TLE och beräknar pass för närmaste 24 h...")
    schedule = build_schedule(lat, lon, elev, min_el, hours=24)

    if not schedule:
        print(f"\n  ⚠️  Inga pass med {qual_name} hittades de närmaste 24 timmarna.")
        print("  Prova en lägre kvalitetströskel eller en annan tid.")
        return

    print(f"\n  Hittade {len(schedule)} pass att bevaka:\n")
    print_schedule(schedule)
    print(f"\n  Tryck Enter för att starta autotrack, eller Ctrl+C för att avbryta: ", end="")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print("\n  Avbruten.")
        return

    # ── Huvudloop ────────────────────────────────────────────────────
    from modes.iss import receive_pass as iss_receive
    from modes.satellite import countdown_and_record as met_receive
    from modes.noaa import record_pass as noaa_receive

    def sat_label(sat: str) -> str:
        return {"iss": "🛸 ISS", "meteor": "🛰️  Meteor-M2-3",
                "noaa_15": "📡 NOAA 15", "noaa_18": "📡 NOAA 18",
                "noaa_19": "📡 NOAA 19"}.get(sat, sat.upper())

    completed = 0
    for idx, p in enumerate(schedule):
        now = datetime.now(timezone.utc)

        # Hoppa över pass som redan passerat
        if p["los"] < now:
            print(f"\n  ⏭  Pass {idx+1}/{len(schedule)} ({sat_label(p['sat'])}) har redan passerat, hoppar över.")
            continue

        remaining = len(schedule) - idx - 1
        print(f"\n{'═'*60}")
        print(f"  Pass {idx+1}/{len(schedule)}  –  {sat_label(p['sat'])}")
        print(f"  {remaining} pass kvar efter detta")
        print(f"{'═'*60}")

        try:
            if p["sat"] == "iss":
                iss_receive(p, settings)
            elif p["sat"] == "meteor":
                met_receive(p, settings)
            else:
                # NOAA 15/18/19 – record_pass förväntar sig freq i p
                noaa_receive(p, settings)
            completed += 1
        except KeyboardInterrupt:
            print(f"\n\n  Autotrack avbruten efter {completed} avklarade pass.")
            return

        # Kort paus mellan pass
        if idx < len(schedule) - 1:
            next_p  = schedule[idx + 1]
            next_aos = next_p["aos"].astimezone().strftime("%H:%M")
            gap_s    = (next_p["aos"] - datetime.now(timezone.utc)).total_seconds()
            gap_m    = int(gap_s // 60)
            print(f"\n  ✅ Pass klart. Nästa: {sat_label(next_p['sat'])} kl {next_aos} (om {gap_m} min)")
            print("  (Ctrl+C för att avbryta autotrack)\n")

    print(f"\n{'═'*60}")
    print(f"  🏁 Autotrack klar!  {completed} av {len(schedule)} pass genomförda.")
    print(f"  ISS-data:      ~/sdr_data/iss/")
    print(f"  Meteorbilder:  ~/sdr_bilder/meteor/")
    print(f"  NOAA-bilder:   ~/sdr_bilder/noaa/")
    print(f"{'═'*60}\n")
