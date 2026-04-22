#!/usr/bin/env python3
"""
SDR Mottagare – Huvudmeny
Välj mottagningsläge och justera inställningar (gain, squelch m.m.)
"""

import sys
import subprocess

# ── Globala inställningar – delas av alla moduler ─────────────────────────────
SETTINGS = {
    "gain":         "auto",   # dB eller "auto"
    "squelch_db":   -40,      # dB, används av röstläget
    "ppm":          0,        # Frekvenskorrigering (parts per million)
}


def menu_text() -> str:
    g   = SETTINGS["gain"]
    sq  = SETTINGS["squelch_db"]
    ppm = SETTINGS["ppm"]
    gain_str = f"{g} dB" if g != "auto" else "auto"
    return f"""
╔══════════════════════════════════════╗
║         SDR Mottagare v1.6           ║
╠══════════════════════════════════════╣
║  1. Vädersensorer    (433 MHz)       ║
║  2. Flygtrafik ADS-B (1090 MHz)      ║
║  3. Fartyg AIS       (162 MHz)       ║
║  4. ACARS flygdata   (129-132 MHz)   ║
║  5. POCSAG/FLEX      (148-932 MHz)   ║
║  6. Spektrum & Signal (skanner)      ║
║  7. Röst flyg/marin  (AM/FM)         ║
║  8. 🚂 Järnväg       (153-156 MHz)   ║
║  9. 📡 IoT-sniffning (868 MHz)       ║
║ 10. 🛰️  Satellit Meteor-M2 (137 MHz)  ║
╠══════════════════════════════════════╣
║  S. Inställningar                    ║
║  A. Avsluta                          ║
╠══════════════════════════════════════╣
║  Gain: {gain_str:<8}  Squelch: {sq:<5} dB  ║
║  PPM:  {ppm:<+4}                         ║
╚══════════════════════════════════════╝
"""

def show_settings():
    """Interaktiv inställningsmeny."""
    while True:
        g   = SETTINGS["gain"]
        sq  = SETTINGS["squelch_db"]
        ppm = SETTINGS["ppm"]
        gain_str = f"{g} dB" if g != "auto" else "auto"

        print(f"""
┌─────────────────────────────────────┐
│           Inställningar             │
├─────────────────────────────────────┤
│  1. Gain        : {gain_str:<18} │
│  2. Squelch     : {sq:<3} dB              │
│  3. PPM-korr.   : {ppm:<+4}                │
│  4. Tillbaka                        │
└─────────────────────────────────────┘""")

        val = input("  Val: ").strip().lower()

        if val == "1":
            print(f"\n  Nuvarande: {gain_str}")
            print("  Ange gain i dB (0–49) eller 'auto'")
            print("  Låga värden = lägre förstärkning, 'auto' = dongeln väljer själv")
            raw = input("  Gain [auto]: ").strip().lower() or "auto"
            if raw == "auto":
                SETTINGS["gain"] = "auto"
            else:
                try:
                    v = float(raw)
                    if 0 <= v <= 49:
                        SETTINGS["gain"] = v
                    else:
                        print("  ⚠️  Måste vara mellan 0 och 49.")
                except ValueError:
                    print("  ⚠️  Ogiltigt värde.")

        elif val == "2":
            print(f"\n  Nuvarande squelch: {sq} dB")
            print("  Lägre värde = känsligare (öppnar vid svagare signaler)")
            print("  Typiska värden: -50 (känslig)  -40 (normal)  -30 (bara starka)")
            raw = input(f"  Squelch dB [{sq}]: ").strip()
            if raw:
                try:
                    SETTINGS["squelch_db"] = float(raw)
                except ValueError:
                    print("  ⚠️  Ogiltigt värde.")

        elif val == "3":
            print(f"\n  Nuvarande PPM: {ppm:+d}")
            print("  PPM-korrigering kompenserar för kristallfel i dongeln.")
            print("  Hitta rätt värde med ett känt program (t.ex. GQRX) eller spektrumskannern.")
            print("  Typiska värden: -60 till +60. Noll är bra startpunkt.")
            raw = input(f"  PPM [{ppm:+d}]: ").strip()
            if raw:
                try:
                    SETTINGS["ppm"] = int(raw)
                except ValueError:
                    print("  ⚠️  Ogiltigt värde, måste vara ett heltal.")

        elif val in ("4", "b", ""):
            return
        else:
            print("  Ogiltigt val.")


def apply_settings(sdr) -> None:
    """Applicera globala inställningar på ett öppet RtlSdr-objekt."""
    sdr.gain            = SETTINGS["gain"]
    sdr.freq_correction = SETTINGS["ppm"]


def check_dependencies() -> None:
    """Kontrollera att nödvändiga verktyg finns installerade."""
    missing = []

    # Kontrollera rtl_433
    try:
        subprocess.run(["rtl_433", "-V"], capture_output=True)
    except FileNotFoundError:
        missing.append("rtl_433  →  brew install rtl_433  (macOS) / apt install rtl-433 (Linux)")

    # Kontrollera pyModeS (Python-paket)
    try:
        import pyModeS  # noqa: F401
    except ImportError:
        missing.append("pyModeS  →  pip install pyModeS")

    # Kontrollera pyrtlsdr
    try:
        import rtlsdr  # noqa: F401
    except ImportError:
        missing.append("pyrtlsdr →  pip install pyrtlsdr")

    # Kontrollera pyais
    try:
        import pyais  # noqa: F401
    except ImportError:
        missing.append("pyais    →  pip install pyais")

    if missing:
        print("\n⚠️  Följande beroenden saknas:\n")
        for m in missing:
            print(f"   • {m}")
        print("\nInstallera dem och starta om programmet.\n")
        sys.exit(1)


def main():
    check_dependencies()

    try:
        while True:
            print(menu_text())
            val = input("Välj alternativ: ").strip().lower()

            if val == "1":
                from modes.weather import run_weather
                run_weather()
            elif val == "2":
                from modes.adsb import run_adsb
                run_adsb(settings=SETTINGS)
            elif val == "3":
                from modes.ais import run_ais
                run_ais(settings=SETTINGS)
            elif val == "4":
                from modes.acars import run_acars
                run_acars(settings=SETTINGS)
            elif val == "5":
                from modes.paging import run_paging
                run_paging(settings=SETTINGS)
            elif val == "6":
                from modes.scanner import run_scanner_mode
                run_scanner_mode(settings=SETTINGS)
            elif val == "7":
                from modes.voice import run_voice
                run_voice(settings=SETTINGS)
            elif val == "8":
                from modes.railway import run_railway
                run_railway(settings=SETTINGS)
            elif val == "9":
                from modes.iot import run_iot
                run_iot(settings=SETTINGS)
            elif val == "10":
                from modes.satellite import run_satellite
                run_satellite(settings=SETTINGS)
            elif val in ("s", "i"):
                show_settings()
            elif val in ("a", "0", "q"):
                print("Hejdå!")
                sys.exit(0)
            else:
                print("Ogiltigt val, försök igen.")
    except KeyboardInterrupt:
        print("\n\nHejdå!")


if __name__ == "__main__":
    main()
