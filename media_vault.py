import logging
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event, Thread

from cryptography.fernet import InvalidToken

from archive import Archive
from telegram_api import TelegramError


CHUNK_SIZE = 1024 * 1024


class MediaVault:
    def __init__(self, directory, cipher):
        self.directory = directory
        self.cipher = cipher
        directory.mkdir(exist_ok=True)

    def path(self, key):
        return self.directory / key

    def exists(self, key):
        return self.path(key).is_file()

    def capture(self, key, source, expected, stop):
        path = self.path(key)
        temporary = None
        size = 0
        index = 0
        try:
            with NamedTemporaryFile(dir=self.directory, suffix=".part", delete=False) as destination:
                temporary = Path(destination.name)
                destination.write(b"\x00" * 8)
                while not stop.is_set():
                    chunk = source.read(CHUNK_SIZE)
                    context = key.encode() + index.to_bytes(8, "big")
                    destination.write(self.cipher.encrypt(context + chunk) + b"\n")
                    if not chunk:
                        break
                    size += len(chunk)
                    index += 1
                if stop.is_set():
                    return
                if expected is not None and size != expected:
                    raise TelegramError("download")
                destination.seek(0)
                destination.write(size.to_bytes(8, "big"))
            try:
                if not path.exists():
                    temporary.replace(path)
            except PermissionError:
                if not path.exists():
                    raise
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    @contextmanager
    def read(self, key):
        with self.path(key).open("rb") as source:
            size = source.read(8)
            if len(size) != 8:
                raise InvalidToken
            length = int.from_bytes(size, "big")
            yield length, self.chunks(key, source, length)

    def chunks(self, key, source, length):
        index = 0
        total = 0
        while line := source.readline(CHUNK_SIZE * 2):
            context = key.encode() + index.to_bytes(8, "big")
            payload = self.cipher.decrypt(line.rstrip(b"\n"))
            if not payload.startswith(context):
                raise InvalidToken
            chunk = payload[len(context):]
            if not chunk:
                if total != length or source.read(1):
                    raise InvalidToken
                return
            total += len(chunk)
            if total > length:
                raise InvalidToken
            yield chunk
            index += 1
        raise InvalidToken

    def download(self, api, archive, stop):
        while not stop.is_set():
            pending = archive.next_download()
            if pending is None:
                stop.wait(1)
                continue
            key, file_id = pending
            retry_after = None
            try:
                if not self.exists(key):
                    with api.open_file(file_id) as (source, expected):
                        self.capture(key, source, expected, stop)
            except TelegramError as error:
                logging.warning("Результат загрузки вложения (%s)", error)
                if error.temporary:
                    retry_after = error.retry_after or 15
                elif error.method == "download" and error.code in (403, 404):
                    retry_after = 60
            except OSError:
                logging.warning("Не удалось записать локальную копию вложения")
                retry_after = 60
            if stop.is_set():
                return
            with archive.db:
                archive.finish_download(key, file_id, retry_after)


@contextmanager
def download_worker(api, vault, database, key):
    stop = Event()

    def run():
        while not stop.is_set():
            try:
                archive = Archive(database, key)
                with closing(archive.db):
                    vault.download(api, archive, stop)
            except sqlite3.OperationalError:
                logging.warning("Архив временно недоступен для загрузки вложений")
                stop.wait(5)

    thread = Thread(target=run, name="telegram-media", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)
