#!/usr/bin/env python3
"""
SDR Mottagare – Huvudmeny
Välj mellan vädersensorer (433 MHz) och flygtrafik (ADS-B 1090 MHz)
"""

import sys
import subprocess

MENU = """
╔══════════════════════════════════════╗
║         SDR Mottagare v1.5           ║
╠══════════════════════════════════════╣
║  1. Vädersensorer    (433 MHz)       ║
║  2. Flygtrafik ADS-B (1090 MHz)      ║
║  3. Fartyg AIS       (162 MHz)       ║
║  4. ACARS flygdata   (129-132 MHz)   ║
║  5. POCSAG/FLEX      (148-932 MHz)   ║
║  6. Spektrum & Signal (skanner)      ║
║  7. Röst flyg/marin  (AM/FM)         ║
║  8. Avsluta                          ║
╚══════════════════════════════════════╝
"""

def check_dependencies():
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

    while True:
        print(MENU)
        val = input("Välj alternativ: ").strip()

        if val == "1":
            from modes.weather import run_weather
            run_weather()
        elif val == "2":
            from modes.adsb import run_adsb
            run_adsb()
        elif val == "3":
            from modes.ais import run_ais
            run_ais()
        elif val == "4":
            from modes.acars import run_acars
            run_acars()
        elif val == "5":
            from modes.paging import run_paging
            run_paging()
        elif val == "6":
            from modes.scanner import run_scanner_mode
            run_scanner_mode()
        elif val == "7":
            from modes.voice import run_voice
            run_voice()
        elif val == "8":
            print("Hejdå!")
            sys.exit(0)
        else:
            print("Ogiltigt val, försök igen.")


if __name__ == "__main__":
    main()
