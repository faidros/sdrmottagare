"""
HRPT-mottagning – MetOp-B/C och Fengyun-3 (1701 MHz)

HRPT (High Resolution Picture Transmission) är det L-band (1.7 GHz)
satellitsystem som används av bl.a. MetOp och Fengyun-3.
Bildkvaliteten är betydligt högre än Meteor LRPT (~1 km/pixel).

Satellit-översikt:
  MetOp-B   – EUMETSAT, europeisk polare vädersatellit, 1701.3 MHz
  MetOp-C   – EUMETSAT, primär operativ satellit sedan 2018, 1701.3 MHz
  FY-3D     – Kina NSMC, 1701.4 MHz
  FY-3E     – Kina NSMC, morgonpassningsorbit, 1701.4 MHz

Flöde:
  ephem (passprediktion) → nedräkning → SatDump live (RTL-SDR → HRPT) → PNG-bilder

Krav på hårdvara:
  • RTL-SDR klarar HRPT men kräver minst 2.4 Msps och är i överkant av
    sin frekvensräckvidd (~1.75 GHz max). Airspy Mini/R2 (6 Msps) eller
    SDRplay ger avsevärt bättre resultat.
  • Antenn: patch-antenn för 1.7 GHz (t.ex. Sawbird GOES LNA + patch)
    eller liten parabolskål (30–60 cm). En vanlig dongelantenn fungerar INTE.
  • Under passet behöver antennen pekas mot satelliten (manuellt eller
    med ett rotatorsystem).
"""

import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import ephem
except ImportError:
    ephem = None

from modes.location import ask_position

# ── Konfiguration ─────────────────────────────────────────────────────────────

CONFIG_FILE   = Path.home() / ".sdrmottagare.json"
IMAGES_DIR    = Path.home() / "sdr_bilder" / "hrpt"
TLE_URL       = "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle"
MIN_ELEVATION = 10   # grader

# Stödda satelliter
SATELLITES = {
    "1": {
        "name":     "MetOp-C",
        "tle_name": "METOP-C",
        "pipeline": "metop_hrpt",
        "freq":     1_701_300_000,
        "sr":       2_400_000,
        "note":     "Primär operativ MetOp-satellit (EUMETSAT)",
    },
    "2": {
        "name":     "MetOp-B",
        "tle_name": "METOP-B",
        "pipeline": "metop_hrpt",
        "freq":     1_701_300_000,
        "sr":       2_400_000,
        "note":     "Backup MetOp-satellit (EUMETSAT)",
    },
    "3": {
        "name":     "Fengyun 3E",
        "tle_name": "FENGYUN 3E",
        "pipeline": "fengyun3_hrpt",
        "freq":     1_701_400_000,
        "sr":       2_400_000,
        "note":     "Kinesisk vädersatellit, morgonorbit",
    },
    "4": {
        "name":     "Fengyun 3D",
        "tle_name": "FENGYUN 3D",
        "pipeline": "fengyun3_hrpt",
        "freq":     1_701_400_000,
        "sr":       2_400_000,
        "note":     "Kinesisk vädersatellit, eftermiddagsorbit",
    },
}


# ── Konfigurationsfil ─────────────────────────────────────────────────────────

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


# ── TLE-hämtning ──────────────────────────────────────────────────────────────

def fetch_tle(name: str) -> tuple[str, str, str] | None:
    print(f"\n  Hämtar TLE-data för {name} från Celestrak...", end="", flush=True)
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(TLE_URL, timeout=10, context=ctx) as resp:
            lines = resp.read().decode().splitlines()
    except Exception as e:
        print(f"\n  ❌ Kunde inte hämta TLE: {e}")
        return None

    name_upper = name.upper()
    for i, line in enumerate(lines):
        if name_upper in line.upper() and not line.startswith("1 ") and not line.startswith("2 "):
            if i + 2 < len(lines):
                print(" ✅")
                return lines[i].strip(), lines[i+1].strip(), lines[i+2].strip()

    print(f"\n  ❌ Hittade inte '{name}' i TLE-datan.")
    return None


# ── Passprediktion ────────────────────────────────────────────────────────────

def ephem_date_to_dt(d) -> datetime:
    t = d.tuple()
    return datetime(t[0], t[1], t[2], t[3], t[4], int(t[5]), tzinfo=timezone.utc)


def find_passes(lat: float, lon: float, elev: int,
                tle: tuple, count: int = 5) -> list[dict]:
    obs = ephem.Observer()
    obs.lat       = str(lat)
    obs.lon       = str(lon)
    obs.elevation = elev
    obs.horizon   = f"{MIN_ELEVATION}"

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
                "aos":    aos_dt,
                "los":    los_dt,
                "max_el": round(float(max_elev) * 180 / 3.14159, 1),
                "dur_s":  int(dur),
            })

        obs.date = los + ephem.minute

    return passes


def format_pass(p: dict, idx: int) -> str:
    now     = datetime.now(timezone.utc)
    delta   = p["aos"] - now
    wait_m  = int(delta.total_seconds() // 60)
    aos_loc = p["aos"].astimezone().strftime("%H:%M:%S")
    los_loc = p["los"].astimezone().strftime("%H:%M:%S")
    qual    = "🟢 Bra" if p["max_el"] > 40 else "🟡 OK" if p["max_el"] > 20 else "🔴 Låg"
    return (f"  {idx}. AOS {aos_loc}  LOS {los_loc}  "
            f"({p['dur_s']//60}m {p['dur_s']%60:02d}s)  "
            f"Max {p['max_el']:.0f}°  {qual}   (om {wait_m} min)")


# ── SatDump ───────────────────────────────────────────────────────────────────

def run_satdump_live(output_dir: Path, sat: dict, gain, ppm: int, timeout_s: int) -> subprocess.Popen:
    cmd = [
        "satdump", "live",
        sat["pipeline"],
        str(output_dir),
        "--source",     "rtlsdr",
        "--samplerate", str(sat["sr"]),
        "--frequency",  str(sat["freq"]),
        "--timeout",    str(timeout_s + 30),
    ]
    if gain != "auto":
        cmd += ["--general_gain", str(int(gain))]
    if ppm != 0:
        cmd += ["--ppm", str(ppm)]

    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)


def countdown_and_record(p: dict, sat: dict, settings: dict):
    gain = settings.get("gain", 40)
    ppm  = settings.get("ppm", 0)

    now    = datetime.now(timezone.utc)
    wait_s = (p["aos"] - now).total_seconds()

    aos_local = p["aos"].astimezone().strftime("%H:%M:%S")
    los_local = p["los"].astimezone().strftime("%H:%M:%S")

    ts         = p["aos"].astimezone().strftime("%Y-%m-%d_%H%M")
    sat_slug   = sat["name"].replace(" ", "_").lower()
    output_dir = IMAGES_DIR / f"{sat_slug}_{ts}"
    output_dir.mkdir(parents=True, exist_ok=True)

    freq_mhz = sat["freq"] / 1e6

    print(f"\n  🛰️  {sat['name']}  |  {freq_mhz:.1f} MHz  |  {sat['pipeline']}")
    print(f"  AOS {aos_local}  →  LOS {los_local}")
    print(f"  Max elevation: {p['max_el']:.0f}°  |  Varaktighet: {p['dur_s']//60}m {p['dur_s']%60:02d}s")
    print(f"  Bilder sparas i: {output_dir}\n")
    print("─" * 60)
    print("  ⚠️  Peka antennen mot satelliten! Börja ~1 min före AOS.")
    print("  Väntar på AOS... (Ctrl+C för att avbryta)\n")

    try:
        while True:
            now    = datetime.now(timezone.utc)
            remain = (p["aos"] - now).total_seconds()
            if remain <= 0:
                break

            m, s = divmod(int(remain), 60)
            h, m = divmod(m, 60)
            bar = ("█" * max(0, 30 - int(remain / max(wait_s, 1) * 30)) +
                   "░" * min(30, int(remain / max(wait_s, 1) * 30)))
            print(f"\r  ⏳ {h:02d}:{m:02d}:{s:02d}  [{bar}]  AOS {aos_local}  ", end="", flush=True)
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\n  Avbruten.")
        shutil.rmtree(output_dir, ignore_errors=True)
        return

    print(f"\n\n  🟢 AOS! Startar SatDump live-avkodning ({sat['pipeline']})...\n")

    try:
        proc = run_satdump_live(output_dir, sat, gain, ppm, p["dur_s"])
    except FileNotFoundError:
        print("  ❌ satdump hittades inte. Installera med: brew install satdump")
        return

    end_time = p["los"]

    try:
        while True:
            now    = datetime.now(timezone.utc)
            remain = (end_time - now).total_seconds()

            line = proc.stdout.readline()
            if line:
                if any(kw in line for kw in ["Writing", "Decoded", "Image", "ERROR", "error", "Frame"]):
                    print(f"  {line.rstrip()}")

            if remain <= 0:
                break

            m2, s2 = divmod(int(remain), 60)
            print(f"\r  🔴 REC  LOS om {m2:02d}:{s2:02d}  |  {los_local}  ", end="", flush=True)
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n\n  Avbruten av användaren.")
    finally:
        proc.terminate()
        proc.wait()

    print(f"\n\n  🏁 LOS. Passet avslutat.")
    print("  SatDump processar bilder...")
    time.sleep(3)

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
        print("  • Antennen pekade fel eller signalen var för svag")
        print("  • RTL-SDR klarar inte alltid 2.4 Msps vid 1.7 GHz – prova Airspy")
        print("  • Kontrollera att LNA är ansluten (Sawbird eller liknande)")
        shutil.rmtree(output_dir, ignore_errors=True)
        print(f"  🗑️  Tom mapp borttagen.")


# ── Satellitval ───────────────────────────────────────────────────────────────

def choose_satellite() -> dict | None:
    print("\n  Välj satellit:\n")
    for key, s in SATELLITES.items():
        freq_mhz = s["freq"] / 1e6
        print(f"  {key}. {s['name']:<14} {freq_mhz:.1f} MHz   {s['note']}")
    print()
    val = input("  Val [1]: ").strip() or "1"
    return SATELLITES.get(val)


# ── Huvudfunktion ─────────────────────────────────────────────────────────────

def run_hrpt(settings: dict | None = None):
    settings = settings or {}

    print("\n" + "=" * 60)
    print(" 🛰️  HRPT – MetOp & Fengyun-3 (1701 MHz, L-band)")
    print(" Högupplöst väderbild  |  ~1 km/pixel  |  ~10 min/pass")
    print("=" * 60)

    if ephem is None:
        print("\n❌ Python-paketet 'ephem' saknas.")
        print("   Installera: pip install ephem")
        return

    try:
        subprocess.run(["satdump", "--help"], capture_output=True)
    except FileNotFoundError:
        print("\n❌ 'satdump' saknas.")
        print("   macOS: brew install satdump")
        return

    print("""
  MetOp (EUMETSAT) och Fengyun-3 (Kina) sänder HRPT-data på
  ~1701 MHz. Bildkvaliteten är ~1 km/pixel med 5–6 spektralband.

  ⚠️  Hårdvarukrav:
      • RTL-SDR fungerar men är i överkant på 1.7 GHz (~1.75 GHz max).
        Airspy Mini (6 Msps) eller SDRplay rekommenderas.
      • LNA krävs nära antennen (t.ex. Sawbird GOES eller liknande
        1.7 GHz LNA med ~20 dB förstärkning).
      • Patch-antenn (1.7 GHz, t.ex. 2×2-element) eller parabolskål
        30–60 cm. Vanlig dongelantenn räcker INTE.
      • Antennen behöver pekas mot satelliten under hela passet
        (manuellt eller med rotator).
""")

    # Satellitval
    sat = choose_satellite()
    if sat is None:
        print("  Ogiltigt val.")
        return

    # Position
    cfg = load_config()
    cfg = ask_position(cfg)
    lat  = cfg["lat"]
    lon  = cfg["lon"]
    elev = cfg.get("elevation", 0)

    # TLE
    tle = fetch_tle(sat["tle_name"])
    if tle is None:
        return

    print(f"\n  TLE: {tle[0]}")

    # Passager
    print(f"\n  Beräknar passager för {lat:.4f}°N {lon:.4f}°E ...\n")
    passes = find_passes(lat, lon, elev, tle, count=6)

    if not passes:
        print("  ❌ Inga passager hittades de närmaste timmarna.")
        return

    print(f"  Nästa passager med {sat['name']} (min elevation >{MIN_ELEVATION}°):\n")
    for i, p in enumerate(passes):
        print(format_pass(p, i + 1))

    print()
    val = input(f"  Välj pass att fånga [1–{len(passes)}] eller Enter för nästa: ").strip()
    try:
        idx = int(val) - 1 if val else 0
        if not 0 <= idx < len(passes):
            idx = 0
    except ValueError:
        idx = 0

    countdown_and_record(passes[idx], sat, settings)
