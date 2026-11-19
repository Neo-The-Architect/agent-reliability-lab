"""Crash-conscious, append-only operation journal. Python stdlib + Linux flock.

This is a standalone *public teaching implementation*, NOT the private
Adinkra ICM runner. It performs no external actions or model inference.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ZERO_HASH = "0" * 64


class IntegrityError(RuntimeError):
    """Authoritative evidence cannot be safely trusted."""


class OutcomeUnknown(RuntimeError):
    """An operation may have run, but no durable result proves its outcome."""


def _encode(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf8")


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(_encode(value)).hexdigest()


class Journal:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "events.jsonl"
        self.lock_path = self.root / "journal.lock"

    @contextmanager
    def _lock(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_verified(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise IntegrityError("torn journal tail; requires independent reconciliation")
        previous = ZERO_HASH
        entries = []
        for number, line in enumerate(raw.splitlines(), 1):
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                raise IntegrityError(f"invalid journal record {number}") from exc
            if entry.get("seq") != number or entry.get("previous_hash") != previous:
                raise IntegrityError(f"record {number} breaks the monotonic hash chain")
            actual = _digest({k: v for k, v in entry.items() if k != "hash"})
            if actual != entry.get("hash"):
                raise IntegrityError(f"record {number} has a mismatched content digest")
            if entry.get("event") not in {"intent", "result"}:
                raise IntegrityError(f"unrecognized event {number}")
            previous = entry["hash"]
            entries.append(entry)
        return entries

    def inspect(self) -> list[dict[str, Any]]:
        with self._lock():
            return self._read_verified()

    def append(self, *, operation: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not operation or len(operation) > 128 or not operation.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid operation identity")
        if event not in {"intent", "result"}:
            raise ValueError("unknown event")
        with self._lock():
            prior = self._read_verified()
            existing = [x for x in prior if x["operation"] == operation and x["event"] == event]
            if existing:
                if existing[0]["payload"] == payload:
                    return {**existing[0], "duplicate": True}
                raise IntegrityError("conflicting evidence under an existing operation identity")
            intent = next((x for x in prior if x["operation"] == operation and x["event"] == "intent"), None)
            if event == "result" and intent is None:
                raise IntegrityError("result cannot be written without a durable intent")
            if event == "result" and payload.get("input_digest") != intent["payload"]["input_digest"]:
                raise IntegrityError("result does not match the original operation input")
            record = {
                "seq": len(prior) + 1,
                "previous_hash": prior[-1]["hash"] if prior else ZERO_HASH,
                "operation": operation,
                "event": event,
                "payload": payload,
            }
            record["hash"] = _digest(record)
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                written = os.write(fd, _encode(record) + b"\n")
                if written != len(_encode(record)) + 1:
                    raise OSError("incomplete journal append")
                os.fsync(fd)
            finally:
                os.close(fd)
            return record

    def begin(self, operation: str, input_data: bytes) -> dict[str, Any]:
        """Prepare an operation; do NOT execute a second time after an uncertain intent."""
        request_digest = hashlib.sha256(input_data).hexdigest()
        with self._lock():
            previous = self._read_verified()
            intent = next((x for x in previous if x["operation"] == operation and x["event"] == "intent"), None)
            result = next((x for x in previous if x["operation"] == operation and x["event"] == "result"), None)
            if intent is not None:
                if request_digest != intent["payload"]["input_digest"]:
                    raise IntegrityError("existing operation has a different input")
                if result is None:
                    raise OutcomeUnknown("intent exists without completion proof; automatic replay prohibited")
                return {"status": "completed", "invoked": False, "result": result["payload"]}
        # append() independently serializes concurrency. A production dispatcher must lock
        # the full check-and-dispatch window, not just this journal operation.
        record = self.append(
            operation=operation, event="intent", payload={"input_digest": request_digest}
        )
        if record.get("duplicate"):
            raise OutcomeUnknown("concurrent intent exists; do not dispatch twice")
        return {"status": "admitted", "invoked": False, "input_digest": request_digest}

    def complete(self, operation: str, input_data: bytes, result: bytes) -> dict[str, Any]:
        """Persist a deterministic result *after* the authorized operation has completed."""
        return self.append(operation=operation, event="result", payload={
            "input_digest": hashlib.sha256(input_data).hexdigest(),
            "result_sha256": hashlib.sha256(result).hexdigest(),
            "result_bytes": len(result),
        })
