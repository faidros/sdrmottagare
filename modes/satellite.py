"""
Satellit-mottagning – Meteor-M2-3 LRPT (137.9 MHz)

Meteor-M2-3 är en rysk vädersatellit i låg omloppsbana (LEO, ~820 km).
Den sänder realtidsbilder via LRPT-protokollet på 137.9 MHz med QPSK-modulering.
Upplösning: ~1 km/pixel, tre kanaler (synligt ljus + infraröd).

Flöde:
  ephem (passprediktion) → nedräkning → SatDump live (RTL-SDR → LRPT-avkodning) → PNG-bilder

Krav:
  pip install ephem
  brew install satdump          (macOS)
  sudo apt install satdump      (Linux, om paket finns – annars bygg från källa)
"""

import json
import os
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

# ── Konfiguration ─────────────────────────────────────────────────────────────

CONFIG_FILE   = Path.home() / ".sdrmottagare.json"
IMAGES_DIR    = Path.home() / "sdr_bilder" / "meteor"
TLE_URL       = "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle"
METEOR_NAME   = "METEOR-M2 3"
METEOR_FREQ   = 137_900_000   # Hz
METEOR_SR     = 1_200_000     # Sps – 1.2 Msps räcker för LRPT 72 kbps QPSK
MIN_ELEVATION = 10            # grader – lägre ger ofta dålig bild


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


def ask_position(cfg: dict) -> dict:
    """Fråga efter position om den inte redan är sparad."""
    if "lat" in cfg and "lon" in cfg:
        lat, lon = cfg["lat"], cfg["lon"]
        elev = cfg.get("elevation", 0)
        print(f"\n  Sparad position: {lat:.4f}°N  {lon:.4f}°E  ({elev} m ö.h.)")
        print("  Tryck Enter för att använda, eller 'c' för att ändra: ", end="")
        if input().strip().lower() != "c":
            return cfg

    print("\n  Ange din position (används för passprediktion):")
    print("  (Ungefärlig position på stadsnivå räcker)\n")
    try:
        lat  = float(input("  Latitud  (°N, t.ex. 59.33 för Stockholm): "))
        lon  = float(input("  Longitud (°E, t.ex. 18.07 för Stockholm): "))
        elev = int(input("  Höjd (m ö.h., tryck Enter för 0): ").strip() or "0")
    except ValueError:
        print("  Ogiltigt värde, använder Stockholm som standard.")
        lat, lon, elev = 59.33, 18.07, 20

    cfg.update({"lat": lat, "lon": lon, "elevation": elev})
    save_config(cfg)
    print(f"\n  ✅ Position sparad: {lat:.4f}°N  {lon:.4f}°E  {elev} m")
    return cfg


# ── TLE-hämtning ──────────────────────────────────────────────────────────────

def fetch_tle(name: str = METEOR_NAME) -> tuple[str, str, str] | None:
    """Hämta aktuell TLE för satelliten från Celestrak."""
    print(f"\n  Hämtar TLE-data från Celestrak...", end="", flush=True)
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(TLE_URL, timeout=10, context=ctx) as resp:
            lines = resp.read().decode().splitlines()
    except Exception as e:
        print(f"\n  ❌ Kunde inte hämta TLE: {e}")
        return None

    # Sök efter satellitnamnet (matcha partiellt, skiftlägesokänsligt)
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
    """Konvertera ephem.Date till Python datetime (UTC).
    ephem.Date.tuple() returnerar (år, månad, dag, h, m, s) – säkrare än str()."""
    t = d.tuple()
    return datetime(t[0], t[1], t[2], t[3], t[4], int(t[5]),
                    tzinfo=timezone.utc)


def find_passes(lat: float, lon: float, elev: int,
                tle: tuple[str, str, str],
                count: int = 5) -> list[dict]:
    """Hitta de N nästa passagerna med minst MIN_ELEVATION graders maxelevation."""
    obs = ephem.Observer()
    obs.lat       = str(lat)
    obs.lon       = str(lon)
    obs.elevation = elev
    obs.horizon   = f"{MIN_ELEVATION}"   # Skippa låga passager direkt

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

        if dur > 60:   # Skippa passeringar kortare än 1 minut
            passes.append({
                "aos":     aos_dt,
                "los":     los_dt,
                "max_el":  round(float(max_elev) * 180 / 3.14159, 1),
                "dur_s":   int(dur),
            })

        obs.date = los + ephem.minute   # Starta sökning efter LOS

    return passes


def format_pass(p: dict, idx: int) -> str:
    now    = datetime.now(timezone.utc)
    delta  = p["aos"] - now
    wait_m = int(delta.total_seconds() // 60)
    wait_s = int(delta.total_seconds() % 60)

    aos_local = p["aos"].astimezone().strftime("%H:%M:%S")
    los_local = p["los"].astimezone().strftime("%H:%M:%S")

    qual = "🟢 Bra" if p["max_el"] > 40 else "🟡 OK" if p["max_el"] > 20 else "🔴 Låg"
    return (f"  {idx}. AOS {aos_local}  LOS {los_local}  "
            f"({p['dur_s']//60}m {p['dur_s']%60:02d}s)  "
            f"Max {p['max_el']:.0f}°  {qual}")


# ── Inspelning och avkodning ──────────────────────────────────────────────────

def run_satdump_live(output_dir: Path, gain, ppm: int, timeout_s: int) -> subprocess.Popen:
    """Starta satdump live-avkodning av Meteor-M2-3."""
    cmd = [
        "satdump", "live",
        "meteor_m2-x_lrpt_decode",
        str(output_dir),
        "--source",     "rtlsdr",
        "--samplerate", str(METEOR_SR),
        "--frequency",  str(METEOR_FREQ),
        "--timeout",    str(timeout_s + 30),   # Lite extra tid
    ]

    if gain != "auto":
        cmd += ["--general_gain", str(int(gain))]
    if ppm != 0:
        cmd += ["--ppm", str(ppm)]

    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)


def countdown_and_record(p: dict, settings: dict):
    """Vänta på AOS, spela in passet, avkoda med SatDump."""
    gain = settings.get("gain", 40)
    ppm  = settings.get("ppm",  0)

    now = datetime.now(timezone.utc)
    wait_s = (p["aos"] - now).total_seconds()

    aos_local = p["aos"].astimezone().strftime("%H:%M:%S")
    los_local = p["los"].astimezone().strftime("%H:%M:%S")

    ts = p["aos"].astimezone().strftime("%Y-%m-%d_%H%M")
    output_dir = IMAGES_DIR / f"meteor_{ts}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  🛰️  Meteor-M2-3  |  AOS {aos_local}  →  LOS {los_local}")
    print(f"  Max elevation: {p['max_el']:.0f}°  |  Varaktighet: {p['dur_s']//60}m {p['dur_s']%60:02d}s")
    print(f"  Bilder sparas i: {output_dir}\n")
    print("─" * 55)

    # ── Nedräkning ────────────────────────────────────────────────
    print("  Väntar på AOS... (Ctrl+C för att avbryta)\n")
    try:
        while True:
            now    = datetime.now(timezone.utc)
            remain = (p["aos"] - now).total_seconds()
            if remain <= 0:
                break

            m, s = divmod(int(remain), 60)
            h, m = divmod(m, 60)
            bar_w  = 30
            filled = max(0, bar_w - int(remain / (p["aos"] - now + timedelta(seconds=remain)).total_seconds() * bar_w))
            bar = "█" * min(bar_w, max(0, bar_w - int(remain / max(wait_s, 1) * bar_w))) + \
                  "░" * max(0, int(remain / max(wait_s, 1) * bar_w))

            print(f"\r  ⏳ {h:02d}:{m:02d}:{s:02d}  [{bar}]  AOS {aos_local}  ", end="", flush=True)
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\n  Avbruten.")
        return

    # ── Pass börjar ───────────────────────────────────────────────
    print(f"\n\n  🟢 AOS! Startar SatDump live-avkodning...\n")

    try:
        proc = run_satdump_live(output_dir, gain, ppm, p["dur_s"])
    except FileNotFoundError:
        print("  ❌ satdump hittades inte. Installera med: brew install satdump")
        return

    end_time = p["los"]

    try:
        while True:
            now = datetime.now(timezone.utc)
            remain = (end_time - now).total_seconds()

            # Läs eventuell output från satdump
            line = proc.stdout.readline()
            if line:
                # Visa bara intressanta rader (inte debug-spam)
                if any(kw in line for kw in ["Writing", "Decoded", "Image", "ERROR", "error"]):
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

    # ── Pass slut ─────────────────────────────────────────────────
    print(f"\n\n  🏁 LOS. Passet avslutat.")
    print(f"\n  SatDump processar bilder...")
    time.sleep(3)   # Ge satdump lite tid att skriva klart

    # Lista resultatfiler
    images = list(output_dir.rglob("*.png"))
    if images:
        print(f"\n  ✅ {len(images)} bilder sparade i:\n     {output_dir}\n")
        for img in sorted(images):
            size_kb = img.stat().st_size // 1024
            print(f"     📷 {img.name:<45} ({size_kb} KB)")
        print(f"\n  Öppna mappen: open \"{output_dir}\"")
    else:
        print(f"\n  ⚠️  Inga PNG-bilder hittades i {output_dir}")
        print("  Möjliga orsaker:")
        print("  • Signalen var för svag (prova högre elevation nästa gång)")
        print("  • Antennen är inte anpassad för 137 MHz")
        print("  • SatDump misslyckades – kolla loggar i output-mappen")


# ── Huvudfunktion ─────────────────────────────────────────────────────────────

def run_satellite(settings: dict | None = None):
    settings = settings or {}

    print("\n" + "=" * 55)
    print(" 🛰️  Meteor-M2-3 – Vädersatellitbilder (137.9 MHz)")
    print(" LRPT QPSK  |  ~1 km/pixel  |  ~10 min per pass")
    print("=" * 55)

    # Krav: ephem
    if ephem is None:
        print("\n❌ Python-paketet 'ephem' saknas.")
        print("   Installera med: pip install ephem")
        return

    # Krav: satdump
    try:
        subprocess.run(["satdump", "--help"], capture_output=True)
    except FileNotFoundError:
        print("\n❌ 'satdump' saknas.")
        print("   macOS:  brew install satdump")
        print("   Linux:  se https://github.com/SatDump/SatDump/releases")
        return

    print("""
  Meteor-M2-3 är en rysk vädersatellit i låg omloppsbana
  (LEO, ~820 km). Den passerar varje plats ~4–6 ggr/dag
  och varje pass varar 10–15 minuter.

  ⚠️  Antenn: En enkel 137 MHz dipol (~54 cm per arm)
      eller turniket-antenn ger bäst resultat.
      Den medföljande dongel-antennen kan fungera
      men ger sämre bildkvalitet.
""")

    # Ladda/spara position
    cfg = load_config()
    cfg = ask_position(cfg)

    lat  = cfg["lat"]
    lon  = cfg["lon"]
    elev = cfg.get("elevation", 0)

    # Hämta TLE
    tle = fetch_tle(METEOR_NAME)
    if tle is None:
        return

    print(f"\n  TLE: {tle[0]}")

    # Beräkna passager
    print(f"\n  Beräknar passager för {lat:.4f}°N {lon:.4f}°E ...\n")
    passes = find_passes(lat, lon, elev, tle, count=6)

    if not passes:
        print("  ❌ Inga passager hittades de närmaste timmarna.")
        print("  Kontrollera att din position är korrekt.")
        return

    print("  Nästa passager (min elevation >10°):\n")
    now = datetime.now(timezone.utc)
    for i, p in enumerate(passes):
        delta = p["aos"] - now
        wait_m = int(delta.total_seconds() // 60)
        print(format_pass(p, i + 1) + f"   (om {wait_m} min)")

    print()
    val = input(f"  Välj pass att fånga [1–{len(passes)}] eller Enter för nästa: ").strip()
    try:
        idx = int(val) - 1 if val else 0
        if not 0 <= idx < len(passes):
            idx = 0
    except ValueError:
        idx = 0

    chosen = passes[idx]
    countdown_and_record(chosen, settings)
