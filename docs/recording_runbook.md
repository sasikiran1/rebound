# Recording runbook: the live screen

Goal: show that customer permission survives an outage, changed availability and a worker crash.

## Before recording

1. Open only one demo tab: `http://127.0.0.1:8092/`. Every open tab drives the same session.
2. Check the model name (top right, and in the Story-view caption). The narration below assumes Muse Spark. If the
   screen shows qwen3-14b, the caption says "(local)": call it the local model and label Muse evaluation results
   separately.
3. If the running server already uses the intended model, keep it. Otherwise start it from WSL in `~/rebound`:

   ```bash
   scripts/resolve_live_ui.sh --model muse-spark-1.3 --max-calls 20 --max-cost 0.45 --case D1
   ```

   The model key must already be in `.env`. The script stops the `resolve-worker` container so it cannot take the
   demonstration's requests. Scenario 2 costs 2 model calls per take on Muse (the opening message and the reply; a
   plain "yes" needs no model call), about $0.02 to $0.03.
4. Dismiss the introduction.
5. Select "2 · Messy request + system down + slot taken + crash" (already selected when started with `--case D1`).
6. Click **Rebound** explicitly: changing scenarios keeps the previous lane.
7. Rehearse once before recording.

## Recommended recording method

Record the screen first; add narration afterwards, following the events that actually appear.

Click **Story view**, then **▶ Play scenario**. The page sends the case file's customer messages and stages the
outage, the taken slot, the confirmation and the crash. Do not type messages or press fault buttons during
autoplay.

Story view shows a caption in the bottom-right corner: "Synthetic customer · real model, API and faults · model:
<name> · recorded run". If you cut waiting time in editing, add "Waiting shortened". Keep the order of events and the
actual outcomes.

## Manual walkthrough (rehearsal, or a controlled recording)

Use Full view: Story view hides the fault buttons and the message chips.

1. Select scenario 2 (case D1) and click **Rebound**.
2. Click **Booking system down**.
3. Send: "I need someone after three this week. Dana if possible. Don't push it into next week without checking with
   me."
4. Wait until the screen shows a failed attempt or a retry (step 5 turns amber: "booking system is down: waiting
   safely").
5. Click **Someone takes the slot**: Friday 15:30 with Dana turns amber, "taken".
6. Click the outage button again to restore service.
7. Wait for Rebound's question. On the local model it reads "Nothing open fits … Is there anything you can change";
   the wording varies by model, but it asks rather than booking outside her conditions.
8. Send: "Monday is okay, but not before four."
9. Wait for the exact-slot confirmation. Expected: Monday Nov 16 at 4:30 PM with Alex (purple, "proposed").
10. Click **Crash after booking**.
11. Send: "Yes, please book it."
12. Show the crash (step 5 red), the lease wait and the recovery. Let it finish.
13. Show CORRECT, one database change, and the `CAL` marker on Mon 16:30 in Alex's column.
14. Briefly show Full view for the database row and the replay evidence; return to Story view for the final shot.

## Five moments to capture clearly

- Maya's original conditions.
- Dana's slot becoming unavailable.
- Rebound asking, and receiving the Monday-after-four correction.
- The worker crashing and recovering.
- One database change, the correct final appointment, and the calendar read-back.

Do not claim an outcome before it appears.

## Fallback

- If the model proposes an unexpected slot, read it before confirming. Correct it honestly, or stop that attempt.
- If the run stalls, use a previously captured successful run and label it "Previously recorded execution". Never
  present retained footage as a fresh live run.
- If the one-shot baseline gets it right, say so. The main evidence is the complete workflow and the recorded
  comparison, not a guaranteed baseline failure.

## Afterwards

```bash
docker compose -f docker/compose.yml --profile resolve start resolve-worker
```

Last checked on Nov 3, 2026, local model: scenario 2 through Play, clicked in a browser, ended CORRECT with one change,
the crash recovered with `replayed`, and the event in Alex's calendar.
