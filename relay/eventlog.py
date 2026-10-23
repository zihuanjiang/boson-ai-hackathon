"""JSONL event log, one file per server run: `out/logs/<talk>-<YYYYmmdd-HHMMSS>.jsonl` (decision D17).

Each line is `{"t": seconds since start, "ts": local ISO time, "kind": kind, **data}`. Audio payloads (long
base64 strings under `pcm`, `audio` or `delta`, at any depth) are replaced by their lengths, so the log stays small
and readable. An `EventLog` is a `relay.ports.LogFn`; it never raises (a broken log must not break the show).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import OUT

AUDIO_KEYS = frozenset({"pcm", "audio", "delta"})
MIN_PAYLOAD_CHARS = 64  # shorter strings (e.g. a transcript delta) are kept as they are
RESERVED = ("t", "ts", "kind")


def _is_payload(value: Any) -> bool:
    """Base64 audio looks like a long string without whitespace; transcript text has spaces."""
    return isinstance(value, str) and len(value) >= MIN_PAYLOAD_CHARS and not any(c.isspace() for c in value)


def redact(value: Any) -> Any:
    """A copy of `value` with audio payloads replaced by their lengths (bytes anywhere → their length). Never
    modifies its argument: the same event dict is still dispatched to the controller after it is logged."""
    if isinstance(value, dict):
        return {key: len(item) if key in AUDIO_KEYS and _is_payload(item) else redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    return value


def _fallback(value: Any) -> str:
    return repr(value)


class EventLog:
    """Append-only JSONL log. `EventLog(None)` is a valid no-op log."""

    def __init__(self, path: Path | str | None) -> None:
        self.path = Path(path) if path is not None else None
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._file = None
        self._warned = False
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - no log is better than no show
            self._warn(f"cannot open {self.path}: {exc}")

    @classmethod
    def for_talk(cls, talk_id: str, out_root: Path = OUT) -> EventLog:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return cls(Path(out_root) / "logs" / f"{talk_id}-{stamp}.jsonl")

    def __call__(self, kind: str, data: dict | None = None) -> None:
        if self._file is None:
            return
        try:
            record: dict[str, Any] = {
                "t": round(time.monotonic() - self._t0, 3),
                "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "kind": kind,
            }
            payload = redact(data) if isinstance(data, dict) else ({} if data is None else {"data": redact(data)})
            for key, value in payload.items():
                record[f"data_{key}" if key in RESERVED else key] = value
            line = json.dumps(record, ensure_ascii=False, default=_fallback)
            with self._lock:
                if self._file is not None:
                    self._file.write(line + "\n")
                    self._file.flush()
        except Exception as exc:  # noqa: BLE001 - logging must never take the show down
            self._warn(f"dropped a {kind!r} record: {exc!r}")

    def close(self) -> None:
        with self._lock:
            file, self._file = self._file, None
        if file is not None:
            try:
                file.close()
            except Exception:  # noqa: BLE001
                pass

    def _warn(self, message: str) -> None:
        """Tell the operator once, on stderr; later problems stay silent."""
        if self._warned:
            return
        self._warned = True
        try:
            print(f"relay: event log: {message}", file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001
            pass
