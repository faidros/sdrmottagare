"""
POCSAG & FLEX – Personsökarprotokoll
POCSAG: FM/FSK, 512/1200/2400 baud, vanligt hos räddningstjänst och sjukhus
FLEX:   4-nivå FSK, 1600/3200/6400 baud, kommersiella personsökare

Signal chain (POCSAG):
  RTL-SDR IQ → FM-demod → LP-filter → bit-extraktion →
  preamble-detektion → sync-ord → kodeords-avkodning → meddelande

Signal chain (FLEX):
  RTL-SDR IQ → FM-demod → 4-nivå kvantisering →
  sync-detektion → blockavkodning → meddelande
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
    print(f"❌ Saknat paket: {e}"); sys.exit(1)

# ── Generella SDR-parametrar ──────────────────────────────────────────────────
SAMPLE_RATE = 250_000
GAIN        = 42
CHUNK       = 512 * 1024

# FIR low-pass-filter (cutoff 10 kHz)
_N      = 63
_n      = np.arange(_N) - _N // 2
_h      = np.sinc(2 * 10_000 / SAMPLE_RATE * _n) * np.blackman(_N)
LP_FILT = (_h / _h.sum()).astype(np.float32)

# ── Meddelandelogg ────────────────────────────────────────────────────────────
log_messages  = deque(maxlen=40)
log_lock      = threading.Lock()
stats         = {"bitar": 0, "sync": 0, "meddelanden": 0}

# ═══════════════════════════════════════════════════════════════════════════════
#  POCSAG
# ═══════════════════════════════════════════════════════════════════════════════

POCSAG_SYNC   = 0x7CD215D8   # Synkroniseringsord
POCSAG_IDLE   = 0x7A89C197   # Tomt kodeord
POCSAG_BAUD   = [512, 1200, 2400]

# BCH(31,21) generator: x^10+x^9+x^8+x^6+x^5+x^3+1
BCH_GEN = 0b11101101001      # 0x769

# POCSAG numerisk teckentabell (4-bitars koder)
NUMERIC_TABLE = {
    0b0000: "0", 0b0001: "1", 0b0010: "2", 0b0011: "3",
    0b0100: "4", 0b0101: "5", 0b0110: "6", 0b0111: "7",
    0b1000: "8", 0b1001: "9", 0b1010: " ", 0b1011: "*",
    0b1100: "U", 0b1101: "-", 0b1110: ")",
}


def bch_valid(codeword: int) -> bool:
    """Kontrollera BCH(31,21)-paritet för ett POCSAG-kodeord."""
    # Kontrollera jämn paritet (bit 0)
    if bin(codeword).count("1") % 2 != 0:
        return False
    # BCH-syndromberkäkning på 31 MSB
    data = codeword >> 1
    for i in range(20, -1, -1):
        if data & (1 << (i + 10)):
            data ^= BCH_GEN << i
    return (data & 0x3FF) == 0


def decode_numeric(bits_20: list) -> str:
    """Avkoda POCSAG numeriskt meddelande (4 bitar per siffra)."""
    result = []
    for i in range(0, len(bits_20) - 3, 4):
        nibble = (bits_20[i] << 3 | bits_20[i+1] << 2 |
                  bits_20[i+2] << 1 | bits_20[i+3])
        ch = NUMERIC_TABLE.get(nibble, "?")
        if nibble == 0b1111:   # EOT
            break
        result.append(ch)
    return "".join(result)


def decode_alphanumeric(all_bits: list) -> str:
    """Avkoda POCSAG alfanumeriskt meddelande (7-bitars ASCII, LSB-first)."""
    result = []
    for i in range(0, len(all_bits) - 6, 7):
        val = sum(all_bits[i + j] << j for j in range(7))  # LSB-first
        if val == 0 or val == 4:   # NUL/EOT
            break
        if 32 <= val <= 126:
            result.append(chr(val))
    return "".join(result)


def bits_to_codeword(bits: list, pos: int) -> int | None:
    """Extrahera ett 32-bitars kodeord ur bitlistan från position pos."""
    if pos + 32 > len(bits):
        return None
    cw = 0
    for i in range(32):
        cw = (cw << 1) | bits[pos + i]
    return cw


def decode_pocsag_batch(bits: list, start: int) -> list:
    """
    Avkoda en POCSAG-batch (sync + 8 ramar × 2 kodeord).
    Returnerar lista av avkodade meddelanden.
    """
    results   = []
    pos       = start + 32      # Hoppa förbi synkordet
    msg_bits  = []
    capcode   = None
    func      = None
    msg_type  = None

    for frame in range(8):
        for slot in range(2):
            cw = bits_to_codeword(bits, pos)
            if cw is None:
                break
            pos += 32

            if cw == POCSAG_IDLE:
                # Spara eventuellt pågående meddelande
                if msg_bits and capcode is not None:
                    text = _finish_message(msg_bits, msg_type)
                    if text:
                        results.append(_make_msg(capcode, func, text))
                    msg_bits = []; capcode = None
                continue

            if not bch_valid(cw):
                continue

            cw_type = (cw >> 31) & 1

            if cw_type == 0:   # Adresskodeord
                if msg_bits and capcode is not None:
                    text = _finish_message(msg_bits, msg_type)
                    if text:
                        results.append(_make_msg(capcode, func, text))
                    msg_bits = []

                # Adress: 18 bitar + 2 funktionsbitar + ramlägeskorrektionpag
                capcode  = (((cw >> 13) & 0x3FFFF) << 3) | (frame & 0x7)
                func     = (cw >> 11) & 0x3
                msg_type = func          # 3=alfanumerisk, 0/1/2=numerisk
                msg_bits = []

            else:              # Meddelandekodeord
                # Extrahera 20 databitar (bit 30 ner till bit 11)
                for bit_i in range(30, 10, -1):
                    msg_bits.append((cw >> bit_i) & 1)

    # Avsluta eventuellt öppet meddelande
    if msg_bits and capcode is not None:
        text = _finish_message(msg_bits, msg_type)
        if text:
            results.append(_make_msg(capcode, func, text))

    return results


def _finish_message(bits: list, func: int | None) -> str:
    if func == 3:
        return decode_alphanumeric(bits).strip()
    else:
        return decode_numeric(bits).strip()


def _make_msg(capcode: int, func: int | None, text: str) -> dict:
    stats["meddelanden"] += 1
    func_labels = {0: "Numerisk", 1: "Numerisk", 2: "Numerisk", 3: "Alfanumerisk"}
    return {
        "tid":      datetime.now().strftime("%H:%M:%S"),
        "protokoll": "POCSAG",
        "capcode":  str(capcode),
        "typ":      func_labels.get(func or 0, "Okänd"),
        "text":     text or "(tomt)",
    }


def fm_demod(iq: np.ndarray) -> np.ndarray:
    return np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float32)


def extract_bits_at_baud(sig: np.ndarray, baud: int) -> list:
    sps = SAMPLE_RATE / baud
    n   = int(len(sig) / sps)
    return [1 if sig[int(i * sps):int((i + 1) * sps)].mean() > 0 else 0
            for i in range(n)]


def find_pocsag_preamble(bits: list, min_len: int = 32) -> list:
    """Hitta alla positioner med POCSAG-preamble (alternerande 01010101...)."""
    positions = []
    n = len(bits)
    run = 0
    for i in range(1, n):
        if bits[i] != bits[i - 1]:
            run += 1
        else:
            if run >= min_len:
                # Sök synkord direkt efter preamble
                positions.append(i - run)
            run = 0
    return positions


def find_sync_after_preamble(bits: list, preamble_pos: int) -> int:
    """Sök POCSAG synkord (0x7CD215D8) nära preamble-position."""
    search_start = preamble_pos
    search_end   = min(preamble_pos + 1200, len(bits) - 32)

    for pos in range(search_start, search_end):
        cw = bits_to_codeword(bits, pos)
        if cw is None:
            break
        if cw == POCSAG_SYNC:
            return pos

    return -1


def process_pocsag(iq: np.ndarray) -> list:
    """Processa ett IQ-block och returnera POCSAG-meddelanden."""
    demod    = fm_demod(iq)
    filtered = np.convolve(demod, LP_FILT, mode="same")
    results  = []

    for baud in POCSAG_BAUD:
        bits = extract_bits_at_baud(filtered, baud)
        stats["bitar"] += len(bits)

        # Prova både normal och inverterad polaritet
        for polarity in (bits, [1 - b for b in bits]):
            preamble_positions = find_pocsag_preamble(polarity, min_len=24)

            for pre_pos in preamble_positions:
                sync_pos = find_sync_after_preamble(polarity, pre_pos)
                if sync_pos >= 0:
                    stats["sync"] += 1
                    msgs = decode_pocsag_batch(polarity, sync_pos)
                    results.extend(msgs)

    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  FLEX
# ═══════════════════════════════════════════════════════════════════════════════

# FLEX synkordpar (fas A, 1600 baud)
FLEX_SYNC_A  = 0xA8C740    # Primärt syncmönster
FLEX_BAUD    = 1600
FLEX_SPS     = SAMPLE_RATE / FLEX_BAUD

# Trösklar för 4-nivå FSK (normaliserat ±1)
FLEX_LEVELS  = [-0.67, 0.0, 0.67]   # Gränser för -3, -1, +1, +3


def quantize_flex(sig: np.ndarray) -> list:
    """Kvantisera FM-demodulerat FLEX-signal till 4 nivåer (0,1,2,3)."""
    result = []
    for s in sig:
        if s < FLEX_LEVELS[0]:
            result.append(0)
        elif s < FLEX_LEVELS[1]:
            result.append(1)
        elif s < FLEX_LEVELS[2]:
            result.append(2)
        else:
            result.append(3)
    return result


def symbols_to_dibits(symbols: list) -> list:
    """Konvertera FLEX 4-nivå symboler till dibitpar (2 bitar per symbol)."""
    # FLEX dibit-avbildning: 3→00, 1→01, 0→10, 2→11 (Gray-kodad)
    DIBIT_MAP = {3: (0, 0), 1: (0, 1), 0: (1, 0), 2: (1, 1)}
    bits = []
    for s in symbols:
        bits.extend(DIBIT_MAP.get(s, (0, 0)))
    return bits


def find_flex_sync(symbols: list) -> list:
    """Hitta FLEX sync-sekvenser i symbolströmmen."""
    # Sync A pattern i symbol-form: A8C740 → bits → dibits → symboler
    sync_bits = []
    for byte in [0xA8, 0xC7, 0x40]:
        for i in range(7, -1, -1):
            sync_bits.append((byte >> i) & 1)

    # Konvertera till förväntade symbolpar
    positions = []
    dibits = symbols_to_dibits(symbols)
    n      = len(dibits)
    target = len(sync_bits)

    for i in range(n - target + 1):
        if dibits[i:i + target] == sync_bits:
            positions.append(i // 2)   # Symbol-position

    return positions


def decode_flex_frame(symbols: list, sync_pos: int) -> list:
    """
    Grov FLEX-ramdekodning från sync-position.
    Extraherar adress och meddelandetext (förenklad implementering).
    """
    results = []
    dibits  = symbols_to_dibits(symbols[sync_pos:])

    # FLEX-block: 11 ord × 32 bitar = 352 bitar
    if len(dibits) < 352 + 64:
        return results

    # Block-informationsord (BI): position 64 bitar in (efter sync + frame info)
    bi_start = 64

    # Enkel avkodning: sök printbara ASCII-tecken i databitarna
    text_bits = dibits[bi_start:bi_start + 288]
    text = ""
    for i in range(0, len(text_bits) - 6, 7):
        val = sum(text_bits[i + j] << j for j in range(7))
        if 32 <= val <= 126:
            text += chr(val)

    text = text.strip()
    if len(text) > 3:
        stats["meddelanden"] += 1
        results.append({
            "tid":       datetime.now().strftime("%H:%M:%S"),
            "protokoll": "FLEX",
            "capcode":   "–",
            "typ":       "Alfanumerisk",
            "text":      text,
        })

    return results


def process_flex(iq: np.ndarray) -> list:
    """Processa ett IQ-block och returnera FLEX-meddelanden."""
    demod    = fm_demod(iq)
    filtered = np.convolve(demod, LP_FILT, mode="same")

    # Normalisera
    peak = np.abs(filtered).max()
    if peak > 0:
        filtered /= peak

    # Integrate-and-dump till FLEX-symbolrate
    n_syms = int(len(filtered) / FLEX_SPS)
    symbols = [
        filtered[int(i * FLEX_SPS):int((i + 1) * FLEX_SPS)].mean()
        for i in range(n_syms)
    ]

    quant   = quantize_flex(np.array(symbols))
    results = []

    sync_positions = find_flex_sync(quant)
    for pos in sync_positions:
        stats["sync"] += 1
        results.extend(decode_flex_frame(quant, pos))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  Presentation & huvudloop
# ═══════════════════════════════════════════════════════════════════════════════

FUNC_ICONS = {"POCSAG": "📟", "FLEX": "📱"}


def format_msg(msg: dict) -> str:
    icon  = FUNC_ICONS.get(msg["protokoll"], "📡")
    proto = msg["protokoll"]
    lines = [
        "",
        f"  ┌─ [{msg['tid']}]  {icon} {proto}  │  CAPCODE: {msg['capcode']}  │  {msg['typ']}",
        f"  └─ {msg['text']}",
    ]
    return "\n".join(lines)


def print_log(stop_event: threading.Event, freq: int, protocol: str):
    while not stop_event.is_set():
        print("\033[2J\033[H", end="")
        print(f"  📟 {protocol}-mottagare  –  {datetime.now().strftime('%H:%M:%S')}")
        print(f"  Frekvens: {freq/1e6:.4f} MHz  |  Ctrl+C för att avsluta")
        print(f"  Bitar: {stats['bitar']:,}  |  Synkar: {stats['sync']}  |  Meddelanden: {stats['meddelanden']}\n")
        print("  " + "─" * 60)

        with log_lock:
            snapshot = list(log_messages)

        if not snapshot:
            print("\n  (Inga meddelanden ännu – väntar på personsökartrafik...)")
            print("\n  Tips: POCSAG används aktivt av räddningstjänst och sjukhus.")
            print("  Prova frekvenser runt 148–170 MHz och 439–466 MHz.")
        else:
            for msg in reversed(snapshot):
                print(format_msg(msg))

        time.sleep(2)


# ── Frekvens- och protokollval ────────────────────────────────────────────────

POCSAG_FREQS = {
    "1": ("169.6375 MHz  (RAKEL – svenska blåljus)",  169_637_500),
    "2": ("466.075  MHz  (Europeisk sidning)",          466_075_000),
    "3": ("439.9875 MHz  (Amatörradio-POCSAG)",        439_987_500),
    "4": ("148.150  MHz  (Sjukhus/larmcentraler)",     148_150_000),
    "5": ("153.350  MHz  (Räddningstjänst, varierar)", 153_350_000),
}

FLEX_FREQS = {
    "1": ("931.9125 MHz  (FLEX Europa, primär)",  931_912_500),
    "2": ("931.7375 MHz  (FLEX Europa)",          931_737_500),
    "3": ("169.8000 MHz  (FLEX Sverige, varierar)", 169_800_000),
}


def choose_freq_and_protocol() -> tuple[str, int]:
    print("\n  Välj protokoll:")
    print("    1. POCSAG (512/1200/2400 baud) – räddningstjänst, sjukhus")
    print("    2. FLEX   (1600 baud, 4-FSK)   – kommersiella personsökare")
    prot_val = input("\n  Val [1]: ").strip() or "1"
    protocol = "FLEX" if prot_val == "2" else "POCSAG"

    freq_map = FLEX_FREQS if protocol == "FLEX" else POCSAG_FREQS
    print(f"\n  Välj frekvens ({protocol}):")
    for k, (desc, _) in freq_map.items():
        print(f"    {k}. {desc}")
    last = str(max(int(k) for k in freq_map) + 1)
    print(f"    {last}. Ange manuellt (Hz)")

    fval = input("\n  Val [1]: ").strip() or "1"
    if fval in freq_map:
        _, freq = freq_map[fval]
    elif fval == last:
        try:
            freq = int(input("  Frekvens i Hz: ").strip())
        except ValueError:
            freq = list(freq_map.values())[0][1]
    else:
        freq = list(freq_map.values())[0][1]

    return protocol, freq


def run_paging(settings: dict | None = None):
    gain = (settings or {}).get("gain", GAIN)
    ppm  = (settings or {}).get("ppm",  0)

    print("\n" + "=" * 50)
    print(" POCSAG & FLEX – Personsökaravkodare")
    print("=" * 50)

    protocol, freq = choose_freq_and_protocol()

    try:
        sdr = RtlSdr()
    except Exception as e:
        print(f"\n❌ Kunde inte öppna SDR-dongle: {e}")
        return

    sdr.sample_rate     = SAMPLE_RATE
    sdr.center_freq     = freq
    sdr.gain            = gain
    sdr.freq_correction = ppm

    gain_str = f"{gain} dB" if gain != "auto" else "auto"
    print(f"\n  Protokoll  : {protocol}")
    print(f"  Frekvens   : {freq/1e6:.4f} MHz")
    print(f"  Sampling   : {SAMPLE_RATE/1e3:.0f} kHz")
    print(f"  Förstärkning: {gain_str}  |  PPM: {ppm:+d}\n")
    if protocol == "POCSAG":
        print(f"  Testar baudrater: {', '.join(map(str, POCSAG_BAUD))} baud")
    else:
        print(f"  Symbolrate: {FLEX_BAUD} baud (4-nivå FSK)")
    print("\n  Startar mottagning...\n")

    stop_event    = threading.Event()
    display_thread = threading.Thread(
        target=print_log, args=(stop_event, freq, protocol), daemon=True
    )
    display_thread.start()

    processor = process_flex if protocol == "FLEX" else process_pocsag

    try:
        while True:
            iq = sdr.read_samples(CHUNK)
            for msg in processor(iq):
                with log_lock:
                    log_messages.append(msg)

    except KeyboardInterrupt:
        print("\n\nAvbruten av användaren.")
    except Exception as e:
        print(f"\n❌ Fel under mottagning: {e}")
    finally:
        stop_event.set()
        sdr.close()
