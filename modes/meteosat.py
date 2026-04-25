"""
Meteosat LRIT-mottagning (1691 MHz, geostationär)

Meteosat är ESA/EUMETSATs geostationära vädersatelliter i 36 000 km höjd.
De sänder kontinuerligt LRIT (Low Rate Information Transmission) på 1691 MHz.
Eftersom de är geostationära är de alltid på samma punkt på himlen –
ingen passberäkning behövs, men antennen måste pekas korrekt.

Operativa satelliter (2026):
  Meteosat-12  (MTG-I1)  –  0°          Primär, Meteosat Third Generation
  Meteosat-11            –  3.4°V        Backup MSG

Synlighet från Sverige (Stockholm, 59°N 18°E):
  Satellit vid  0°:  Elevation ~28°,  Azimut ~195° (SSV)
  Satellit vid  9°E: Elevation ~27°,  Azimut ~198°
  Satellit vid  3°V: Elevation ~27°,  Azimut ~193°

Frekvens:
  LRIT: 1691.0 MHz  (Meteosat 10/11/12)
  Alla aktiva Meteosat sänder på samma frekvens.

Hårdvarukrav:
  • RTL-SDR v3/v4 klarar 1691 MHz (inom intervall).
  • LNA krävs (t.ex. Sawbird GOES/Meteosat ~1.7 GHz med bias-tee).
  • Parabolskål 60–90 cm eller riktad patch-antenn.
    En vanlig dongelantenn räcker INTE.
  • Antennen pekas en gång och behöver inte justeras under mottagning.

SatDump-pipeline:
  msg_lrit  →  avkodar LRIT-strömmen från MSG-satelliterna
  (Meteosat-12 / MTG-I1 kräver eventuellt 'mtg_lrit' i SatDump ≥ 1.3)

Data som tas emot:
  • Bilder var 15:e minut (MSG) eller var 10:e minut (MTG)
  • Fulldiskbilder (hela jordgloben) + regionala segmentbilder
  • Spektralband: synligt, IR, vattenånga m.fl.
  • Filformat: PNG + NetCDF/GeoTIFF
  • Sparas kontinuerligt under hela mottagningssessionen
"""

import json
import math
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from modes.location import ask_position

# ── Konfiguration ─────────────────────────────────────────────────────────────

CONFIG_FILE  = Path.home() / ".sdrmottagare.json"
OUTPUT_DIR   = Path.home() / "sdr_data" / "meteosat"
LRIT_FREQ    = 1_691_000_000   # Hz
LRIT_SR      = 2_400_000       # 2.4 Msps – LRIT-symbolhastighet 1 Msps, lite marginal
PIPELINE     = "msg_lrit"

# Kända Meteosat-positioner (grader östlig longitud)
SATELLITES = {
    "1": {"name": "Meteosat-12 (MTG-I1)",  "lon_deg":  0.0, "pipeline": "msg_lrit",
          "note": "Primär, Meteosat Third Generation"},
    "2": {"name": "Meteosat-11",            "lon_deg": -3.4, "pipeline": "msg_lrit",
          "note": "Backup MSG, 3.4°V"},
    "3": {"name": "Meteosat-10",            "lon_deg":  9.5, "pipeline": "msg_lrit",
          "note": "Backup MSG, 9.5°E"},
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


# ── Pekningsberäkning (geostationär satellit) ─────────────────────────────────

def geo_pointing(obs_lat_deg: float, obs_lon_deg: float,
                 sat_lon_deg: float) -> tuple[float, float]:
    """
    Beräkna elevation och azimut till en geostationär satellit.
    Returnerar (elevation_grader, azimut_grader).
    Azimut räknat från norr medurs.
    """
    lat  = math.radians(obs_lat_deg)
    dlon = math.radians(sat_lon_deg - obs_lon_deg)

    # Satellitradiens vinkel från jordcenter
    # Geostationär omloppsbana: 42 164 km från jordcenter, R_earth = 6 371 km
    r_e = 6_371.0
    r_s = 42_164.0

    # Beräkning
    a = math.cos(lat) * math.cos(dlon)
    b = math.sqrt(1 - a * a)

    el_rad = math.atan((a - r_e / r_s) / b)
    el_deg = math.degrees(el_rad)

    # Azimut
    az_rad = math.atan2(math.sin(dlon),
                        math.tan(lat) * math.cos(dlon) - math.sin(lat) * math.cos(dlon) / math.tan(lat))
    # Enklare formel för azimut
    az_rad = math.atan2(-math.sin(dlon),
                         math.tan(lat) * b - math.cos(lat) * math.cos(dlon) * 0)

    # Robust azimut-formel
    S = math.sin(dlon) * math.cos(lat)
    C = math.sqrt((math.cos(lat) * math.sin(dlon)) ** 2 +
                  (math.sin(lat) - a * (r_e / r_s)) ** 2)

    # Azimut från söder
    az_from_s = math.atan2(S, math.sin(lat) * a - math.cos(lat) * (r_e / r_s))
    # Konvertera till azimut från norr (N=0, E=90, S=180, W=270)
    az_deg = 180 + math.degrees(az_from_s)
    az_deg %= 360

    return round(el_deg, 1), round(az_deg, 1)


def az_to_compass(az: float) -> str:
    dirs = ["N", "NNO", "NO", "ONO", "O", "OSO", "SO", "SSO",
            "S", "SSV", "SV", "VSV", "V", "VNV", "NV", "NNV"]
    return dirs[round(az / 22.5) % 16]


# ── SatDump ───────────────────────────────────────────────────────────────────

def run_satdump_lrit(output_dir: Path, pipeline: str,
                     gain, ppm: int) -> subprocess.Popen:
    cmd = [
        "satdump", "live",
        pipeline,
        str(output_dir),
        "--source",     "rtlsdr",
        "--samplerate", str(LRIT_SR),
        "--frequency",  str(LRIT_FREQ),
    ]
    if gain != "auto":
        cmd += ["--general_gain", str(int(gain))]
    if ppm != 0:
        cmd += ["--ppm", str(ppm)]

    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)


# ── Huvudfunktion ─────────────────────────────────────────────────────────────

def run_meteosat(settings: dict | None = None):
    settings = settings or {}

    print("\n" + "=" * 60)
    print(" 📡 Meteosat LRIT – Geostationär vädersatellit (1691 MHz)")
    print(" Bilder var 10–15 min  |  Full diskbild  |  Alltid synlig")
    print("=" * 60)

    try:
        subprocess.run(["satdump", "--help"], capture_output=True)
    except FileNotFoundError:
        print("\n❌ 'satdump' saknas. macOS: brew install satdump")
        return

    print("""
  Meteosat är alltid synlig från Sverige – ingen passberäkning
  behövs. Antennen pekas en gång mot satelliten och du kan ta
  emot kontinuerligt hur länge du vill.

  ⚠️  Hårdvarukrav:
      • RTL-SDR v3/v4 fungerar (1691 MHz är inom räckvidden).
      • LNA vid antennen krävs (t.ex. Sawbird GOES 1.7 GHz).
      • Parabolskål 60–90 cm mot söder, eller riktad patch-antenn.
        Vanlig dongelantenn räcker INTE.
      • Bias-tee på RTL-SDR måste aktiveras för att mata LNA.
""")

    # Position
    cfg = load_config()
    cfg = ask_position(cfg)
    lat = cfg["lat"]
    lon = cfg["lon"]

    # Satellitval + pekningsinfo
    print("\n  Välj satellit:\n")
    for key, s in SATELLITES.items():
        el, az = geo_pointing(lat, lon, s["lon_deg"])
        compass = az_to_compass(az)
        lon_str = f"{abs(s['lon_deg']):.1f}°{'E' if s['lon_deg'] >= 0 else 'V'}"
        visible = "✅" if el > 5 else "❌ Under horisont"
        print(f"  {key}. {s['name']:<28} {lon_str:<6}  "
              f"Elevation {el:>5.1f}°  Azimut {az:>5.1f}° ({compass})  {visible}")
        print(f"       {s['note']}")
        print()

    val = input("  Val [1]: ").strip() or "1"
    sat = SATELLITES.get(val)
    if sat is None:
        print("  Ogiltigt val.")
        return

    el, az = geo_pointing(lat, lon, sat["lon_deg"])
    compass = az_to_compass(az)

    print(f"\n  ── Pekningsinformation för {sat['name']} ──")
    print(f"  Elevation : {el:.1f}°  (luta upp antennen {el:.0f}° från horisont)")
    print(f"  Azimut    : {az:.1f}° ({compass})  (vrid antennen {az:.0f}° från norr, medurs)")

    if el < 10:
        print(f"\n  ⚠️  Låg elevation ({el:.1f}°) – kan ge dålig signal pga atmosfärsabsorption.")
    elif el < 20:
        print(f"\n  🟡 Måttlig elevation ({el:.1f}°) – LNA är extra viktigt vid låg elevation.")
    else:
        print(f"\n  🟢 Bra elevation ({el:.1f}°).")

    # Fråga hur länge
    print()
    dur_str = input("  Hur länge ska mottagning pågå? [30 min]: ").strip() or "30"
    try:
        dur_min = int(dur_str)
    except ValueError:
        dur_min = 30
    dur_s = dur_min * 60

    # Outputmapp
    ts         = datetime.now().strftime("%Y-%m-%d_%H%M")
    sat_slug   = sat["name"].split()[0].lower() + "_" + sat["name"].split()[1].lower().replace("-", "")
    output_dir = OUTPUT_DIR / f"{sat_slug}_{ts}"
    output_dir.mkdir(parents=True, exist_ok=True)

    gain     = settings.get("gain", 40)
    ppm      = settings.get("ppm", 0)
    pipeline = sat["pipeline"]

    print(f"\n  Pipeline  : {pipeline}")
    print(f"  Frekvens  : {LRIT_FREQ / 1e6:.1f} MHz")
    print(f"  Samplingsrate: {LRIT_SR / 1e6:.1f} Msps")
    print(f"  Sparas i  : {output_dir}")
    print(f"  Varaktighet: {dur_min} minuter")
    print()
    print("  Kontrollera att antennen pekar rätt, LNA är på och tryck Enter...")
    input("  [Enter för att starta] ")

    print(f"\n  🟢 Startar SatDump LRIT-mottagning...\n")

    try:
        proc = run_satdump_lrit(output_dir, pipeline, gain, ppm)
    except FileNotFoundError:
        print("  ❌ satdump hittades inte.")
        return

    start = time.time()
    end   = start + dur_s

    try:
        while True:
            now    = time.time()
            remain = end - now
            if remain <= 0:
                break

            line = proc.stdout.readline()
            if line:
                if any(kw in line for kw in ["Image", "Writing", "Decoded", "Saving",
                                              "ERROR", "error", "LRIT", "File"]):
                    ts_now = datetime.now().strftime("%H:%M:%S")
                    print(f"  [{ts_now}] {line.rstrip()}")

            m2, s2 = divmod(int(remain), 60)
            elapsed = int(now - start)
            em, es = divmod(elapsed, 60)
            print(f"\r  📡 Mottar...  Förfluten: {em:02d}:{es:02d}  |  Kvar: {m2:02d}:{s2:02d}  ",
                  end="", flush=True)
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n\n  Avbruten av användaren.")
    finally:
        proc.terminate()
        proc.wait()

    print(f"\n\n  🏁 Mottagning avslutad.")
    time.sleep(2)

    images = list(output_dir.rglob("*.png"))
    if images:
        print(f"\n  ✅ {len(images)} bilder sparade i:\n     {output_dir}\n")
        for img in sorted(images)[:10]:   # Visa max 10
            size_kb = img.stat().st_size // 1024
            print(f"     📷 {img.name:<55} ({size_kb} KB)")
        if len(images) > 10:
            print(f"     ... och {len(images) - 10} till.")
        print(f"\n  Öppna mappen: open \"{output_dir}\"")
    else:
        print(f"\n  ⚠️  Inga PNG-bilder hittades i {output_dir}")
        print("  Möjliga orsaker:")
        print("  • Antennen pekar fel – kontrollera azimut/elevation")
        print("  • LNA saknas eller är inte påslagen (bias-tee av?)")
        print("  • SatDump-pipeline 'msg_lrit' kräver SatDump ≥ 1.2")
        print("    För Meteosat-12 (MTG-I1) kan pipeline 'mtg_lrit' behövas")
        shutil.rmtree(output_dir, ignore_errors=True)
        print(f"  🗑️  Tom mapp borttagen.")
