"""
NOAA APT – Vädersatellitbilder (137 MHz)

NOAA 15, 18 och 19 är amerikanska vädersatelliter i låg polär bana (~850 km).
De sänder APT (Automatic Picture Transmission) kontinuerligt på 137 MHz –
en enkel analog AM-bildsignal med ~4 km/pixel upplösning i svartvitt
(eller falskfärg om SatDump enhancear bilden).

Jämfört med Meteor-M2-3:
  ✅ Tre satelliter → fler pass per dygn
  ✅ Enklare signal, tåligare mot svag mottagning
  ✅ Samma 137 MHz V-dipol fungerar perfekt
  ❌ Lägre upplösning (~4 km/pixel mot Meteors ~1 km/pixel)

Frekvenser:
  NOAA 15: 137.620 MHz  (NORAD 25338)
  NOAA 18: 137.9125 MHz (NORAD 28654)
  NOAA 19: 137.100 MHz  (NORAD 33591)

Flöde:
  ephem (passprediktion) → val av pass → nedräkning → satdump live noaa_apt → PNG

Krav:
  pip install ephem
  satdump  (brew install satdump + symlänkar, se README)
"""

import json
import os
import ssl
import subprocess
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import ephem
except ImportError:
    ephem = None

# ── Konfiguration ──────────────────────────────────────────────────────────────

CONFIG_FILE = Path.home() / ".sdrmottagare.json"
IMAGES_DIR  = Path.home() / "sdr_bilder" / "noaa"

# Individuella TLE-URL:er per NORAD-ID (15/18/19 är ej längre i weather-gruppen)
NOAA_SATS = {
    "NOAA 15": {"freq": 137_620_000,  "norad_id": "25338"},
    "NOAA 18": {"freq": 137_912_500,  "norad_id": "28654"},
    "NOAA 19": {"freq": 137_100_000,  "norad_id": "33591"},
}

SAMPLERATE   = 1_200_000   # 1.2 Msps – fungerar för APT (~40 kHz bandbredd)
MIN_ELEVATION = 10          # Lägsta maxelevation vi visar


# ── Konfigurationshjälpare ─────────────────────────────────────────────────────

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
    if "lat" in cfg and "lon" in cfg:
        lat, lon = cfg["lat"], cfg["lon"]
        elev = cfg.get("elevation", 0)
        print(f"\n  Sparad position: {lat:.4f}°N  {lon:.4f}°E  ({elev} m ö.h.)")
        print("  Tryck Enter för att använda, eller 'c' för att ändra: ", end="")
        if input().strip().lower() != "c":
            return cfg

    print("\n  Ange din position:")
    try:
        lat  = float(input("  Latitud  (°N, t.ex. 59.33): "))
        lon  = float(input("  Longitud (°E, t.ex. 18.07): "))
        elev = int(input("  Höjd (m ö.h., Enter=0): ").strip() or "0")
    except ValueError:
        print("  Ogiltigt värde, använder Stockholm.")
        lat, lon, elev = 59.33, 18.07, 20

    cfg.update({"lat": lat, "lon": lon, "elevation": elev})
    save_config(cfg)
    print(f"  ✅ Sparad: {lat:.4f}°N  {lon:.4f}°E  {elev} m")
    return cfg


# ── TLE-hämtning ───────────────────────────────────────────────────────────────

def fetch_all_tles() -> dict[str, tuple[str, str, str]]:
    """Hämta TLE för alla tre NOAA-satelliterna individuellt via NORAD-ID."""
    print("  Hämtar TLE-data från Celestrak...", end="", flush=True)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    tles = {}
    for name, info in NOAA_SATS.items():
        url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={info['norad_id']}&FORMAT=TLE"
        try:
            with urllib.request.urlopen(url, timeout=10, context=ctx) as resp:
                lines = [l.strip() for l in resp.read().decode().splitlines() if l.strip()]
            if len(lines) >= 3:
                tles[name] = (lines[0], lines[1], lines[2])
        except Exception as e:
            print(f"\n  ⚠️  Kunde inte hämta TLE för {name}: {e}")

    print(f" ✅  ({len(tles)}/3 satelliter)")
    return tles


# ── Passprediktion ─────────────────────────────────────────────────────────────

def ephem_date_to_dt(d) -> datetime:
    t = d.tuple()
    return datetime(t[0], t[1], t[2], t[3], t[4], int(t[5]), tzinfo=timezone.utc)


def find_passes(lat: float, lon: float, elev: int,
                tle: tuple[str, str, str],
                sat_name: str,
                count: int = 6) -> list[dict]:
    obs = ephem.Observer()
    obs.lat       = str(lat)
    obs.lon       = str(lon)
    obs.elevation = elev
    obs.horizon   = str(MIN_ELEVATION)

    sat = ephem.readtle(*tle)
    passes = []
    obs.date = ephem.now()

    while len(passes) < count:
        try:
            aos, _, _, max_elev, los, _ = obs.next_pass(sat)
        except Exception:
            break

        aos_dt = ephem_date_to_dt(aos)
        los_dt = ephem_date_to_dt(los)
        dur    = (los_dt - aos_dt).total_seconds()

        if dur > 60:
            passes.append({
                "sat":    sat_name,
                "freq":   NOAA_SATS[sat_name]["freq"],
                "aos":    aos_dt,
                "los":    los_dt,
                "max_el": round(float(max_elev) * 180 / 3.14159, 1),
                "dur_s":  int(dur),
            })

        obs.date = los + ephem.minute

    return passes


def qual_symbol(elev: float) -> str:
    if elev >= 40:
        return "🟢 Bra "
    elif elev >= 20:
        return "🟡 OK  "
    else:
        return "🔴 Låg "


def print_pass_table(passes: list[dict]):
    now = datetime.now(timezone.utc)
    print(f"\n  {'#':<3}  {'Satellit':<10}  {'Datum':<7}  "
          f"{'AOS–LOS':<13}  {'Dur':<8}  {'Max el':<7}  {'Kvalitet':<8}  Väntan")
    print("  " + "─" * 70)
    for i, p in enumerate(passes, 1):
        date  = p["aos"].astimezone().strftime("%d %b")
        aos_s = p["aos"].astimezone().strftime("%H:%M")
        los_s = p["los"].astimezone().strftime("%H:%M")
        dur   = f"{p['dur_s']//60}m{p['dur_s']%60:02d}s"
        qual  = qual_symbol(p["max_el"])
        freq_mhz = p["freq"] / 1e6
        delta_s = (p["aos"] - now).total_seconds()
        if delta_s < 0:
            wait = "  pågår  "
        elif delta_s < 3600:
            wait = f"  om {int(delta_s//60)}m{int(delta_s%60):02d}s"
        else:
            h2 = int(delta_s // 3600)
            m2 = int((delta_s % 3600) // 60)
            wait = f"  om {h2}h{m2:02d}m  "
        print(f"  {i:<3}  {p['sat']:<10}  {date:<7}  {aos_s}–{los_s:<5}  "
              f"{dur:<8}  {p['max_el']:4.0f}°   {qual}  {wait}")
    print("  " + "─" * 70)


# ── Inspelning ─────────────────────────────────────────────────────────────────

def record_pass(p: dict, settings: dict):
    """Räkna ner till AOS och spela in med satdump."""
    gain = settings.get("gain", 40)
    ppm  = settings.get("ppm",  0)

    freq_mhz  = p["freq"] / 1e6
    aos_local = p["aos"].astimezone().strftime("%H:%M:%S")
    los_local = p["los"].astimezone().strftime("%H:%M:%S")

    ts         = p["aos"].astimezone().strftime("%Y-%m-%d_%H%M")
    output_dir = IMAGES_DIR / f"{p['sat'].replace(' ', '_').lower()}_{ts}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  📡 {p['sat']}  |  {freq_mhz:.4f} MHz")
    print(f"  AOS {aos_local}  →  LOS {los_local}")
    print(f"  Max elevation: {p['max_el']:.0f}°  |  Varaktighet: {p['dur_s']//60}m {p['dur_s']%60:02d}s")
    print(f"  Bilder sparas i: {output_dir}\n")
    print("─" * 55)

    # ── Nedräkning ─────────────────────────────────────────────────
    print("  Väntar på AOS... (Ctrl+C för att avbryta)\n")
    wait_s_total = max(1, (p["aos"] - datetime.now(timezone.utc)).total_seconds())

    try:
        while True:
            now    = datetime.now(timezone.utc)
            remain = (p["aos"] - now).total_seconds()
            if remain <= 0:
                break
            m, s = divmod(int(remain), 60)
            h, m = divmod(m, 60)
            bar_w = 28
            bar   = "█" * int((1 - remain / wait_s_total) * bar_w) + \
                    "░" * int(remain / wait_s_total * bar_w)
            print(f"\r  ⏳ {h:02d}:{m:02d}:{s:02d}  [{bar}]  AOS {aos_local}  ",
                  end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n  Avbruten.")
        return

    # ── Pass börjar ────────────────────────────────────────────────
    print(f"\n\n  🟢 AOS! Startar SatDump APT-avkodning på {freq_mhz:.4f} MHz...\n")

    cmd = [
        "satdump", "live", "noaa_apt",
        str(output_dir),
        "--source",     "rtlsdr",
        "--samplerate", str(SAMPLERATE),
        "--frequency",  str(p["freq"]),
        "--timeout",    str(p["dur_s"] + 30),
    ]
    if gain != "auto":
        cmd += ["--general_gain", str(int(gain))]
    if ppm != 0:
        cmd += ["--ppm", str(int(ppm))]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print("  ❌ satdump hittades inte.")
        print("     macOS: brew install satdump  (+ symlänkar, se README)")
        return

    end_time = p["los"]

    try:
        while True:
            now    = datetime.now(timezone.utc)
            remain = (end_time - now).total_seconds()

            line = proc.stdout.readline()
            if line:
                l = line.rstrip()
                if any(kw in l for kw in
                       ["Writing", "Decoded", "Image", "SNR", "ERROR", "error", "Viterbi"]):
                    # Visa SNR-rader (visar signalkvalitet)
                    if "SNR" in l:
                        snr_part = l[l.find("SNR"):]
                        print(f"\r  🔴 REC  LOS om {int(remain//60):02d}:{int(remain%60):02d}"
                              f"  |  {snr_part[:40]}  ", end="", flush=True)
                    else:
                        print(f"\n  {l}")

            if remain <= 0:
                break
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n\n  Avbruten av användaren.")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ── Resultat ────────────────────────────────────────────────────
    print(f"\n\n  🏁 LOS. Passet avslutat.")
    time.sleep(2)

    images = list(output_dir.rglob("*.png"))
    if images:
        print(f"\n  ✅ {len(images)} bilder sparade i:\n     {output_dir}\n")
        for img in sorted(images):
            size_kb = img.stat().st_size // 1024
            print(f"     📷 {img.name:<50} ({size_kb} KB)")
        print(f"\n  Öppna mappen: open \"{output_dir}\"")
    else:
        print(f"\n  ⚠️  Inga PNG-bilder hittades i {output_dir}")
        print("  Möjliga orsaker:")
        print("  • Passet var för lågt (<20° ger ofta inget resultat)")
        print("  • Antennen är inte anpassad för 137 MHz")
        print("  • Signalen för svag – prova ett 🟢 Bra-pass (>40°)")
        # Rensa mappen – oavsett om den är tom eller bara innehåller råfiler utan bilddata
        try:
            import shutil
            shutil.rmtree(output_dir)
            print(f"  🗑️  Mapp borttagen (inga bilder): {output_dir.name}")
        except Exception:
            pass


# ── Huvudfunktion ──────────────────────────────────────────────────────────────

def run_noaa(settings: dict | None = None):
    settings = settings or {}

    print("\n" + "=" * 58)
    print(" 📡 NOAA APT – Vädersatellitbilder (137 MHz)")
    print(" NOAA 15 / NOAA 18 / NOAA 19  |  ~4 km/pixel")
    print("=" * 58)

    print("""
  NOAA 15, 18 och 19 passerar Sverige 4–6 ggr per dygn var,
  totalt ~15 pass/dygn med alla tre. APT-signalen är enkel
  analog FM/AM och tål svagare mottagning än Meteors LRPT.

  Bilderna visar moln, isutbredning och temperaturgradienter
  i svartvitt eller falskfärg (IR-kanal).

  ⚠️  Antenn: Samma V-dipol (54 cm armar) som för Meteor
      fungerar utmärkt. Fri sikt mot himlen hjälper.
""")

    if ephem is None:
        print("  ❌ 'ephem' saknas.  pip install ephem")
        return

    import shutil
    if not shutil.which("satdump"):
        print("  ❌ 'satdump' saknas.")
        print("     macOS: brew install satdump  (+ symlänkar, se README)")
        return

    # Position
    cfg = load_config()
    cfg = ask_position(cfg)
    lat  = cfg["lat"]
    lon  = cfg["lon"]
    elev = cfg.get("elevation", 0)

    # TLE
    tles = fetch_all_tles()
    if not tles:
        return

    # Beräkna pass för alla tre satelliter, slå ihop och sortera
    print(f"\n  Beräknar passager (>{MIN_ELEVATION}° elevation)...", end="", flush=True)
    all_passes = []
    for name, tle in tles.items():
        all_passes.extend(find_passes(lat, lon, elev, tle, name, count=6))
    all_passes.sort(key=lambda p: p["aos"])
    # Visa bara de närmaste 10 passen
    all_passes = all_passes[:10]
    print(f" {len(all_passes)} pass hittade.\n")

    if not all_passes:
        print("  ❌ Inga passager hittades.")
        return

    print("  Kommande NOAA-passager:")
    print_pass_table(all_passes)

    print("\n  Vilket pass vill du ta emot? "
          f"(1–{len(all_passes)}, Enter=1, 0=avsluta): ", end="")
    try:
        val = input().strip()
        if val == "0":
            return
        idx = int(val) - 1 if val else 0
        if not (0 <= idx < len(all_passes)):
            print("  Ogiltigt val.")
            return
    except (ValueError, KeyboardInterrupt):
        print("\n  Avbruten.")
        return

    record_pass(all_passes[idx], settings)
