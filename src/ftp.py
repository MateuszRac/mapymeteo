import ftplib
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)


# ── Konfiguracja ──────────────────────────────────────────────
FTP_HOST     = os.getenv("FTP_HOST")
FTP_PORT     = int(os.getenv("FTP_PORT", 21))
FTP_USER     = os.getenv("FTP_USER")
FTP_PASSWORD = os.getenv("FTP_PASSWORD")
FTP_TLS      = os.getenv("FTP_TLS", "false").lower() == "true"  # True = FTPS (explicit TLS)

# ── Opcje ─────────────────────────────────────────────────────
DELETE_AFTER_UPLOAD = False   # usuń lokalny plik po udanym uploadzie
OVERWRITE_EXISTING  = False   # nadpisuj istniejące pliki na serwerze
# ─────────────────────────────────────────────────────────────


def connect_ftp() -> ftplib.FTP:
    """Łączy z serwerem FTP lub FTPS (explicit TLS)."""
    ftp = ftplib.FTP_TLS() if FTP_TLS else ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT)
    ftp.login(FTP_USER, FTP_PASSWORD)
    if FTP_TLS:
        ftp.prot_p()  # włącz szyfrowanie kanału danych
    ftp.set_pasv(True)
    print(f"[połączono] {FTP_HOST}:{FTP_PORT} {'(FTPS)' if FTP_TLS else '(FTP)'}")
    return ftp


def remote_exists(ftp: ftplib.FTP, path: str) -> bool:
    """Sprawdza czy plik istnieje na serwerze."""
    try:
        ftp.size(path)
        return True
    except ftplib.error_perm:
        return False


def ensure_remote_dir(ftp: ftplib.FTP, remote_path: str):
    """Tworzy folder zdalny jeśli nie istnieje (rekurencyjnie)."""
    parts = Path(remote_path).parts
    current = ""
    for part in parts:
        current = str(Path(current) / part).replace("\\", "/")
        try:
            ftp.mkd(current)
            print(f"  [mkdir] {current}")
        except ftplib.error_perm:
            pass  # folder już istnieje


def transfer_files(local_dir: str, remote_dir: str, recursive: bool = False):
    local_dir = Path(local_dir)
    files     = list(local_dir.rglob("*") if recursive else local_dir.glob("*"))
    files     = [f for f in files if f.is_file()]

    if not files:
        print("Brak plików do transferu.")
        return

    print(f"Znaleziono {len(files)} plików do transferu...\n")

    ftp = connect_ftp()
    uploaded = 0
    skipped  = 0
    errors   = 0

    try:
        for local_file in files:
            relative        = local_file.relative_to(local_dir)
            remote_file_str = (Path(remote_dir) / relative).as_posix()

            # Upewnij się że folder zdalny istnieje
            # ensure_remote_dir(ftp, Path(remote_file_str).parent.as_posix())

            if not OVERWRITE_EXISTING and remote_exists(ftp, remote_file_str):
                print(f"  [pominięto] {relative}")
                skipped += 1
                continue

            try:
                print(f"  [DEBUG] lokalny: {local_file}")
                print(f"  [DEBUG] zdalny:  {remote_file_str}")

                with open(local_file, "rb") as f:
                    ftp.storbinary(f"STOR {remote_file_str}", f)

                print(f"  [OK] {relative} → {remote_file_str}")
                uploaded += 1

                if DELETE_AFTER_UPLOAD:
                    local_file.unlink()
                    print(f"       (usunięto lokalny plik)")

            except ftplib.all_errors as e:
                print(f"  [BŁĄD] {relative}: {e}")
                errors += 1

    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()

    print(f"\nGotowe: {uploaded} wysłano, {skipped} pominięto, {errors} błędów.")