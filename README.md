# SDR Mottagare

Ett Python-program för att lyssna på radiosignaler med en **RTL-SDR-dongel**.
Kör i terminalen och presenterar all information som text.

## Vad kan programmet ta emot?

| # | Läge | Frekvens | Vad du hör/ser |
|---|------|----------|----------------|
| 1 | **Vädersensorer** | 433 MHz | Temperatur, luftfuktighet, vind, regn från trådlösa sensorer |
| 2 | **Flygtrafik ADS-B** | 1090 MHz | Tabell med flygplan: position, höjd, hastighet, anropssignal |
| 3 | **Fartyg AIS** | 162 MHz | Tabell med fartyg: position, namn, fart, kurs |
| 4 | **ACARS** | 129–132 MHz | Textmeddelanden och positionsrapporter från flygplan |
| 5 | **POCSAG/FLEX** | 148–932 MHz | Personsökare: räddningstjänst, sjukhus |
| 6 | **Spektrum & Skanner** | Valfritt | Realtids-FFT, frekvensskanner, signalstyrkemätare |
| 7 | **Röst flyg/marin** | 118–400 MHz / 156–174 MHz | Lyssna på flygkontroll (AM) och båttrafik (FM) |
| 8 | **🚂 Järnväg** | 153–156 MHz | Analogt tågradio – Trafikverket, SJ, lokförare (FM) |
| 9 | **📡 IoT-sniffning** | 868 MHz | LoRa, Z-Wave, smarta mätare, larm, dörrklockor (3 lägen) |

---

## Hårdvarukrav

- En **RTL-SDR-dongel** (R820T/R828D-baserad rekommenderas, kostar ~150–300 kr)
- En antenn anpassad för aktuellt frekvensband (medföljer ofta dongeln)
- USB-port

---

## Installation

### 1. Klona repot

```bash
git clone https://github.com/faidros/sdrmottagare.git
cd sdrmottagare
```

### 2. Installera systemberoenden

#### macOS
```bash
brew install librtlsdr rtl_433
```

#### Linux (Debian/Ubuntu/Raspberry Pi)
```bash
sudo apt update
sudo apt install librtlsdr-dev rtl-433 python3-venv python3-pip
```

> **Linux-tips:** Om dongeln inte känns igen, lägg till en udev-regel:
> ```bash
> echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE="0666"' \
>   | sudo tee /etc/udev/rules.d/99-rtlsdr.rules
> sudo udevadm control --reload-rules
> ```
> Koppla ur och in dongeln efteråt.

#### Windows
1. Installera [Zadig](https://zadig.akeo.ie/) och byt drivrutin för RTL-SDR till **WinUSB**
2. Installera [Python 3.11+](https://www.python.org/downloads/)
3. Installera `librtlsdr` – enklast via [conda](https://conda.io):
   ```cmd
   conda install -c conda-forge librtlsdr
   ```
4. Installera `rtl_433` via [GitHub releases](https://github.com/merbanan/rtl_433/releases)

---

### 3. Skapa virtuell miljö och installera Python-paket

> ⚠️ Använd alltid en virtuell miljö (venv) – undviker konflikter med systemets Python.

```bash
python3.13 -m venv .venv          # Skapa venv (använd python3 om python3.13 saknas)
source .venv/bin/activate         # macOS/Linux
# .venv\Scripts\activate          # Windows

pip install -r requirements.txt
```

> **Obs macOS/Python 3.14+:** Om du får felmeddelande om `pkg_resources`, kör:
> ```bash
> pip install setuptools
> ```
> Om det inte hjälper, se till att använda Python 3.11–3.13 i din venv.

---

### 4. Starta programmet

```bash
source .venv/bin/activate   # Om inte redan aktiverad
python main.py
```

Eller utan att aktivera venv:
```bash
.venv/bin/python main.py
```

---

## Felsökning

### "Kunde inte öppna SDR-dongle"
- Kontrollera att dongeln är inkopplad
- macOS: `brew reinstall librtlsdr`
- Linux: se udev-regeln ovan
- Stäng andra program som kan använda dongeln (SDR#, GQRX, etc.)

### Inget ljud i röstläget
- Kontrollera datorns standardljudutgång
- Justera squelch-nivån lägre (t.ex. `-50` i stället för `-30`)
- Testa med en stark signal, t.ex. en lokal FM-station via marin-FM-läget

### Inga flygplan / fartyg syns
- Placera antennen nära ett fönster eller utomhus
- Prova att öka förstärkning: ändra `GAIN` i respektive `.py`-fil
- ADS-B kräver fri sikt mot himlen för bäst mottagning
- Använd **Spektrum & Skanner → Signalstyrkemätare** för att optimera antennplacering

### "PLL not locked"
- Normalt varningsmeddelande från tunern, inget fel
- Kan uppstå vid frekvenser nära gränserna för dongeln (~24 MHz–1,75 GHz)

### Inga järnvägssignaler hörs
- Tågradio är VHF FM med relativt låg effekt – du behöver vara nära en järnvägslinje
- Prova nödkanalen **154.000 MHz** först (mest aktiv)
- En vertikal antenn för ~150 MHz (~50 cm) ger bäst resultat

### Inga IoT-signaler på 868 MHz
- LoRaWAN-paket kan komma sällan (var 10:e sekund till var 15:e minut beroende på enhet)
- Använd **Burst-detektor**-läget – det visar alla signaler oavsett protokoll
- Z-Wave och smarta mätare är aktiva i bostadsområden, inte utomhus/landsbygd
- En kortare antenn (~8,6 cm för kvartsvåg vid 868 MHz) fungerar bättre än en 433 MHz-antenn

---

## Projektstruktur

```
sdrmottagare/
├── main.py                # Startar programmet och visar huvudmeny
├── requirements.txt       # Python-beroenden
├── README.md
└── modes/
    ├── weather.py         # Vädersensorer 433 MHz  (via rtl_433)
    ├── adsb.py            # Flygtrafik ADS-B 1090 MHz  (via pyModeS)
    ├── ais.py             # Fartyg AIS 162 MHz  (ren Python)
    ├── acars.py           # Flygdata ACARS 129–132 MHz  (ren Python)
    ├── paging.py          # POCSAG & FLEX personsökare  (ren Python)
    ├── scanner.py         # Spektrumanalysator & frekvensskanner
    ├── voice.py           # Röstmottagning flyg AM / marin FM
    ├── railway.py         # Analogt tågradio 153–156 MHz  (ren Python FM)
    └── iot.py             # IoT-sniffning 868 MHz: LoRa/Z-Wave/M-Bus (rtl_433 + ren Python)
```

---

## Rekommenderade frekvenser att börja med

| Signal | Frekvens | Tips |
|--------|----------|------|
| Flygplan | 1090 MHz | Fungerar nästan alltid nära en flygplats |
| Marin nöd | **156.800 MHz** (kanal 16) | Alltid aktiv nära kusten |
| Flyg guard | **121.500 MHz** | Alltid monitorerad, ibland testtrafik |
| ACARS | 129.125 MHz | Tätast trafik nära en flygplats |
| POCSAG | 169.6375 MHz | RAKEL – svenska blåljus |
| Vädersensor | 433.920 MHz | Kräver att du har en kompatibel sensor |
| Järnväg nöd | **154.000 MHz** | Alltid aktiv längs svenska järnvägar |
| LoRaWAN | **868.100 / 868.300 / 868.500 MHz** | Primära EU-kanaler, trafik dygnet runt i städer |
| Z-Wave | **868.420 MHz** | Aktiv i områden med hemautomation |
| Smarta mätare | **868.950 MHz** | Wireless M-Bus, aktiv i bostadsområden |

---

## Licens

MIT – gör vad du vill med koden. Pull requests välkomna!
- För 433 MHz räcker ofta en kort trådantenn (~17 cm för kvartsvåg)
- För ADS-B (1090 MHz) ger en dedikerad 1090 MHz-antenn bäst resultat
- Justera `gain` i `adsb.py` om du får dålig mottagning (prova 20–40 dB)
