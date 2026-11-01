---
status: accepted
---

# Write tools are opt-in, and updates merge into what Hevy already has

The server started read-only, so an assistant couldn't change Hevy data by accident. Logging workouts and measurements by talking to Claude is worth having, so the server now has tools for all eight of Hevy's write endpoints: create and update for workouts, routines and body measurements, and create for routine folders and custom exercises. Hevy's API has no delete endpoints, so neither does the server.

The write tools are only registered when `HEVY_ENABLE_WRITES=true`. With it off, the default, the tool list is exactly the read-only one, so a deployment only gets writes when someone sets the flag.

Hevy's PUT endpoints replace the whole record: a body-measurement field left out becomes null, and a workout or routine's exercises are replaced. An assistant that sends only the field it means to change would wipe the rest. So the update tools take only the fields to change. The server reads the current record, applies those changes and sends the whole result. A list of exercises, when given, replaces the old list, because there's no stable id to match one set against another.

## Considered options

- **Writes always on.** Rejected: anyone pulling a new image would get write access without choosing it.
- **Updates that send exactly what the caller gives (full replace).** Rejected: an incomplete call would erase data, with no delete or undo in Hevy's API to recover it.
- **Retrying every write like a read.** Rejected for creates: if Hevy's answer is lost after the request arrived, a retry can create a duplicate, and the API can't delete it. Creates are retried only when Hevy can't have acted (a 429, or a connection that never opened). Otherwise the error says to check before trying again. Updates are idempotent and are retried as before.

## Consequences

- Hevy never returns a workout's `is_private` or a routine's `notes`, so a merge can't carry them over. `update_workout` makes `is_private` a required argument, because Hevy resets a workout to public when it's left out. A live test showed Hevy erases a routine's notes the same way, so `update_routine` requires `notes` too.
- An update costs two Hevy requests (a read, then the write), and another change made between the two would be overwritten.
- Hevy answers 403 both for a bad API key and for "routine limit" or "custom exercise limit". On those two create endpoints, 403 is reported as the limit, not as an auth failure.
- Creating a custom exercise empties the exercise-template cache, so `search_exercise_templates` finds it straight away.
- A live test showed Hevy refuses some nulls its own `GET`s return: a routine set's `rep_range`, and every empty body-measurement field. The merged bodies leave those out instead, which for body measurements is also how `clear` empties a field. The SPEC lists these and the other places the live API differs from its document.
- The tool annotations tell clients which tools write: creates aren't read-only, updates are also destructive and idempotent. Clients such as Claude can ask before running them.
