"""Upload i usuwanie plików przez FTP/FTPS — sesja wielokrotnego użytku."""

import contextlib
import ftplib
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

log = logging.getLogger(__name__)


class FtpSession:
    """Otwarte połączenie FTP — reużywa jednego socketu dla wielu operacji."""

    def __init__(self, ftp: ftplib.FTP):
        self._ftp = ftp
        self._created_dirs: set[str] = set()

    def upload(self, local_path: Path, remote_path: str) -> bool:
        remote_dir = str(Path(remote_path).parent).replace("\\", "/")
        if remote_dir not in self._created_dirs:
            self._ensure_dir(remote_dir)
            self._created_dirs.add(remote_dir)
        try:
            with open(local_path, "rb") as f:
                self._ftp.storbinary(f"STOR {remote_path}", f)
            log.info("FTP upload: %s", remote_path)
            return True
        except ftplib.all_errors as e:
            log.error("FTP upload failed (%s): %s", remote_path, e)
            return False

    def delete(self, remote_path: str) -> bool:
        try:
            self._ftp.delete(remote_path)
            log.info("FTP delete: %s", remote_path)
            return True
        except ftplib.all_errors as e:
            log.warning("FTP delete failed (%s): %s", remote_path, e)
            return False

    def _ensure_dir(self, path: str):
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            try:
                self._ftp.mkd(current)
                log.debug("FTP mkdir: %s", current)
            except ftplib.error_perm:
                pass  # katalog już istnieje


class FtpUploader:
    """Zarządza połączeniami FTP/FTPS i synchronizacją plików."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        tls: bool | None = None,
    ):
        self.host     = host     or os.getenv("FTP_HOST", "")
        self.port     = port     or int(os.getenv("FTP_PORT", 21))
        self.user     = user     or os.getenv("FTP_USER", "")
        self.password = password or os.getenv("FTP_PASSWORD", "")
        self.tls      = tls if tls is not None else (os.getenv("FTP_TLS", "false").lower() == "true")

    def is_configured(self) -> bool:
        return bool(self.host and self.user)

    def connect(self) -> ftplib.FTP:
        log.info("FTP łączenie z %s:%s%s", self.host, self.port,
                 " (FTPS)" if self.tls else "")
        ftp = ftplib.FTP_TLS() if self.tls else ftplib.FTP()
        ftp.connect(self.host, self.port)
        ftp.login(self.user, self.password)
        if self.tls:
            ftp.prot_p()
        ftp.set_pasv(True)
        log.info("FTP połączono z %s:%s", self.host, self.port)
        return ftp

    @contextlib.contextmanager
    def session(self):
        """Context manager zwracający FtpSession z jednym połączeniem dla całego bloku."""
        ftp = self.connect()
        try:
            yield FtpSession(ftp)
        finally:
            try:
                ftp.quit()
                log.debug("FTP połączenie zamknięte")
            except Exception:
                ftp.close()

    def transfer_files(
        self,
        local_dir: str | Path,
        remote_dir: str,
        recursive: bool = False,
        overwrite_existing: bool = False,
        delete_after_upload: bool = False,
    ):
        """Transfer wielu plików z katalogu lokalnego na zdalny."""
        local_dir = Path(local_dir)
        files = list(local_dir.rglob("*") if recursive else local_dir.glob("*"))
        files = [f for f in files if f.is_file()]

        if not files:
            log.info("Brak plików do transferu w: %s", local_dir)
            return

        log.info("Transfer %d pliku/ów z %s → %s", len(files), local_dir, remote_dir)
        uploaded = skipped = errors = 0

        with self.session() as sess:
            for local_file in files:
                relative    = local_file.relative_to(local_dir)
                remote_path = (Path(remote_dir) / relative).as_posix()

                if not overwrite_existing:
                    try:
                        sess._ftp.size(remote_path)
                        log.debug("FTP pominięto (istnieje): %s", relative)
                        skipped += 1
                        continue
                    except ftplib.error_perm:
                        pass

                if sess.upload(local_file, remote_path):
                    uploaded += 1
                    if delete_after_upload:
                        local_file.unlink()
                        log.debug("Usunięto lokalny: %s", local_file)
                else:
                    errors += 1

        log.info("FTP transfer zakończony: %d wysłano, %d pominięto, %d błędów",
                 uploaded, skipped, errors)
