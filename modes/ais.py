"""
AIS Fartygsidentifikation
Använder AIS-catcher som extern process för demodulering och avkodning.
NMEA-rader levereras via TCP port 10110 och parsas med pyais.
"""

import socket
import subprocess
import threading
import time
from datetime import datetime

from pyais import decode as ais_decode

# ── Konstanter ────────────────────────────────────────────────────────────────

GAIN         = 40         # dB (heltal eller "auto")
AIS_PORT     = 10110      # TCP-port AIS-catcher skickar NMEA till
CONN_TIMEOUT = 10         # sekunder att vänta på att AIS-catcher startar

STATUS_CODES = {
    0: "Under gång (motor)", 1: "För ankar",    2: "Inte manöverbar",
    3: "Begränsad manöverförmåga",              5: "Förtöjd",
    7: "Fiskar",                                15: "–",
}

# ── Globalt fartygsregister ───────────────────────────────────────────────────

vessels: dict = {}
vessels_lock  = threading.Lock()
stats         = {"meddelanden": 0, "giltiga": 0}


def update_vessel(mmsi: str, **fields):
    with vessels_lock:
        if mmsi not in vessels:
            vessels[mmsi] = {}
        vessels[mmsi].update(fields)
        vessels[mmsi]["sedd"] = datetime.now()


# ── AIS-catcher subprocess ────────────────────────────────────────────────────

def find_aiscatcher() -> str | None:
    import shutil
    for name in ("AIS-catcher", "aiscatcher"):
        path = shutil.which(name)
        if path:
            return path
    return None


def build_command(binary: str, gain, ppm: int) -> list:
    cmd = [binary, "-r", "rtlsdr"]
    if gain == "auto":
        cmd += ["-gr", "TUNER", "auto"]
    else:
        cmd += ["-gr", "TUNER", str(int(gain))]
    if ppm != 0:
        cmd += ["-p", str(ppm)]
    cmd += ["-S", str(AIS_PORT), "-q"]
    return cmd


# ── NMEA-parsning ─────────────────────────────────────────────────────────────

def parse_nmea_line(line: str):
    line = line.strip()
    if not line.startswith("!AIVDM") and not line.startswith("!AIVDO"):
        return
    try:
        stats["meddelanden"] += 1
        msg = ais_decode(line.encode())
        d   = msg.asdict()
        mmsi = str(d.get("mmsi", ""))
        if not mmsi:
            return
        update_vessel(mmsi, **{k: v for k, v in d.items() if v is not None})
        stats["giltiga"] += 1
    except Exception:
        pass


# ── Presentation ──────────────────────────────────────────────────────────────

def print_table(stop_event: threading.Event):
    while not stop_event.is_set():
        now       = datetime.now()
        max_age_s = 600

        with vessels_lock:
            aktiva = {
                mmsi: info for mmsi, info in vessels.items()
                if (now - info.get("sedd", now)).total_seconds() < max_age_s
            }

        print("\033[2J\033[H", end="")
        print(f"  ⚓ AIS Fartygsidentifikation  –  {now.strftime('%H:%M:%S')}")
        print(f"  Kanaler: 161.975 & 162.025 MHz  |  Ctrl+C för att avsluta")
        print(f"  Meddelanden: {stats['meddelanden']}  |  Giltiga: {stats['giltiga']}  |  Fartyg synliga: {len(aktiva)}\n")

        if not aktiva:
            print("  (Inga fartyg synliga ännu – väntar på signaler...)")
        else:
            print(f"  {'MMSI':<12} {'Namn':<22} {'Fart (kt)':<11} {'Kurs':<8} {'Lat':>10} {'Lon':>11}  Status")
            print("  " + "─" * 82)
            for mmsi, info in sorted(aktiva.items()):
                namn      = str(info.get("shipname", info.get("name", "–"))).strip() or "–"
                fart      = f"{info['speed']:.1f}"    if info.get("speed")  is not None else "–"
                kurs      = f"{info['course']:.0f}°"  if info.get("course") is not None else "–"
                lat       = f"{info['lat']:.4f}"      if info.get("lat")    is not None else "–"
                lon       = f"{info['lon']:.4f}"      if info.get("lon")    is not None else "–"
                status_nr = info.get("status", 15)
                status    = STATUS_CODES.get(status_nr, str(status_nr))
                print(f"  {mmsi:<12} {namn:<22} {fart:<11} {kurs:<8} {lat:>10} {lon:>11}  {status}")

        time.sleep(3)


# ── TCP-läsning ───────────────────────────────────────────────────────────────

def tcp_reader(stop_event: threading.Event):
    buf  = ""
    sock = None

    deadline = time.time() + CONN_TIMEOUT
    while time.time() < deadline and not stop_event.is_set():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect(("127.0.0.1", AIS_PORT))
            break
        except OSError:
            if sock:
                sock.close()
            sock = None
            time.sleep(0.5)

    if sock is None:
        if not stop_event.is_set():
            print("\n❌ Kunde inte ansluta till AIS-catcher – kontrollera att dongeln är inkopplad.")
        stop_event.set()
        return

    sock.settimeout(1)
    try:
        while not stop_event.is_set():
            try:
                chunk = sock.recv(4096).decode("ascii", errors="ignore")
                if not chunk:
                    break
                buf  += chunk
                lines = buf.split("\n")
                buf   = lines[-1]
                for line in lines[:-1]:
                    parse_nmea_line(line)
            except socket.timeout:
                continue
            except OSError:
                break
    finally:
        sock.close()
    stop_event.set()


# ── Huvudloop ─────────────────────────────────────────────────────────────────

def run_ais(settings: dict | None = None):
    gain = (settings or {}).get("gain", GAIN)
    ppm  = (settings or {}).get("ppm",  0)

    binary = find_aiscatcher()
    if not binary:
        print("❌ AIS-catcher hittades inte.")
        print("   Bygg och installera med:")
        print("   git clone https://github.com/jvde-github/AIS-catcher")
        print("   cd AIS-catcher && mkdir build && cd build")
        print("   cmake .. && make -j4 && sudo cp AIS-catcher /usr/local/bin/")
        return

    cmd = build_command(binary, gain, ppm)

    print("\n" + "=" * 50)
    print(" Lyssnar på fartyg (AIS 161.975 / 162.025 MHz)")
    print(" Tryck Ctrl+C för att avsluta")
    print("=" * 50 + "\n")
    gain_str = f"{gain} dB" if gain != "auto" else "auto"
    print(f"  Avkodare       : AIS-catcher ({binary})")
    print(f"  Förstärkning   : {gain_str}  |  PPM: {ppm:+d}")
    print(f"  NMEA TCP-port  : {AIS_PORT}")
    print(f"  Kommando       : {' '.join(cmd)}\n")
    print("  Startar AIS-catcher...\n")
    time.sleep(1)

    stop_event = threading.Event()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"❌ Kunde inte starta AIS-catcher: {e}")
        return

    display_thread = threading.Thread(target=print_table, args=(stop_event,), daemon=True)
    display_thread.start()

    reader_thread = threading.Thread(target=tcp_reader, args=(stop_event,), daemon=True)
    reader_thread.start()

    try:
        while not stop_event.is_set():
            if proc.poll() is not None:
                if not stop_event.is_set():
                    print("\n❌ AIS-catcher avslutades oväntat.")
                stop_event.set()
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n\nAvbruten av användaren.")
    finally:
        stop_event.set()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
