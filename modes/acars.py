"""
ACARS – Aircraft Communications Addressing and Reporting System
Använder acarsdec som extern process för demodulering och avkodning.
JSON-output (-o 4) läses från stdout och presenteras i en löpande tabell.

Typiska ACARS-frekvenser i Europa:
  129.125, 130.025, 130.425, 130.450, 131.125, 131.525, 131.725, 131.825 MHz
"""

import json
import shutil
import subprocess
import threading
import time
from datetime import datetime

# ── Konstanter ────────────────────────────────────────────────────────────────

GAIN = 42   # dB  (-10 = AGC)
PPM  = 0

# Frekvenser att lyssna på (MHz).
# acarsdec kan ta emot flera simultant men max ~2 MHz bandbredd med RTL-SDR.
# 129.125 och 130.025 ryms i ett 2 MHz-fönster. Lägg till fler i ett separat fönster
# om du har flera donglar.
FREQS = ["129.125", "130.025"]

LABELS = {
    "H1": "Positionsrapport", "5Z": "ATIS",     "B6": "PDC/clearance",
    "20": "Dörrstatus",       "QK": "Teknisk",  "SA": "SELCAL-test",
    "80": "Driftsmeddelande", "Q0": "ACARS-logon", "_d": "Text",
    "H2": "ADS-C pos",        "16": "Väder",    "1L": "FMC-begäran",
}

# ── Meddelanderegister ────────────────────────────────────────────────────────

messages: list = []       # lista med dict per meddelande
msgs_lock = threading.Lock()
stats = {"totalt": 0}

# ── acarsdec subprocess ───────────────────────────────────────────────────────

def find_acarsdec() -> str | None:
    return shutil.which("acarsdec")


def build_command(binary: str, gain, ppm: int) -> list:
    cmd = [binary]
    # Förstärkning: -10 = AGC
    if gain == "auto":
        cmd += ["-g", "-10"]
    else:
        cmd += ["-g", str(int(gain))]
    # PPM-korrigering
    if ppm != 0:
        cmd += ["-p", str(ppm)]
    # JSON-output, en rad per meddelande
    cmd += ["-o", "4"]
    # RTL-SDR enhet 0 + frekvenser
    cmd += ["-r", "0"] + FREQS
    return cmd


# ── JSON-parsning ─────────────────────────────────────────────────────────────

def parse_line(line: str):
    """Tolka en JSON-rad från acarsdec -o 4."""
    line = line.strip()
    if not line or not line.startswith("{"):
        return
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return

    # Plocka ut fält
    tail    = d.get("tail", "–").strip()
    flight  = d.get("flight", "–").strip()
    freq    = d.get("freq", "–")
    label   = d.get("label", "–")
    text    = (d.get("text") or "").strip().replace("\n", " ").replace("\r", "")
    label_s = LABELS.get(label, label)

    msg = {
        "tid":    datetime.now().strftime("%H:%M:%S"),
        "reg":    tail   or "–",
        "flight": flight or "–",
        "freq":   freq,
        "label":  f"{label} ({label_s})",
        "text":   text[:80] if text else "(tom)",
    }

    stats["totalt"] += 1
    with msgs_lock:
        messages.append(msg)
        if len(messages) > 200:
            messages.pop(0)


# ── Presentation ──────────────────────────────────────────────────────────────

def print_table(stop_event: threading.Event):
    """Skriv ut de senaste ACARS-meddelandena var 2:a sekund."""
    while not stop_event.is_set():
        with msgs_lock:
            recent = list(messages[-20:])   # visa de 20 senaste

        print("\033[2J\033[H", end="")
        print(f"  ✈  ACARS-mottagare  –  {datetime.now().strftime('%H:%M:%S')}")
        print(f"  Frekvenser: {', '.join(FREQS)} MHz  |  Ctrl+C för att avsluta")
        print(f"  Meddelanden totalt: {stats['totalt']}\n")

        if not recent:
            print("  (Inga meddelanden ännu – väntar på flygtrafik...)")
        else:
            print(f"  {'Tid':<10} {'Reg':<10} {'Flight':<10} {'Freq':<10} {'Etikett':<24} Text")
            print("  " + "─" * 90)
            for m in recent:
                print(f"  {m['tid']:<10} {m['reg']:<10} {m['flight']:<10} {str(m['freq']):<10} "
                      f"{m['label']:<24} {m['text']}")

        time.sleep(2)


# ── Stdout-läsning ────────────────────────────────────────────────────────────

def stdout_reader(proc: subprocess.Popen, stop_event: threading.Event):
    """Läs JSON-rader från acarsdec stdout."""
    try:
        for line in proc.stdout:
            if stop_event.is_set():
                break
            parse_line(line)
    except Exception:
        pass
    stop_event.set()


# ── Huvudloop ─────────────────────────────────────────────────────────────────

def run_acars(settings: dict | None = None):
    gain = (settings or {}).get("gain", GAIN)
    ppm  = (settings or {}).get("ppm",  PPM)

    binary = find_acarsdec()
    if not binary:
        print("❌ acarsdec hittades inte.")
        print("   Bygg och installera med:")
        print("   git clone https://github.com/TLeconte/acarsdec")
        print("   cd acarsdec && mkdir build && cd build")
        print("   cmake .. -DRTLSDR=ON -DCMAKE_POLICY_VERSION_MINIMUM=3.5")
        print("   make -j4 && sudo cp acarsdec /usr/local/bin/")
        return

    cmd = build_command(binary, gain, ppm)

    print("\n" + "=" * 55)
    print(" ACARS-mottagare")
    print(" Tryck Ctrl+C för att avsluta")
    print("=" * 55 + "\n")
    gain_str = f"{gain} dB" if gain != "auto" else "auto (AGC)"
    print(f"  Avkodare      : acarsdec ({binary})")
    print(f"  Frekvenser    : {', '.join(FREQS)} MHz")
    print(f"  Förstärkning  : {gain_str}  |  PPM: {ppm:+d}")
    print(f"  Kommando      : {' '.join(cmd)}\n")
    print("  Startar acarsdec...\n")
    time.sleep(1)

    stop_event = threading.Event()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except Exception as e:
        print(f"❌ Kunde inte starta acarsdec: {e}")
        return

    display_thread = threading.Thread(target=print_table, args=(stop_event,), daemon=True)
    display_thread.start()

    reader_thread = threading.Thread(target=stdout_reader, args=(proc, stop_event), daemon=True)
    reader_thread.start()

    try:
        while not stop_event.is_set():
            if proc.poll() is not None:
                if not stop_event.is_set():
                    print("\n❌ acarsdec avslutades oväntat.")
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
