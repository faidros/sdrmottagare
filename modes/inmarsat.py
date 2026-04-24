"""
Inmarsat – mottagning av L-band satellitmeddelanden (läge 13)

Inmarsat är ett geostationärt satellitsystem (fixerat på himlen, ingen passprediktion
behövs). På 1.5 GHz-bandet sänder de flera typer av digitala signaler som kan
avkodas med RTL-SDR + SatDump:

  STD-C / EGC  – Sjöfartsmeddelanden: NAVTEX, väderprognoser, SAR-larm (BPSK 1200 baud)
  AERO 0.6k    – Flygplanskommunikation via satellit, korta ACARS-paket (600 baud)
  AERO 1.2k    – Samma typ, högre baud (1200 baud)
  AERO 10.5k   – Bredbandigare flygdata (OQPSK 10500 baud)
  AERO-C 8.4k  – Äldre Inmarsat-C för flyg (OQPSK 8400 baud)

Bästa satelliten för Sverige: Alphasat (I-4 F4) vid 25°E
  Elevation från Sverige: ~27–30° (syd-sydöst, azimut ~160°)

Krav:
  satdump  – avkodning
  Patch-antenn eller helikantenn för ~1.5 GHz (RTL-SDR:s normalantenn räcker ej)

Avkodade meddelanden sparas i ~/sdr_data/inmarsat/<signal>_<tidsstämpel>/
"""

import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DATA_DIR = Path.home() / "sdr_data" / "inmarsat"

# ── Signaltyper ───────────────────────────────────────────────────────────────
SIGNALS = {
    "1": {
        "label":    "STD-C / EGC  – Sjöfart & SAR-larm",
        "desc":     "Väderprognoser, navigationsvarningar, SAR-larm. Enklast att ta emot.",
        "pipeline": "inmarsat_std_c",
        "freq":     1_537_500_000,
        "sr":       1_000_000,
    },
    "2": {
        "label":    "AERO 0.6k    – Flygkommunikation (600 baud)",
        "desc":     "Korta ACARS-paket och positioner från flygplan via satellit.",
        "pipeline": "inmarsat_aero_6",
        "freq":     1_545_025_000,
        "sr":       1_000_000,
    },
    "3": {
        "label":    "AERO 1.2k    – Flygkommunikation (1200 baud)",
        "desc":     "Samma som ovan, något högre hastighet.",
        "pipeline": "inmarsat_aero_12",
        "freq":     1_545_050_000,
        "sr":       1_000_000,
    },
    "4": {
        "label":    "AERO 10.5k   – Flygdata bredband (10 500 baud)",
        "desc":     "Vanligaste snabba kanalen på Inmarsat-4-satelliter.",
        "pipeline": "inmarsat_aero_105",
        "freq":     1_545_940_000,
        "sr":       2_000_000,
    },
    "5": {
        "label":    "AERO-C 8.4k  – Äldre flygkommunikation (8 400 baud)",
        "desc":     "Inmarsat-C för flyg, används av äldre utrustning.",
        "pipeline": "inmarsat_aero_84",
        "freq":     1_545_940_000,
        "sr":       2_000_000,
    },
}

# ── Satelliter synliga från Europa ────────────────────────────────────────────
SATELLITES = {
    "1": {"name": "Alphasat / I-4 F4  (25°E)  ← bäst för Sverige",
          "elev_se": "~28°", "az_se": "~160° (SSÖ)"},
    "2": {"name": "I-4 F2             (15.5°W)  – Atlantiken",
          "elev_se": "~21°", "az_se": "~213° (SSV)"},
    "3": {"name": "I-3 F2             (64°E)   – Indiska oceanen",
          "elev_se": "~16°", "az_se": "~129° (SÖ)"},
}


def run_satdump_live(pipeline: str, freq: int, sr: int,
                     output_dir: Path, gain, ppm: int) -> subprocess.Popen:
    cmd = [
        "satdump", "live", pipeline,
        str(output_dir),
        "--source",      "rtlsdr",
        "--gain",        str(gain),
        "--samplerate",  str(sr),
        "--frequency",   str(freq),
    ]
    if ppm:
        cmd += ["--ppm", str(ppm)]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)


def run_inmarsat(settings: dict | None = None):
    settings = settings or {}

    print("\n" + "=" * 60)
    print(" 📡 Inmarsat L-band  (1 537–1 546 MHz)")
    print(" Sjöfartsmeddelanden (STD-C) och flygkommunikation (AERO)")
    print("=" * 60)

    if not shutil.which("satdump"):
        print("\n  ❌ satdump hittades inte.")
        print("     macOS: brew install satdump  (+ symlänkar, se README)")
        return

    print("""
  Inmarsat är geostationärt – satelliten sitter stilla på himlen.
  Ingen passprediktion behövs; ta emot när du vill.

  ⚠️  ANTENN: En vanlig RTL-SDR-antenn räcker INTE för 1.5 GHz.
     Du behöver en patchantenn eller helikantenn (~1.5 GHz).
     Utan rätt antenn: inga paket, oavsett inställning.
""")

    # ── Välj signaltyp ────────────────────────────────────────────────────────
    print("  Välj signaltyp:\n")
    for k, s in SIGNALS.items():
        print(f"  {k}. {s['label']}")
        print(f"       {s['desc']}")
        print(f"       Frekvens: {s['freq']/1e6:.3f} MHz\n")

    print("  Val [1]: ", end="", flush=True)
    try:
        sig_val = input().strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print("\n  Avbruten.")
        return

    if sig_val not in SIGNALS:
        print("  Ogiltigt val, använder STD-C.")
        sig_val = "1"
    sig = SIGNALS[sig_val]

    # ── Välj satellit ─────────────────────────────────────────────────────────
    print("\n  Välj satellit (för antennpekning):\n")
    for k, sat in SATELLITES.items():
        print(f"  {k}. {sat['name']}")
        print(f"       Elevation från Sverige: {sat['elev_se']}   Azimut: {sat['az_se']}")
    print()
    print("  Val [1]: ", end="", flush=True)
    try:
        sat_val = input().strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print("\n  Avbruten.")
        return

    sat = SATELLITES.get(sat_val, SATELLITES["1"])

    # ── Inställningar ─────────────────────────────────────────────────────────
    gain = settings.get("gain", 40)
    ppm  = settings.get("ppm", 0)

    freq_mhz = sig["freq"] / 1e6
    ts        = datetime.now().strftime("%Y-%m-%d_%H%M")
    short_key = sig["pipeline"].replace("inmarsat_", "").replace("_", "-")
    output_dir = DATA_DIR / f"{short_key}_{ts}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  Satellit:    {sat['name']}")
    print(f"  Signal:      {sig['label']}")
    print(f"  Frekvens:    {freq_mhz:.3f} MHz")
    print(f"  Pipeline:    {sig['pipeline']}")
    print(f"  Data sparas: {output_dir}")
    print(f"\n  Rikta antennen: elevation {sat['elev_se']}, azimut {sat['az_se']}")
    print("\n  Startar SatDump... (Ctrl+C för att avsluta)\n")

    # ── Starta SatDump ────────────────────────────────────────────────────────
    try:
        proc = run_satdump_live(sig["pipeline"], sig["freq"], sig["sr"],
                                output_dir, gain, ppm)
    except FileNotFoundError:
        print("  ❌ satdump hittades inte.")
        return

    # ── Visa output tills Ctrl+C ──────────────────────────────────────────────
    msg_count = 0
    try:
        for line in proc.stdout:
            line = line.rstrip()
            # Visa bara intressanta rader (meddelanden, fel, status)
            if any(kw in line for kw in
                   ["[msg]", "[MSG]", "Message", "message",
                    "(I)", "(W)", "(E)", "Locked", "locked",
                    "ACARS", "EGC", "SafetyNET", "FleetNET"]):
                if "(D)" not in line:   # hoppa över debug-spam
                    print(f"  {line}")
                    if "[msg]" in line.lower() or "message" in line.lower():
                        msg_count += 1
    except KeyboardInterrupt:
        print("\n\n  Avbruten av användaren.")
    finally:
        proc.terminate()
        proc.wait()

    # ── Resultat ──────────────────────────────────────────────────────────────
    print(f"\n  🏁 Avslutad.  {msg_count} meddelanden noterade i terminalen.")

    # Kolla sparade filer
    saved = list(output_dir.rglob("*.json")) + list(output_dir.rglob("*.txt"))
    if saved:
        print(f"\n  ✅ {len(saved)} filer sparade i:\n     {output_dir}")
        for f in sorted(saved)[:10]:
            size_kb = f.stat().st_size // 1024
            print(f"     📄 {f.name:<50} ({size_kb} KB)")
        if len(saved) > 10:
            print(f"     … och {len(saved)-10} till")
        print(f"\n  Öppna mappen: open \"{output_dir}\"")
    else:
        print(f"\n  ⚠️  Inga datafiler sparades.")
        print("  Möjliga orsaker:")
        print("  • Antennen är inte anpassad för 1.5 GHz")
        print("  • Signalen för svag – kontrollera antennpekningens elevation/azimut")
        print("  • Fel satellit vald för din position")
        try:
            shutil.rmtree(output_dir)
            print(f"  🗑️  Tom mapp borttagen.")
        except Exception:
            pass
