"""
ACARS – Aircraft Communications Addressing and Reporting System
VHF AM, 2400 baud MSK, typiska frekvenser i Europa:
  129.125 MHz (primär), 131.525 MHz, 131.725 MHz

Signalkedja:
  RTL-SDR IQ (250 kHz) → AM-demod → decimering → FSK-detektion → bitar → ACARS-ram
"""

import sys
import time
import threading
from datetime import datetime
from collections import deque

import numpy as np

try:
    from rtlsdr import RtlSdr
except ImportError as e:
    print(f"❌ Saknat paket: {e}")
    sys.exit(1)

# ── Signalparametrar ──────────────────────────────────────────────────────────
SAMPLE_RATE = 250_000
DECIMATE    = 5
FS_AUDIO    = SAMPLE_RATE // DECIMATE   # 50 000 Hz
BAUD_RATE   = 2_400
SPS         = FS_AUDIO / BAUD_RATE      # ~20.83 samplingar per bit
SPS_INT     = round(SPS)               # 21
GAIN        = 42
CHUNK       = 512 * 1024

MARK_FREQ   = 1_200    # Hz – bit '1'
SPACE_FREQ  = 2_400    # Hz – bit '0'
THRESHOLD   = (MARK_FREQ + SPACE_FREQ) / 2

# Preberäknade referenstoner för korreleringsdetektion
_t         = np.arange(SPS_INT) / FS_AUDIO
MARK_REF   = np.exp(2j * np.pi * MARK_FREQ  * _t).astype(np.complex64)
SPACE_REF  = np.exp(2j * np.pi * SPACE_FREQ * _t).astype(np.complex64)

# Decimeringsfilter (boxcar)
_LP        = np.ones(DECIMATE, dtype=np.float32) / DECIMATE

# ── ACARS-kontrollkoder ───────────────────────────────────────────────────────
SYN      = 0x16
SOH      = 0x01
STX      = 0x02
ETX      = 0x17
DEL      = 0x7F
PREAMBLE = 0x2B   # 8 bitar: 00101011

# Vanliga ACARS-meddelandeetiketter
LABELS = {
    "H1": "Positionsrapport", "5Z": "ATIS", "B6": "PDC (avgångsclearance)",
    "20": "Dörrstatus",       "QK": "Teknisk rapport", "SA": "SELCAL-test",
    "80": "Driftsmeddelande", "Q0": "ACARS-logon",     "_d": "Textmeddelande",
}

# ── Meddelandelogg ────────────────────────────────────────────────────────────
messages      = deque(maxlen=30)
messages_lock = threading.Lock()
stats         = {"bitar": 0, "ramar": 0, "giltiga": 0}


# ── Signalbearbetning ─────────────────────────────────────────────────────────

def am_demod_decimate(iq: np.ndarray) -> np.ndarray:
    """AM-demodulering (envelope) + decimering till FS_AUDIO."""
    mag = np.abs(iq).astype(np.float32)
    # Boxcar-LP-filter för anti-aliasing
    filtered = np.convolve(mag, _LP, mode="valid")
    # Decimera
    n = (len(filtered) // DECIMATE) * DECIMATE
    audio = filtered[:n:DECIMATE]
    # Ta bort DC-komponent och normalisera
    audio -= audio.mean()
    peak = np.abs(audio).max()
    if peak > 0:
        audio /= peak
    return audio


def fsk_bits(audio: np.ndarray) -> list:
    """
    FSK-detektion via korreleringsjämförelse.
    För varje bit-period: jämför energin vid MARK- och SPACE-frekvensen.
    """
    bits = []
    n_bits = len(audio) // SPS_INT
    for i in range(n_bits):
        seg = audio[i * SPS_INT:(i + 1) * SPS_INT].astype(np.complex64)
        e_mark  = abs(np.dot(seg, MARK_REF))  ** 2
        e_space = abs(np.dot(seg, SPACE_REF)) ** 2
        bits.append(1 if e_mark > e_space else 0)
    stats["bitar"] += n_bits
    return bits


# ── ACARS-ramavkodning ────────────────────────────────────────────────────────

def bits_to_byte(bits_8: list) -> tuple:
    """
    Konvertera 8 bitar (LSB-first) till byte-värde och kontrollera paritet.
    Returnerar (värde, paritet_ok).
    """
    val    = sum(b << i for i, b in enumerate(bits_8[:7]))  # 7 databitar
    parity = bin(val).count("1") % 2                        # jämn paritet
    return val, parity == bits_8[7]


def bits_to_chars(bits: list) -> list:
    """Omvandla bitlista till lista av (char, paritet_ok)-tupler."""
    chars = []
    for i in range(0, len(bits) - 7, 8):
        val, ok = bits_to_byte(bits[i:i + 8])
        chars.append((chr(val & 0x7F), ok))
    return chars


def find_acars_frames(bits: list) -> list:
    """
    Leta efter ACARS-ramar i bitstömmen.
    Returnerar lista av avkodade meddelanden (dict).
    """
    found = []
    chars = bits_to_chars(bits)
    n = len(chars)

    i = 0
    while i < n - 30:
        # Hitta SOH (0x01) föregången av SYN (0x16)
        if (ord(chars[i][0]) == SOH and
                i > 0 and ord(chars[i - 1][0]) == SYN):

            frame_start = i + 1
            raw = [c for c, _ in chars[frame_start:frame_start + 200]]

            msg = parse_acars_frame(raw)
            if msg:
                found.append(msg)
                stats["giltiga"] += 1
                i = frame_start + 40   # Hoppa förbi ramen
                continue
        i += 1

    return found


def parse_acars_frame(chars: list) -> dict | None:
    """
    Tolka en ACARS-ram från och med tecknet efter SOH.
    Returnerar dict med fältens innehåll, eller None om ramen är ogiltig.
    """
    if len(chars) < 15:
        return None

    try:
        pos = 0

        # Flygregistrering: 7 tecken (sista ofta space/punkt)
        reg_chars = [c for c in chars[pos:pos + 7]
                     if c.isalnum() or c in "-. "]
        reg = "".join(reg_chars).strip()
        pos += 7

        # Teknisk bekräftelse (NAK/ACK) – 1 tecken, hoppa över
        pos += 1

        # Flight ID: upp till 6 tecken tills STX
        flight = ""
        while pos < len(chars) and ord(chars[pos]) != STX and len(flight) < 6:
            if chars[pos].isalnum():
                flight += chars[pos]
            pos += 1

        if pos >= len(chars) or ord(chars[pos]) != STX:
            return None
        pos += 1  # Hoppa STX

        # Meddelandeetikett: 2 tecken
        if pos + 2 > len(chars):
            return None
        label = "".join(chars[pos:pos + 2]).strip()
        pos  += 2

        # Block-ID: 1 tecken
        block_id = chars[pos] if pos < len(chars) else "?"
        pos += 1

        # Meddelandenummer (ACARS-standard): 4 tecken (t.ex. M01A)
        msg_no = "".join(chars[pos:pos + 4]).strip()
        pos   += 4

        # Meddelandetext: till ETX eller DEL
        text_chars = []
        while pos < len(chars):
            c = chars[pos]
            b = ord(c)
            if b in (ETX, DEL, 0x03):
                break
            if c.isprintable():
                text_chars.append(c)
            pos += 1

        text = "".join(text_chars).strip()

        if not reg and not flight:
            return None

        stats["ramar"] += 1
        return {
            "tid":      datetime.now().strftime("%H:%M:%S"),
            "reg":      reg      or "–",
            "flight":   flight   or "–",
            "label":    label    or "–",
            "block_id": block_id or "–",
            "msg_no":   msg_no   or "–",
            "text":     text     or "(tomt)",
        }

    except Exception:
        return None


def process_chunk(iq: np.ndarray) -> list:
    """Processa ett IQ-block och returnera avkodade ACARS-meddelanden."""
    audio  = am_demod_decimate(iq)
    bits   = fsk_bits(audio)
    frames = find_acars_frames(bits)
    return frames


# ── Presentation ──────────────────────────────────────────────────────────────

def format_message(msg: dict) -> str:
    label_desc = LABELS.get(msg["label"], "Okänd typ")
    lines = [
        "",
        f"  ┌─ [{msg['tid']}]  ✈  {msg['reg']}  │  Flygning: {msg['flight']}",
        f"  │  Etikett: {msg['label']} – {label_desc}   Nr: {msg['msg_no']}",
        f"  └─ {msg['text']}",
    ]
    return "\n".join(lines)


def print_log(stop_event: threading.Event):
    """Visa löpande ACARS-meddelandelogg."""
    while not stop_event.is_set():
        print("\033[2J\033[H", end="")
        print(f"  📡 ACARS mottagare  –  {datetime.now().strftime('%H:%M:%S')}")
        print(f"  VHF AM 2400 baud  |  Ctrl+C för att avsluta")
        print(f"  Bitar: {stats['bitar']:,}  |  Ramar detekterade: {stats['ramar']}  |  Giltiga meddelanden: {stats['giltiga']}\n")
        print("  " + "─" * 60)

        with messages_lock:
            log = list(messages)

        if not log:
            print("\n  (Inga meddelanden ännu – väntar på ACARS-trafik...)")
            print("\n  Tips: ACARS är intermittent. Det kan ta några minuter")
            print("  mellan mottagna meddelanden beroende på flygtrafiken.")
        else:
            for msg in reversed(log):
                print(format_message(msg))

        time.sleep(3)


# ── Frekvensval ───────────────────────────────────────────────────────────────

FREQ_OPTIONS = {
    "1": ("129.125 MHz  (Europa primär)",  129_125_000),
    "2": ("131.525 MHz  (Europa sekundär)", 131_525_000),
    "3": ("131.725 MHz  (Europa sekundär)", 131_725_000),
    "4": ("130.025 MHz",                   130_025_000),
}


def choose_frequency() -> int:
    print("\n  Välj ACARS-frekvens:")
    for k, (desc, _) in FREQ_OPTIONS.items():
        print(f"    {k}. {desc}")
    print("    5. Ange manuellt (Hz)")
    val = input("\n  Val [1]: ").strip() or "1"

    if val in FREQ_OPTIONS:
        _, freq = FREQ_OPTIONS[val]
        return freq
    elif val == "5":
        try:
            return int(input("  Frekvens i Hz: ").strip())
        except ValueError:
            return 129_125_000
    else:
        return 129_125_000


# ── Huvudloop ─────────────────────────────────────────────────────────────────

def run_acars(settings: dict | None = None):
    gain = (settings or {}).get("gain", GAIN)
    ppm  = (settings or {}).get("ppm",  0)

    print("\n" + "=" * 50)
    print(" ACARS – Flygplansdatakommunikation")
    print(" VHF AM, 2400 baud")
    print("=" * 50)

    center_freq = choose_frequency()

    try:
        sdr = RtlSdr()
    except Exception as e:
        print(f"\n❌ Kunde inte öppna SDR-dongle: {e}")
        print("   Kontrollera att dongeln är inkopplad.")
        return

    sdr.sample_rate = SAMPLE_RATE
    sdr.center_freq = center_freq
    sdr.gain        = gain
    if ppm != 0:
        sdr.freq_correction = ppm

    gain_str = f"{gain} dB" if gain != "auto" else "auto"
    print(f"\n  Samplingsfrekvens : {SAMPLE_RATE/1e3:.0f} kHz")
    print(f"  Frekvens          : {center_freq/1e6:.3f} MHz")
    print(f"  Förstärkning      : {gain_str}  |  PPM: {ppm:+d}")
    print(f"  Mark/Space        : {MARK_FREQ}/{SPACE_FREQ} Hz @ {BAUD_RATE} baud\n")
    print("  Startar mottagning...\n")

    stop_event    = threading.Event()
    display_thread = threading.Thread(target=print_log, args=(stop_event,), daemon=True)
    display_thread.start()

    try:
        while True:
            iq = sdr.read_samples(CHUNK)
            for msg in process_chunk(iq):
                with messages_lock:
                    messages.append(msg)

    except KeyboardInterrupt:
        print("\n\nAvbruten av användaren.")
    except Exception as e:
        print(f"\n❌ Fel under mottagning: {e}")
    finally:
        stop_event.set()
        sdr.close()
