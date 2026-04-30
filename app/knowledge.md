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

## Follow / unfollow toggles in social apps

Follow buttons are TOGGLES — one click flips their state. On every
social platform (Douyin, Weibo, Twitter/X, Instagram, YouTube, Bilibili,
TikTok) the label inverts the moment you click:

  • Currently following → button reads "已关注" / "Following" /
    "Subscribed" (usually GRAY, outlined, low-emphasis).
  • Currently NOT following → button reads "关注" / "Follow" /
    "Subscribe" (usually RED or BLUE, solid-filled, high-emphasis).

When the task is **unfollow** (取消关注 / unfollow / unsubscribe):
- ONLY click GRAY "已关注" / "Following" buttons. NEVER click RED/BLUE
  "关注" / "Follow" buttons — those are accounts you (or someone) have
  ALREADY unfollowed, and clicking them would RE-FOLLOW, undoing your
  work.
- After an unfollow click, that row's button immediately flips to the
  opposite RED "关注" state. DO NOT re-click it.
- If the list shows a mix of "已关注" (gray) and "关注" (red), the red
  rows are already-unfollowed. Skip them, target only gray rows.
- Track unfollowed account NAMES in your progress checklist so closing
  and reopening the list doesn't trick you into reprocessing.

When the task is **follow**, the rules mirror: only click RED/BLUE
"关注" / "Follow" buttons; gray "已关注" rows are already done.

Same symmetric rule for like / save / subscribe / mute toggles — read
the current label, decide whether the action you'd take matches the
task, click only when the label says "not yet". Then move on.
