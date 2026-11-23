"""A journal of every write to Hevy, so a bad edit can be put back by hand.

Hevy's API can't delete or undo. Before a write is sent, the journal records
who asked for it, what is being sent and, for an update, the record as Hevy
had it. Afterwards it records how the write ended. Each entry is one line of
JSON; the two entries of a write share an ``op`` id.

Entries go to the file at ``HEVY_JOURNAL_PATH``, or to the log when that is
unset. A file outlives the container, whose log is lost when it is recreated.
With a file, a write is refused if its first entry can't be saved: a change
that can't be undone isn't made without a record of what it replaced.
"""

import json
import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hevy_mcp.hevy import HevyError

logger = logging.getLogger(__name__)


class JournalError(HevyError):
    def __init__(self, reason: str) -> None:
        super().__init__(
            f"The write journal can't be saved ({reason}), so nothing was sent to Hevy. "
            "Fix the journal file, or unset HEVY_JOURNAL_PATH to journal to the log."
        )


def _line(entry: dict[str, Any]) -> str:
    # One line, with every control character escaped, so text from a tool call
    # can't start a new journal entry or log line.
    return json.dumps(entry, ensure_ascii=True, separators=(",", ":"), default=str)


class Journal:
    def __init__(self, path: Path | None) -> None:
        self._path = path

    def check(self) -> None:
        """Make sure the journal file can be appended to. Raises JournalError."""
        self._append("")

    def begin(self, **entry: Any) -> str:
        """Record a write that is about to be sent, and return its op id.

        Raises JournalError, before anything reaches Hevy, if the entry can't
        be saved to the journal file.
        """
        op = secrets.token_hex(4)
        self._write({"op": op, "event": "attempt"} | entry)
        return op

    def end(self, op: str, outcome: str, **entry: Any) -> None:
        """Record how a write ended. The write has happened by now, so a journal
        file that can't be written is logged and not raised."""
        try:
            self._write({"op": op, "event": "result", "outcome": outcome} | entry)
        except JournalError as exc:
            logger.error("Write %s ended %s, but its journal entry was lost: %s", op, outcome, exc)

    def _write(self, entry: dict[str, Any]) -> None:
        entry = {"ts": datetime.now(UTC).isoformat(timespec="seconds")} | {
            key: value for key, value in entry.items() if value is not None
        }
        if self._path is None:
            logger.info("%s", _line(entry))
            return
        self._append(_line(entry) + "\n")
        # The log gets a pointer, never the record: its contents stay in the file.
        logger.info(
            "%s %s %s",
            entry["op"],
            entry["event"],
            " ".join(f"{key}={entry[key]!r}" for key in ("tool", "id", "outcome") if key in entry),
        )

    def _append(self, text: str) -> None:
        assert self._path is not None
        try:
            # Owner-only: the journal holds workout and body-measurement data.
            descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
        except OSError as exc:
            raise JournalError(f"{type(exc).__name__}: {exc.strerror or exc}") from exc
