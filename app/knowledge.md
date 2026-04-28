# Phantom-Click knowledge base

Generic, cross-app procedural tips appended to the system prompt every turn.
Phantom-Click drives ARBITRARY apps via vision; do not add app-specific
flows here — they bias the model and hurt generality. Keep entries to
patterns that apply broadly across desktop apps.

## Launching an app on macOS

If the task names an app and the screenshot doesn't already show that app:
1. `key: cmd+space` — opens Spotlight.
2. `type: <app name>` — using the app's native script (e.g. Chinese
   characters for a Chinese app, not its English alias).
3. `key: enter` — launches the highlighted result.

The runner keeps the named target app frontmost on subsequent turns once
launched, so don't switch away.

## Returning to a previous view

If you navigate into a sub-view and need to go back:
- Look for an in-app back arrow in the current screenshot — usually `<` or
  ‹ or a left-pointing chevron near the top-left of the content area.
- Some apps have a sidebar entry that returns you home; if the current
  screenshot shows one, click it.
- If neither is visible in the current screenshot, the screen may need a
  scroll up or a different navigation step entirely. Don't guess at a
  control that isn't there.

## Working through a list of items

When the task involves processing N items in a list (e.g. "for each of
my followed accounts, …"):
- Maintain `progress.examined_names` (or similar) so re-renders of the
  list don't make you re-process items you already handled.
- Scroll within the list when the visible items are exhausted; the
  runner's `scroll` action moves the cursor to about the middle of the
  visible content area before dispatching the wheel event so it scrolls
  the right element.
