"""Gestione config.json per FC26 Dynamic Music."""
from __future__ import annotations

import json
import os
import unicodedata

DEFAULT_CONFIG = {
    "players": {},
    "settings": {
        "fade_out": 0.5,
        "fade_in": 0.5,
        "confidence_threshold": 0.55,
        "detection_interval": 0.4,
        "consecutive_hits": 1,
        "unknown_behavior": "keep",   # "keep" | "stop"
        "ocr_backend": "auto",        # "auto" | "rapidocr" | "easyocr" | "tesseract"
        "ocr_language": "en",
        "monitor_index": 1,
        "roi": None,                  # [x, y, w, h] oppure None = schermo intero
        "use_indicator_tracking": True,
        "burst_on_switch_key": True,
        "switch_keys": ["q", "tab"],
        "master_volume": 0.8,
        "debug": False,
    },
}

AUDIO_EXTS = (".mp3", ".wav", ".ogg")


def normalize_name(s: str) -> str:
    """Minuscolo, senza accenti, spazi compattati: 'Kylian Mbappé' -> 'kylian mbappe'."""
    s = s.strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


class ConfigManager:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.data = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
        self.load()

    # ---------- load / save ----------
    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                # merge con default per restare compatibili con versioni future
                merged = json.loads(json.dumps(DEFAULT_CONFIG))
                merged["players"] = raw.get("players", {})
                for k, v in raw.get("settings", {}).items():
                    merged["settings"][k] = v
                self.data = merged
            except Exception as e:
                print(f"[config] Errore lettura {self.path}: {e}, uso default.")
        else:
            self.save()
        return self.data

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    # ---------- accessors ----------
    @property
    def players(self) -> dict:
        return self.data.setdefault("players", {})

    @property
    def settings(self) -> dict:
        return self.data.setdefault("settings", {})

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value
        self.save()

    # ---------- CRUD giocatori ----------
    def add_or_update_player(self, name: str, song: str, start_time: float = 0, volume: float = 0.8):
        name = name.strip()
        if not name:
            raise ValueError("Nome giocatore vuoto.")
        self.players[name] = {
            "song": song,
            "start_time": float(start_time or 0),
            "volume": float(volume if volume is not None else 0.8),
        }
        self.save()

    def remove_player(self, name: str):
        if name in self.players:
            del self.players[name]
            self.save()

    def rename_player(self, old: str, new: str):
        new = new.strip()
        if old not in self.players:
            raise KeyError(old)
        if not new:
            raise ValueError("Nuovo nome vuoto.")
        if old != new:
            self.players[new] = self.players.pop(old)
            self.save()

    def normalized_index(self) -> dict:
        """normalized -> (display_name, entry) per fuzzy match veloce."""
        return {normalize_name(k): (k, v) for k, v in self.players.items()}
