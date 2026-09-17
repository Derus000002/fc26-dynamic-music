"""Audio manager con pygame.mixer: MP3/WAV/OGG, fade, start_time, volumi."""
from __future__ import annotations

import os
import threading


class AudioManager:
    def __init__(self, songs_base_dir: str = "."):
        self.songs_base = os.path.abspath(songs_base_dir)
        self._lock = threading.Lock()
        self._available = False
        self._error: str | None = None
        self.current_song: str | None = None   # path assoluto
        self.current_player: str | None = None
        self._paused = False
        try:
            import pygame
            pygame.mixer.init(frequency=44100)
            self._pygame = pygame
            self._available = True
        except Exception as e:  # pygame assente o no audio device
            self._pygame = None
            self._error = str(e)

    # ---------- stato ----------
    @property
    def available(self) -> bool:
        return self._available

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def is_playing(self) -> bool:
        if not self._available:
            return False
        try:
            return bool(self._pygame.mixer.music.get_busy() and not self._paused)
        except Exception:
            return False

    # ---------- helpers ----------
    def resolve(self, song_rel_or_abs: str) -> str:
        p = song_rel_or_abs
        if not os.path.isabs(p):
            p = os.path.join(self.songs_base, p)
        return os.path.abspath(p)

    # ---------- controlli ----------
    def play_for_player(self, player_name: str, song: str, start_time: float = 0,
                        volume: float = 0.8, master_volume: float = 0.8,
                        fade_in: float = 0.5) -> tuple[bool, str]:
        """Avvia la canzone del giocatore. Ritorna (changed, msg).
        Se è già la stessa canzone -> non fa nulla (evita restart)."""
        path = self.resolve(song)
        with self._lock:
            if not self._available:
                return False, f"Audio non disponibile: {self._error}"
            if not os.path.exists(path):
                return False, f"File non trovato: {path}"
            if self.current_song == path and self.is_playing and player_name == self.current_player:
                return False, "Stessa canzone già in riproduzione."
            vol = max(0.0, min(1.0, float(volume) * float(master_volume)))
            fade_ms = max(0, int(float(fade_in) * 1000))
            try:
                self._pygame.mixer.music.load(path)
                self._pygame.mixer.music.set_volume(vol)
                try:
                    # start= funziona per OGG/MP3 (pygame>=2); per WAV può lanciare -> fallback
                    self._pygame.mixer.music.play(fade_ms=fade_ms, start=float(start_time or 0))
                except Exception:
                    self._pygame.mixer.music.play(fade_ms=fade_ms)
                self.current_song = path
                self.current_player = player_name
                self._paused = False
                return True, f"▶ {player_name} — {os.path.basename(path)} (vol {vol:.2f})"
            except Exception as e:
                return False, f"Errore play: {e}"

    def stop(self, fade_out: float = 0.5):
        with self._lock:
            if not self._available:
                self.current_song = None
                self.current_player = None
                return
            try:
                fade_ms = max(0, int(float(fade_out) * 1000))
                if fade_ms > 0:
                    self._pygame.mixer.music.fadeout(fade_ms)
                else:
                    self._pygame.mixer.music.stop()
            except Exception:
                pass
            self.current_song = None
            self.current_player = None
            self._paused = False

    def pause(self):
        with self._lock:
            if self._available:
                try:
                    self._pygame.mixer.music.pause()
                    self._paused = True
                except Exception:
                    pass

    def resume(self):
        with self._lock:
            if self._available:
                try:
                    self._pygame.mixer.music.unpause()
                    self._paused = False
                except Exception:
                    pass

    def set_volume(self, volume: float, master_volume: float = 1.0):
        if self._available:
            try:
                self._pygame.mixer.music.set_volume(max(0.0, min(1.0, volume * master_volume)))
            except Exception:
                pass
