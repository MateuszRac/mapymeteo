"""Konfiguracja logowania dla całej aplikacji."""

import logging
import logging.handlers
from pathlib import Path


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    """
    Konfiguruje root logger: konsola + plik rotacyjny.
    Wywołać raz na starcie aplikacji (w fetch_new.py / main()).
    Wszystkie getLogger(__name__) w modułach dziedziczą tę konfigurację.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    if root.handlers:
        return  # już skonfigurowany (np. przy ponownym imporcie)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = logging.handlers.RotatingFileHandler(
        log_dir / "fetch_new.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
