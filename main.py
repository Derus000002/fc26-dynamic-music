"""FC26 Dynamic Music — GUI Windows (tkinter) + loop di rilevamento ibrido."""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SONGS_DIR = os.path.join(BASE_DIR, "songs")
DEBUG_DIR = os.path.join(BASE_DIR, "debug")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
os.makedirs(SONGS_DIR, exist_ok=True)
os.makedirs(DEBUG_DIR, exist_ok=True)

from config_manager import ConfigManager, AUDIO_EXTS  # noqa: E402
from audio_manager import AudioManager  # noqa: E402

# DPI awareness (Windows): con ridimensionamento schermo a 125%/150% le coordinate
# del selettore ROI di tkinter devono coincidere con i pixel reali usati da mss.
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# Prestazioni: limita i thread delle librerie numeriche (onnxruntime/numpy) e
# abbassa la priorita' di questo processo cosi' FC26 tiene la CPU per il gioco.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
try:
    import ctypes as _ct
    _ct.windll.kernel32.SetPriorityClass(_ct.windll.kernel32.GetCurrentProcess(), 0x00004000)
    # Affinita' CPU: confina questo processo su pochi core (gli ultimi, che su
    # Intel ibridi sono gli E-core efficienti) cosi' FC26 ha il resto libero.
    # Senza questo, onnxruntime e screenshot possono rubare CPU al gioco.
    try:
        ncpu = os.cpu_count() or 4
        k = 2 if ncpu >= 6 else 1
        cpus = list(range(ncpu - k, ncpu))
        mask = sum(1 << c for c in cpus)
        _ct.windll.kernel32.SetProcessAffinityMask(_ct.windll.kernel32.GetCurrentProcess(), mask)
        print(f"[cpu] Affinita' limitata ai core {cpus} su {ncpu} (anti-lag gioco)")
    except Exception as e:
        print(f"[cpu] Affinita' non impostata: {e}")
except Exception:
    pass


# ============================================================ ROI selector
class ROISelector(tk.Toplevel):
    """Overlay fullscreen per trascinare il rettangolo ROI."""

    def __init__(self, parent, on_done):
        super().__init__(parent)
        self.on_done = on_done
        self.attributes("-fullscreen", True)
        self.attributes("-alpha", 0.35)
        self.configure(bg="black")
        self.attributes("-topmost", True)
        self.start = None
        self.rect_id = None
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0, cursor="cross")
        self.canvas.pack(fill="both", expand=True)
        self.label = tk.Label(self, text="Trascina per selezionare l'area (ESC per annullare)",
                              bg="yellow", fg="black", font=("Segoe UI", 12, "bold"))
        self.label.place(relx=0.5, rely=0.02, anchor="n")
        self.canvas.bind("<ButtonPress-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _down(self, e):
        self.start = (e.x, e.y)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

    def _drag(self, e):
        if self.start and self.rect_id:
            self.canvas.coords(self.rect_id, self.start[0], self.start[1], e.x, e.y)

    def _up(self, e):
        if not self.start:
            self.destroy()
            return
        x1, y1 = self.start
        x, y = min(x1, e.x), min(y1, e.y)
        w, h = abs(e.x - x1), abs(e.y - y1)
        self.destroy()
        if w > 40 and h > 25:
            # coordinate relative al monitor primario (mss le offsetta col monitor_index)
            self.on_done([int(x), int(y), int(w), int(h)])


# ============================================================ App
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FC26 Dynamic Music")
        self.geometry("980x720")
        self.minsize(880, 640)
        self.cfg = ConfigManager(CONFIG_PATH)
        self.audio = AudioManager(SONGS_DIR)
        self.running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ui_queue: queue.Queue = queue.Queue()
        self._last_candidate: str | None = None
        self._hits = 0
        self._confirmed: str | None = None
        self._burst_until = 0.0
        self._preview_img = None
        self._test_thread: threading.Thread | None = None
        self._test_stop_event = threading.Event()
        self._roi_counting = False
        self._monitors: list = []

        self._build_style()
        self._build_ui()
        self._refresh_player_list()
        self._update_status_labels()
        self.after(120, self._pump_queue)
        self._install_switch_listener()

    # ---------------- stile ----------------
    def _build_style(self):
        self.configure(bg="#14161c")
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background="#14161c", foreground="#e8eaf0", font=("Segoe UI", 10))
        st.configure("TFrame", background="#14161c")
        st.configure("Card.TFrame", background="#1d2029")
        st.configure("TLabel", background="#14161c", foreground="#e8eaf0")
        st.configure("Card.TLabel", background="#1d2029", foreground="#e8eaf0")
        st.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        st.configure("Big.TLabel", font=("Segoe UI", 13, "bold"))
        # Pulsanti standard: sfondo scuro + testo chiaro (il default clam e'
        # grigio chiaro e col foreground chiaro diventava bianco-su-bianco).
        st.configure("TButton", padding=8, font=("Segoe UI", 10),
                     background="#2a2e3a", foreground="#ffffff",
                     bordercolor="#3a3f52", darkcolor="#2a2e3a", lightcolor="#2a2e3a")
        st.map("TButton", background=[("active", "#3a3f52"), ("pressed", "#23262f")],
               foreground=[("disabled", "#7a7f8f")])
        st.configure("Accent.TButton", background="#2f7bff", foreground="white")
        st.map("Accent.TButton", background=[("active", "#3f8bff")])
        # Campi di testo: sfondo scuro + testo chiaro + cursore visibile.
        st.configure("TEntry", fieldbackground="#0e1014", foreground="#ffffff",
                     insertcolor="#ffffff", bordercolor="#3a3f52")
        st.configure("TCombobox", fieldbackground="#0e1014", background="#2a2e3a",
                     foreground="#ffffff", arrowcolor="#ffffff", bordercolor="#3a3f52")
        st.map("TCombobox", fieldbackground=[("readonly", "#0e1014")],
               foreground=[("readonly", "#ffffff")],
               background=[("readonly", "#2a2e3a")])
        st.configure("TSpinbox", fieldbackground="#0e1014", background="#2a2e3a",
                     foreground="#ffffff", arrowcolor="#ffffff", bordercolor="#3a3f52")
        st.configure("TCheckbutton", background="#14161c", foreground="#e8eaf0")
        st.map("TCheckbutton", background=[("active", "#14161c")])
        st.configure("TLabelframe", background="#14161c", foreground="#e8eaf0",
                     bordercolor="#3a3f52")
        st.configure("TLabelframe.Label", background="#14161c", foreground="#9fb4ff")
        st.configure("Treeview", background="#0e1014", fieldbackground="#0e1014",
                     foreground="#ffffff", rowheight=24, bordercolor="#3a3f52")
        st.configure("Treeview.Heading", background="#2a2e3a", foreground="#ffffff")
        st.map("Treeview", background=[("selected", "#2f7bff")],
               foreground=[("selected", "#ffffff")])
        # Menu a tendina dei Combobox (e' un Listbox classico): tema scuro.
        self.option_add("*TCombobox*Listbox.background", "#0e1014")
        self.option_add("*TCombobox*Listbox.foreground", "#ffffff")
        self.option_add("*TCombobox*Listbox.selectBackground", "#2f7bff")
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    # ---------------- UI ----------------
    def _build_ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        # header
        head = ttk.Frame(root)
        head.pack(fill="x")
        ttk.Label(head, text="⚽ FC26 Dynamic Music", style="Title.TLabel").pack(side="left")
        self.status_var = tk.StringVar(value="● OFF")
        self.status_lbl = ttk.Label(head, textvariable=self.status_var, font=("Segoe UI", 12, "bold"))
        self.status_lbl.pack(side="right")

        cols = ttk.Frame(root)
        cols.pack(fill="both", expand=True, pady=10)
        left = ttk.Frame(cols, style="Card.TFrame", padding=12)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        right = ttk.Frame(cols, style="Card.TFrame", padding=12)
        right.pack(side="right", fill="both", expand=True, padx=(6, 0))

        # ---- sinistra: stato + controlli ----
        ttk.Label(left, text="STATO", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.det_var = tk.StringVar(value="Giocatore rilevato: —")
        self.conf_var = tk.StringVar(value="Confidence OCR: —")
        self.song_var = tk.StringVar(value="Canzone: —")
        self.audio_state_var = tk.StringVar(value="Audio: pronto")
        for v in (self.det_var, self.conf_var, self.song_var, self.audio_state_var):
            ttk.Label(left, textvariable=v, style="Card.TLabel").pack(anchor="w", pady=1)

        vol_row = ttk.Frame(left, style="Card.TFrame")
        vol_row.pack(fill="x", pady=6)
        ttk.Label(vol_row, text="Volume master:", style="Card.TLabel").pack(side="left")
        self.master_vol = tk.DoubleVar(value=float(self.cfg.get_setting("master_volume", 0.8)))
        tk.Scale(vol_row, from_=0, to=1, resolution=0.05, orient="horizontal",
                 variable=self.master_vol, bg="#1d2029", fg="white",
                 highlightthickness=0, command=lambda *_: self._on_master_vol()).pack(side="left", fill="x", expand=True)

        btns = ttk.Frame(left, style="Card.TFrame")
        btns.pack(fill="x", pady=4)
        self.start_btn = ttk.Button(btns, text="▶ Start", style="Accent.TButton", command=self.toggle)
        self.start_btn.pack(side="left", padx=2)
        self.test_btn = ttk.Button(btns, text="🎯 Test Detection", command=self.test_detection)
        self.test_btn.pack(side="left", padx=2)
        ttk.Button(btns, text="📁 Apri cartella canzoni", command=self.open_songs).pack(side="left", padx=2)
        ttk.Button(btns, text="⏸ Pausa/Riprendi", command=self._pause_resume).pack(side="left", padx=2)

        testrow = ttk.Frame(left, style="Card.TFrame")
        testrow.pack(fill="x", pady=2)
        ttk.Label(testrow, text="Durata test (s):", style="Card.TLabel").pack(side="left")
        self.var_test_secs = tk.IntVar(value=10)
        ttk.Spinbox(testrow, textvariable=self.var_test_secs,
                    from_=3, to=60, increment=1, width=5).pack(side="left", padx=4)
        self.test_count_var = tk.StringVar(value="")
        ttk.Label(testrow, textvariable=self.test_count_var, style="Card.TLabel").pack(side="left")

        # impostazioni rapide
        setf = ttk.LabelFrame(left, text="Impostazioni rilevamento", padding=8)
        setf.pack(fill="x", pady=8)
        self.var_threshold = tk.DoubleVar(value=float(self.cfg.get_setting("confidence_threshold", 0.55)))
        self.var_interval = tk.DoubleVar(value=float(self.cfg.get_setting("detection_interval", 0.5)))
        self.var_consec = tk.IntVar(value=int(self.cfg.get_setting("consecutive_hits", 2)))
        self.var_fadein = tk.DoubleVar(value=float(self.cfg.get_setting("fade_in", 0.5)))
        self.var_fadeout = tk.DoubleVar(value=float(self.cfg.get_setting("fade_out", 0.5)))
        self.var_unknown = tk.StringVar(value=str(self.cfg.get_setting("unknown_behavior", "keep")))
        self.var_debug = tk.BooleanVar(value=bool(self.cfg.get_setting("debug", False)))
        self.var_backend = tk.StringVar(value=str(self.cfg.get_setting("ocr_backend", "auto")))
        self.var_monitor = tk.IntVar(value=int(self.cfg.get_setting("monitor_index", 1)))

        def slider(parent, label, var, a, b, res):
            r = ttk.Frame(parent)
            r.pack(fill="x")
            ttk.Label(r, text=label, width=22).pack(side="left")
            tk.Scale(r, from_=a, to=b, resolution=res, orient="horizontal",
                     variable=var, length=220).pack(side="left")
            ttk.Label(r, textvariable=var, width=6).pack(side="left")

        slider(setf, "Confidence threshold", self.var_threshold, 0.2, 0.95, 0.05)
        slider(setf, "Intervallo (s)", self.var_interval, 0.2, 2.0, 0.1)
        slider(setf, "Conferme consecutive", self.var_consec, 1, 5, 1)
        slider(setf, "Fade-in (s)", self.var_fadein, 0, 3, 0.1)
        slider(setf, "Fade-out (s)", self.var_fadeout, 0, 3, 0.1)

        r2 = ttk.Frame(setf)
        r2.pack(fill="x", pady=4)
        ttk.Label(r2, text="Se sconosciuto:").pack(side="left")
        ttk.Combobox(r2, textvariable=self.var_unknown, values=["keep", "stop"],
                     width=8, state="readonly").pack(side="left", padx=4)
        ttk.Label(r2, text="OCR:").pack(side="left", padx=(10, 0))
        ttk.Combobox(r2, textvariable=self.var_backend,
                     values=["auto", "rapidocr", "easyocr", "tesseract"],
                     width=10, state="readonly").pack(side="left", padx=4)
        ttk.Label(r2, text="Monitor:").pack(side="left", padx=(10, 0))
        self.mon_combo = ttk.Combobox(r2, textvariable=self.var_monitor, values=[1, 2, 3],
                                      width=4, state="readonly")
        self.mon_combo.pack(side="left", padx=4)
        self.mon_combo.bind("<<ComboboxSelected>>", self._update_mon_info)

        r2b = ttk.Frame(setf)
        r2b.pack(fill="x", pady=2)
        self.mon_info_var = tk.StringVar(value="Monitor: …")
        ttk.Label(r2b, textvariable=self.mon_info_var).pack(side="left")
        ttk.Button(r2b, text="📷 Prova monitor", command=self._snapshot_monitor).pack(side="left", padx=8)

        r3 = ttk.Frame(setf)
        r3.pack(fill="x", pady=4)
        ttk.Checkbutton(r3, text="Modalità DEBUG (preview + screenshot)", variable=self.var_debug,
                        command=self._save_settings).pack(side="left")
        ttk.Button(r3, text="Seleziona area…", command=self._select_roi).pack(side="left", padx=8)
        ttk.Button(r3, text="📸 Area da screenshot (5s)", command=self._select_roi_delayed).pack(side="left", padx=2)
        ttk.Button(r3, text="Reset area (schermo intero)", command=self._reset_roi).pack(side="left")
        self.roi_var = tk.StringVar(value=self._roi_text())
        ttk.Label(setf, textvariable=self.roi_var).pack(anchor="w")
        ttk.Button(setf, text="💾 Salva impostazioni", command=self._save_settings).pack(anchor="w", pady=4)

        # ---- destra: giocatori ----
        ttk.Label(right, text="GIOCATORI CONFIGURATI", style="Card.TLabel",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.tree = ttk.Treeview(right, columns=("song", "start", "vol"), height=10, selectmode="browse")
        self.tree.heading("#0", text="Giocatore")
        self.tree.heading("song", text="File")
        self.tree.heading("start", text="Start(s)")
        self.tree.heading("vol", text="Vol")
        self.tree.column("#0", width=150)
        self.tree.column("song", width=170)
        self.tree.column("start", width=60, anchor="center")
        self.tree.column("vol", width=50, anchor="center")
        self.tree.pack(fill="both", expand=True, pady=6)

        pe = ttk.Frame(right, style="Card.TFrame")
        pe.pack(fill="x")
        ttk.Button(pe, text="➕ Aggiungi", command=self._add_player).pack(side="left", padx=2)
        ttk.Button(pe, text="✏️ Modifica", command=self._edit_player).pack(side="left", padx=2)
        ttk.Button(pe, text="🗑 Rimuovi", command=self._del_player).pack(side="left", padx=2)
        ttk.Button(pe, text="▶ Forza riproduzione", command=self._force_selected).pack(side="left", padx=2)

        # ---- debug/log in basso ----
        bot = ttk.Frame(root, style="Card.TFrame", padding=8)
        bot.pack(fill="both", expand=False)
        ttk.Label(bot, text="LOG / DEBUG", style="Card.TLabel", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        pan = ttk.Frame(bot, style="Card.TFrame")
        pan.pack(fill="both", expand=True)
        self.log = tk.Text(pan, height=7, bg="#0e1014", fg="#c8ffb0", font=("Consolas", 9))
        self.log.pack(side="left", fill="both", expand=True)
        self.preview_lbl = ttk.Label(pan, text="preview", style="Card.TLabel", width=28, anchor="center")
        self.preview_lbl.pack(side="right", padx=8)

        self._refresh_monitor_list()
        if not self.audio.available:
            self._log(f"⚠ Audio non disponibile: {self.audio.error} (pip install pygame)")
        self._log("Pronto. Premi 'Test Detection' con FC26 in primo piano per tarare.")

    # ---------------- helpers UI ----------------
    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.insert("end", f"[{ts}] {msg}\n")
        self.log.see("end")

    def _roi_text(self):
        roi = self.cfg.get_setting("roi")
        return f"Area: schermo intero (tracking indicatore)" if not roi else f"Area: x={roi[0]} y={roi[1]} w={roi[2]} h={roi[3]}"

    def _refresh_player_list(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for name, e in sorted(self.cfg.players.items()):
            self.tree.insert("", "end", text=name,
                             values=(os.path.basename(str(e.get("song", ""))),
                                     e.get("start_time", 0), e.get("volume", 0.8)))

    def _update_status_labels(self):
        self.status_var.set("● ON (rilevamento attivo)" if self.running else "● OFF")
        self.status_lbl.configure(foreground="#4dff88" if self.running else "#ff5d5d")
        self.start_btn.configure(text="⏹ Stop" if self.running else "▶ Start")

    def _pump_queue(self):
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "detect":
                    self.det_var.set(f"Giocatore rilevato: {payload.get('player') or '—'}")
                    self.conf_var.set(f"Confidence OCR: {payload.get('confidence', 0):.2f}"
                                      + (f"  (raw: {payload.get('top_text', '')})" if payload.get("top_text") else ""))
                elif kind == "song":
                    self.song_var.set(f"Canzone: {payload}")
                    self.audio_state_var.set(f"Audio: {payload}")
                elif kind == "log":
                    self._log(payload)
                elif kind == "preview":
                    self._show_preview(payload)
                elif kind == "test_btn":
                    self.test_btn.configure(text=payload)
                elif kind == "test_count":
                    self.test_count_var.set(payload)
                elif kind == "roi_image":
                    self._open_roi_from_image(payload)
        except queue.Empty:
            pass
        self.after(120, self._pump_queue)

    def _show_preview(self, annotated_bgr):
        try:
            from PIL import Image, ImageTk
            import cv2
            rgb = cv2.cvtColor(annotated_bgr, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            img.thumbnail((260, 150))
            self._preview_img = ImageTk.PhotoImage(img)
            self.preview_lbl.configure(image=self._preview_img, text="")
        except Exception:
            pass

    # ---------------- impostazioni ----------------
    def _save_settings(self):
        s = self.cfg.settings
        s.update({
            "confidence_threshold": float(self.var_threshold.get()),
            "detection_interval": float(self.var_interval.get()),
            "consecutive_hits": int(self.var_consec.get()),
            "fade_in": float(self.var_fadein.get()),
            "fade_out": float(self.var_fadeout.get()),
            "unknown_behavior": str(self.var_unknown.get()),
            "ocr_backend": str(self.var_backend.get()),
            "monitor_index": int(self.var_monitor.get()),
            "master_volume": float(self.master_vol.get()),
            "debug": bool(self.var_debug.get()),
        })
        self.cfg.save()
        self._log("Impostazioni salvate.")
        self.roi_var.set(self._roi_text())

    def _on_master_vol(self):
        self.cfg.set_setting("master_volume", float(self.master_vol.get()))

    def _select_roi(self):
        ROISelector(self, self._on_roi)

    def _select_roi_delayed(self, delay: int = 5):
        """Per chi non riesce a tenere FC aperto sotto l'overlay (Alt+Tab riduce
        il gioco): fa uno screenshot tra N secondi e apre la selezione su quella
        foto statica. Premi, torna in partita, poi disegna il rettangolo."""
        if self._roi_counting:
            return
        self._roi_counting = True
        threading.Thread(target=self._roi_delayed_worker, args=(delay,), daemon=True).start()

    def _roi_delayed_worker(self, delay: int):
        try:
            for i in range(delay, 0, -1):
                self._ui_queue.put(("log", f"📸 Screenshot tra {i}s — torna in partita su FC26!"))
                time.sleep(1)
            import detector
            frame, _grab = detector.capture(int(self.cfg.get_setting("monitor_index", 1) or 1), None)
            self._ui_queue.put(("roi_image", frame))
        except Exception as e:
            self._ui_queue.put(("log", f"Screenshot fallito: {e}"))
        finally:
            self._roi_counting = False

    def _open_roi_from_image(self, frame_bgr):
        """Selezione ROI su screenshot statico. Coordinate riconvertite 1:1."""
        try:
            from PIL import Image, ImageTk
        except Exception as e:
            self._log(f"Pillow mancante: {e}")
            return
        rgb = frame_bgr[:, :, ::-1].copy()
        img = Image.fromarray(rgb)
        ow, oh = img.size
        scale = min(1.0, 1400 / ow, 800 / oh)
        dw, dh = max(1, int(ow * scale)), max(1, int(oh * scale))
        win = tk.Toplevel(self)
        win.title("Trascina il rettangolo sul nome (ESC annulla)")
        win.attributes("-topmost", True)
        canvas = tk.Canvas(win, width=dw, height=dh, highlightthickness=0, cursor="cross")
        canvas.pack()
        tkim = ImageTk.PhotoImage(img.resize((dw, dh)))
        canvas.create_image(0, 0, anchor="nw", image=tkim)
        canvas.image = tkim  # evita garbage collection
        state = {"start": None, "rect": None}

        def down(e):
            state["start"] = (e.x, e.y)
            if state["rect"]:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

        def drag(e):
            if state["start"] and state["rect"]:
                canvas.coords(state["rect"], state["start"][0], state["start"][1], e.x, e.y)

        def up(e):
            if not state["start"]:
                return
            x1, y1 = state["start"]
            x, y = min(x1, e.x), min(y1, e.y)
            w, h = abs(e.x - x1), abs(e.y - y1)
            win.destroy()
            if w > 20 and h > 12:
                self._on_roi([int(x / scale), int(y / scale),
                              max(10, int(w / scale)), max(8, int(h / scale))])
            else:
                self._log("Selezione troppo piccola, annullata.")

        canvas.bind("<ButtonPress-1>", down)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", up)
        win.bind("<Escape>", lambda e: win.destroy())

    def _on_roi(self, roi):
        self.cfg.set_setting("roi", roi)
        self.roi_var.set(self._roi_text())
        self._log(f"ROI impostata: {roi}")

    def _reset_roi(self):
        self.cfg.set_setting("roi", None)
        self.roi_var.set(self._roi_text())
        self._log("ROI resettata a schermo intero.")

    # ---------------- monitor ----------------
    def _refresh_monitor_list(self):
        try:
            import detector
            mons = detector.list_monitors()
            if mons:
                self._monitors = mons
                self.mon_combo.configure(values=[m["index"] for m in mons])
                if int(self.var_monitor.get()) not in [m["index"] for m in mons]:
                    self.var_monitor.set(mons[0]["index"])
        except Exception as e:
            self._log(f"Lista monitor non disponibile: {e}")
        self._update_mon_info()

    def _update_mon_info(self, *_):
        try:
            idx = int(self.var_monitor.get())
        except Exception:
            idx = 1
        info = next((m for m in self._monitors if m["index"] == idx), None)
        if info:
            tag = "principale" if info["primary"] else "secondario"
            self.mon_info_var.set(
                f"Monitor {idx}: {info['width']}x{info['height']} "
                f"({tag}). N.B.: l'ordine segue la posizione fisica, NON i numeri di Windows!")
        else:
            self.mon_info_var.set(f"Monitor {idx}")
        try:
            self.cfg.set_setting("monitor_index", idx)  # salvataggio silenzioso
        except Exception:
            pass

    def _snapshot_monitor(self):
        """Foto di prova del monitor selezionato: mostra cosa inquadra davvero."""
        threading.Thread(target=self._snapshot_worker, daemon=True).start()

    def _snapshot_worker(self):
        try:
            import detector
            idx = int(self.var_monitor.get() or 1)
            frame, _grab = detector.capture(idx, None)
            h, w = frame.shape[:2]
            self._ui_queue.put(("preview", frame))
            try:
                import cv2
                p = os.path.join(DEBUG_DIR, f"monitor{idx}_{datetime.now():%Y%m%d_%H%M%S}.png")
                cv2.imwrite(p, frame)
                self._ui_queue.put(("log", f"📷 Monitor {idx} = {w}x{h}, foto: {p} — APRILA e verifica che sia FC26!"))
            except Exception:
                self._ui_queue.put(("log", f"📷 Monitor {idx} = {w}x{h} (anteprima nel riquadro a destra)"))
        except Exception as e:
            self._ui_queue.put(("log", f"Snapshot fallito: {e}"))

    # ---------------- file / player CRUD ----------------
    def open_songs(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(SONGS_DIR)  # type: ignore
            else:
                subprocess.Popen(["xdg-open", SONGS_DIR])
        except Exception as e:
            messagebox.showerror("Errore", str(e))

    def _pick_song(self, initial="") -> str:
        p = filedialog.askopenfilename(
            title="Scegli file audio",
            initialdir=SONGS_DIR,
            filetypes=[("Audio", "*.mp3 *.wav *.ogg"), ("Tutti", "*.*")])
        if not p:
            return initial
        # copia dentro songs/ per path relativi stabili
        try:
            if os.path.abspath(p) != os.path.join(SONGS_DIR, os.path.basename(p)):
                shutil.copy2(p, os.path.join(SONGS_DIR, os.path.basename(p)))
            rel = os.path.join("songs", os.path.basename(p))
            return rel
        except Exception:
            return p

    def _player_dialog(self, name="", song="", start=0, vol=0.8):
        d = tk.Toplevel(self)
        d.title("Giocatore")
        d.grab_set()
        d.resizable(False, False)
        vars_ = {"name": tk.StringVar(value=name), "song": tk.StringVar(value=song),
                 "start": tk.DoubleVar(value=start), "vol": tk.DoubleVar(value=vol)}

        def row(lbl, widget):
            f = ttk.Frame(d, padding=4)
            f.pack(fill="x")
            ttk.Label(f, text=lbl, width=14).pack(side="left")
            widget.pack(side="left", fill="x", expand=True)

        row("Nome", ttk.Entry(d, textvariable=vars_["name"], width=30))
        song_row = ttk.Frame(d, padding=4)
        song_row.pack(fill="x")
        ttk.Label(song_row, text="File audio", width=14).pack(side="left")
        ttk.Entry(song_row, textvariable=vars_["song"]).pack(side="left", fill="x", expand=True)
        ttk.Button(song_row, text="Sfoglia…",
                   command=lambda: vars_["song"].set(self._pick_song(vars_["song"].get()))).pack(side="left")
        row("Start (s)", ttk.Spinbox(d, textvariable=vars_["start"], from_=0, to=600, increment=1))
        row("Volume", ttk.Spinbox(d, textvariable=vars_["vol"], from_=0, to=1, increment=0.05))
        ttk.Label(d, text=f"Formati: {', '.join(AUDIO_EXTS)} — metti gli MP3 in songs/",
                  padding=6).pack()
        out = {}

        def ok():
            out.update({k: v.get() for k, v in vars_.items()})
            d.destroy()

        ttk.Button(d, text="Salva", style="Accent.TButton", command=ok).pack(pady=8)
        self.wait_window(d)
        return out if out else None

    def _add_player(self):
        r = self._player_dialog()
        if r and r["name"].strip():
            try:
                self.cfg.add_or_update_player(r["name"], r["song"] or "", r["start"], r["vol"])
                self._refresh_player_list()
                self._log(f"Aggiunto: {r['name']} -> {r['song']}")
            except Exception as e:
                messagebox.showerror("Errore", str(e))

    def _edit_player(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Seleziona un giocatore dalla lista.")
            return
        old = self.tree.item(sel[0], "text")
        e = self.cfg.players.get(old, {})
        r = self._player_dialog(old, e.get("song", ""), e.get("start_time", 0), e.get("volume", 0.8))
        if r:
            try:
                if r["name"].strip() != old:
                    self.cfg.rename_player(old, r["name"].strip())
                self.cfg.add_or_update_player(r["name"].strip(), r["song"] or "", r["start"], r["vol"])
                self._refresh_player_list()
                self._log(f"Modificato: {r['name']}")
            except Exception as ex:
                messagebox.showerror("Errore", str(ex))

    def _del_player(self):
        sel = self.tree.selection()
        if not sel:
            return
        name = self.tree.item(sel[0], "text")
        if messagebox.askyesno("Conferma", f"Rimuovere {name}?"):
            self.cfg.remove_player(name)
            self._refresh_player_list()

    def _force_selected(self):
        """Override manuale (sistema ibrido): click = riproduci subito."""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Seleziona un giocatore e premi 'Forza riproduzione'.")
            return
        name = self.tree.item(sel[0], "text")
        self._switch_to(name, 1.0, manual=True)

    def _pause_resume(self):
        self.audio.pause() if self.audio.is_playing else self.audio.resume()

    # ---------------- detection ----------------
    def toggle(self):
        if self.running:
            self.stop_loop()
        else:
            self.start_loop()

    def start_loop(self):
        if not self.cfg.players:
            messagebox.showwarning("Attenzione", "Configura almeno un giocatore prima di avviare.")
            return
        self._save_settings()
        self.running = True
        self._stop_event.clear()
        self._last_candidate = None
        self._hits = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._update_status_labels()
        self._log("Rilevamento AVVIATO.")

    def stop_loop(self):
        self.running = False
        self._stop_event.set()
        self.audio.stop(fade_out=float(self.cfg.get_setting("fade_out", 0.5)))
        self._confirmed = None
        self._update_status_labels()
        self._ui_queue.put(("song", "— (stopped)"))
        self._log("Rilevamento FERMATO.")

    def test_detection(self):
        """Test a sessione: campiona per N secondi così hai il tempo di cambiare
        giocatore in FC26 (il nome appare solo ~1.5s dopo lo switch).
        Se premuto durante un test, lo ferma."""
        if self._test_thread and self._test_thread.is_alive():
            self._test_stop_event.set()
            return
        self._save_settings()
        try:
            secs = max(3, min(60, int(self.var_test_secs.get() or 10)))
        except Exception:
            secs = 10
        self._test_stop_event = threading.Event()
        self._test_thread = threading.Thread(target=self._test_session, args=(secs,), daemon=True)
        self._test_thread.start()

    def _test_session(self, secs: int):
        import detector
        t_end = time.time() + secs
        n = 0
        last_logged = None
        last_player = None
        self._ui_queue.put(("log", f"🎯 Test detection per {secs}s — cambia giocatore in FC26 ORA!"))
        self._ui_queue.put(("test_btn", "⏹ Ferma test"))
        try:
            while time.time() < t_end and not self._test_stop_event.is_set():
                res = detector.detect_once(self.cfg.players, self.cfg.settings)
                n += 1
                remain = max(0, int(t_end - time.time()))
                self._ui_queue.put(("test_count", f"⏳ {remain}s — scansioni: {n}"))
                if res.get("error"):
                    self._ui_queue.put(("log", f"[test] ERRORE — {res['error']}"))
                    self._test_stop_event.wait(1.0)
                    continue
                self._ui_queue.put(("detect", res))
                player = res.get("player")
                raw = ", ".join(f"{t}({c:.2f})" for t, c in res.get("raw", [])[:6]) or "(nessun testo)"
                sig = (player, tuple(res.get("raw", [])[:3]))
                if sig != last_logged:
                    last_logged = sig
                    mark = "✅" if player else "…"
                    self._ui_queue.put(("log", f"[test] {mark} player={player} "
                                               f"conf={res.get('confidence', 0):.2f} raw=[{raw}] "
                                               f"{res.get('ms', 0)}ms"))
                if player and player != last_player:
                    last_player = player
                    if res.get("annotated") is not None and self.cfg.get_setting("debug"):
                        try:
                            import cv2
                            p = os.path.join(DEBUG_DIR, f"test_{datetime.now():%Y%m%d_%H%M%S}.png")
                            cv2.imwrite(p, res["annotated"])
                            self._ui_queue.put(("log", f"[test] Screenshot: {p}"))
                        except Exception:
                            pass
                elif not player:
                    last_player = None
                if res.get("annotated") is not None and self.cfg.get_setting("debug"):
                    self._ui_queue.put(("preview", res["annotated"]))
                self._test_stop_event.wait(0.3)
        except Exception:
            self._ui_queue.put(("log", "Test fallito:\n" + traceback.format_exc()))
        self._ui_queue.put(("log", f"🏁 Test terminato ({n} scansioni)."))
        self._ui_queue.put(("test_btn", "🎯 Test Detection"))
        self._ui_queue.put(("test_count", ""))

    def _install_switch_listener(self):
        """Burst OCR quando premi il tasto cambio-giocatore (default Q).

        Il cognome in FC appare solo ~1-1.5s dopo lo switch: il burst campiona
        a raffica proprio in quella finestra invece di sprecare CPU sempre.
        """
        def worker():
            try:
                import keyboard  # type: ignore
            except Exception:
                return
            keys = self.cfg.get_setting("switch_keys", ["q"]) or ["q"]
            try:
                for k in keys:
                    try:
                        keyboard.on_press_key(k, lambda _e: self._trigger_burst(), suppress=False)
                    except Exception:
                        pass
                keyboard.wait()  # mantiene vivo il listener
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _trigger_burst(self):
        if self.running and self.cfg.get_setting("burst_on_switch_key", True):
            self._burst_until = time.time() + 2.0  # 2s di campionamento rapido

    def _loop(self):
        import detector
        s = self.cfg.settings
        while not self._stop_event.is_set():
            try:
                interval = float(s.get("detection_interval", 0.5) or 0.5)
                if time.time() < self._burst_until:
                    interval = min(interval, 0.15)
                res = detector.detect_once(self.cfg.players, s)
                if res.get("error"):
                    self._ui_queue.put(("log", f"OCR: {res['error']}"))
                    self._stop_event.wait(max(1.0, interval * 3))
                    continue

                cand = res.get("player")
                conf = float(res.get("confidence", 0) or 0)
                need = int(s.get("consecutive_hits", 2) or 2)

                # conteggio conferme consecutive (anti-falsi-positivi)
                if cand and cand == self._last_candidate:
                    self._hits += 1
                elif cand:
                    self._last_candidate, self._hits = cand, 1
                else:
                    # nessun match: scala ma non azzera di colpo
                    self._hits = max(0, self._hits - 1)
                    if self._hits == 0:
                        self._last_candidate = None

                self._ui_queue.put(("detect", res))
                if s.get("debug") and res.get("annotated") is not None:
                    self._ui_queue.put(("preview", res["annotated"]))

                if cand and self._hits >= need and cand != self._confirmed:
                    self._switch_to(cand, conf)
                elif not cand and self._hits == 0 and self._confirmed:
                    # giocatore non configurato
                    if str(s.get("unknown_behavior", "keep")) == "stop":
                        self.audio.stop(fade_out=float(s.get("fade_out", 0.5) or 0.5))
                        self._confirmed = None
                        self._ui_queue.put(("song", "— (giocatore non configurato)"))
                        self._ui_queue.put(("log", "Giocatore non configurato → stop."))
                    # else "keep": lascia la musica precedente

                self._stop_event.wait(interval)
            except Exception:
                self._ui_queue.put(("log", "Loop errore:\n" + traceback.format_exc()))
                self._stop_event.wait(1.0)

    def _switch_to(self, player_name: str, conf: float, manual: bool = False):
        e = self.cfg.players.get(player_name)
        if not e:
            return
        s = self.cfg.settings
        # fade-out precedente gestito da pygame (stop implicito nel load) —
        # per un crossfade vero facciamo fadeout breve prima del load
        try:
            if self.audio.current_song is not None:
                import pygame  # type: ignore
                pygame.mixer.music.fadeout(max(1, int(float(s.get("fade_out", 0.5) or 0.5) * 1000)))
                time.sleep(min(0.6, float(s.get("fade_out", 0.5) or 0.5)))
        except Exception:
            pass
        changed, msg = self.audio.play_for_player(
            player_name, e.get("song", ""), e.get("start_time", 0),
            e.get("volume", 0.8), s.get("master_volume", 0.8),
            s.get("fade_in", 0.5))
        self._confirmed = player_name
        self._last_candidate, self._hits = player_name, int(s.get("consecutive_hits", 2) or 2)
        tag = "MANUALE" if manual else f"conf={conf:.2f}"
        self._ui_queue.put(("log", f"🎵 Switch → {player_name} ({tag}): {msg}"))
        self._ui_queue.put(("song", f"{player_name} — {os.path.basename(str(e.get('song', '')))}"))


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
