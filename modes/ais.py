"""
AIS Fartygsidentifikation på 161.975 / 162.025 MHz
Tar emot IQ-data med pyrtlsdr, demodulerar FM, avkodar HDLC/NRZI
och presenterar fartygsdata med pyais.
"""

import sys
import time
import threading
from datetime import datetime
from collections import defaultdict

import numpy as np

try:
    from rtlsdr import RtlSdr
    from pyais import decode as ais_decode
    from pyais.exceptions import InvalidNMEAMessageException
except ImportError as e:
    print(f"❌ Saknat paket: {e}")
    print("   Installera med: pip install pyrtlsdr pyais numpy")
    sys.exit(1)

# ── Konstanter ───────────────────────────────────────────────────────────────
SAMPLE_RATE  = 250_000        # Hz – täcker båda AIS-kanaler
BAUD_RATE    = 9_600          # baud
SPS          = SAMPLE_RATE / BAUD_RATE   # ~26.04 samplingar per symbol
GAIN         = 40             # dB
CHUNK        = 512 * 1024

# AIS VHF-kanaler
CHANNELS = {
    "A": 161_975_000,   # Kanal 87B
    "B": 162_025_000,   # Kanal 88B
}

# FIR low-pass-filter (cutoff 8 kHz)
_N      = 63
_n      = np.arange(_N) - _N // 2
_cutoff = 8_000 / SAMPLE_RATE
_h      = np.sinc(2 * _cutoff * _n) * np.blackman(_N)
LP_FILTER = (_h / _h.sum()).astype(np.float32)

HDLC_FLAG = [0, 1, 1, 1, 1, 1, 1, 0]

# ── Fartygsregister ───────────────────────────────────────────────────────────
vessels      = defaultdict(dict)
vessels_lock = threading.Lock()
stats        = {"meddelanden": 0, "giltiga": 0}


def update_vessel(mmsi: str, **kwargs):
    with vessels_lock:
        vessels[mmsi].update(kwargs)
        vessels[mmsi]["sedd"] = datetime.now()


# ── Signalbearbetning ─────────────────────────────────────────────────────────

def mix_to_baseband(iq: np.ndarray, offset_hz: float, fs: float) -> np.ndarray:
    """Frekvensskifta IQ-signal med offset_hz Hz."""
    t = np.arange(len(iq)) / fs
    return iq * np.exp(-1j * 2 * np.pi * offset_hz * t).astype(np.complex64)


def fm_demod(iq: np.ndarray) -> np.ndarray:
    """FM-demodulering via fasens derivata."""
    diff = iq[1:] * np.conj(iq[:-1])
    return np.angle(diff).astype(np.float32)


def lowpass(sig: np.ndarray) -> np.ndarray:
    """FIR low-pass-filter."""
    return np.convolve(sig, LP_FILTER, mode="same")


def extract_bits(sig: np.ndarray, sps: float) -> list:
    """Integrate-and-dump bit-extraktion."""
    bits = []
    n_bits = int(len(sig) / sps)
    for i in range(n_bits):
        chunk = sig[int(i * sps):int((i + 1) * sps)]
        bits.append(1 if chunk.mean() > 0 else 0)
    return bits


def nrzi_decode(bits: list) -> list:
    """NRZI: transition→0, ingen transition→1."""
    out = [0]
    for i in range(1, len(bits)):
        out.append(0 if bits[i] != bits[i - 1] else 1)
    return out


def remove_bit_stuffing(bits: list) -> list:
    """Ta bort bit-stuffing: 0 efter 5 på varandra följande 1:or."""
    result = []
    ones = 0
    i = 0
    while i < len(bits):
        b = bits[i]
        if ones == 5:
            if b == 0:          # Stuffad nolla – hoppa över
                ones = 0
                i += 1
                continue
            else:               # Flag eller fel – nollställ
                ones = 0
        ones = ones + 1 if b == 1 else 0
        result.append(b)
        i += 1
    return result


def bits_to_bytes(bits: list) -> bytes:
    """Konvertera bitar (LSB-first per byte) till bytes."""
    n = (len(bits) // 8) * 8
    result = []
    for i in range(0, n, 8):
        byte = sum(bits[i + j] << j for j in range(8))
        result.append(byte)
    return bytes(result)


def crc16_ibm(data: bytes) -> int:
    """CRC-16/IBM – polynomial 0x8005, init 0xFFFF, refin, refout."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8005 if crc & 1 else crc >> 1
    return crc


def bytes_to_nmea_payload(frame_bytes: bytes) -> str:
    """
    Konvertera rå HDLC-bytes till AIS NMEA-payload-sträng.
    Bytes är LSB-first → läs MSB-first per byte, ta 6-bit grupper,
    koda enligt AIS 6-bit ASCII.
    """
    all_bits = []
    for byte in frame_bytes:
        for i in range(7, -1, -1):       # MSB-first
            all_bits.append((byte >> i) & 1)

    payload = []
    for i in range(0, len(all_bits) - 5, 6):
        val = sum(all_bits[i + j] << (5 - j) for j in range(6))
        val += 48
        if val > 87:
            val += 8
        payload.append(chr(val))
    return "".join(payload)


def nmea_checksum(sentence: str) -> str:
    """Beräkna NMEA-checksumma (XOR av alla tecken mellan ! och *)."""
    chk = 0
    for c in sentence:
        chk ^= ord(c)
    return f"{chk:02X}"


def extract_hdlc_frames(bits: list) -> list:
    """
    Leta efter HDLC-ramar i bitströmmen.
    Returnerar lista av (data_bytes, kanal_bokstav).
    """
    frames = []
    flag = HDLC_FLAG
    n = len(bits)
    flag_len = 8

    # Hitta flaggor
    flag_positions = []
    for i in range(n - flag_len + 1):
        if bits[i:i + flag_len] == flag:
            flag_positions.append(i)

    # Extrahera ramar mellan på varandra följande flaggor
    for k in range(len(flag_positions) - 1):
        start = flag_positions[k] + flag_len
        end   = flag_positions[k + 1]
        frame_bits = bits[start:end]

        if len(frame_bits) < 40:
            continue

        unstuffed = remove_bit_stuffing(frame_bits)

        if len(unstuffed) < 40:
            continue

        frame_bytes = bits_to_bytes(unstuffed)

        if len(frame_bytes) < 5:
            continue

        # CRC-kontroll
        data          = frame_bytes[:-2]
        recv_crc      = frame_bytes[-2] | (frame_bytes[-1] << 8)
        computed_crc  = crc16_ibm(data)

        if computed_crc == recv_crc:
            frames.append(data)

    return frames


def process_channel(iq: np.ndarray, center_freq: int, channel_freq: int, channel_id: str):
    """Processa en AIS-kanal ur IQ-data och returnera avkodade fartygsmeddelanden."""
    results = []

    # Frekvensskifta till kanalens basband
    offset = channel_freq - center_freq
    bb     = mix_to_baseband(iq, offset, SAMPLE_RATE)

    # FM-demodulering
    demod  = fm_demod(bb)

    # Low-pass-filtrering
    filtered = lowpass(demod)

    # Bit-extraktion
    bits = extract_bits(filtered, SPS)

    if len(bits) < 64:
        return results

    # NRZI-avkodning
    decoded = nrzi_decode(bits)

    # HDLC-ramar
    frames = extract_hdlc_frames(decoded)

    for frame_data in frames:
        try:
            payload  = bytes_to_nmea_payload(frame_data)
            body     = f"AIVDM,1,1,,{channel_id},{payload},0"
            checksum = nmea_checksum(body)
            nmea     = f"!{body}*{checksum}".encode()

            msg = ais_decode(nmea)
            results.append(msg)
            stats["giltiga"] += 1
        except Exception:
            pass

    return results


def decode_and_store(msg):
    """Packa upp ett avkodat AIS-meddelande och spara i fartygsregistret."""
    stats["meddelanden"] += 1
    try:
        d    = msg.asdict()
        mmsi = str(d.get("mmsi", ""))
        if not mmsi:
            return

        update_vessel(mmsi, **{k: v for k, v in d.items() if v is not None})
    except Exception:
        pass


# ── Presentation ──────────────────────────────────────────────────────────────

STATUS_CODES = {
    0: "Under gång (motor)", 1: "För ankar", 2: "Inte manöverbar",
    3: "Begränsad manöverförmåga", 5: "Förtöjd", 7: "Fiskar", 15: "–",
}


def print_table(stop_event: threading.Event):
    """Skriv ut fartygsregistret var 3:e sekund."""
    while not stop_event.is_set():
        now = datetime.now()
        max_age_s = 120

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
                namn  = str(info.get("shipname", info.get("name", "–"))).strip() or "–"
                fart  = f"{info['speed']:.1f}"   if info.get("speed")   is not None else "–"
                kurs  = f"{info['course']:.0f}°" if info.get("course")  is not None else "–"
                lat   = f"{info['lat']:.4f}"     if info.get("lat")     is not None else "–"
                lon   = f"{info['lon']:.4f}"     if info.get("lon")     is not None else "–"
                status_nr = info.get("status", 15)
                status = STATUS_CODES.get(status_nr, str(status_nr))
                print(f"  {mmsi:<12} {namn:<22} {fart:<11} {kurs:<8} {lat:>10} {lon:>11}  {status}")

        time.sleep(3)


# ── Huvudloop ─────────────────────────────────────────────────────────────────

def run_ais():
    """Starta AIS-mottagning."""
    print("\n" + "=" * 50)
    print(" Lyssnar på fartyg (AIS 161.975 / 162.025 MHz)")
    print(" Tryck Ctrl+C för att avsluta")
    print("=" * 50 + "\n")

    center_freq = 162_000_000   # Mitt mellan kanal A och B

    try:
        sdr = RtlSdr()
    except Exception as e:
        print(f"❌ Kunde inte öppna SDR-dongle: {e}")
        print("   Kontrollera att dongeln är inkopplad.")
        return

    sdr.sample_rate = SAMPLE_RATE
    sdr.center_freq = center_freq
    sdr.gain        = GAIN

    print(f"  Samplingsfrekvens : {SAMPLE_RATE/1e3:.0f} kHz")
    print(f"  Centerfrekvens    : {center_freq/1e6:.3f} MHz")
    print(f"  Förstärkning      : {GAIN} dB")
    print(f"  Kanaler           : A ({CHANNELS['A']/1e6:.3f} MHz)  B ({CHANNELS['B']/1e6:.3f} MHz)\n")
    print("  Startar mottagning...\n")

    stop_event    = threading.Event()
    display_thread = threading.Thread(target=print_table, args=(stop_event,), daemon=True)
    display_thread.start()

    try:
        while True:
            iq = sdr.read_samples(CHUNK)

            for ch_id, ch_freq in CHANNELS.items():
                for msg in process_channel(iq, center_freq, ch_freq, ch_id):
                    decode_and_store(msg)

    except KeyboardInterrupt:
        print("\n\nAvbruten av användaren.")
    except Exception as e:
        print(f"\n❌ Fel under mottagning: {e}")
    finally:
        stop_event.set()
        sdr.close()
