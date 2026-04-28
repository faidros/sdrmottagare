"""
ISS – Internationella rymdstationen (NORAD 25544)

ISS sänder på 145.825 MHz med AFSK1200-modulerat APRS under varje pass.
Paketen innehåller positioner, statusmeddelanden och tele­metri från
amatör­radio­utrustningen ombord (ARISS – Amateur Radio on ISS).

Ibland sänds även SSTV-bilder (145.800 MHz) under speciella evenemang –
programmet visar när nästa pass är och avkodar APRS-paket i realtid.

Flöde:
  ephem (passprediktion) → nedräkning → rtl_fm | multimon-ng (AFSK1200) → APRS-display

Krav:
  pip install ephem
  rtl_fm       – ingår i librtlsdr / rtl-sdr  (brew install librtlsdr)
  multimon-ng  – AFSK1200-avkodare             (brew install multimon-ng)
"""

import json
import os
import re
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

# ── Konfiguration ──────────────────────────────────────────────────────────────

CONFIG_FILE   = Path.home() / ".sdrmottagare.json"  # Delas med satellite.py
DATA_DIR      = Path.home() / "sdr_data" / "iss"    # Sparas här
TLE_URL       = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"
ISS_NAME      = "ISS (ZARYA)"
APRS_FREQ     = "145.825M"   # Hz – ARISS APRS nedlänk
SSTV_FREQ     = "145.800M"   # Hz – ARISS SSTV (sporadiskt)
MIN_ELEVATION = 10            # Minsta maxelevation för ett bra pass
AUDIO_RATE    = 22050         # Hz – output till multimon-ng (kräver 22050 Hz för -t raw)
IQ_RATE       = 480000        # Hz – IQ-samplingsfrekvens för rtl_fm (ger bättre FM-demodulering)


# ── Hjälpfunktioner ────────────────────────────────────────────────────────────

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


from modes.location import ask_position


# ── TLE-hämtning ───────────────────────────────────────────────────────────────

def fetch_tle() -> tuple[str, str, str] | None:
    """Hämta ISS TLE från Celestrak (NORAD 25544)."""
    print(f"\n  Hämtar TLE-data från Celestrak...", end="", flush=True)
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(TLE_URL, timeout=10, context=ctx) as resp:
            lines = [l.strip() for l in resp.read().decode().splitlines() if l.strip()]
    except Exception as e:
        print(f"\n  ❌ Kunde inte hämta TLE: {e}")
        return None

    # TLE-svaret för en enskild satellit innehåller exakt 3 rader
    if len(lines) >= 3:
        print(" ✅")
        return lines[0], lines[1], lines[2]

    print(f"\n  ❌ Oväntat TLE-format från Celestrak.")
    return None


# ── Passprediktion ─────────────────────────────────────────────────────────────

def ephem_date_to_dt(d) -> datetime:
    t = d.tuple()
    return datetime(t[0], t[1], t[2], t[3], t[4], int(t[5]), tzinfo=timezone.utc)


def find_passes(lat: float, lon: float, elev: int,
                tle: tuple[str, str, str], count: int = 6) -> list[dict]:
    """Hitta de N nästa passagerna med minst MIN_ELEVATION° maxelevation."""
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
                "aos":    aos_dt,
                "los":    los_dt,
                "max_el": round(float(max_elev) * 180 / 3.14159, 1),
                "dur_s":  int(dur),
            })

        obs.date = los + ephem.minute  # sök efter LOS

    return passes


def format_pass(p: dict, idx: int) -> str:
    now       = datetime.now(timezone.utc)
    delta_s   = (p["aos"] - now).total_seconds()
    wait_m    = int(delta_s // 60)
    wait_h    = wait_m // 60
    wait_m2   = wait_m % 60

    aos_local = p["aos"].astimezone().strftime("%H:%M")
    los_local = p["los"].astimezone().strftime("%H:%M")
    date_str  = p["aos"].astimezone().strftime("%d %b")

    if delta_s < 0:
        wait_str = "  pågår nu "
    elif wait_h > 0:
        wait_str = f"  om {wait_h}h {wait_m2:02d}m"
    else:
        wait_str = f"  om {wait_m:3d} min "

    qual = ("🟢 Bra " if p["max_el"] > 40
            else "🟡 OK  " if p["max_el"] > 20
            else "🔴 Låg ")

    return (f"  {idx}. {date_str}  {aos_local}–{los_local}"
            f"  ({p['dur_s']//60}m {p['dur_s']%60:02d}s)"
            f"  Max {p['max_el']:4.0f}°  {qual} {wait_str}")


# ── APRS-parsning ──────────────────────────────────────────────────────────────

def parse_aprs(line: str) -> dict | None:
    """
    Tolka en rad från multimon-ng, t.ex.:
      AFSK1200: RS0ISS-3>CQ,ARISS:>ISS has a clear view
    Returnerar dict med from, to, path, message – eller None om raden inte är APRS.
    """
    m = re.match(r"AFSK1200:\s+(.+?)>([^,:]+)(?:,([^:]+))?:(.*)", line)
    if not m:
        return None
    return {
        "from":    m.group(1).strip(),
        "to":      m.group(2).strip(),
        "path":    m.group(3).strip() if m.group(3) else "",
        "message": m.group(4).strip(),
    }


def format_aprs(pkt: dict, ts: str) -> str:
    """Formatera ett APRS-paket för terminalen."""
    src  = pkt["from"]
    msg  = pkt["message"]

    # Försök extrahera koordinater ur APRS-meddelande (rå positionsformat)
    pos_m = re.search(r"(\d{4}\.\d+)([NS]).([\d]{5}\.\d+)([EW])", msg)
    pos_str = ""
    if pos_m:
        lat_deg = int(pos_m.group(1)[:2]) + float(pos_m.group(1)[2:]) / 60
        lon_deg = int(pos_m.group(3)[:3]) + float(pos_m.group(3)[3:]) / 60
        if pos_m.group(2) == "S":
            lat_deg = -lat_deg
        if pos_m.group(4) == "W":
            lon_deg = -lon_deg
        pos_str = f"  📍 {lat_deg:.4f}°N  {lon_deg:.4f}°E"

    # Ta bort positionsdata ur meddelandet för renare visning
    clean_msg = re.sub(r"[=!/\\]?\d{4}\.\d+[NS].[\d]{5}\.\d+[EW].", "", msg).strip()
    if not clean_msg:
        clean_msg = msg

    return f"  {ts}  {src:<12}  {clean_msg[:60]}{pos_str}"


# ── Mottagning ─────────────────────────────────────────────────────────────────

def make_pass_dir(p: dict) -> Path:
    """Skapa och returnera en katalog för detta pass."""
    ts  = p["aos"].astimezone().strftime("%Y-%m-%d_%H%M")
    d   = DATA_DIR / ts
    d.mkdir(parents=True, exist_ok=True)
    return d


def _decode_audio_post(audio_file: Path, jsonl_file: Path, txt_file: Path) -> int:
    """Avkoda audio.raw med multimon-ng i efterhand. Returnerar antal paket."""
    try:
        result = subprocess.run(
            ["multimon-ng", "-t", "raw", "-q", "-a", "AFSK1200", str(audio_file)],
            capture_output=True, text=True, timeout=120,
        )
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        pkts = [parse_aprs(l) for l in lines]
        pkts = [p for p in pkts if p]
        if not pkts:
            return 0
        with open(jsonl_file, "a") as fj, open(txt_file, "a") as ft:
            ft.write("\n── Post-pass avkodning av audio.raw ──\n")
            for pkt in pkts:
                record = {
                    "time":    datetime.now().isoformat(),
                    "from":    pkt["from"],
                    "to":      pkt["to"],
                    "path":    pkt["path"],
                    "message": pkt["message"],
                    "raw":     pkt.get("raw", ""),
                    "source":  "post-decode",
                }
                fj.write(json.dumps(record, ensure_ascii=False) + "\n")
                ft.write(f"[post]  {pkt['from']:<12}  {pkt['message']}\n")
                print(f"  📦 {pkt['from']:<10}  {pkt['message']}")
        return len(pkts)
    except Exception as e:
        print(f"  (post-avkodning misslyckades: {e})")
        return 0


def receive_pass(p: dict, settings: dict):
    """Vänta på AOS och ta emot APRS + spela in FM-ljud under passet."""
    import select

    gain = settings.get("gain", 40)
    ppm  = settings.get("ppm",  0)

    aos_local = p["aos"].astimezone().strftime("%H:%M:%S")
    los_local = p["los"].astimezone().strftime("%H:%M:%S")

    # Skapa katalog för detta pass
    pass_dir   = make_pass_dir(p)
    jsonl_file = pass_dir / "aprs.jsonl"   # Maskinläsbar, ett JSON-objekt per rad
    txt_file   = pass_dir / "aprs.txt"     # Läsbar logg
    audio_file = pass_dir / "audio.raw"    # Raw signed 16-bit 22050 Hz mono FM (IQ samplas vid 480 kHz)

    print(f"\n  🛸 ISS  |  AOS {aos_local}  →  LOS {los_local}")
    print(f"  Max elevation: {p['max_el']:.0f}°  |  Varaktighet: {p['dur_s']//60}m {p['dur_s']%60:02d}s")
    print(f"  Frekvens: {APRS_FREQ} (ARISS APRS)")
    print(f"  Sparar till: {pass_dir}\n")
    print("─" * 60)

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
            print(f"\r  ⏳ {h:02d}:{m:02d}:{s:02d}  [{bar}]  AOS {aos_local}  ", end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n  Avbruten.")
        return

    # ── Pass börjar ────────────────────────────────────────────────
    print(f"\n\n  🟢 AOS! Startar APRS-mottagning + inspelning...\n")
    print(f"  {'Tid':<10}  {'Källa':<12}  {'Meddelande'}")
    print("  " + "─" * 56)

    # Pipeline: rtl_fm → tee (→ audio.raw) → multimon-ng
    rtlfm_cmd = [
        "rtl_fm",
        "-f", APRS_FREQ,
        "-M", "fm",
        "-s", str(IQ_RATE),    # IQ-samplingsfrekvens (480 kHz → bättre filtrering)
        "-r", str(AUDIO_RATE), # Resampla till 22050 Hz (multimon-ng kräver detta)
        "-g", str(int(gain)),  # Manuell gain – stänger av hårdvaru-AGC
        "-p", str(int(ppm)),
    ]
    tee_cmd = ["tee", str(audio_file)]   # Skriver en kopia till disk, skickar vidare
    multimon_cmd = [
        "multimon-ng",
        "-t", "raw",
        "-q",           # Tyst – skriv bara avkodade paket
        "-a", "AFSK1200",
        "-",
    ]

    rtlfm   = None
    tee_p   = None
    multimon = None
    pkt_count = 0

    try:
        rtlfm = subprocess.Popen(
            rtlfm_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        # tee skriver FM-rådata till audio_file OCH skickar vidare till multimon-ng
        tee_p = subprocess.Popen(
            tee_cmd,
            stdin=rtlfm.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        rtlfm.stdout.close()  # Tillåt rtlfm att få SIGPIPE om tee avslutas

        multimon = subprocess.Popen(
            multimon_cmd,
            stdin=tee_p.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        tee_p.stdout.close()  # Tillåt tee att få SIGPIPE om multimon avslutas

        end_time = p["los"]

        # Öppna loggfiler
        with open(jsonl_file, "w") as fj, open(txt_file, "w") as ft:
            # Skriv header i textfilen
            ft.write(f"ISS APRS-logg – {p['aos'].astimezone().strftime('%Y-%m-%d')}\n")
            ft.write(f"AOS: {aos_local}  LOS: {los_local}  "
                     f"Max el: {p['max_el']:.0f}°  Frekvens: {APRS_FREQ}\n")
            ft.write("─" * 60 + "\n")
            ft.flush()

            while True:
                now    = datetime.now(timezone.utc)
                remain = (end_time - now).total_seconds()
                if remain <= 0:
                    break

                rlist, _, _ = select.select([multimon.stdout], [], [], 0.5)
                if rlist:
                    line = multimon.stdout.readline()
                    if not line:
                        break
                    raw  = line.rstrip()
                    pkt  = parse_aprs(raw)
                    if pkt:
                        ts_now = datetime.now()
                        ts_str = ts_now.strftime("%H:%M:%S")

                        # Visa i terminalen
                        print(format_aprs(pkt, ts_str))

                        # Spara som JSON (maskinläsbar)
                        record = {
                            "time":    ts_now.isoformat(),
                            "from":    pkt["from"],
                            "to":      pkt["to"],
                            "path":    pkt["path"],
                            "message": pkt["message"],
                            "raw":     raw,
                        }
                        fj.write(json.dumps(record, ensure_ascii=False) + "\n")
                        fj.flush()

                        # Spara som text (läsbar)
                        ft.write(f"{ts_str}  {pkt['from']:<12}  {pkt['message']}\n")
                        ft.flush()

                        pkt_count += 1

                m2, s2 = divmod(int(remain), 60)
                print(f"\r  🔴 LOS om {m2:02d}:{s2:02d}  |  {pkt_count} paket mottagna  ",
                      end="", flush=True)

    except FileNotFoundError as e:
        missing = "rtl_fm" if "rtl_fm" in str(e) else "multimon-ng"
        print(f"\n  ❌ '{missing}' hittades inte.")
        if missing == "rtl_fm":
            print("     Installera: brew install librtlsdr")
        else:
            print("     Installera: brew install multimon-ng")
        return
    except KeyboardInterrupt:
        print("\n\n  Avbruten av användaren.")
    finally:
        for proc in (multimon, tee_p, rtlfm):
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()

    # ── Sammanfattning ──────────────────────────────────────────────
    print(f"\n\n  🏁 LOS. Passet avslutat.")
    print(f"\n  ─── Sparade filer ───────────────────────────────────")
    print(f"  📁 {pass_dir}/")

    if jsonl_file.exists() and jsonl_file.stat().st_size > 0:
        print(f"  📄 aprs.jsonl  – {pkt_count} APRS-paket  (JSON Lines, ett per rad)")
        print(f"  📄 aprs.txt    – läsbar logg")
    else:
        print(f"  ⚠️  Inga APRS-paket togs emot under passet.")
        # Försök avkoda audio.raw i efterhand
        if audio_file.exists() and audio_file.stat().st_size > 0:
            print(f"\n  🔄 Försöker avkoda audio.raw i efterhand...")
            post_count = _decode_audio_post(audio_file, jsonl_file, txt_file)
            if post_count > 0:
                print(f"  ✅ Hittade {post_count} APRS-paket i inspelningen!")
                print(f"  📄 aprs.jsonl  – {post_count} paket")
                print(f"  📄 aprs.txt    – uppdaterad")
            else:
                print(f"  ❌ Inga paket hittades i audio.raw (svag signal eller ISS sände ej).")

    if audio_file.exists():
        kb = audio_file.stat().st_size // 1024
        print(f"  🎙️  audio.raw   – {kb} KB  (signed 16-bit {AUDIO_RATE} Hz mono FM)")
        print(f"\n  Ljud kan öppnas i Audacity: File → Import → Raw Data")
        print(f"  Inställningar: Signed 16-bit PCM, {AUDIO_RATE} Hz, Mono")
        print(f"  SSTV-avkodning: öppna i QSSTV och välj auto-detect")

    print(f"\n  Öppna mappen: open \"{pass_dir}\"")


# ── Huvudfunktion ──────────────────────────────────────────────────────────────

def run_iss(settings: dict | None = None):
    settings = settings or {}

    print("\n" + "=" * 60)
    print(" 🛸 ISS – Internationella rymdstationen (ARISS APRS)")
    print(" 145.825 MHz  |  AFSK1200  |  APRS-paket i realtid")
    print("=" * 60)

    print("""
  ISS passerar varje plats 4–6 gånger per dygn och varje
  pass varar 5–15 minuter. Amatörradioutrustningen ombord
  (ARISS) sänder kontinuerligt APRS-paket på 145.825 MHz
  med positioner, statusmeddelanden och telemetri.

  Sporadiskt sänds även SSTV-bilder på 145.800 MHz under
  specialarrangemang – kolla ariss.net för schema.

  ⚠️  Antenn: En enkel 2m dipol (~50 cm per arm) eller
      handhållen antenn fungerar bra under höga pass.
""")

    # Krav: ephem
    if ephem is None:
        print("  ❌ Python-paketet 'ephem' saknas.")
        print("     Installera med: pip install ephem")
        return

    # Krav: rtl_fm
    if not any(Path(d) / "rtl_fm" for d in os.environ.get("PATH", "").split(":")):
        import shutil
        if not shutil.which("rtl_fm"):
            print("  ❌ 'rtl_fm' saknas.")
            print("     Installera: brew install librtlsdr")
            return

    # Krav: multimon-ng
    import shutil
    if not shutil.which("multimon-ng"):
        print("  ❌ 'multimon-ng' saknas.")
        print("     Installera: brew install multimon-ng")
        return

    # Position
    cfg = load_config()
    cfg = ask_position(cfg)
    lat  = cfg["lat"]
    lon  = cfg["lon"]
    elev = cfg.get("elevation", 0)

    # TLE
    tle = fetch_tle()
    if tle is None:
        return

    print(f"\n  TLE: {tle[0]}")

    # Passlista
    print(f"\n  Beräknar passager (minst {MIN_ELEVATION}° maxelevation)...", end="", flush=True)
    passes = find_passes(lat, lon, elev, tle, count=8)
    print(f" hittade {len(passes)} pass.\n")

    if not passes:
        print("  ❌ Inga passager hittades. Kontrollera positionen.")
        return

    print("  Kommande ISS-passager:")
    print("  " + "─" * 56)
    for i, p in enumerate(passes, 1):
        print(format_pass(p, i))
    print("  " + "─" * 56)

    print("""
  SSTV-evenemang: https://ariss.net/sstv.html
  APRS-historik:  https://aprs.fi/#call=RS0ISS
""")

    # Val av pass
    print("  Vilket pass vill du lyssna på? (1–{}, Enter=1, 0=avsluta): ".format(len(passes)), end="")
    try:
        val = input().strip()
        if val == "0":
            return
        idx = int(val) - 1 if val else 0
        if not (0 <= idx < len(passes)):
            print("  Ogiltigt val.")
            return
    except (ValueError, KeyboardInterrupt):
        print("\n  Avbruten.")
        return

    chosen = passes[idx]

    # Starta mottagning
    receive_pass(chosen, settings)
