"""Rilevamento giocatore controllato FC26: screenshot + CV indicatore + OCR + fuzzy match.

Strategia (ibrida, senza toccare i file/memoria del gioco):
  1. Screenshot con mss (monitor selezionato, oppure ROI fissa scelta dall'utente).
  2. Se attivo l'indicator-tracking: cerca il triangolino di selezione sopra il
     giocatore (contorni triangolari chiari) e fa OCR solo nella striscia sopra.
     Questo evita di scansionare tutto lo schermo ed è molto più robusto.
  3. OCR con backend disponibile: rapidocr_onnxruntime > easyocr > pytesseract.
  4. Fuzzy-match dei testi OCR contro i nomi configurati (gestione accenti,
     'MBAPPE' vs 'Kylian Mbappe', solo cognome, ecc.).
"""
from __future__ import annotations

import os
import time
import unicodedata

import numpy as np

# Lazy / opzionali: importati solo quando servono per non rompere la GUI.
_cv2 = None
_mss = None


def _cv2_lib():
    global _cv2
    if _cv2 is None:
        import cv2  # type: ignore
        _cv2 = cv2
    return _cv2


def _mss_lib():
    global _mss
    if _mss is None:
        import mss  # type: ignore
        _mss = mss
    return _mss


def normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = "".join(c if (c.isalnum() or c.isspace()) else " " for c in s)
    return " ".join(s.split())


def fuzzy_score(a: str, b: str) -> float:
    """0..1 tra due stringhe già normalizzate."""
    if not a or not b:
        return 0.0
    try:
        from rapidfuzz import fuzz  # type: ignore
        return float(fuzz.ratio(a, b)) / 100.0
    except Exception:
        import difflib
        return float(difflib.SequenceMatcher(None, a, b).ratio())


def match_player(ocr_texts: list[tuple[str, float]], players: dict, threshold: float = 0.55):
    """Ritorna (nome_display | None, confidence_combinata, testo_migliore).

    - ocr_texts: [(testo, conf_ocr 0..1)]
    - Per ogni testo prova match contro nome completo E solo cognome.
    - confidence = 0.45*conf_ocr + 0.55*fuzzy
    """
    best = (None, 0.0, "")
    if not ocr_texts or not players:
        return best
    norm_players = [(display, normalize(display), normalize(display).split()[-1]) for display in players]
    for text, ocr_conf in ocr_texts:
        nt = normalize(text)
        if len(nt) < 2:
            continue
        for display, full, surname in norm_players:
            s_full = fuzzy_score(nt, full)
            # match su cognome: contiene / contenuto / ratio
            s_sur = fuzzy_score(nt, surname)
            if surname and surname in nt.split():
                s_sur = max(s_sur, 0.97)
            s = max(s_full, s_sur)
            combined = 0.45 * float(ocr_conf or 0.5) + 0.55 * s
            if s >= threshold and combined > best[1]:
                best = (display, round(combined, 3), text)
            elif best[0] is None and combined > best[1]:
                # tiene traccia del miglior candidato anche sotto soglia (utile per debug)
                best = (None, round(combined, 3), text)
    # Applica soglia finale
    if best[0] is not None and best[1] < threshold:
        return (None, best[1], best[2])
    return best


# ---------------------------------------------------------------- OCR backends
_ocr_instances: dict = {}


def available_backends() -> list[str]:
    out = []
    try:
        import rapidocr_onnxruntime  # noqa
        out.append("rapidocr")
    except Exception:
        pass
    try:
        import easyocr  # noqa
        out.append("easyocr")
    except Exception:
        pass
    try:
        import pytesseract  # noqa
        out.append("tesseract")
    except Exception:
        pass
    return out


def _ocr_rapidocr(image_bgr, langs=("en",)):
    from rapidocr_onnxruntime import RapidOCR  # type: ignore
    key = "rapidocr"
    if key not in _ocr_instances:
        # Fondamentale per non laggare il gioco: di default onnxruntime usa
        # TUTTI i core. Lo cappiamo a 2 thread (parametri ufficiali verificati
        # su config.yaml: Global.intra/inter_op_num_threads -> Det/Cls/Rec).
        # use_cls=False: il classificatore di orientamento e' inutile per
        # scritte HUD orizzontali e fa solo perdere tempo/CPU.
        _ocr_instances[key] = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1,
                                       use_cls=False)
    engine = _ocr_instances[key]
    result, _ = engine(image_bgr)
    out = []
    if result:
        for item in result:
            try:
                # formato: [box, text, conf]
                _, text, conf = item
                if text and str(text).strip():
                    out.append((str(text).strip(), float(conf or 0.0)))
            except Exception:
                continue
    return out


def _ocr_easyocr(image_bgr, langs=("en",)):
    import easyocr  # type: ignore
    key = f"easyocr-{','.join(langs)}"
    if key not in _ocr_instances:
        _ocr_instances[key] = easyocr.Reader(list(langs), gpu=False, verbose=False)
    reader = _ocr_instances[key]
    cv2 = _cv2_lib()
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    res = reader.readtext(rgb)
    out = []
    for item in res:
        try:
            _, text, conf = item
            if text and str(text).strip():
                out.append((str(text).strip(), float(conf or 0.0)))
        except Exception:
            continue
    return out


def _ocr_tesseract(image_bgr, langs=("en",)):
    import shutil
    import pytesseract  # type: ignore
    # Su Windows l'installer UB-Mannheim finisce qui: configuralo in automatico
    # se l'utente non ha spuntato "Add to PATH" in installazione.
    if shutil.which("tesseract") is None:
        for cand in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                     r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
            if os.path.exists(cand):
                pytesseract.pytesseract.tesseract_cmd = cand
                break
    cv2 = _cv2_lib()
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    # upscale + threshold per nomi piccoli
    h, w = gray.shape[:2]
    gray = cv2.resize(gray, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
    gray = cv2.medianBlur(gray, 3)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    data = pytesseract.image_to_data(th, lang="eng", output_type=pytesseract.Output.DATAFRAME)
    out = []
    try:
        for _, row in data.iterrows():
            t = str(row.get("text", "") or "").strip()
            c = row.get("conf", -1)
            try:
                c = float(c) / 100.0
            except Exception:
                c = 0.0
            if len(t) >= 2 and c > 0:
                out.append((t, max(0.0, min(1.0, c))))
    except Exception:
        txt = pytesseract.image_to_string(th, lang="eng")
        for line in txt.splitlines():
            if len(line.strip()) >= 2:
                out.append((line.strip(), 0.5))
    return out


def run_ocr(image_bgr, backend: str = "auto", langs=("en",)):
    """image_bgr: numpy BGR. Ritorna lista [(text, conf)]."""
    order: list[str] = []
    if backend == "auto":
        order = ["rapidocr", "easyocr", "tesseract"]
    else:
        order = [backend]
    avail = set(available_backends())
    errors = []
    for b in order:
        if b not in avail:
            errors.append(f"{b} non installato")
            continue
        try:
            if b == "rapidocr":
                return _ocr_rapidocr(image_bgr, langs)
            if b == "easyocr":
                return _ocr_easyocr(image_bgr, langs)
            if b == "tesseract":
                return _ocr_tesseract(image_bgr, langs)
        except Exception as e:
            errors.append(f"{b}: {e}")
            continue
    raise RuntimeError("Nessun backend OCR utilizzabile. " + "; ".join(errors)
                       + ". Su Python 3.11/3.12: pip install rapidocr-onnxruntime. "
                         "Su Python 3.13/3.14: pip install pytesseract + installa "
                         "Tesseract-OCR per Windows (UB-Mannheim) e seleziona backend 'tesseract'.")


# ---------------------------------------------------------------- capture + CV
def capture(monitor_index: int = 1, roi: list | tuple | None = None):
    """Screenshot -> (frame_bgr, grab_dict). roi=[x,y,w,h] in coordinate schermo."""
    mss_lib = _mss_lib()
    with mss_lib.mss() as sct:
        mons = sct.monitors  # [0]=tutto, [1..]=monitor
        idx = max(1, min(int(monitor_index or 1), len(mons) - 1))
        mon = dict(mons[idx])
        if roi:
            x, y, w, h = [int(v) for v in roi]
            grab = {"left": mon["left"] + x, "top": mon["top"] + y,
                    "width": max(50, w), "height": max(30, h)}
        else:
            grab = mon
        shot = sct.grab(grab)
        frame = np.array(shot)[:, :, :3]  # BGRA -> BGR
        cv2 = _cv2_lib()
        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        return frame, grab


def list_monitors():
    """Ritorna [{index, left, top, width, height, primary}] con index da 1.

    ATTENZIONE: l'ordine segue la posizione fisica (da sinistra a destra),
    NON i numeri assegnati da Windows nelle Impostazioni schermo!"""
    mss_lib = _mss_lib()
    out = []
    with mss_lib.mss() as sct:
        for i, m in enumerate(sct.monitors[1:], start=1):
            out.append({"index": i, "left": int(m["left"]), "top": int(m["top"]),
                        "width": int(m["width"]), "height": int(m["height"]),
                        "primary": (m["left"] == 0 and m["top"] == 0)})
    return out


def preprocess_for_ocr(crop_bgr):
    """Upscale + contrasto per scritte piccole di FC26."""
    cv2 = _cv2_lib()
    h, w = crop_bgr.shape[:2]
    scale = 2.0 if max(h, w) < 600 else 1.5
    crop = cv2.resize(crop_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.5).apply(l)
    crop = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
    return crop


def find_indicator(frame_bgr):
    """Cerca il triangolo di selezione del giocatore controllato.

    Ritorna (x, y, annotated) — coordinate del centro indicatore o (None,None,annotated).
    Euristica: contorni triangolari piccoli e molto luminosi (bianco/rosso HUD).
    """
    cv2 = _cv2_lib()
    annotated = frame_bgr.copy()
    h, w = frame_bgr.shape[:2]
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    # bianco luminoso + rosso HUD (due range)
    mask_w = cv2.inRange(hsv, np.array([0, 0, 200]), np.array([180, 40, 255]))
    mask_r1 = cv2.inRange(hsv, np.array([0, 120, 120]), np.array([10, 255, 255]))
    mask_r2 = cv2.inRange(hsv, np.array([165, 120, 120]), np.array([180, 255, 255]))
    mask = cv2.bitwise_or(mask_w, cv2.bitwise_or(mask_r1, mask_r2))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    best_score = 0.0
    area_frame = float(w * h)
    for c in contours:
        area = cv2.contourArea(c)
        if area < 25 or area > area_frame * 0.002:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.04 * peri, True)
        if len(approx) not in (3, 4):
            continue
        x, y, cw, ch = cv2.boundingRect(approx)
        aspect = cw / max(1.0, ch)
        if not (0.5 < aspect < 2.5):
            continue
        # preferisci forme in alto-centro e di dimensione plausibile
        score = area * (1.5 if len(approx) == 3 else 1.0)
        if score > best_score:
            best_score = score
            best = (x + cw // 2, y + ch // 2, (x, y, cw, ch))
    if best:
        cx, cy, (x, y, cw, ch) = best
        cv2.rectangle(annotated, (x, y), (x + cw, y + ch), (0, 255, 0), 2)
        cv2.circle(annotated, (cx, cy), 4, (0, 255, 0), -1)
        return cx, cy, annotated
    return None, None, annotated


def name_roi_from_indicator(frame, cx: int, cy: int, w: int = 320, h: int = 70, y_offset: int = 12):
    """Ritaglio sopra l'indicatore dove FC mostra il cognome al cambio giocatore."""
    H, W = frame.shape[:2]
    x1 = max(0, cx - w // 2)
    y1 = max(0, cy - h - y_offset)
    x2 = min(W, x1 + w)
    y2 = min(H, y1 + h)
    return frame[y1:y2, x1:x2], (x1, y1, x2 - x1, y2 - y1)


def detect_once(players: dict, settings: dict):
    """Un singolo ciclo: capture -> (smart skip?) -> (tracking?) -> OCR -> match.

    Ritorna dict {player, confidence, raw, annotated_path/crop info, ms}.
    Non lancia eccezioni per librerie mancanti: le riporta in 'error'.
    """
    t0 = time.time()
    try:
        backend = settings.get("ocr_backend", "auto") or "auto"
        langs = tuple([settings.get("ocr_language", "en") or "en"])
        roi = settings.get("roi")
        mon = int(settings.get("monitor_index", 1) or 1)
        frame, _ = capture(mon, roi)
    except Exception as e:
        return {"player": None, "confidence": 0.0, "raw": [], "error": f"Screenshot fallito: {e}. pip install mss opencv-python", "ms": 0}

    # --- smart skip: frame (quasi) identico al precedente -> riusa senza OCR.
    # Con ROI fissa sul riquadro nome l'HUD e' statico per lunghi tratti:
    # si risparmia quasi tutta la CPU e la reazione al cambio resta immediata
    # (al primo frame diverso parte subito l'OCR).
    sig = None
    skey = None
    if settings.get("smart_skip", True):
        try:
            skey = (tuple(sorted(players.keys())),
                    float(settings.get("confidence_threshold", 0.55) or 0.55),
                    str(backend), str(roi), mon)
            sig = _frame_sig(frame)
            c = _skip_cache
            if c["sig"] is not None and c["key"] == skey and c["res"] is not None:
                if float(np.mean(np.abs(sig - c["sig"]))) < 2.0:
                    res = dict(c["res"])
                    res["reused"] = True
                    res["ms"] = 0
                    return res
        except Exception:
            sig = None

    annotated = frame.copy()
    crops = []
    try:
        if settings.get("use_indicator_tracking", True) and not roi:
            cx, cy, annotated = find_indicator(frame)
            if cx is not None:
                crop, (x1, y1, cw, ch) = name_roi_from_indicator(frame, cx, cy)
                crops.append(("indicator", crop))
                cv2 = _cv2_lib()
                cv2.rectangle(annotated, (x1, y1), (x1 + cw, y1 + ch), (255, 0, 0), 2)
            else:
                # nessun indicatore visibile (nome mostrato solo ~1.5s dopo lo switch):
                # scansiona una fascia centrale per non sprecare OCR su tutto lo schermo
                H, W = frame.shape[:2]
                crops.append(("center-band", frame[int(H * 0.15):int(H * 0.85), :]))
        else:
            crops.append(("roi", frame))
    except Exception as e:
        return {"player": None, "confidence": 0.0, "raw": [], "error": f"CV fallita: {e}", "ms": 0}

    all_texts: list[tuple[str, float]] = []
    try:
        for _, crop in crops:
            if crop.size == 0:
                continue
            proc = preprocess_for_ocr(crop)
            all_texts.extend(run_ocr(proc, backend=backend, langs=langs))
    except Exception as e:
        return {"player": None, "confidence": 0.0, "raw": [],
                "error": str(e), "annotated": annotated, "ms": int((time.time() - t0) * 1000)}

    threshold = float(settings.get("confidence_threshold", 0.55) or 0.55)
    player, conf, top = match_player(all_texts, players, threshold)
    res = {"player": player, "confidence": float(conf or 0.0), "raw": all_texts[:12],
           "top_text": top, "annotated": annotated, "ms": int((time.time() - t0) * 1000), "error": None}
    if sig is not None:
        try:
            stored = dict(res)
            stored.pop("annotated", None)  # non tenere frame vecchi in memoria
            _skip_cache.update({"key": skey, "sig": sig, "res": stored})
        except Exception:
            pass
    return res


# ---------------------------------------------------------------- smart skip
_skip_cache: dict = {"key": None, "sig": None, "res": None}


def _frame_sig(frame):
    """Firma leggera del frame (64x64 grayscale) per confrontare due scatti."""
    cv2 = _cv2_lib()
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(g, (64, 64), interpolation=cv2.INTER_AREA)
    return small.astype(np.float32)
