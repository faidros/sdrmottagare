"""
Röstmottagning – Flyg (AM) och Marin VHF (FM)

Flyg VHF AM  : 118–137 MHz (civil), 225–400 MHz (militär)
Marin VHF FM : 156–174 MHz  (kanal 16 = 156.800 MHz är nödkanal)

Signalkedja:
  RTL-SDR IQ (240 kHz) → AM/FM-demod → LP-filter → decimering →
  AGC → squelch → sounddevice-uppspelning
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
SAMPLE_RATE  = 240_000     # Hz – jämnt delbart med audiorate
AUDIO_RATE   = 48_000      # Hz – standard ljudkvalitet
DECIMATE     = SAMPLE_RATE // AUDIO_RATE   # = 5
GAIN         = 42
CHUNK        = 48_000      # IQ-samplingar per block (~0.2 s)
AUDIO_CHUNK  = CHUNK // DECIMATE           # Ljud-samplingar per block

# FIR anti-aliasing-filter inför decimering (cutoff ~20 kHz)
_N      = 63
_n      = np.arange(_N) - _N // 2
_h      = np.sinc(2 * 20_000 / SAMPLE_RATE * _n) * np.blackman(_N)
LP_FILT = (_h / _h.sum()).astype(np.float32)

# ── Kanaldefinitioner ─────────────────────────────────────────────────────────

AVIATION_FREQS = {
     "1": ("121.500 MHz  🚨 Guard/nöd (alltid aktiv)",      121_500_000),
     "2": ("123.500 MHz  Allmänt flygfält",                 123_500_000),
     "3": ("118.000 MHz  ATIS/approach (typisk)",           118_000_000),
     "4": ("122.800 MHz  Unicom (obemannade flygfält)",     122_800_000),
     "5": ("130.000 MHz  Militär kontroll (SE)",            130_000_000),
     "6": ("243.000 MHz  🚨 Militär guard/nöd (UHF)",       243_000_000),
     "7": ("282.800 MHz  Militär approach",                 282_800_000),
}

MARINE_CHANNELS = {
     "1": ("Kanal 16 – 156.800 MHz  🚨 Nöd & anrop (alltid!)", 156_800_000),
     "2": ("Kanal  6 – 156.300 MHz  Säkerhet fartyg–fartyg",   156_300_000),
     "3": ("Kanal  9 – 156.450 MHz  Fritidsbåtar",             156_450_000),
     "4": ("Kanal 12 – 156.600 MHz  Hamntrafikservice",        156_600_000),
     "5": ("Kanal 13 – 156.650 MHz  Brygga till brygga",       156_650_000),
     "6": ("Kanal 67 – 156.375 MHz  Kustbevakning (SE)",       156_375_000),
     "7": ("Kanal 77 – 156.875 MHz  Fritidsbåtar sekundär",    156_875_000),
     "8": ("Kanal 70 – 156.525 MHz  DSC digital nödsignal",    156_525_000),
}

# ── Demodulering ──────────────────────────────────────────────────────────────

def demod_am(iq: np.ndarray) -> np.ndarray:
    """AM-demodulering: envelopp-detektion."""
    mag = np.abs(iq).astype(np.float32)
    # Ta bort DC (lågpass med stor tidskonstant = enkelt HF-filter)
    mag -= np.convolve(mag, np.ones(DECIMATE * 4) / (DECIMATE * 4), mode="same")
    return mag


def demod_fm(iq: np.ndarray) -> np.ndarray:
    """FM-demodulering: fasens derivata (diskriminator)."""
    diff  = iq[1:] * np.conj(iq[:-1])
    demod = np.angle(diff).astype(np.float32)
    demod = np.append(demod, 0.0)   # Pad till samma längd som indata (CHUNK)
    # Skala till ±1 (anpassa för marin 5 kHz deviation vid 240 kHz fs)
    demod *= AUDIO_RATE / (2 * np.pi * 5_000)
    return demod


def decimate(sig: np.ndarray) -> np.ndarray:
    """LP-filtrera och decimera med faktor DECIMATE – alltid exakt CHUNK//DECIMATE samples."""
    filtered = np.convolve(sig, LP_FILT, mode="same")
    # Trimma till exakt jämnt antal så vi alltid får samma längd ut
    n = (len(filtered) // DECIMATE) * DECIMATE
    return filtered[:n:DECIMATE]


def agc(audio: np.ndarray, target: float = 0.5) -> np.ndarray:
    """Enkel automatic gain control – normalisera till målnivå."""
    peak = np.abs(audio).max()
    if peak < 1e-6:
        return audio
    return audio * (target / peak)


def squelch(audio: np.ndarray, threshold: float) -> tuple[np.ndarray, float]:
    """
    Tysta signalen om effekten är under tröskeln.
    Returnerar (audio, signal_db).
    """
    power  = float(np.mean(audio ** 2))
    sig_db = 10 * np.log10(max(power, 1e-20))
    if sig_db < threshold:
        return np.zeros_like(audio), sig_db
    return audio, sig_db


# ── Display ───────────────────────────────────────────────────────────────────

def rssi_bar(db: float, db_min: float = -60, db_max: float = -10,
             width: int = 30) -> str:
    ratio  = (db - db_min) / max(db_max - db_min, 1)
    ratio  = max(0.0, min(1.0, ratio))
    filled = round(ratio * width)
    if ratio > 0.75:
        color = "\033[91m"
    elif ratio > 0.45:
        color = "\033[93m"
    else:
        color = "\033[92m"
    return color + "█" * filled + "\033[0m" + "░" * (width - filled)


# ── Mottagningsloop ───────────────────────────────────────────────────────────

def receive_loop(sdr: RtlSdr, audio_q: queue.Queue,
                 mode: str, squelch_db: float,
                 status: dict, stop_event: threading.Event):
    """
    SDR-läsning och demodulering i bakgrundstråd.
    Lägger avkodade ljudblockar i audio_q.
    """
    demod_fn = demod_am if mode == "AM" else demod_fm

    while not stop_event.is_set():
        try:
            iq    = sdr.read_samples(CHUNK)
            raw   = demod_fn(np.asarray(iq, dtype=np.complex64))
            audio = decimate(raw)
            audio = agc(audio)
            audio, db = squelch(audio, squelch_db)

            status["db"]     = db
            status["squelch_open"] = db >= squelch_db

            # Lägg i kö, kasta gamla block om kön är full
            if audio_q.qsize() < 8:
                audio_q.put(audio.astype(np.float32))
        except Exception:
            pass


def display_loop(freq: int, mode: str, name: str,
                 squelch_db: float, status: dict,
                 stop_event: threading.Event):
    """Uppdatera statusraden i terminalen."""
    chars = " ▁▂▃▄▅▆▇█"
    history = []

    while not stop_event.is_set():
        db   = status.get("db", -60)
        open_ = status.get("squelch_open", False)
        history.append(db)
        if len(history) > 40:
            history.pop(0)

        spark = "".join(
            chars[max(0, min(8, round((h + 60) / 50 * 8)))]
            for h in history
        )
        squelch_ind = "\033[92m● ÖPPEN\033[0m" if open_ else "\033[90m○ stängd\033[0m"
        bar  = rssi_bar(db)

        print(f"\r  {bar}  {db:>6.1f} dB  Squelch {squelch_ind}  {spark}",
              end="", flush=True)
        time.sleep(0.15)


def run_voice_rx(freq: int, mode: str, name: str, squelch_db: float):
    """Öppna SDR och starta mottagning med ljuduppspelning."""
    try:
        sdr = RtlSdr()
    except Exception as e:
        print(f"\n❌ Kunde inte öppna SDR-dongle: {e}")
        return

    sdr.sample_rate = SAMPLE_RATE
    sdr.center_freq = freq
    sdr.gain        = GAIN

    print(f"\n  Frekvens    : {freq/1e6:.4f} MHz  ({mode})")
    print(f"  Kanal       : {name}")
    print(f"  Sampling    : {SAMPLE_RATE/1e3:.0f} kHz → {AUDIO_RATE/1e3:.0f} kHz ljud")
    print(f"  Squelch     : {squelch_db:.0f} dB")
    print(f"\n  Lyssnar... (Ctrl+C för att avsluta)\n")

    audio_q    = queue.Queue(maxsize=12)
    stop_event = threading.Event()
    status     = {"db": -60, "squelch_open": False}

    # SDR-tråd
    rx_thread = threading.Thread(
        target=receive_loop,
        args=(sdr, audio_q, mode, squelch_db, status, stop_event),
        daemon=True,
    )
    rx_thread.start()

    # Display-tråd
    disp_thread = threading.Thread(
        target=display_loop,
        args=(freq, mode, name, squelch_db, status, stop_event),
        daemon=True,
    )
    disp_thread.start()

    # Ljuduppspelning i huvudtråden via sounddevice-callback
    # Använd callback-modellen – undviker segfault från blocksize-mismatch
    SILENCE = np.zeros(AUDIO_CHUNK, dtype=np.float32)

    def audio_callback(outdata: np.ndarray, frames: int, time_info, status):
        try:
            chunk = audio_q.get_nowait()
        except queue.Empty:
            chunk = SILENCE

        # Säkerställ exakt rätt antal frames och klipp till [-1, 1]
        chunk = np.asarray(chunk, dtype=np.float32).flatten()
        if len(chunk) < frames:
            chunk = np.pad(chunk, (0, frames - len(chunk)))
        else:
            chunk = chunk[:frames]
        outdata[:, 0] = np.clip(chunk, -1.0, 1.0)

    try:
        with sd.OutputStream(samplerate=AUDIO_RATE, channels=1,
                             dtype="float32", blocksize=AUDIO_CHUNK,
                             callback=audio_callback):
            while True:
                time.sleep(0.1)   # Callback sköter uppspelningen

    except KeyboardInterrupt:
        print(f"\n\n  Avslutat @ {datetime.now().strftime('%H:%M:%S')}\n")
    except Exception as e:
        print(f"\n❌ Ljudfel: {e}")
    finally:
        stop_event.set()
        sdr.close()


# ── Menyval ───────────────────────────────────────────────────────────────────

def choose_channel(label: str, freq_map: dict) -> tuple[int, str]:
    print(f"\n  Välj {label}-kanal:")
    for k, (desc, _) in freq_map.items():
        print(f"    {k}. {desc}")
    last = str(max(int(k) for k in freq_map) + 1)
    print(f"    {last}. Ange manuellt (MHz)")

    val = input(f"\n  Val [1]: ").strip() or "1"
    if val in freq_map:
        desc, freq = freq_map[val]
        return freq, desc.split("  ")[0]
    elif val == last:
        try:
            f = int(float(input("  Frekvens (MHz): ")) * 1e6)
            return f, f"{f/1e6:.4f} MHz"
        except ValueError:
            return list(freq_map.values())[0]
    else:
        desc, freq = freq_map["1"]
        return freq, desc.split("  ")[0]


def ask_squelch(default: float) -> float:
    raw = input(f"\n  Squelch-nivå i dB [{default:.0f}]"
                f"  (lägre = känsligare, t.ex. -45): ").strip()
    try:
        return float(raw)
    except ValueError:
        return default


def run_voice():
    print("\n" + "=" * 50)
    print(" Röstmottagning")
    print("=" * 50)

    print("\n  Välj typ:")
    print("    1. ✈️  Flyg VHF/UHF  (AM)  – civilt och militärt")
    print("    2. ⚓ Marin VHF      (FM)  – båtar och kustbevakning")

    typ = input("\n  Val [1]: ").strip() or "1"

    if typ == "2":
        freq, name   = choose_channel("marin", MARINE_CHANNELS)
        mode         = "FM"
        squelch_db   = ask_squelch(-42)
    else:
        freq, name   = choose_channel("flyg", AVIATION_FREQS)
        mode         = "AM"
        squelch_db   = ask_squelch(-40)

    run_voice_rx(freq, mode, name, squelch_db)
