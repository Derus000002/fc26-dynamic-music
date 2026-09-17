# FC26 Dynamic Music

Musica dinamica stile edit/TikTok per EA Sports FC 26 (PC, Windows): quando controlli
Mbappé parte la sua canzone, quando passi a Yamal fa fade-out e parte quella di Yamal.

## 1. Come rileva il giocatore (nessuna API inventata)

EA FC 26 **non espone API ufficiali** per "giocatore controllato", e leggere la memoria
del gioco (Frostbite + EA AntiCheat) rischierebbe ban + rotture a ogni patch.
Quindi il programma usa il metodo più affidabile e sicuro:

1. **Screenshot periodico** con `mss` (niente hook nel gioco).
2. **Computer vision (OpenCV)**: cerca il triangolino di selezione sopra il giocatore
   controllato e ritaglia la striscia dove FC mostra il **cognome al cambio** (~1-1.5 s).
3. **OCR** (`rapidocr` → `easyocr` → `tesseract`, in ordine automatico).
4. **Fuzzy match** con soglia di confidence + **N rilevamenti consecutivi** (anti-falsi-positivi).
5. **Sistema ibrido**: siccome il nome appare solo dopo lo switch, il programma
   - fa un **burst OCR di 2 s** quando premi il tasto cambio (default `Q`, configurabile),
   - offre **override manuale** (doppio click / "Forza riproduzione" / hotkey futuri).

Se il giocatore non è configurato: `unknown_behavior = "keep"` (mantieni musica) oppure `"stop"`.

## 2. Installazione (Windows)

Verifica la versione Python: `python --version`.

**Caso A — Python 3.11 o 3.12 (consigliato, tutto automatico):**
```powershell
cd FC26_DynamicMusic
pip install -r requirements.txt   # include rapidocr, il miglior OCR
python main.py
```

**Caso B — Python 3.13 o 3.14 (rapidocr non supportato su queste versioni):**
```powershell
cd FC26_DynamicMusic
pip install -r requirements.txt   # rapidocr viene saltato in automatico
```
poi installa Tesseract-OCR per Windows (installer UB-Mannheim, gratis):
https://github.com/UB-Mannheim/tesseract/wiki —
durante il setup spunta "Add to PATH" (se lo dimentichi, il programma cerca
comunque in `C:\Program Files\Tesseract-OCR\`). Nella GUI seleziona backend
OCR = `tesseract` prima di premere Start.
```powershell
python main.py
```

> `keyboard` per il burst sul tasto cambio: se dà problemi di permessi, il programma
> funziona comunque (senza burst).

## 3. Uso

1. Metti gli MP3 in `songs/` (oppure usa Sfoglia: li copia lui).
2. Avvia `python main.py` → Aggiungi giocatori (Nome → file, start_time, volume).
3. Porta FC 26 in primo piano (stesso monitor impostato, default 1).
4. Premi **🎯 Test Detection**: parte una sessione di N secondi (default 10, modificabile
   accanto al pulsante) con una scansione ogni 0.3 s — cambia giocatore in partita
   durante il countdown e leggi i risultati nel LOG. Il pulsante fa da stop.
5. Premi **▶ Start**. Passa da Mbappé a Yamal: fade-out + nuova traccia dallo start_time.
6. **DEBUG**: spunta la modalità debug → preview live + screenshot in `debug/`.

## 4. Config (`config.json`)

```json
{
  "players": {
    "Kylian Mbappe": {"song": "songs/Mbappe.mp3", "start_time": 0, "volume": 0.8}
  },
  "settings": {
    "fade_out": 0.5, "fade_in": 0.5,
    "confidence_threshold": 0.55,
    "detection_interval": 0.5,
    "consecutive_hits": 2,
    "unknown_behavior": "keep",
    "ocr_backend": "auto",
    "monitor_index": 1,
    "roi": null
  }
}
```

Consigli taratura:
- Troppi falsi positivi → alza `confidence_threshold` (0.65-0.75) e `consecutive_hits` a 3.
- Non rileva mai → abbassa soglia a 0.45, usa ROI fissa sulla zona nome, controlla backend OCR.
- OCR lento → aumenta `detection_interval` a 0.7-1.0, usa `rapidocr`.

## 5. File

- `main.py` — GUI + loop rilevamento
- `detector.py` — screenshot + tracking indicatore + OCR + fuzzy match
- `audio_manager.py` — play/fade/start_time/volumi (pygame)
- `config_manager.py` — load/save config + normalizzazione nomi
- `songs/` — le tue canzoni
- `debug/` — screenshot di debug
