from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlparse

from .transcripts import (
    audio_sources,
    candidate_identities,
    discover,
    load_hint_document,
    parse_markdown,
    review_progress,
    resolve_identifier,
    transcript_payload,
    write_hint_document,
)
from .jobs import RegenerationQueue


STATIC = (Path(__file__).parent / "static").resolve()


class ReviewServer(ThreadingHTTPServer):
    def __init__(self, address, root: Path):
        super().__init__(address, ReviewHandler)
        self.review_root = root.expanduser().resolve()
        self.jobs = RegenerationQueue(self.review_root)

    def server_close(self) -> None:
        jobs = getattr(self, "jobs", None)
        if jobs is not None:
            jobs.close()
        super().server_close()


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def do_GET(self) -> None:
        try:
            self._get()
        except FileNotFoundError:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_PUT(self) -> None:
        try:
            self._put()
        except RuntimeError as error:
            self._json({"error": str(error)}, HTTPStatus.CONFLICT)
        except (ValueError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        try:
            self._post()
        except FileNotFoundError:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def _get(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/api/transcripts":
            summaries = []
            for transcript in discover(self.server.review_root):
                try:
                    parsed = parse_markdown(transcript.markdown)
                except (FileNotFoundError, ValueError):
                    parsed = None
                hints, _ = load_hint_document(transcript.hint_path)
                progress = review_progress(parsed, hints) if parsed else {
                    "complete": False,
                    "unassignedRunCount": None,
                    "unassignedSpeakerCount": None,
                }
                summaries.append({
                    "id": transcript.identifier,
                    "name": str(transcript.relative),
                    "title": hints.get("title") or (parsed["title"] if parsed else transcript.markdown.stem),
                    "status": transcript.status,
                    "startedAt": hints.get("started_at") or (parsed["frontmatter"].get("started_at") if parsed else None),
                    "turnCount": len(parsed["turns"]) if parsed else 0,
                    "review": progress,
                })
            self._json(summaries)
            return
        if path == "/api/jobs":
            self._json(self.server.jobs.payload())
            return
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["api", "transcripts"]:
            transcript = resolve_identifier(self.server.review_root, parts[2])
            if len(parts) == 3:
                self._json(transcript_payload(transcript))
                return
            if len(parts) == 5 and parts[3] == "audio":
                source = audio_sources(transcript)[int(parts[4])]
                self._file(source["path"])
                return
            if len(parts) == 5 and parts[3] == "candidates":
                self._json(candidate_identities(self.server.review_root, transcript, parts[4]))
                return
        static_path = "index.html" if path == "/" else path.removeprefix("/")
        target = (STATIC / static_path).resolve()
        if target.parent != STATIC and STATIC not in target.parents:
            raise FileNotFoundError(target)
        self._file(target)

    def _put(self) -> None:
        parts = unquote(urlparse(self.path).path).strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "transcripts"] or parts[3] != "hints":
            raise FileNotFoundError(self.path)
        transcript = resolve_identifier(self.server.review_root, parts[2])
        length = int(self.headers.get("Content-Length", "0"))
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict) or not isinstance(value.get("hints"), dict):
            raise ValueError("request must contain a hints object")
        revision = write_hint_document(
            transcript.hint_path,
            value["hints"],
            value.get("revision"),
        )
        self._json({"revision": revision})

    def _post(self) -> None:
        parts = unquote(urlparse(self.path).path).strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "transcripts"] or parts[3] != "regenerate":
            raise FileNotFoundError(self.path)
        transcript = resolve_identifier(self.server.review_root, parts[2])
        if transcript.requested is None:
            raise ValueError("the original speech2md input is unavailable")
        job = self.server.jobs.enqueue(
            transcript,
            prefer_cache=transcript.status != "unprocessed",
        )
        self._json(job.payload(), HTTPStatus.ACCEPTED)

    def _json(self, value, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            raw_start, _, raw_end = range_header.removeprefix("bytes=").partition("-")
            start = int(raw_start or 0)
            end = min(int(raw_end) if raw_end else size - 1, size - 1)
            if start < 0 or start > end:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, format: str, *args) -> None:
        print(f"speech-review: {format % args}")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Review speech2md transcripts in a folder")
    value.add_argument("folder", type=Path, nargs="?", default=Path.cwd())
    value.add_argument("--host", default="127.0.0.1")
    value.add_argument("--port", type=int, default=8765)
    return value


def main() -> None:
    arguments = parser().parse_args()
    server = ReviewServer((arguments.host, arguments.port), arguments.folder)
    print(f"Reviewing {server.review_root} at http://{arguments.host}:{arguments.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
