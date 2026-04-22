# SDR Mottagare

Ett enkelt Python-program för att använda en RTL-SDR-dongel för att lyssna på:
- **433 MHz** – Vädersensorer (Oregon Scientific, Bresser, Nexus m.fl.)
- **1090 MHz** – Flygtrafik via ADS-B

## Krav

### Systemberoenden

**macOS:**
```bash
brew install librtlsdr rtl_433
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt install librtlsdr-dev rtl-433
```

### Python-paket
```bash
pip install -r requirements.txt
```

> För ADS-B med inbyggd RtlReader:
> ```bash
> pip install "pyModeS[rtl]"
> ```

## Starta

```bash
python main.py
```

## Struktur

```
sdr/
├── main.py              # Huvudmeny
├── requirements.txt
└── modes/
    ├── weather.py       # 433 MHz – vädersensorer via rtl_433
    └── adsb.py          # 1090 MHz – flygtrafik via pyModeS
```

## Tips

- Placera antennen nära ett fönster för bäst mottagning
- För 433 MHz räcker ofta en kort trådantenn (~17 cm för kvartsvåg)
- För ADS-B (1090 MHz) ger en dedikerad 1090 MHz-antenn bäst resultat
- Justera `gain` i `adsb.py` om du får dålig mottagning (prova 20–40 dB)
