---
status: accepted
---

# A plan preview saves exactly what it showed, once

`create_plan` writes as soon as the model calls it. The confirmation before that happens in prose ("here's the plan, shall I create it?"), and the user has to read exercise template ids and kilogram weights back out of the model's summary. Hevy's API can't delete, so a wrong exercise or a typo in a weight stays until someone removes it in the Hevy app. Hevy's own Claude connector shows plans and routines as cards before saving them, and that was the main thing hevy-mcp was missing.

`preview_plan` takes the same arguments as `create_plan` and writes nothing. It checks the plan, keeps it in memory under a random one-time `preview_id`, and shows it as an MCP Apps UI built with Prefab (`fastmcp[apps]`): each routine's exercises by name, the planned sets in the user's units, rest times and supersets, and a **Save to Hevy** button. The model gets an outline and the `preview_id`.

The button posts a chat message, "Save the plan 'X' to Hevy (preview …)". The model then calls `save_plan(preview_id)`. The user can also just say "save it". `save_plan` takes the stored plan out of memory and saves that, so:

- what is saved is what was shown, not whatever the model passes the second time;
- a preview saves at most once, so a repeated message or a retry can't create a duplicate plan that the API can't delete. The one exception is a save that Hevy refused before writing anything (the routine folder's create was refused outright): the preview stays, so the same plan can be saved again.

Saving goes through the same code as `create_plan`, journaled under `save_plan`, with the same message when it stops partway.

## Why the button doesn't save by itself

The first version had the button call a UI-only `save_plan` tool directly (`CallTool` from a `FastMCPApp`), so a click saved with no model in the loop. In Claude the card rendered, but the click failed with "Method not found" and never reached the server. It failed the same way after `save_plan` was made visible to the model and Claude listed it. So in Claude, a custom connector's card can't call the connector's tools. A chat message from the card (`ui/message`) is the path left, so the model does the save. Hevy's own connector has separate `render_hevy_*` and `create_hevy_*` tools, which suggests it works the same way.

## Considered options

- **The `Approval` provider from FastMCP** (an Approve/Reject card whose answer the model acts on with `create_plan`). Rejected: the model writes a second time from its own arguments, so the approved plan and the saved plan can differ, and the card can't show the plan.
- **A button that calls the save tool itself.** Tried first, and it's what the MCP Apps spec allows. Claude refuses it, as described above. It could come back if Claude starts forwarding card tool calls for custom connectors; `save_plan` would stay as it is.
- **Keeping previews somewhere durable.** Not worth it: a preview is only useful for the minutes it's on screen. A restart forgets them, and the model shows the plan again.
- **Replacing `create_plan`.** Not yet: it stays for callers that already use it. Clients that can't show app UIs get the plan as text from `preview_plan` and save it with `save_plan` the same way.

## Consequences

- The model, not the user's click, calls `save_plan`. It's told to wait for the user's confirmation, and like every write tool it's annotated as a write, so Claude can ask before running it. The guarantee is about what gets saved and how often, not about who presses the last button.
- Previews live in one process's memory: at most 50 at a time, each for an hour. Several replicas would need a shared store.
- A preview costs a user-info request for the units, and the exercise templates come from the cache that search already uses. An id the cached list lacks (it may be cut short by the page cap, or older than a template made in the Hevy app) is looked up on its own, and one that isn't a template is refused before anything is shown, since saving it would leave a folder behind.
- The model gets the whole plan in the user's units, as text, along with the card, so it can show the plan if the card doesn't appear. A client without app UIs gets that same text, which has everything `save_plan` writes: notes, sets, rest times and supersets.
- The renderer page loads Prefab's version-pinned assets from its CDN, and Claude loads them. `prefab-ui` is pinned exactly, as FastMCP advises, because it often breaks between versions.
- Only plans have a preview so far. A single routine, or an update with a before-and-after view, can reuse `plan_preview.py`.
