"""
Järnvägskommunikation – Analogt tågradio Sverige (FM, 153–156 MHz)

Sverige använder ett analogt VHF FM-system parallellt med GSM-R.
Frekvenserna är uppdelade i bandsektioner och används av:
  - Trafikverket (trafikledning)
  - SJ, Green Cargo, tågpersonal
  - Räddningstjänst/nöd längs spåret

Signalkedja:
  RTL-SDR IQ (240 kHz) → FM-demod → LP-filter → decimering →
  Squelch → sounddevice-uppspelning
"""

import sys
import time
import queue
import threading
from datetime import datetime

import numpy as np

try:
    from rtlsdr import RtlSdr
    import sounddevice as sd
except ImportError as e:
    print(f"❌ Saknat paket: {e}")
    print("   Installera med: pip install pyrtlsdr sounddevice")
    sys.exit(1)

# ── Signalparametrar ──────────────────────────────────────────────────────────
SAMPLE_RATE = 240_000
AUDIO_RATE  = 48_000
DECIMATE    = SAMPLE_RATE // AUDIO_RATE   # = 5
GAIN        = 42
CHUNK       = 48_000
AUDIO_CHUNK = CHUNK // DECIMATE

# FIR anti-aliasing-filter (cutoff ~20 kHz)
_N      = 63
_n      = np.arange(_N) - _N // 2
_h      = np.sinc(2 * 20_000 / SAMPLE_RATE * _n) * np.blackman(_N)
LP_FILT = (_h / _h.sum()).astype(np.float32)

# ── Kanalplan – analogt tågradio Sverige ─────────────────────────────────────
# Källa: Trafikverket, PTS frekvenstillstånd, järnvägsbranschens kanallista
# Kanalseparation: 12,5 kHz / 25 kHz (beroende på region och syfte)
RAILWAY_CHANNELS = {
    "1":  ("153.025 MHz  Tågradio kanal 1  – södra stambanan",    153_025_000),
    "2":  ("153.050 MHz  Tågradio kanal 2  – Västra stambanan",   153_050_000),
    "3":  ("153.075 MHz  Tågradio kanal 3  – Södra stambanan",    153_075_000),
    "4":  ("153.100 MHz  Tågradio kanal 4  – Norra stambanan",    153_100_000),
    "5":  ("153.125 MHz  Tågradio kanal 5  – Bergslagsbanan",     153_125_000),
    "6":  ("153.150 MHz  Tågradio kanal 6  – Botniabanan",        153_150_000),
    "7":  ("153.175 MHz  Tågradio kanal 7  – Ostkustbanan",       153_175_000),
    "8":  ("153.200 MHz  Tågradio kanal 8  – Dalabanan",          153_200_000),
    "9":  ("153.225 MHz  Tågradio kanal 9  – Godstrafik",         153_225_000),
    "10": ("153.250 MHz  Tågradio kanal 10 – Rangerbangård",      153_250_000),
    "11": ("153.275 MHz  Tågradio kanal 11 – Underhåll/bana",     153_275_000),
    "12": ("153.300 MHz  Tågradio kanal 12 – Malmtrafik (nord)",  153_300_000),
    "13": ("153.350 MHz  Tågradio kanal 13 – Lokaltåg Stockholm", 153_350_000),
    "14": ("153.400 MHz  Tågradio kanal 14 – Lokaltåg Göteborg",  153_400_000),
    "15": ("153.450 MHz  Tågradio kanal 15 – Lokaltåg Malmö",     153_450_000),
    "16": ("154.000 MHz  🚨 Nöd/säkerhet   – alla operatörer",    154_000_000),
    "17": ("155.250 MHz  Trafikledning TRV – region nord",        155_250_000),
    "18": ("155.500 MHz  Trafikledning TRV – region mitt",        155_500_000),
    "19": ("155.750 MHz  Trafikledning TRV – region syd",         155_750_000),
    "20": ("156.000 MHz  Depå/verkstad",                          156_000_000),
    "0":  ("Ange manuell frekvens",                                       0),
}


# ── Signalbehandling ──────────────────────────────────────────────────────────

def fm_demod(iq: np.ndarray) -> np.ndarray:
    """FM-demodulering via fasvinkelskillnad."""
    phase = np.angle(iq[1:] * np.conj(iq[:-1]))
    phase = np.append(phase, 0.0)
    return phase.astype(np.float32)


def compute_power_db(audio: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(audio ** 2))) + 1e-12
    return 20 * np.log10(rms)


def receive_loop(sdr, audio_q: queue.Queue, squelch_db: float,
                 status: dict, stop: threading.Event):
    prev_iq = np.zeros(1, dtype=np.complex64)
    while not stop.is_set():
        try:
            raw = sdr.read_samples(CHUNK)
            iq  = raw.astype(np.complex64)

            # FM-demod
            combined = np.concatenate([prev_iq[-1:], iq])
            audio    = fm_demod(combined)
            prev_iq  = iq

            # LP-filter + decimering
            filtered = np.convolve(audio, LP_FILT, mode="same")
            decimated = filtered[::DECIMATE].copy()

            # Signalstyrka
            db = compute_power_db(decimated)
            status["db"] = db
            status["squelch_open"] = db >= squelch_db

            if status["squelch_open"]:
                # Normalisera
                peak = np.max(np.abs(decimated)) + 1e-9
                out  = (decimated / peak * 0.9).astype(np.float32)
                try:
                    audio_q.put_nowait(out)
                except queue.Full:
                    pass
            else:
                try:
                    audio_q.put_nowait(np.zeros(AUDIO_CHUNK, dtype=np.float32))
                except queue.Full:
                    pass
        except Exception:
            break


def display_loop(freq: int, ch_name: str, squelch_db: float,
                 status: dict, stop: threading.Event):
    while not stop.is_set():
        db  = status.get("db", -99)
        sq  = status.get("squelch_open", False)
        bar_len = 30
        filled  = max(0, min(bar_len, int((db + 60) / 60 * bar_len)))
        bar     = "█" * filled + "░" * (bar_len - filled)
        icon    = "🔊" if sq else "🔇"
        ts      = datetime.now().strftime("%H:%M:%S")
        line    = (f"\r  [{ts}]  {icon}  [{bar}]  {db:+.1f} dB"
                   f"  (squelch {squelch_db:.0f} dB)   ")
        print(line, end="", flush=True)
        time.sleep(0.15)


def run_railway_rx(freq: int, ch_name: str, squelch_db: float,
                   gain="auto", ppm: int = 0):
    """Öppna SDR och starta järnvägsmottagning."""
    try:
        sdr = RtlSdr()
    except Exception as e:
        print(f"\n❌ Kunde inte öppna SDR-dongle: {e}")
        return

    sdr.sample_rate = SAMPLE_RATE
    sdr.center_freq = freq
    sdr.gain        = gain
    if ppm != 0:
        sdr.freq_correction = ppm

    gain_str = f"{gain} dB" if gain != "auto" else "auto"
    print(f"\n  Frekvens    : {freq/1e6:.4f} MHz  (FM)")
    print(f"  Kanal       : {ch_name}")
    print(f"  Sampling    : {SAMPLE_RATE/1e3:.0f} kHz → {AUDIO_RATE/1e3:.0f} kHz ljud")
    print(f"  Squelch     : {squelch_db:.0f} dB  |  Förstärkning: {gain_str}  |  PPM: {ppm:+d}")
    print(f"\n  Lyssnar... (Ctrl+C för att avsluta)\n")

    audio_q    = queue.Queue(maxsize=12)
    stop_event = threading.Event()
    status     = {"db": -60, "squelch_open": False}

    rx_thread = threading.Thread(
        target=receive_loop,
        args=(sdr, audio_q, squelch_db, status, stop_event),
        daemon=True,
    )
    rx_thread.start()

    disp_thread = threading.Thread(
        target=display_loop,
        args=(freq, ch_name, squelch_db, status, stop_event),
        daemon=True,
    )
    disp_thread.start()

    buf = np.zeros(AUDIO_CHUNK, dtype=np.float32)

    def callback(outdata, frames, time_info, cb_status):
        nonlocal buf
        try:
            buf = audio_q.get_nowait()
        except queue.Empty:
            pass
        n = min(frames, len(buf))
        outdata[:n, 0] = buf[:n]
        if n < frames:
            outdata[n:, 0] = 0.0

    try:
        with sd.OutputStream(samplerate=AUDIO_RATE, channels=1,
                              dtype="float32", blocksize=AUDIO_CHUNK,
                              callback=callback):
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n\nAvbruten av användaren.")
    except Exception as e:
        print(f"\n❌ Ljudfel: {e}")
    finally:
        stop_event.set()
        sdr.close()


# ── Hjälpfunktioner ───────────────────────────────────────────────────────────

def ask_squelch(default: float = -40) -> float:
    raw = input(f"\n  Squelch-nivå [{default:.0f} dB]: ").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def choose_channel() -> tuple[int, str]:
    print("\n  Järnvägskanaler (analogt tågradio Sverige):\n")
    for k, (name, _) in RAILWAY_CHANNELS.items():
        if k == "0":
            continue
        marker = "  " if "🚨" not in name else "❗"
        print(f"  {marker} {k:>2}. {name}")
    print()
    val = input("  Val [16]: ").strip() or "16"

    if val == "0":
        try:
            mhz = float(input("  Frekvens (MHz): "))
            freq = int(mhz * 1e6)
            name = f"{mhz:.4f} MHz (manuell)"
            return freq, name
        except ValueError:
            print("  Ogiltigt värde, använder nödkanal 154.000 MHz")
            return 154_000_000, "154.000 MHz 🚨 Nöd/säkerhet"

    if val in RAILWAY_CHANNELS:
        name, freq = RAILWAY_CHANNELS[val]
        return freq, name.split("  ")[0]

    print("  Ogiltigt val, använder nödkanal 154.000 MHz")
    return 154_000_000, "154.000 MHz 🚨 Nöd/säkerhet"


# ── Huvudfunktion ─────────────────────────────────────────────────────────────

def run_railway(settings: dict | None = None):
    gain       = (settings or {}).get("gain",       GAIN)
    ppm        = (settings or {}).get("ppm",        0)
    sq_default = (settings or {}).get("squelch_db", -40)

    print("\n" + "=" * 50)
    print(" 🚂 Järnvägskommunikation – Analogt tågradio")
    print(" VHF FM  153–156 MHz  |  Trafikverket / SJ")
    print("=" * 50)
    print("""
  Det analoga tågradiosystemet används i Sverige för
  kommunikation mellan lokförare, trafikledning och
  bangårdspersonal. GSM-R används parallellt men det
  analoga systemet finns kvar på många sträckor.

  Tänk på:
  • Kommunikationen är okrypterad men avlyssning för
    annat än hobbyändamål kan vara olagligt.
  • Signalerna är FM med 12,5–25 kHz kanalbredd.
  • Nödkanalen 154.000 MHz är alltid aktiv.
""")

    freq, ch_name = choose_channel()
    squelch_db    = ask_squelch(sq_default)

    run_railway_rx(freq, ch_name, squelch_db, gain=gain, ppm=ppm)
