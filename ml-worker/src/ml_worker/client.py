"""The mdstore worker transport; all repository access is lease scoped."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, quote
from urllib.request import Request, urlopen

class Client:
    def __init__(self, server: str, token: str):
        parsed = urlparse(server)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("server must be an HTTP(S) origin")
        if not token:
            raise ValueError("worker token is required")
        self.server = server.rstrip("/")
        self.token = token

    def request(self, path: str, value=None, *, body: bytes | None = None, method: str | None = None):
        headers = {"Authorization": f"Bearer {self.token}"}
        if value is not None:
            body = json.dumps(value, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.server + path, data=body, headers=headers, method=method)
        # Completion is replay-safe by attempt and output hashes, including
        # after a server restart. Retry the same bytes if its response is lost.
        attempts = 2 if path == "/worker" and value and value.get("op") == "complete" else 1
        for attempt in range(attempts):
            try:
                with urlopen(request, timeout=60) as response:
                    return json.load(response)
            except HTTPError as error:
                detail = error.read(8192).decode("utf-8", errors="replace")
                if error.code >= 500 and attempt + 1 < attempts:
                    continue
                raise RuntimeError(f"mdstore returned {error.code}: {detail}") from error
            except (URLError, TimeoutError, ConnectionError, IncompleteRead, json.JSONDecodeError):
                if attempt + 1 == attempts:
                    raise

    def search(self, query: dict):
        query = dict(query)
        job, attempt = query.pop("job"), query.pop("attempt")
        return self.request("/worker", {"op": "search", "id": job, "attempt": attempt, "query": query})["results"]

    def download(self, query: dict, target: Path, asset: dict):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and digest(target) == asset["oid"]:
            return
        temporary = target.with_name(target.name + ".part")
        request = Request(self.server + "/" + quote(query["path"], safe="/") + "?" + urlencode({k: v for k, v in query.items() if k != "path"}), headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    if output.tell() > asset["size"]:
                        raise ValueError("download exceeds assigned size")
            if temporary.stat().st_size != asset["size"] or digest(temporary) != asset["oid"]:
                raise ValueError("assigned input checksum mismatch")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_path(root: Path, path: str) -> Path:
    target = (root / path).resolve()
    if not path or Path(path).is_absolute() or not target.is_relative_to(root.resolve()):
        raise ValueError("job path escapes the worker directory")
    return target
