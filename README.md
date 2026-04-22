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
| 10 | **🛰️ Meteor-M2-3** | 137.9 MHz | Vädersatellitbilder – PNG-filer på hårddisken (~1 km/pixel) |

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

Programmet använder två typer av beroenden:

| Typ | Verktyg | Används av |
|-----|---------|-----------|
| Systembibliotek | `librtlsdr` | Alla lägen (kommunikation med dongeln) |
| Externt program | `rtl_433` | Vädersensorer (läge 1) + IoT-avkodare (läge 9) |
| Python-paket | `pyrtlsdr` | Alla lägen med direkt IQ-läsning (2–9) |
| Python-paket | `pyModeS` | ADS-B flygplan (läge 2, 1090 MHz) |
| Python-paket | `pyais` | Fartyg AIS (läge 3, 162 MHz) |
| Python-paket | `numpy` | All signalbehandling |
| Python-paket | `sounddevice` | Röstmottagning och järnväg (ljud) |
| Python-paket | `ephem` | Passprediktion för Meteor-M2-3 (läge 10) |
| Externt program | `satdump` | Meteor-M2-3 LRPT-avkodning → PNG-bilder (läge 10) |

#### macOS
```bash
brew install librtlsdr rtl_433 satdump
```

#### Linux (Debian/Ubuntu x64)

`satdump` finns inte i standard apt-repot. Enklast på Ubuntu/Debian x64 är att hämta den färdiga `.deb`-filen från [SatDump releases](https://github.com/SatDump/SatDump/releases):

```bash
# Installera beroenden
sudo apt update
sudo apt install librtlsdr-dev rtl-433 python3-venv python3-pip \
  libfftw3-dev libvolk-dev libpng-dev libjpeg-dev libtiff-dev \
  libusb-1.0-0 portaudio19-dev

# Ladda ner och installera senaste SatDump-release (kontrollera versionsnummer på releases-sidan)
wget https://github.com/SatDump/SatDump/releases/download/1.2.2/satdump_1.2.2_amd64.deb
sudo dpkg -i satdump_1.2.2_amd64.deb
sudo apt --fix-broken install   # Löser eventuella beroendeproblem
```

#### Linux (Raspberry Pi / ARM / övriga)

Bygg satdump från källkod:

```bash
sudo apt update
sudo apt install librtlsdr-dev rtl-433 python3-venv python3-pip \
  cmake git build-essential libfftw3-dev libvolk-dev libpng-dev \
  libjpeg-dev libtiff-dev libusb-1.0-0-dev portaudio19-dev libnng-dev

git clone https://github.com/SatDump/SatDump.git
cd SatDump && mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DBUILD_GUI=OFF -DCMAKE_INSTALL_PREFIX=/usr ..
make -j$(nproc)    # Använd make -j1 på Raspberry Pi 3 eller äldre
sudo make install
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
5. Installera `satdump` via [SatDump releases](https://github.com/SatDump/SatDump/releases) – ladda ner och kör `.exe`-installationsfilen

---

### 3. Skapa virtuell miljö och installera Python-paket

> ⚠️ Använd alltid en virtuell miljö (venv) – undviker konflikter med systemets Python.

```bash
python3.13 -m venv .venv          # Skapa venv (använd python3 om python3.13 saknas)
source .venv/bin/activate         # macOS/Linux
# .venv\Scripts\activate          # Windows

pip install -r requirements.txt
```

`requirements.txt` innehåller:
```
pyrtlsdr>=0.3.0    # RTL-SDR-gränssnitt (ADS-B, AIS, ACARS, röst m.fl.)
pyModeS>=2.10      # ADS-B-avkodning (läge 2: flygplan 1090 MHz)
pyais>=3.0.0       # AIS-avkodning (läge 3: fartyg 162 MHz)
numpy>=1.24.0      # Signalbehandling
sounddevice>=0.4.0 # Ljuduppspelning (röst och järnväg)
ephem>=4.1         # Passprediktion för Meteor-M2-3 (läge 10)
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

### 🛰️ Meteor-M2-3 – satellitbilder i detalj

Meteor-M2-3 är en rysk vädersatellit i polär bana (~820 km höjd) som sänder bilder i realtid på **137.9 MHz** med LRPT-protokollet (digital QPSK). Programmet hämtar aktuell TLE-data från Celestrak, beräknar de kommande passagerna över din position och startar automatiskt SatDump vid rätt tidpunkt.

#### Passagekvalitet

Varje passage visas med ett kvalitetsmärke baserat på hur högt satelliten når över horisonten (maxelevation):

| Märke | Text | Maxelevation | Innebörd |
|-------|------|-------------|----------|
| 🔴 | **Låg** | < 20° | Knappt synlig – signalen passerar genom mycket atmosfär och blockas lätt av träd och hus. Bilden blir ofta brusig eller tom. |
| 🟡 | **OK** | 20–40° | Hyfsad passage – fungerar med fri sikt och bra antenn. |
| 🟢 | **Bra** | > 40° | Hög passage – kort signalväg, bäst förutsättningar för en komplett och tydlig bild. |

Sikta i första hand på ett **🟢 Bra**-pass. Meteor-M2-3 passerar Sverige 4–6 gånger per dygn och ett Bra-pass inträffar vanligen en eller ett par gånger om dagen.

#### Antenn

En **dipol för 137 MHz** (~54 cm per arm, vinklad till V-form, ~120° öppning) ger bäst mottagning. En turniket-antenn (fyra dipoler i cirkulär polarisation) är ännu bättre men svårare att bygga. Den lilla "piska"-antennen som medföljer dongeln fungerar dåligt för satellitmottagning.

#### Var hittar jag bilderna?

Bilderna sparas i `~/sdr_bilder/meteor/<tidsstämpel>/` som PNG-filer. Öppna dem direkt i Finder/bildvisaren. En fullständig passage med >40° elevation ger normalt en bildremsa på ~2 000 × 800 pixlar (~1 km/pixel).

### Meteor-M2-3: svart bild / inga bilder
- Antennen är det viktigaste – en 137 MHz dipol (~54 cm per arm) eller turniket-antenn krävs
- Välj ett pass med hög maxelevation (>30°) för bäst chans
- Kontrollera att dongeln inte används av annat program under passet
- SatDump behöver hitta ~50+ synkroniserade LRPT-frames – svaga signaler ger tomma filer

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
    ├── iot.py             # IoT-sniffning 868 MHz: LoRa/Z-Wave/M-Bus (rtl_433 + ren Python)
    └── satellite.py       # Meteor-M2-3 vädersatellitbilder 137.9 MHz  (ephem + satdump)
```

---

## Rekommenderade frekvenser att börja med

| Signal | Frekvens | Antenn (λ/4) | Tips |
|--------|----------|--------------|------|
| Flygplan ADS-B | 1090 MHz | ~6,9 cm | Fungerar nästan alltid nära en flygplats |
| Marin nöd | **156.800 MHz** (kanal 16) | ~48 cm | Alltid aktiv nära kusten |
| Flyg guard | **121.500 MHz** | ~62 cm | Alltid monitorerad, ibland testtrafik |
| ACARS | 129.125 MHz | ~58 cm | Tätast trafik nära en flygplats |
| POCSAG | 169.6375 MHz | ~44 cm | RAKEL – svenska blåljus |
| Vädersensor | 433.920 MHz | ~17 cm | Kräver att du har en kompatibel sensor |
| Järnväg nöd | **154.000 MHz** | ~49 cm | Alltid aktiv längs svenska järnvägar |
| LoRaWAN | **868.100 / 868.300 / 868.500 MHz** | ~8,6 cm | Primära EU-kanaler, trafik dygnet runt i städer |
| Z-Wave | **868.420 MHz** | ~8,6 cm | Aktiv i områden med hemautomation |
| Smarta mätare | **868.950 MHz** | ~8,6 cm | Wireless M-Bus, aktiv i bostadsområden |
| Meteor-M2-3 | **137.900 MHz** | ~54 cm (dipol, V-form) | Satellitbilder – fri sikt mot himlen krävs |

> **Tips:** Klipp en trådbit till rätt längd och anslut den till antenningången – en enkel kvartsvågs-monopol ger förvånansvärt bra mottagning för de flesta av dessa signaler.

---

## Licens

MIT – gör vad du vill med koden. Pull requests välkomna!
