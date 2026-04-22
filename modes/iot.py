"""
IoT-sniffning – 868 MHz ISM-bandet (Europa)

868 MHz-bandet används av en stor mängd trådlösa IoT-enheter:
  • LoRa / LoRaWAN   – 868.1 / 868.3 / 868.5 MHz (SF7–SF12, FHSS)
  • Z-Wave           – 868.42 MHz (FSK, hemautomation)
  • Wireless M-Bus   – 868.3 / 868.95 MHz (FSK, smarta el-/vattenmätare)
  • Zigbee           – mestadels 2.4 GHz men sub-GHz-varianter på 868 MHz
  • OOK-enheter      – dörrklockor, larm, fjärrkontroller, parkeringssensorer
  • Sigfox            – 868.13 MHz (LPWAN)
  • mioty            – 868 MHz (TDMA LPWAN, Elvaco m.fl.)

Lägen:
  1. Protokollavkodare  – kör rtl_433 (känner igen 200+ protokoll)
  2. Burst-detektor     – visar alla signalpulser i realtid (även okända)
  3. Kanal-FFT-skanner  – sök igenom LoRaWAN-kanaler efter aktivitet
"""

import json
import math
import subprocess
import sys
import time
import threading
from collections import deque
from datetime import datetime

import numpy as np

try:
    from rtlsdr import RtlSdr
except ImportError:
    RtlSdr = None

# ── Konstanter ────────────────────────────────────────────────────────────────

GAIN = 42

# LoRaWAN EU868-kanaler + Z-Wave + M-Bus
IOT_CHANNELS = {
    "1": ("868.100 MHz  LoRaWAN kanal 0  (primär uplink)",       868_100_000),
    "2": ("868.300 MHz  LoRaWAN kanal 1  + Wireless M-Bus",      868_300_000),
    "3": ("868.500 MHz  LoRaWAN kanal 2  (primär uplink)",       868_500_000),
    "4": ("867.100 MHz  LoRaWAN kanal 3  (extra uplink)",        867_100_000),
    "5": ("867.300 MHz  LoRaWAN kanal 4  (extra uplink)",        867_300_000),
    "6": ("867.500 MHz  LoRaWAN kanal 5  (extra uplink)",        867_500_000),
    "7": ("867.700 MHz  LoRaWAN kanal 6  (extra uplink)",        867_700_000),
    "8": ("867.900 MHz  LoRaWAN kanal 7  (extra uplink)",        867_900_000),
    "9": ("868.420 MHz  Z-Wave           (hemautomation FSK)",   868_420_000),
    "10":("868.950 MHz  Wireless M-Bus B (smarta mätare FSK)",   868_950_000),
    "11":("868.130 MHz  Sigfox uplink    (LPWAN 100 Hz)",        868_130_000),
    "12":("869.525 MHz  LoRaWAN RX2      (downlink SF12/125k)",  869_525_000),
}

# Kanaler att skanna i FFT-läget
SCAN_FREQS = [
    868_100_000, 868_300_000, 868_500_000,
    867_100_000, 867_300_000, 867_500_000,
    867_700_000, 867_900_000, 868_420_000,
    868_950_000, 869_525_000,
]

# ── Hjälp: signalstyrka ───────────────────────────────────────────────────────

def iq_to_power_db(iq: np.ndarray) -> float:
    pwr = float(np.mean(np.abs(iq) ** 2)) + 1e-12
    return 10 * math.log10(pwr)


# ─────────────────────────────────────────────────────────────────────────────
# LÄGE 1: rtl_433 protokollavkodare
# ─────────────────────────────────────────────────────────────────────────────

def format_iot_packet(data: dict) -> str:
    """Formatera ett avkodat IoT-paket snyggt."""
    ts    = data.get("time", datetime.now().strftime("%H:%M:%S"))
    model = data.get("model", "Okänd enhet")
    uid   = data.get("id", data.get("channel", "?"))

    # Protokollspecifik ikon
    model_l = model.lower()
    if "lora" in model_l:
        icon = "📡"
    elif "zwave" in model_l or "z-wave" in model_l:
        icon = "🏠"
    elif "mbus" in model_l or "m-bus" in model_l or "meter" in model_l:
        icon = "🔌"
    elif "doorbell" in model_l or "bell" in model_l:
        icon = "🔔"
    elif "smoke" in model_l or "alarm" in model_l:
        icon = "🚨"
    elif "temp" in model_l or "weather" in model_l:
        icon = "🌡️"
    else:
        icon = "📶"

    lines = [f"\n[{ts}] {icon} {model}  (id: {uid})"]

    # Vanliga fält
    field_map = [
        ("temperature_C",  "🌡️  Temp",       lambda v: f"{v:.1f} °C"),
        ("humidity",       "💧 Luftfuktighet", lambda v: f"{v} %"),
        ("battery_ok",     "🔋 Batteri",      lambda v: "OK ✅" if v else "Lågt 🪫"),
        ("rssi",           "📶 RSSI",         lambda v: f"{v:.1f} dB"),
        ("snr",            "〰️  SNR",          lambda v: f"{v:.1f} dB"),
        ("freq",           "📻 Frekvens",     lambda v: f"{float(v)/1e6:.4f} MHz" if float(v) > 1e4 else f"{v} MHz"),
    ]
    shown = {"time", "model", "id", "channel", "mod", "noise"}
    for key, label, fmt in field_map:
        if key in data:
            lines.append(f"   {label:<20}: {fmt(data[key])}")
            shown.add(key)

    # Övriga fält
    for k, v in data.items():
        if k not in shown:
            lines.append(f"   ℹ️  {k:<20}: {v}")

    return "\n".join(lines)


def run_decoder(settings: dict):
    """Starta rtl_433 för 868 MHz protokollavkodning."""
    gain = settings.get("gain", GAIN)
    ppm  = settings.get("ppm",  0)

    print("\n  Välj startfrekvens för avkodaren:")
    for k, (name, _) in IOT_CHANNELS.items():
        print(f"    {k:>2}. {name}")
    print(f"\n  Val [1–12, Enter = 868.3 MHz]: ", end="")
    ch = input().strip() or "2"
    freq_hz = IOT_CHANNELS.get(ch, IOT_CHANNELS["2"])[1]
    freq_str = f"{freq_hz/1e6:.3f}M"

    cmd = [
        "rtl_433",
        "-f", freq_str,
        "-s", "1M",          # 1 Msps – täcker LoRa 125/250 kHz kanalbredd
        "-F", "json",
        "-M", "time:iso",
        "-M", "rssi",
        "-M", "snr",
        "-M", "freq",
    ]
    if gain != "auto":
        cmd += ["-g", str(int(gain))]
    if ppm != 0:
        cmd += ["-p", str(ppm)]

    print(f"\n  Avkodar protokoll på {freq_hz/1e6:.3f} MHz...")
    print(f"  Kommando: {' '.join(cmd)}")
    print("  Väntar på paket... (Ctrl+C för att avsluta)\n")
    print("─" * 55)

    seen_models: dict[str, int] = {}

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                model = data.get("model", "?")
                seen_models[model] = seen_models.get(model, 0) + 1
                print(format_iot_packet(data))
                print(f"  [Totalt sett: {', '.join(f'{m}×{n}' for m, n in seen_models.items())}]")
            except json.JSONDecodeError:
                if line and not line.startswith("{"):
                    print(f"  ℹ️  {line}")

    except KeyboardInterrupt:
        print("\n\nAvbruten.")
        proc.terminate()
    except FileNotFoundError:
        print("❌ rtl_433 hittades inte. Installera med: brew install rtl_433")


# ─────────────────────────────────────────────────────────────────────────────
# LÄGE 2: Burst-detektor (okända signaler)
# ─────────────────────────────────────────────────────────────────────────────

def run_burst_detector(settings: dict):
    """Detektera alla signalpulser på vald frekvens, oavsett protokoll."""
    if RtlSdr is None:
        print("❌ pyrtlsdr ej installerat.")
        return

    gain = settings.get("gain", GAIN)
    ppm  = settings.get("ppm",  0)

    print("\n  Välj centerfrekvens:")
    for k, (name, _) in IOT_CHANNELS.items():
        print(f"    {k:>2}. {name}")
    print(f"\n  Val [Enter = 868.3 MHz]: ", end="")
    ch = input().strip() or "2"
    freq_hz = IOT_CHANNELS.get(ch, IOT_CHANNELS["2"])[1]

    threshold_raw = input(f"  Tröskel för burst-detektion [6 dB över brus, Enter=6]: ").strip()
    threshold_above_noise = float(threshold_raw) if threshold_raw else 6.0

    SAMPLE_RATE = 2_048_000
    CHUNK       = 256_000    # ~125 ms per block

    try:
        sdr = RtlSdr()
    except Exception as e:
        print(f"❌ Kunde inte öppna SDR: {e}")
        return

    sdr.sample_rate = SAMPLE_RATE
    sdr.center_freq = freq_hz
    sdr.gain        = gain
    if ppm != 0:
        sdr.freq_correction = ppm

    print(f"\n  Burst-detektor aktiv på {freq_hz/1e6:.3f} MHz")
    print(f"  Samplingsfrekvens: {SAMPLE_RATE/1e6:.3f} Msps  |  Tröskel: +{threshold_above_noise:.0f} dB")
    print("  Väntar på signaler... (Ctrl+C för att avsluta)\n")
    print("─" * 55)

    noise_history: deque = deque(maxlen=20)
    burst_count   = 0
    in_burst      = False

    try:
        while True:
            iq  = sdr.read_samples(CHUNK).astype(np.complex64)
            mag = np.abs(iq)

            # Brusuppskattning via 10:e percentilen (undviker signaler)
            noise_floor = float(np.percentile(mag, 10)) + 1e-9
            noise_history.append(noise_floor)
            avg_noise = float(np.mean(noise_history))
            noise_db  = 20 * math.log10(avg_noise)

            # Signalstyrka
            peak     = float(np.max(mag))
            peak_db  = 20 * math.log10(peak + 1e-9)
            above_db = peak_db - noise_db

            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

            if above_db >= threshold_above_noise:
                if not in_burst:
                    burst_count += 1
                    in_burst = True

                    # Grov bandbreddsskattning via FFT
                    fft   = np.abs(np.fft.fftshift(np.fft.fft(iq, n=4096)))
                    freqs = np.fft.fftshift(np.fft.fftfreq(4096, 1 / SAMPLE_RATE))
                    thresh_fft = np.max(fft) * 0.2
                    active = freqs[fft > thresh_fft]
                    bw_hz  = (float(np.max(active)) - float(np.min(active))) if len(active) > 1 else 0
                    offset = float(freqs[np.argmax(fft)])

                    bw_str  = f"{bw_hz/1e3:.1f} kHz" if bw_hz < 1e6 else f"{bw_hz/1e6:.2f} MHz"
                    freq_actual = (freq_hz + offset) / 1e6

                    bar_len = 20
                    filled  = min(bar_len, int(above_db / 30 * bar_len))
                    bar     = "█" * filled + "░" * (bar_len - filled)

                    print(f"  [{ts}] 🔴 BURST #{burst_count:04d}  "
                          f"{freq_actual:.4f} MHz  "
                          f"+{above_db:.1f} dB  BW≈{bw_str}  [{bar}]")
            else:
                if in_burst:
                    in_burst = False
                else:
                    # Tyst – visa aktivitetslinje var 2:a sekund
                    bar_len = 20
                    filled  = max(0, min(bar_len, int((above_db + 10) / 20 * bar_len)))
                    bar     = "·" * filled + " " * (bar_len - filled)
                    print(f"  [{ts}]  stilla  {noise_db:+.1f} dB brus  [{bar}]",
                          end="\r", flush=True)

    except KeyboardInterrupt:
        print(f"\n\nAvbruten. Totalt {burst_count} burstar detekterade.")
    finally:
        sdr.close()


# ─────────────────────────────────────────────────────────────────────────────
# LÄGE 3: Kanal-FFT-skanner
# ─────────────────────────────────────────────────────────────────────────────

def run_channel_scanner(settings: dict):
    """Skanna LoRaWAN EU868-kanalerna och visa signalstyrka i realtid."""
    if RtlSdr is None:
        print("❌ pyrtlsdr ej installerat.")
        return

    gain = settings.get("gain", GAIN)
    ppm  = settings.get("ppm",  0)

    # Vi centrar på 868.3 MHz och täcker ±1 MHz med 2.4 Msps
    CENTER   = 868_300_000
    SR       = 2_400_000
    CHUNK    = 131_072
    ROUNDS   = 0

    try:
        sdr = RtlSdr()
    except Exception as e:
        print(f"❌ Kunde inte öppna SDR: {e}")
        return

    sdr.sample_rate = SR
    sdr.center_freq = CENTER
    sdr.gain        = gain
    if ppm != 0:
        sdr.freq_correction = ppm

    print(f"\n  Kanal-FFT-skanner  |  Center: {CENTER/1e6:.1f} MHz  |  BW: {SR/1e6:.1f} MHz")
    print("  Ctrl+C för att avsluta\n")

    # Kanaloffsets relativt center
    chan_offsets = [(f - CENTER) for f in SCAN_FREQS if abs(f - CENTER) < SR / 2]
    chan_labels  = [f"{f/1e6:.3f}" for f in SCAN_FREQS if abs(f - CENTER) < SR / 2]

    try:
        while True:
            iq  = sdr.read_samples(CHUNK).astype(np.complex64)
            N   = len(iq)
            fft = np.abs(np.fft.fftshift(np.fft.fft(iq, n=N))) ** 2
            freqs = np.fft.fftshift(np.fft.fftfreq(N, 1 / SR))  # offset från center

            ROUNDS += 1
            print(f"\n  Skanning #{ROUNDS:04d}  [{datetime.now().strftime('%H:%M:%S')}]")
            print(f"  {'Frekvens':>12}  {'dBm':>6}  {'Bar':<30}")
            print("  " + "─" * 52)

            for offset, label in zip(chan_offsets, chan_labels):
                # Genomsnitt i ±62.5 kHz runt kanalcentret
                mask = np.abs(freqs - offset) < 62_500
                if mask.sum() == 0:
                    continue
                pwr  = float(np.mean(fft[mask]))
                db   = 10 * math.log10(pwr + 1e-12)
                db_n = max(0, min(30, db + 60))   # normera –60…–30 dBm → 0…30
                bar  = "█" * int(db_n) + "░" * (30 - int(db_n))
                flag = " ◀ AKTIV" if db > -45 else ""
                print(f"  {label+' MHz':>12}  {db:>+6.1f}  [{bar}]{flag}")

            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n\nAvbruten.")
    finally:
        sdr.close()


# ─────────────────────────────────────────────────────────────────────────────
# Huvudfunktion
# ─────────────────────────────────────────────────────────────────────────────

def run_iot(settings: dict | None = None):
    settings = settings or {}

    print("\n" + "=" * 55)
    print(" 📡 IoT-sniffning – 868 MHz ISM-bandet (Europa)")
    print("=" * 55)
    print("""
  868 MHz används av hundratals IoT-protokoll:
    LoRa/LoRaWAN  · Z-Wave  · Wireless M-Bus
    Sigfox  · dörrklockor  · larm  · parkeringssensorer
    smarta el-/vatten-/gasmätare  · mioty  · m.fl.

  Välj läge:
    1. 🔍 Protokollavkodare  – rtl_433 känner igen 200+ protokoll
    2. 🔴 Burst-detektor     – visa ALLA signaler, även okända
    3. 📊 Kanal-FFT-skanner  – realtids signalstyrka per LoRa-kanal
""")

    val = input("  Val [1]: ").strip() or "1"

    if val == "1":
        try:
            subprocess.run(["rtl_433", "-V"], capture_output=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            print("❌ rtl_433 saknas. Installera med: brew install rtl_433")
            return
        run_decoder(settings)

    elif val == "2":
        run_burst_detector(settings)

    elif val == "3":
        run_channel_scanner(settings)

    else:
        print("  Ogiltigt val.")
