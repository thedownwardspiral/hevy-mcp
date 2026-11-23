---
status: accepted
---

# Every write is journaled before it is sent, and refused if the journal file can't be written

Hevy's API can't delete or undo, and its PUTs replace whole records. A wrong update loses the old values for good, and the tool-call log only says who called which tool. So every write tool now journals two entries, one line of JSON each: before the write, who asked, what is being sent and, for an update, the record as Hevy returned it; afterwards, how it ended (`ok`, `rejected` or `uncertain`) and the id of anything created. That is enough to put a record back by hand.

The first entry is written before the request, not after. If it can't be saved to the journal file, the write is refused and nothing is sent to Hevy. The second entry is best effort: by then the write has happened, so a failure to record it is logged and the tool still succeeds.

Entries go to the file at `HEVY_JOURNAL_PATH`, or to the log when that's unset. The Compose file sets the path and keeps it in a named volume, because a container's log is lost when it's recreated, which is how every upgrade is deployed.

## Considered options

- **Journal to the log only.** Rejected as the deployed default: the snapshots would vanish at the next `docker compose up` after a pull. It stays the fallback when no path is set, so the server needs no storage to run.
- **Journal after the write.** Rejected: a full disk or a wrong volume permission would then lose exactly the entry that matters, after the change can no longer be stopped.
- **Carrying on when the journal file can't be written.** Rejected for the same reason. The server also checks the file at startup, so a bad path stops it there and not on the first write.
- **A restore tool that replays a `before` snapshot.** Left out for now. A restore is itself a write through `update_*`, and the journal has what it needs.

## Consequences

- This is the server's only persistent state. It's optional, and read-only deployments never touch it.
- The journal holds workout and body-measurement data. The file is created readable only by its owner, and with a file configured the log gets a one-line pointer per entry (op id, tool, record id, outcome), never the record. With no file, the full entries are in the log.
- The journal never holds the API key or an Access token: entries are built from the tool's arguments and Hevy's response bodies.
- A write Hevy refused (a 4xx) is journaled as `rejected`: nothing changed. Anything else that isn't a clean success is `uncertain`, including a response the server couldn't read, so the journal never claims a change didn't happen when it might have. That covers a refusal of a retried update whose earlier try timed out or got a 5xx: Hevy may have saved that try.
- An append that is cut short, by a full disk for example, leaves part of an entry in the file. The next entry starts on a new line, so the fragment stays a broken line of its own and every later entry is still valid JSON.
- Journal entries are logged whatever `LOG_LEVEL` is, so a quiet log level can't drop them when the log is the only place they go.
- Calls that never reach a write (invalid input, an update whose record isn't found) aren't journaled.
- The file is never rotated or trimmed. At a few kilobytes per write, a single user's journal grows slowly.
- Text from a tool call can't forge an entry: each entry is one JSON line with control characters escaped.
