import json
import secrets
from contextlib import contextmanager
from http.client import HTTPException
from itertools import chain
from mimetypes import guess_type
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class TelegramError(Exception):
    def __init__(self, method, code=None, retry_after=None):
        self.method = method
        self.code = code
        self.retry_after = retry_after
        # Telegram responses and request URLs can contain private data.
        super().__init__(f"{method}: {code or 'ошибка связи с Telegram'}")

    @property
    def temporary(self):
        return self.code is None or self.code == 429 or self.code >= 500


class Telegram:
    def __init__(self, token, base="https://api.telegram.org"):
        self.url = f"{base}/bot{token}/"
        self.file_url = f"{base}/file/bot{token}/"
        self.local = base != "https://api.telegram.org"

    def call(self, method, **parameters):
        request = Request(
            self.url + method,
            data=json.dumps(parameters).encode(),
            headers={"Content-Type": "application/json"},
        )
        return self.request(method, request, parameters.get("timeout", 0) + 10)

    def request(self, method, request, timeout):
        http_status = None
        try:
            try:
                with urlopen(request, timeout=timeout) as response:
                    body = response.read()
            except HTTPError as error:
                with error:
                    http_status = error.code
                    body = error.read()
        except (URLError, OSError, HTTPException):
            raise TelegramError(method) from None

        try:
            result = json.loads(body)
        except ValueError:
            raise TelegramError(method, http_status) from None

        if not isinstance(result, dict):
            raise TelegramError(method, http_status)
        if result.get("ok"):
            return result["result"]
        raise TelegramError(
            method, result.get("error_code", http_status),
            result.get("parameters", {}).get("retry_after"),
        )

    @contextmanager
    def open_file(self, file_id):
        info = self.call("getFile", file_id=file_id)
        file_path = info.get("file_path")
        if not file_path:
            raise TelegramError("getFile", 400)
        try:
            if self.local and Path(file_path).is_absolute():
                with Path(file_path).open("rb") as source:
                    yield source, info.get("file_size")
            else:
                url = self.file_url + quote(file_path, safe="/")
                with urlopen(url, timeout=30) as source:
                    yield source, info.get("file_size")
        except HTTPError as error:
            raise TelegramError("download", error.code) from None
        except (URLError, OSError, HTTPException):
            raise TelegramError("download") from None

    def upload(self, method, parameters, files):
        boundary = secrets.token_hex(24)
        prefix = bytearray()
        for field, value in parameters.items():
            encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            prefix.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{encoded}\r\n'.encode())
        parts = [[bytes(prefix)]]
        length = len(prefix)
        for field, filename, size, chunks in files:
            filename = "".join(char for char in filename if char >= " " and char not in '\"/\\') or "media"
            content_type = guess_type(filename)[0] or "application/octet-stream"
            header = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                      f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n').encode()
            parts.extend(([header], chunks, [b"\r\n"]))
            length += len(header) + size + 2
        suffix = f"--{boundary}--\r\n".encode()
        parts.append([suffix])
        request = Request(
            self.url + method,
            data=chain.from_iterable(parts),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(length + len(suffix)),
            },
        )
        return self.request(method, request, 60)
