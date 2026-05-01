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

If you navigate into a sub-view and need to go back / dismiss a modal /
exit fullscreen / close a popover, ALWAYS try `key: escape` FIRST. It's
the cheapest one-shot dismiss and most desktop apps wire it to "close
the topmost layer" — modals, command palettes, fullscreen video, image
viewers, search overlays, dropdowns all respond to it. One key press,
no localization needed, no risk of mis-clicking adjacent UI.

Only fall back to a visible back affordance when escape doesn't work
(the screenshot after pressing it is unchanged):
- Look for an in-app back arrow — usually `<` or ‹ or a left-pointing
  chevron near the top-left of the content area.
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

## Follow buttons in social apps (Douyin / Weibo / Twitter / Instagram / etc.)

Follow buttons in social apps are toggles. The label and color encode
the CURRENT relationship, not the action a click would take:

  • Currently following an account → button reads "已关注" / "Following"
    / "Subscribed", typically gray / outlined / low-emphasis.
  • Currently NOT following an account → button reads "关注" / "Follow"
    / "Subscribe", typically red / blue / solid-filled / high-emphasis.

A click flips the state, so the visible label is also a record of what
just happened: a row that now shows the red/Follow style is one whose
follow relationship was just removed (or never existed).

## CAPTCHA challenges

If a CAPTCHA modal appears (slider puzzle, image-grid quiz, "drag the
piece into place", "click all images containing X", "rotate to upright",
etc.), attempt to SOLVE it. Do not click "refresh / 刷新 / 换一张" as a
first move — refresh just rolls a different challenge of the same type
and burns turns.

Common patterns and how to attack them:

  • Slider / drag puzzle ("拖动滑块完成拼图" or a notch-into-shape):
    estimate the gap's x position and emit a `drag` action from the
    slider handle's current x to the gap's x at the same y. If a drag
    action isn't in your action schema, fall back to clicking the gap
    target — some puzzles accept that.
  • Click-sequence quiz ("依次点击 A、B、C" / "click these characters
    in order"): localize each named target in turn and click them in
    the requested sequence within the same turn-budget.
  • Image grid ("select all images containing X"): click each matching
    cell, then click the confirm button.
  • Rotation / orientation ("rotate the object upright"): use the
    rotate handle (usually a circular arrow) to nudge toward upright.

Only fall back to `刷新` / refresh AFTER a genuine solve attempt failed
(the modal didn't dismiss). Two failed solves in a row → the task is
likely blocked; report and stop instead of looping refresh forever.

## Opening a user's profile from a list

When the task wants you to open a specific user's profile from a list
(following / followers list, comment authors, search results, suggested
users, etc.) — ALWAYS click the user's AVATAR, not the username text or
any other row element. Reasoning:

  • Avatars are the largest consistently-clickable element in a row
    (~40-60 px squares vs ~12-16 px tall text), so coordinate slop
    matters less.
  • Avatar hit-regions are reliably wired to "open profile" across
    almost every social product. Username text sometimes opens a
    hover-card, sometimes just selects the text, sometimes does
    nothing — depends on the platform.
  • Other row elements (the right-aligned follow / unfollow button,
    a "remove" / "..." menu, a verified badge) do entirely different
    things. Clicking near them by mistake mutates state instead of
    just navigating.

In your action's `x`/`y`, target the visual CENTER of the avatar, not
its edge. If the row also has a follow/unfollow button you can see,
make sure your x is well to the LEFT of that button so a small coord
slop doesn't bleed into it.

## Avatar-with-plus follow indicator (short-video players)

In a single-video / full-screen video player (TikTok, Douyin, YouTube
Shorts, Instagram Reels, etc.), the creator's avatar sits in the
right-side action rail. A small "+" badge (usually red, half-overlapping
the avatar's bottom edge) signals the current follow relationship:

  • "+" badge present on the avatar → you do NOT follow this creator.
    Clicking the badge follows them; the badge then disappears.
  • Avatar with NO "+" badge → you ALREADY follow this creator. There is
    no separate "已关注" label here — the absence of the "+" IS the
    indicator. Do not click the avatar expecting it to follow.
