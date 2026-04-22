"""
ADS-B Flygtrafik på 1090 MHz
Läser IQ-data direkt med pyrtlsdr och avkodar Mode S-meddelanden med pyModeS.
Kräver inte pyModeS[rtl] – all signalbearbetning sköts här.
"""

import sys
import time
import threading
from datetime import datetime
from collections import defaultdict

import numpy as np

try:
    from rtlsdr import RtlSdr
    import pyModeS as pms
except ImportError as e:
    print(f"❌ Saknat paket: {e}")
    print("   Installera med: pip install pyrtlsdr pyModeS numpy")
    sys.exit(1)

# ── Konstanter ───────────────────────────────────────────────────────────────
SAMPLE_RATE = 2.0e6          # 2 MHz – standard för ADS-B
CENTER_FREQ = 1090e6         # 1090 MHz
GAIN        = 40             # dB, justera om mottagningen är dålig
CHUNK       = 256 * 1024     # Antal samplingar per läsning

# ── Global flygplanstabell ────────────────────────────────────────────────────
aircraft      = defaultdict(dict)
aircraft_lock = threading.Lock()
stats         = {"meddelanden": 0, "giltiga": 0}


def update_aircraft(icao, **kwargs):
    with aircraft_lock:
        aircraft[icao].update(kwargs)
        aircraft[icao]["sedd"] = datetime.now()


# ── ADS-B signaldetektering ───────────────────────────────────────────────────
# Vid 2 MHz = 2 samplingar per μs.
# Preamble: pulser vid 0, 1, 3.5, 4.5 μs → sample 0,2,7,9.
# Bit-period: 1 μs = 2 samplingar (PPM: hög-låg = 1, låg-hög = 0)

PREAMBLE_LEN  = 16   # 8 μs × 2 samplingar/μs
SHORT_MSG_LEN = 112  # 56 bitar × 2 samplingar/bitar
LONG_MSG_LEN  = 224  # 112 bitar × 2 samplingar/bitar


def detect_messages(mag):
    """
    Sök igenom amplituddata och extrahera ADS-B hex-strängar.
    Returnerar en lista med giltiga hex-strängar (CRC-kontrollerade).
    """
    messages = []
    n = len(mag)
    i = 0

    while i < n - PREAMBLE_LEN - LONG_MSG_LEN:
        p = mag[i:i + PREAMBLE_LEN]

        high = p[0] > p[1] and p[2] > p[3] and p[7] > p[6] and p[9] > p[8]
        low  = (p[1] < p[0] and p[1] < p[2] and
                p[3] < p[2] and p[3] < p[4] and
                p[6] < p[5] and p[6] < p[7] and
                p[8] < p[7] and p[8] < p[9])

        if not (high and low):
            i += 1
            continue

        data_start = i + PREAMBLE_LEN

        for msg_len in (LONG_MSG_LEN, SHORT_MSG_LEN):
            if data_start + msg_len > n:
                continue

            bits = []
            ok = True
            for b in range(msg_len // 2):
                s0 = mag[data_start + b * 2]
                s1 = mag[data_start + b * 2 + 1]
                if s0 == s1:
                    ok = False
                    break
                bits.append(1 if s0 > s1 else 0)

            if not ok:
                continue

            hex_str = ""
            for byte_i in range(len(bits) // 8):
                byte_val = 0
                for bit_i in range(8):
                    byte_val = (byte_val << 1) | bits[byte_i * 8 + bit_i]
                hex_str += f"{byte_val:02X}"

            try:
                if pms.crc(hex_str) == 0:
                    messages.append(hex_str)
                    stats["giltiga"] += 1
                    i += PREAMBLE_LEN + msg_len
                    break
            except Exception:
                pass

        i += 1

    return messages


def decode_message(msg):
    """Avkoda ett Mode S-meddelande och uppdatera flygplanstabellen."""
    stats["meddelanden"] += 1
    try:
        icao = pms.icao(msg)
        if not icao:
            return

        df = pms.df(msg)

        if df == 17 or df == 18:
            tc = pms.adsb.typecode(msg)

            if 1 <= tc <= 4:
                cs = pms.adsb.callsign(msg).strip()
                if cs:
                    update_aircraft(icao, callsign=cs)

            elif 9 <= tc <= 18:
                alt = pms.adsb.altitude(msg)
                if alt is not None:
                    update_aircraft(icao, altitude_ft=alt)

            elif tc == 19:
                velocity = pms.adsb.velocity(msg)
                if velocity:
                    spd, hdg, vr, _ = velocity
                    update_aircraft(icao, speed_kt=spd, heading=hdg, vrate=vr)

        elif df == 11:
            update_aircraft(icao)

    except Exception:
        pass


def iq_to_magnitude(samples):
    """Konvertera komplexa IQ-samplingar till normaliserad amplitud."""
    mag = np.abs(samples).astype(np.float32)
    max_val = mag.max()
    if max_val > 0:
        mag /= max_val
    return mag


def print_table(stop_event):
    """Uppdatera och skriv ut flygplanstabellen var 3:e sekund."""
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
        print(f"  Meddelanden totalt: {stats['meddelanden']}  |  Giltiga: {stats['giltiga']}  |  Flygplan synliga: {len(aktiva)}\n")

        if not aktiva:
            print("  (Inga flygplan synliga ännu – väntar på signaler...)")
        else:
            print(f"  {'ICAO':<8} {'Anropssignal':<12} {'Höjd (ft)':<12} {'Hast. (kt)':<12} {'Kurs':<8} {'Stigbana'}")
            print("  " + "─" * 66)
            for icao, info in sorted(aktiva.items()):
                cs  = info.get("callsign", "–")
                alt = f"{info['altitude_ft']:>8}" if "altitude_ft" in info else "       –"
                spd = f"{info['speed_kt']:>8.0f}"  if "speed_kt"    in info else "       –"
                hdg = f"{info['heading']:>5.0f}°"  if "heading"     in info else "     –"
                vr  = f"{info['vrate']:>+7.0f}"    if "vrate"       in info else "      –"
                print(f"  {icao:<8} {cs:<12} {alt}     {spd}     {hdg}   {vr}")

        time.sleep(3)


def run_adsb(settings: dict | None = None):
    """Starta ADS-B-mottagning med pyrtlsdr."""
    gain = (settings or {}).get("gain", GAIN)
    ppm  = (settings or {}).get("ppm",  0)

    print("\n" + "="*50)
    print(" Lyssnar på flygtrafik (ADS-B 1090 MHz)")
    print(" Tryck Ctrl+C för att avsluta")
    print("="*50 + "\n")

    try:
        sdr = RtlSdr()
    except Exception as e:
        print(f"❌ Kunde inte öppna SDR-dongle: {e}")
        print("   Kontrollera att dongeln är inkopplad.")
        return

    sdr.sample_rate = SAMPLE_RATE
    sdr.center_freq = CENTER_FREQ
    sdr.gain        = gain
    if ppm != 0:
        sdr.freq_correction = ppm

    gain_str = f"{gain} dB" if gain != "auto" else "auto"
    print(f"  Samplingsfrekvens : {SAMPLE_RATE/1e6:.1f} MHz")
    print(f"  Centerfrekvens    : {CENTER_FREQ/1e6:.0f} MHz")
    print(f"  Förstärkning      : {gain_str}  |  PPM: {ppm:+d}\n")
    print("  Startar mottagning...\n")

    stop_event = threading.Event()
    display_thread = threading.Thread(
        target=print_table, args=(stop_event,), daemon=True
    )
    display_thread.start()

    try:
        while True:
            samples = sdr.read_samples(CHUNK)
            mag     = iq_to_magnitude(samples)
            for msg in detect_messages(mag):
                decode_message(msg)

    except KeyboardInterrupt:
        print("\n\nAvbruten av användaren.")
    except Exception as e:
        print(f"\n❌ Fel under mottagning: {e}")
    finally:
        stop_event.set()
        sdr.close()
