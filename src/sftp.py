import paramiko
import os
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()


# ── Konfiguracja ──────────────────────────────────────────────
SFTP_HOST     = os.getenv("SFTP_HOST")
SFTP_PORT     = int(os.getenv("SFTP_PORT"))
SFTP_USER     = os.getenv("SFTP_USER")
SFTP_PASSWORD = os.getenv("SFTP_PASSWORD")
SFTP_KEY_PATH = os.getenv("SFTP_KEY_PATH")

# ── Opcje ─────────────────────────────────────────────────────
DELETE_AFTER_UPLOAD = False   # usuń lokalny plik po udanym uploadzie
OVERWRITE_EXISTING  = False    # nadpisuj istniejące pliki na serwerze
# ─────────────────────────────────────────────────────────────


def connect_sftp():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    if SFTP_KEY_PATH:
        key = paramiko.RSAKey.from_private_key_file(SFTP_KEY_PATH)
        ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, pkey=key)
    else:
        ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, password=SFTP_PASSWORD)

    return ssh, ssh.open_sftp()


def remote_exists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except FileNotFoundError:
        return False


def ensure_remote_dir(sftp, remote_path):
    """Tworzy folder zdalny jeśli nie istnieje (rekurencyjnie)."""
    parts = Path(remote_path).parts
    current = ""
    for part in parts:
        current = str(Path(current) / part)
        if not remote_exists(sftp, current):
            try:
                sftp.mkdir(current)
                print(f"  [mkdir] {current}")
            except Exception:
                pass  # może już istnieć (race condition)


def transfer_files(local_dir, remote_dir, recursive=False):
    local_dir  = Path(local_dir)
    files      = list(local_dir.rglob("*") if recursive else local_dir.glob("*"))
    files      = [f for f in files if f.is_file()]

    if not files:
        print("Brak plików do transferu.")
        return

    print(f"Znaleziono {len(files)} plików do transferu...\n")

    ssh, sftp = connect_sftp()
    uploaded = 0
    skipped  = 0
    errors   = 0

    try:
        for local_file in files:
            # Oblicz ścieżkę zdalną zachowując strukturę podfolderów
            relative  = local_file.relative_to(local_dir)
            remote_file = Path(remote_dir) / relative
            remote_file_str = remote_file.as_posix()

            # Upewnij się że folder zdalny istnieje
            #ensure_remote_dir(sftp, remote_file.parent.as_posix())

            # Sprawdź czy plik już istnieje
            if not OVERWRITE_EXISTING and remote_exists(sftp, remote_file_str):
                print(f"  [pominięto] {relative}")
                skipped += 1
                continue

            try:
                print(f"  [DEBUG] lokalny:  {str(local_file)}")
                print(f"  [DEBUG] zdalny:   {remote_file_str}")
                sftp.put(str(local_file), remote_file_str)
                print(f"  [OK] {relative} → {remote_file_str}")
                uploaded += 1

                if DELETE_AFTER_UPLOAD:
                    local_file.unlink()
                    print(f"       (usunięto lokalny plik)")

            except Exception as e:
                print(f"  [BŁĄD] {relative}: {e}")
                errors += 1

    finally:
        sftp.close()
        ssh.close()

    print(f"\nGotowe: {uploaded} wysłano, {skipped} pominięto, {errors} błędów.")
