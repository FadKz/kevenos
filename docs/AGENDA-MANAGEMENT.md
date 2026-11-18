# Managing the event agenda

Open **Event Administration → Agenda**. This edits the selected Live or Rehearsal workspace. Use the Rehearsal pill in the navigation bar to check which one you are working in.

## Add a break during the day

1. Choose **Add break** and choose the section it should appear before. Use the end option to append it.
2. Set the title and duration. Add any message or instructions attendees should see.
3. Preview the change. Review every changed start/end, the new finish time, and the display warning.
4. Apply the reviewed change. Later sections move by the break's duration; their own durations and existing gaps stay intact.
5. If the programme is manual, open **Event Operations** and select the break when it should appear on the live screen. Agenda editing does not silently advance a manual display.

The editor can also add ordinary sections, edit start time/duration/text/scene, move a section, and remove one. Shortening or removing a section brings later sections forward. Reordering retains the day start and the gaps between timetable positions. The preview is the authority for the exact result.

## During the event

- In manual mode, the current section and its actual start are preserved by stable identity, including when its position changes. Select a different section in Event Operations before removing the current one.
- In clock mode, the event host's local clock chooses the section from the new timings immediately. Review the before/after current section and timezone. If the clock crosses a section boundary before applying, preview again.
- Changing the current section's title, instructions or scene updates the live content immediately. Title or scene changes can also affect quiz availability.
- Judging timers, scores and Final state remain separate. Adjust them in their operational dashboard if needed.
- The planner prevents overlaps, negative times, next-day overflow, invalid pitch capacity, duplicate IDs and an empty agenda. Newly inserted sections require at least one minute.
- Existing zero-minute markers take no time. They may share the following section's start; in clock mode the later ordered section wins that instant, so the marker has no separate display window. Manual mode can still select it.
- A changed agenda, configuration, current programme or workspace makes a preview stale. Refresh and review again. After an uncertain network response, check the original receipt; do not repeat the same change with a new request ID.

## API and MCP

The common API and both local and remote MCP expose `schedule.edit_preview` / `schedule_edit_preview` (read) and `schedule.edit` / `schedule_edit` (write). Read access requires `content:read`; applying requires `content:write` and `content:read` (`event:read` is needed for the separate programme-state read). A tool's presence does not bypass server-side scope checks.

Read `schedule.items` first and use its stable IDs. Preview one edit:

```json
{
  "edit": {
    "action": "insert",
    "before_id": "<ID from schedule.items>",
    "item": {"title": "Coffee break", "min": 15, "scene": "info"}
  },
  "note": "Additional break requested by event operator"
}
```

`before_id` is optional for insert/move; omit it to append. Other operations:

- Update: `{"action":"update","id":"<ID>","changes":{"min":25,"time":"11:10"}}`. The duration is the new total, not an increment. Text fields are `title`, `line`, `who`, `do` (array of instructions) and `scene`.
- Remove: `{"action":"remove","id":"<ID>"}`.
- Move: `{"action":"move","id":"<ID>","before_id":"<anchor ID>"}`.

Show `changes`, `warnings`, `blockers`, `program`, `clock_timezone`, and the old/new event finish. Do not apply a blocked plan. Copy `plan_digest` into the exact original preview arguments; prepare `schedule.edit` using the preview envelope's context/revision, get operator approval, and execute with its confirmation ID, `confirm:"EXECUTE"` and one UUID idempotency key. Reuse that key only for the identical request, or inspect its receipt when the outcome is uncertain. Read the schedule and programme back after success. Content history is available for a separately reviewed restore.

The existing `schedule.retime_preview` and `schedule.retime` remain available when an agent needs the explicit choice to keep following starts fixed. The Admin editor always cascades later timings.
