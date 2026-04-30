"""Zarzadzanie plikiem stanu cache/state.json."""

import json
from pathlib import Path


class StateManager:
    """Laduje i zapisuje stan (ostatnie przetworzone timestampy) do pliku JSON."""

    def __init__(self, state_file: str | Path):
        self._path = Path(state_file)
        self._data: dict = {}

    def load(self) -> "StateManager":
        if self._path.exists():
            with open(self._path, encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            self._data = {}
        return self

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str):
        self._data[key] = value
