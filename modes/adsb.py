"""
ADS-B Flygtrafik på 1090 MHz
Kör readsb (eller dump1090) som subprocess och läser SBS-format på port 30003.
Mycket mer känslig än en ren Python-detektor.
"""

import shutil
import socket
import subprocess
import sys
import time
import threading
from datetime import datetime
from collections import defaultdict

# ── Global flygplanstabell ────────────────────────────────────────────────────
aircraft      = defaultdict(dict)
aircraft_lock = threading.Lock()
stats         = {"meddelanden": 0}

SBS_PORT = 30003


def update_aircraft(icao, **kwargs):
    with aircraft_lock:
        aircraft[icao].update(kwargs)
        aircraft[icao]["sedd"] = datetime.now()


def find_decoder():
    """Hitta readsb eller dump1090 på systemet."""
    for cmd in ("readsb", "dump1090-fa", "dump1090-mutability", "dump1090"):
        path = shutil.which(cmd)
        if path:
            return cmd, path
    return None, None


def build_command(decoder, gain, ppm):
    """Bygg kommandoraden för readsb eller dump1090."""
    cmd = [decoder, "--device-type", "rtlsdr", "--quiet"]

    if "readsb" in decoder:
        cmd += [
            "--net",
            "--net-sbs-port",    str(SBS_PORT),
            "--net-bo-port",     "0",
            "--net-ro-port",     "0",
            "--net-bi-port",     "0",
            "--net-ri-port",     "0",
        ]
    else:
        cmd += ["--net"]

    if gain != "auto":
        cmd += ["--gain", str(int(gain))]

    if ppm != 0:
        cmd += ["--ppm", str(ppm)]

    return cmd


def parse_sbs_line(line):
    """Tolka en SBS BaseStation-rad och uppdatera flygplanstabellen."""
    parts = line.strip().split(",")
    if len(parts) < 10 or parts[0] != "MSG":
        return

    stats["meddelanden"] += 1
    msg_type = parts[1]
    icao = parts[4].upper()
    if not icao:
        return

    update_aircraft(icao)

    try:
        if msg_type == "1":
            cs = parts[10].strip()
            if cs:
                update_aircraft(icao, callsign=cs)

        elif msg_type == "3":
            alt = int(parts[11])   if parts[11] else None
            lat = float(parts[14]) if parts[14] else None
            lon = float(parts[15]) if parts[15] else None
            if alt is not None:
                update_aircraft(icao, altitude_ft=alt)
            if lat is not None and lon is not None:
                update_aircraft(icao, lat=lat, lon=lon)

        elif msg_type == "4":
            spd = float(parts[12]) if parts[12] else None
            hdg = float(parts[13]) if parts[13] else None
            vr  = float(parts[16]) if parts[16] else None
            if spd is not None:
                update_aircraft(icao, speed_kt=spd)
            if hdg is not None:
                update_aircraft(icao, heading=hdg)
            if vr is not None:
                update_aircraft(icao, vrate=vr)

        elif msg_type == "5":
            alt = int(parts[11]) if parts[11] else None
            if alt is not None:
                update_aircraft(icao, altitude_ft=alt)

    except (ValueError, IndexError):
        pass


def print_table(stop_event):
    """Uppdatera och skriv ut flygplanstabellen var 2:a sekund."""
    while not stop_event.is_set():
        now = datetime.now()
        max_age_s = 60

        with aircraft_lock:
            aktiva = {
                icao: info
                for icao, info in aircraft.items()
                if (now - info.get("sedd", now)).total_seconds() < max_age_s
            }

        print("\033[2J\033[H", end="")
        print(f"  ✈️  ADS-B Flygtrafik  –  {now.strftime('%H:%M:%S')}")
        print(f"  Frekvens: 1090 MHz  |  Ctrl+C för att avsluta")
        print(f"  Meddelanden: {stats['meddelanden']}  |  Flygplan synliga: {len(aktiva)}\n")

        if not aktiva:
            print("  (Inga flygplan synliga ännu – väntar på signaler...)")
        else:
            print(f"  {'ICAO':<8} {'Anropssignal':<12} {'Höjd (ft)':<10} {'Hast. (kt)':<11} {'Kurs':<7} {'Stigbana'}")
            print("  " + "─" * 64)
            for icao, info in sorted(aktiva.items()):
                cs  = info.get("callsign", "–")
                alt = f"{info['altitude_ft']:>8}" if "altitude_ft" in info else "       –"
                spd = f"{info['speed_kt']:>7.0f}"  if "speed_kt"    in info else "      –"
                hdg = f"{info['heading']:>5.0f}°"  if "heading"     in info else "     –"
                vr  = f"{info['vrate']:>+7.0f}"    if "vrate"       in info else "      –"
                print(f"  {icao:<8} {cs:<12} {alt}   {spd}    {hdg}  {vr}")

        time.sleep(2)


def run_adsb(settings: dict | None = None):
    """Starta ADS-B-mottagning via readsb/dump1090."""
    gain = (settings or {}).get("gain", "auto")
    ppm  = (settings or {}).get("ppm",  0)

    decoder, path = find_decoder()
    if not decoder:
        print("\n❌ Varken readsb eller dump1090 hittades.")
        print("   macOS:  brew install readsb")
        print("   Linux:  sudo apt install readsb")
        return

    gain_str = f"{gain} dB" if gain != "auto" else "auto"
    print(f"\n  Använder: {path}")
    print(f"  Gain: {gain_str}  |  PPM: {ppm:+d}")
    print("  Startar mottagning...\n")

    cmd = build_command(decoder, gain, ppm)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except Exception as e:
        print(f"❌ Kunde inte starta {decoder}: {e}")
        return

    # Ge readsb tid att starta och öppna SDR-dongeln
    time.sleep(2.5)

    if proc.poll() is not None:
        err = proc.stderr.read().decode(errors="replace")
        print(f"❌ {decoder} avslutades direkt:\n{err}")
        return

    # Anslut till SBS-porten
    sock = None
    for _ in range(10):
        try:
            sock = socket.create_connection(("127.0.0.1", SBS_PORT), timeout=2)
            break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)

    if sock is None:
        print(f"❌ Kunde inte ansluta till port {SBS_PORT}.")
        proc.terminate()
        return

    stop_event = threading.Event()
    display_thread = threading.Thread(
        target=print_table, args=(stop_event,), daemon=True
    )
    display_thread.start()

    buf = ""
    sock.settimeout(1.0)

    try:
        while True:
            try:
                chunk = sock.recv(4096).decode("ascii", errors="replace")
                if not chunk:
                    print("\n⚠️  Nätverksanslutning till readsb stängdes.")
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    parse_sbs_line(line)
            except socket.timeout:
                pass
    except KeyboardInterrupt:
        print("\n\nAvbruten av användaren.")
    finally:
        stop_event.set()
        sock.close()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
