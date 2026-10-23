"""EventLog: one JSON line per call, audio payloads replaced by their lengths, never raises (offline)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from relay.eventlog import EventLog, redact

B64 = "QUJD" * 400  # a long base64-looking payload (1600 chars)


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_writes_one_json_line_per_call_with_time_and_kind(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "logs" / "x.jsonl")
    log("realtime_status", {"status": "ready", "detail": None})
    log("deck_in", {"msg": {"type": "key", "key": "space"}})
    log.close()
    first, second = lines(tmp_path / "logs" / "x.jsonl")
    assert first["kind"] == "realtime_status" and first["status"] == "ready" and first["detail"] is None
    assert isinstance(first["t"], float) and 0 <= first["t"] <= second["t"]
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}", first["ts"])
    assert second["msg"] == {"type": "key", "key": "space"}


def test_flushes_every_line(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "x.jsonl")
    log("one", {})
    assert lines(tmp_path / "x.jsonl")[0]["kind"] == "one"  # readable before close()
    log.close()


def test_audio_payloads_become_lengths_at_any_depth(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "x.jsonl")
    event = {"type": "response.output_audio.delta", "delta": B64}
    log("realtime_in", {"event": event, "msg": {"type": "mic", "pcm": B64}, "audio": B64,
                        "list": [{"delta": B64}]})
    log.close()
    record = lines(tmp_path / "x.jsonl")[0]
    assert record["event"]["delta"] == len(B64)
    assert record["msg"]["pcm"] == len(B64)
    assert record["audio"] == len(B64)
    assert record["list"] == [{"delta": len(B64)}]
    assert event["delta"] == B64  # the caller's dict is untouched (it is still dispatched after logging)


def test_text_deltas_and_config_objects_are_kept() -> None:
    caption = {"type": "response.output_audio_transcript.delta", "delta": "Sure, think of me as an actor"}
    long_caption = {"delta": "word " * 40}
    config = {"audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}}}}
    assert redact(caption) == caption
    assert redact(long_caption) == long_caption  # has spaces: text, not audio
    assert redact(config) == config
    assert redact({"pcm": "short"}) == {"pcm": "short"}
    assert redact({"data": b"\x00\x01\x02"}) == {"data": 3}


def test_reserved_keys_are_renamed_not_lost(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "x.jsonl")
    log("k", {"kind": "other", "t": 5, "ts": "x"})
    log.close()
    record = lines(tmp_path / "x.jsonl")[0]
    assert record["kind"] == "k" and record["data_kind"] == "other" and record["data_t"] == 5
    assert record["data_ts"] == "x" and isinstance(record["t"], float)


def test_never_raises(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "x.jsonl")
    log("unserialisable", {"value": object(), "path": tmp_path})  # falls back to repr()
    log("bad keys", {("a", "b"): 1})  # json cannot encode tuple keys: the record is dropped
    log("none", None)
    log("not a dict", ["a", B64])  # type: ignore[arg-type]
    log("deep", _nested(5000))  # RecursionError inside redact: dropped
    log("after", {"ok": True})
    log.close()
    log("after close", {"ok": True})  # a no-op, not an error
    log.close()
    kinds = [record["kind"] for record in lines(tmp_path / "x.jsonl")]
    assert kinds == ["unserialisable", "none", "not a dict", "after"]
    assert lines(tmp_path / "x.jsonl")[2]["data"] == ["a", B64]  # only pcm/audio/delta values count as audio


def test_unwritable_path_is_a_quiet_no_op(tmp_path: Path, capsys) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    log = EventLog(blocker / "logs" / "x.jsonl")  # parent is a file: cannot create the directory
    log("anything", {"a": 1})
    log.close()
    assert "event log" in capsys.readouterr().err  # told once on stderr
    EventLog(None)("anything", {"a": 1})  # no path: no-op


def test_for_talk_names_the_file_after_the_talk_and_start_time(tmp_path: Path) -> None:
    log = EventLog.for_talk("relay-intro", out_root=tmp_path)
    log("server_start", {})
    log.close()
    assert log.path is not None and log.path.parent == tmp_path / "logs"
    assert re.fullmatch(r"relay-intro-\d{8}-\d{6}\.jsonl", log.path.name)
    assert lines(log.path)[0]["kind"] == "server_start"


def _nested(depth: int) -> dict:
    node: dict = {}
    root = node
    for _ in range(depth):
        node["x"] = {}
        node = node["x"]
    return root
