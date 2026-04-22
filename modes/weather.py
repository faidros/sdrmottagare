"""
Vädersensorer på 433 MHz
Använder rtl_433 för att avkoda sensordata och presenterar resultatet som text.
rtl_433 stöder hundratals sensorprotokoll (Oregon Scientific, Bresser, Nexus m.fl.)
"""

import subprocess
import json
import threading
import sys
from datetime import datetime


def format_sensor(data: dict) -> str:
    """Formatera sensordata till läsbar text."""
    parts = []
    tid = data.get("time", datetime.now().strftime("%H:%M:%S"))
    modell = data.get("model", "Okänd sensor")
    kanal = data.get("channel", data.get("id", "?"))

    parts.append(f"\n[{tid}] 📡 {modell}  (kanal/id: {kanal})")

    if "temperature_C" in data:
        parts.append(f"   🌡️  Temperatur : {data['temperature_C']:.1f} °C")
    if "humidity" in data:
        parts.append(f"   💧 Luftfuktighet: {data['humidity']} %")
    if "wind_avg_km_h" in data:
        parts.append(f"   💨 Vind (medel) : {data['wind_avg_km_h']:.1f} km/h")
    if "wind_max_km_h" in data:
        parts.append(f"   💨 Vind (max)   : {data['wind_max_km_h']:.1f} km/h")
    if "wind_dir_deg" in data:
        parts.append(f"   🧭 Vindriktning : {data['wind_dir_deg']}°  ({degrees_to_compass(data['wind_dir_deg'])})")
    if "rain_mm" in data:
        parts.append(f"   🌧️  Regn          : {data['rain_mm']:.1f} mm")
    if "pressure_hPa" in data:
        parts.append(f"   🔵 Lufttryck    : {data['pressure_hPa']:.1f} hPa")
    if "battery_ok" in data:
        batteri = "✅ OK" if data["battery_ok"] else "🪫 Lågt"
        parts.append(f"   🔋 Batteri      : {batteri}")

    # Visa övriga fält som inte redan visats
    visade = {"time", "model", "channel", "id", "temperature_C", "humidity",
              "wind_avg_km_h", "wind_max_km_h", "wind_dir_deg", "rain_mm",
              "pressure_hPa", "battery_ok", "mod", "freq", "rssi", "snr", "noise"}
    extra = {k: v for k, v in data.items() if k not in visade}
    for k, v in extra.items():
        parts.append(f"   ℹ️  {k:<15}: {v}")

    return "\n".join(parts)


def degrees_to_compass(deg: float) -> str:
    """Konvertera grader till kompassriktning."""
    riktningar = ["N", "NNO", "NO", "ONO", "O", "OSO", "SO", "SSO",
                  "S", "SSV", "SV", "VSV", "V", "VNV", "NV", "NNV"]
    index = round(deg / 22.5) % 16
    return riktningar[index]


def run_weather():
    """Starta rtl_433 och lyssna på vädersensorer på 433.92 MHz."""
    print("\n" + "="*50)
    print(" Lyssnar på vädersensorer (433.92 MHz)")
    print(" Tryck Ctrl+C för att avsluta")
    print("="*50 + "\n")

    cmd = [
        "rtl_433",
        "-f", "433.92M",
        "-s", "250k",
        "-F", "json",
        "-M", "time:iso",
    ]

    print(f"Kommando: {' '.join(cmd)}\n")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print("❌ rtl_433 hittades inte. Installera med: brew install rtl_433")
        return

    # Samla stderr i bakgrunden så den inte blockerar
    stderr_lines = []
    def _read_stderr():
        for line in proc.stderr:
            stderr_lines.append(line.rstrip())
    threading.Thread(target=_read_stderr, daemon=True).start()

    received = 0
    try:
        for rad in proc.stdout:
            rad = rad.strip()
            if not rad:
                continue
            try:
                data = json.loads(rad)
                received += 1
                print(format_sensor(data))
            except json.JSONDecodeError:
                if rad and not rad.startswith("{"):
                    print(f"   ℹ️  {rad}")

    except KeyboardInterrupt:
        print("\n\nAvbruten av användaren.")
        proc.terminate()
        return

    # Om vi kommer hit utan Ctrl+C avslutades rtl_433 av sig självt
    proc.wait()
    if received == 0:
        print("❌ rtl_433 avslutades utan att ta emot några paket.")
        if stderr_lines:
            print("   Felmeddelande från rtl_433:")
            for line in stderr_lines[-10:]:
                print(f"   {line}")
        print("\n   Tips: Kontrollera att dongeln är inkopplad och inte används av annat program.")
