"""
Spektrumanalysator & Frekvensskanner
- Spektrum: realtids FFT-display på en vald frekvens
- Skanner:  sveper över ett frekvensband och hittar aktiva signaler
"""

import sys
import time
import threading
import math
from datetime import datetime

import numpy as np

try:
    from rtlsdr import RtlSdr
except ImportError as e:
    print(f"❌ Saknat paket: {e}"); sys.exit(1)

# ── Konstanter ────────────────────────────────────────────────────────────────
SAMPLE_RATE  = 2_048_000    # 2.048 MHz – bred bandbredd för spektrum
FFT_SIZE     = 2048
GAIN         = "auto"
DISPLAY_W    = 70           # Antal kolumner i spektrumvisningen
DISPLAY_H    = 20           # Antal rader i spektrumvisningen


# ═══════════════════════════════════════════════════════════════════════════════
#  Hjälpfunktioner
# ═══════════════════════════════════════════════════════════════════════════════

def power_db(samples: np.ndarray, fft_size: int = FFT_SIZE) -> np.ndarray:
    """Beräkna FFT-effektspektrum i dB (medelvärde av flera fönster)."""
    n_windows = len(samples) // fft_size
    if n_windows == 0:
        return np.full(fft_size, -100.0)

    window = np.hanning(fft_size)
    acc    = np.zeros(fft_size)

    for i in range(n_windows):
        chunk  = samples[i * fft_size:(i + 1) * fft_size]
        spec   = np.fft.fftshift(np.fft.fft(chunk * window))
        acc   += np.abs(spec) ** 2

    acc /= n_windows
    acc  = np.maximum(acc, 1e-20)
    return 10 * np.log10(acc)


def db_to_bar(db: float, db_min: float, db_max: float, width: int) -> str:
    """Konvertera dB-värde till ASCII-stapel."""
    ratio = (db - db_min) / max(db_max - db_min, 1)
    ratio = max(0.0, min(1.0, ratio))
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def format_freq(hz: float) -> str:
    """Formatera frekvens läsbart."""
    if hz >= 1e9:
        return f"{hz/1e9:.4f} GHz"
    elif hz >= 1e6:
        return f"{hz/1e6:.4f} MHz"
    else:
        return f"{hz/1e3:.1f} kHz"


def measure_rssi(sdr: RtlSdr, samples: int = 65536) -> float:
    """Mät medeleffekt på nuvarande frekvens (dBFS)."""
    s   = sdr.read_samples(samples)
    pwr = np.mean(np.abs(s) ** 2)
    return 10 * np.log10(max(pwr, 1e-20))


# ═══════════════════════════════════════════════════════════════════════════════
#  Läge 1: Realtidsspektrum
# ═══════════════════════════════════════════════════════════════════════════════

def run_spectrum(sdr: RtlSdr):
    """Visa realtids ASCII-spektrum runt vald frekvens."""
    cf   = sdr.center_freq
    fs   = sdr.sample_rate
    bw   = fs / 1e6

    print(f"\n  Spektrum @ {format_freq(cf)}  ±{bw/2:.2f} MHz  |  Ctrl+C för att avsluta\n")
    time.sleep(0.3)

    # Rullande min/max för auto-skala
    db_floor  = -60.0
    db_ceil   = -20.0
    smoothed  = None

    try:
        while True:
            samples = sdr.read_samples(FFT_SIZE * 8)
            psd     = power_db(samples)

            # Exponentiell utjämning (0.3 = snabb respons, 0.7 = trög men snygg)
            if smoothed is None:
                smoothed = psd.copy()
            else:
                smoothed = 0.4 * psd + 0.6 * smoothed

            # Auto-skala (mjuk)
            db_floor = 0.95 * db_floor + 0.05 * (np.percentile(smoothed, 10) - 5)
            db_ceil  = 0.95 * db_ceil  + 0.05 * (np.percentile(smoothed, 99) + 3)

            # Decimera till DISPLAY_W punkter
            step   = len(smoothed) // DISPLAY_W
            bins   = [smoothed[i * step:(i + 1) * step].mean()
                      for i in range(DISPLAY_W)]
            peak_i = int(np.argmax(bins))

            # Bygg display
            lines = []
            lines.append(f"\033[2J\033[H")
            lines.append(f"  📻 Spektrum  {format_freq(cf)}  ±{bw/2:.3f} MHz"
                         f"  [{datetime.now().strftime('%H:%M:%S')}]")
            lines.append(f"  Skala: {db_floor:.0f} dB  →  {db_ceil:.0f} dB"
                         f"  |  Ctrl+C för att avsluta\n")

            # Frekvensaxel (visas ovanpå)
            f_low  = (cf - fs / 2) / 1e6
            f_high = (cf + fs / 2) / 1e6
            f_mid  = cf / 1e6
            lines.append(f"  {f_low:>8.3f} MHz"
                         + " " * (DISPLAY_W - 24)
                         + f"{f_mid:.3f}"
                         + " " * 5
                         + f"{f_high:.3f} MHz")
            lines.append("  " + "┬" + "─" * (DISPLAY_W // 2 - 1)
                         + "┼" + "─" * (DISPLAY_W // 2 - 1) + "┬")

            # Spektrumrader (horisontellt vattenfallsdiagram)
            peak_db = bins[peak_i]
            for row in range(DISPLAY_H, 0, -1):
                threshold = db_floor + (db_ceil - db_floor) * row / DISPLAY_H
                bar = ""
                for b in bins:
                    if b >= threshold:
                        # Färgkodning baserat på styrka
                        rel = (b - db_floor) / max(db_ceil - db_floor, 1)
                        if rel > 0.8:
                            bar += "\033[91m█\033[0m"   # Röd = stark
                        elif rel > 0.5:
                            bar += "\033[93m█\033[0m"   # Gul = medel
                        else:
                            bar += "\033[92m█\033[0m"   # Grön = svag
                    else:
                        bar += " "
                db_label = f"{threshold:6.1f} dB │"
                lines.append(f"  {db_label} {bar}")

            lines.append("  " + "─" * 9 + "┴" + "─" * DISPLAY_W)

            # Starkaste signal
            peak_freq = cf + (peak_i / DISPLAY_W - 0.5) * fs
            lines.append(f"\n  🔺 Starkaste signal: {format_freq(peak_freq)}"
                         f"  ({peak_db:.1f} dB)")

            print("".join(lines), end="", flush=True)
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n")


# ═══════════════════════════════════════════════════════════════════════════════
#  Läge 2: Frekvensskanner
# ═══════════════════════════════════════════════════════════════════════════════

SCAN_PRESETS = {
    "1": ("FM-radio",          87_500_000,   108_000_000,  200_000),
    "2": ("Flygband VHF",     118_000_000,   137_000_000,  100_000),
    "3": ("ACARS-band",       129_000_000,   132_000_000,  100_000),
    "4": ("Militär luftfart", 225_000_000,   400_000_000, 1_000_000),
    "5": ("Marin VHF",        156_000_000,   174_000_000,  100_000),
    "6": ("PMR/POCSAG",       446_000_000,   470_000_000,   25_000),
    "7": ("AIS/marin",        161_000_000,   163_000_000,   25_000),
    "8": ("70 cm amatör",     430_000_000,   440_000_000,   25_000),
    "9": ("FLEX personsök",   929_000_000,   932_000_000,   25_000),
}


def run_scanner(sdr: RtlSdr):
    """Svep över ett frekvensband och visa signalstyrka per kanal."""
    print("\n  Välj band att skanna:")
    for k, (name, f_low, f_high, step) in SCAN_PRESETS.items():
        span_mhz = (f_high - f_low) / 1e6
        print(f"    {k}. {name:<22} "
              f"{f_low/1e6:.1f}–{f_high/1e6:.1f} MHz  "
              f"({span_mhz:.0f} MHz, steg {step/1e3:.0f} kHz)")
    print(f"    0. Ange manuellt")

    val = input("\n  Val: ").strip()

    if val in SCAN_PRESETS:
        name, f_start, f_stop, f_step = SCAN_PRESETS[val]
    elif val == "0":
        try:
            f_start = int(float(input("  Startfrekvens (MHz): ")) * 1e6)
            f_stop  = int(float(input("  Stoppfrekvens (MHz): ")) * 1e6)
            f_step  = int(float(input("  Kanalsteg (kHz) [25]: ") or "25") * 1e3)
            name    = "Manuell skanning"
        except ValueError:
            print("  Ogiltigt värde.")
            return
    else:
        return

    freqs      = list(range(f_start, f_stop, f_step))
    n_freqs    = len(freqs)
    results    = {}   # freq → dB
    stop_event = threading.Event()

    print(f"\n  Skannar {name}: {n_freqs} kanaler  |  Ctrl+C för att avbryta\n")

    # Bakgrundstråd som uppdaterar displayen
    def display_loop():
        while not stop_event.is_set():
            if not results:
                time.sleep(0.5)
                continue

            sorted_freqs = sorted(results.keys())
            db_vals      = [results[f] for f in sorted_freqs]
            db_min       = min(db_vals) - 2
            db_max       = max(db_vals) + 2
            bar_w        = 40

            print("\033[2J\033[H", end="")
            print(f"  🔍 Frekvensskanner: {name}")
            print(f"  {f_start/1e6:.3f} – {f_stop/1e6:.3f} MHz  |"
                  f"  {len(results)}/{n_freqs} kanaler skannade  |  Ctrl+C stoppar\n")
            print(f"  {'Frekvens':<16} {'dB':>6}  {'Signalstyrka'}")
            print("  " + "─" * (16 + 6 + bar_w + 4))

            # Sortera efter signalstyrka (starkast överst) när skanningen är klar,
            # annars visa i frekvensordning
            if len(results) == n_freqs:
                display_order = sorted(results, key=results.get, reverse=True)
                print(f"  (Sorterat efter styrka – {len(results)} kanaler)\n")
            else:
                display_order = sorted_freqs

            for f in display_order[:40]:   # Max 40 rader
                db  = results[f]
                bar = db_to_bar(db, db_min, db_max, bar_w)
                rel = (db - db_min) / max(db_max - db_min, 1)
                if rel > 0.7:
                    color = "\033[91m"   # Röd
                elif rel > 0.4:
                    color = "\033[93m"   # Gul
                else:
                    color = "\033[92m"   # Grön
                print(f"  {format_freq(f):<16} {db:>6.1f}  "
                      f"{color}{bar}\033[0m")

            time.sleep(0.5)

    display_thread = threading.Thread(target=display_loop, daemon=True)
    display_thread.start()

    try:
        sdr.sample_rate = 250_000

        for i, freq in enumerate(freqs):
            sdr.center_freq = freq
            time.sleep(0.02)   # Låt tunern stabilisera sig
            db = measure_rssi(sdr, samples=16384)
            results[freq] = db

        stop_event.set()
        time.sleep(0.8)

        # Slutrapport
        if results:
            top = sorted(results, key=results.get, reverse=True)[:10]
            print(f"\n\n  ═══ Topp 10 starkaste signaler ═══\n")
            for rank, f in enumerate(top, 1):
                print(f"  {rank:2}.  {format_freq(f):<16}  {results[f]:.1f} dB")
            print()

    except KeyboardInterrupt:
        stop_event.set()
        print("\n\n  Skanning avbruten.")


# ═══════════════════════════════════════════════════════════════════════════════
#  Läge 3: RSSI-mätare (enkel signalstyrka på en frekvens)
# ═══════════════════════════════════════════════════════════════════════════════

def run_rssi(sdr: RtlSdr):
    """Realtids signalstyrkemätare med stor ASCII-stapel."""
    cf = sdr.center_freq

    print(f"\n  Signalstyrkemätare @ {format_freq(cf)}")
    print(f"  Flytta antennen för max utslag  |  Ctrl+C för att avsluta\n")

    history = []
    MAX_H   = 30

    try:
        while True:
            db = measure_rssi(sdr, samples=32768)
            history.append(db)
            if len(history) > MAX_H:
                history.pop(0)

            db_min = min(history) - 2
            db_max = max(history) + 2
            bar_w  = 50
            bar    = db_to_bar(db, db_min, db_max, bar_w)

            rel = (db - db_min) / max(db_max - db_min, 1)
            if rel > 0.75:
                color = "\033[91m"
            elif rel > 0.45:
                color = "\033[93m"
            else:
                color = "\033[92m"

            peak = max(history)

            # Enkelt sparkline-diagram (historik)
            sparkline = ""
            chars = " ▁▂▃▄▅▆▇█"
            for h in history:
                idx = round((h - db_min) / max(db_max - db_min, 1) * 8)
                sparkline += chars[max(0, min(8, idx))]

            print(f"\r  {color}{bar}\033[0m  {db:>7.1f} dB  "
                  f"(max: {peak:.1f} dB)  {sparkline}",
                  end="", flush=True)
            time.sleep(0.15)

    except KeyboardInterrupt:
        print(f"\n\n  Avslutat. Max uppmätt: {max(history):.1f} dB\n")


# ═══════════════════════════════════════════════════════════════════════════════
#  Huvudmeny för scanner-läget
# ═══════════════════════════════════════════════════════════════════════════════

BAND_PRESETS = {
    "1": ("Vädersensorer",      433_920_000),
    "2": ("Marin VHF (AIS)",    162_000_000),
    "3": ("ACARS",              129_125_000),
    "4": ("POCSAG (RAKEL)",     169_637_500),
    "5": ("ADS-B",            1_090_000_000),
    "6": ("FM-radio",            98_000_000),
    "7": ("Flygband",           124_000_000),
}


def run_scanner_mode():
    print("\n" + "=" * 50)
    print(" Spektrum & Signalstyrka")
    print("=" * 50)

    print("\n  Välj läge:")
    print("    1. 📊 Spektrumanalysator  – realtids FFT-display")
    print("    2. 🔍 Frekvensskanner    – hitta aktiva signaler i ett band")
    print("    3. 📶 Signalstyrkemätare – optimera antennplacering\n")

    mode = input("  Val [1]: ").strip() or "1"

    if mode not in ("1", "2", "3"):
        print("  Ogiltigt val.")
        return

    # Välj frekvens (för läge 1 och 3)
    if mode in ("1", "3"):
        print("\n  Välj frekvens:")
        for k, (name, freq) in BAND_PRESETS.items():
            print(f"    {k}. {name:<22} {format_freq(freq)}")
        print(f"    0. Ange manuellt")

        fval = input("\n  Val [1]: ").strip() or "1"
        if fval in BAND_PRESETS:
            _, center_freq = BAND_PRESETS[fval]
        elif fval == "0":
            try:
                center_freq = int(float(input("  Frekvens (MHz): ")) * 1e6)
            except ValueError:
                center_freq = 100_000_000
        else:
            center_freq = 433_920_000
    else:
        center_freq = 100_000_000   # Används ej för skannern

    # Öppna SDR
    try:
        sdr = RtlSdr()
    except Exception as e:
        print(f"\n❌ Kunde inte öppna SDR-dongle: {e}")
        return

    try:
        sdr.gain = GAIN

        if mode == "1":
            sdr.sample_rate = SAMPLE_RATE
            sdr.center_freq = center_freq
            run_spectrum(sdr)

        elif mode == "2":
            run_scanner(sdr)

        elif mode == "3":
            sdr.sample_rate = 250_000
            sdr.center_freq = center_freq
            run_rssi(sdr)

    except Exception as e:
        print(f"\n❌ Fel: {e}")
    finally:
        sdr.close()
