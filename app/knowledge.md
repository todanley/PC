# Phantom-Click knowledge base

Procedural tips appended to the system prompt every turn. Two kinds of
entries:

- **General patterns** — apply broadly across apps (launching, navigating
  back, list traversal, toggle buttons). Keep these app-agnostic.
- **App-specific flows** — concrete navigation/known quirks for a particular
  site or app (e.g. Douyin). These are allowed and encouraged: they save the
  model from rediscovering a multi-step flow every run and from re-learning a
  quirk that already cost a debugging session. Put them under "## App: <name>"
  headings so they only inform the model when that app is in play.

When you (or an operator) discover something non-obvious about how a specific
app behaves — a navigation path, a confirm-dialog, a two-step button — record
it here under the right "## App:" heading so the next run starts knowing it.

## Launching an app on macOS

If the task names an app and the screenshot doesn't already show that app:
1. `key: cmd+space` — opens Spotlight.
2. `type: <app name>` — using the app's native script (e.g. Chinese
   characters for a Chinese app, not its English alias).
3. `key: enter` — launches the highlighted result.

The runner keeps the named target app frontmost on subsequent turns once
launched, so don't switch away.

## Returning to a previous view

Navigate with the MOUSE, not the keyboard. If you go into a sub-view and
need to go back / dismiss a modal / exit fullscreen / close a popover,
click a visible control — do NOT press `key: escape` (the runner rejects
it) or any other keyboard shortcut:
- Look for an in-app back arrow — usually `<` or ‹ or a left-pointing
  chevron near the top-left of the content area — and click it.
- To dismiss a modal / popover, click its `×` / close button, or click
  empty space outside it.
- Some apps have a sidebar entry that returns you home; if the current
  screenshot shows one, click it.
- If none is visible in the current screenshot, the screen may need a
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

  • Slider / drag-into-shape puzzle ("拖动滑块完成拼图", "拖动完成上方
    拼图", or a piece-into-matching-shadow): the runner tags the puzzle
    PIECE and each candidate GAP/shadow with magenta marks. Do NOT try to
    drag by eye. Instead identify the piece's mark and the GAP mark whose
    SHAPE matches the piece, then emit
    `{"action":"slide_captcha","piece_mark":<piece>,"gap_mark":<gap>}` —
    the runner computes the exact distance and drags the slider handle.
    If the puzzle image is still blank/loading (加载中), `wait` first.
    If no marks appear on the puzzle, fall back to a manual `drag` from
    the slider handle rightward toward the matching shadow.
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

## Toggle actions may take more than one click — always verify state

A button that toggles state (follow / like / save / subscribe / mute) does
NOT always flip on the first click. Common reasons: the first click only
focuses/hovers the control, or the app pops a confirmation ("确定取消关注？"
/ "Unfollow?") that needs a second click to confirm. So:

- After clicking a toggle, READ THE NEXT SCREENSHOT and confirm the button's
  label/colour actually changed to the new state before you count it done.
- If it still shows the OLD state, click the SAME button once more (or click
  the confirm button if a dialog appeared).
- Once it shows the NEW state, STOP — clicking again just undoes it. Never
  click a control that is already in the state you want.

## App: Douyin (douyin.com) — managing who you follow / unfollowing

To see or manage the accounts you follow (your "Following" roster), do NOT
use the 关注 item in the left sidebar — that opens the following FEED
(videos/lives), not the list of accounts. Instead:

1. Click **我的** (My) in the left sidebar to open your own profile.
2. In the profile header stats line, click the **关注** count NUMBER — the
   DIGITS immediately after the small grey "关注" label (e.g. the "54"). The
   stats line reads left-to-right: **关注 N** (following) → **粉丝 N** (fans) →
   **获赞 N** (likes); you want the FIRST one (关注), not 粉丝/获赞. Clicking the
   关注 count opens the **Following roster modal** — a scrollable popup listing
   each followed account.
   ⚠️ DISTRACTORS to the RIGHT of the 关注 count, in order: a "**N人正在直播**"
   live badge, then the **粉丝** (fans) count, then **获赞**. The 关注 count is
   the LEFT-MOST number in the row. If your click misses, do NOT just "move
   right" — that overshoots onto 正在直播 or 粉丝 (which opens the FANS list).
   The "关注 N" is usually a single tagged element; **click that MARK directly**
   rather than guessing a coordinate. If you must use x/y, aim at the digits
   touching the 关注 label and, if anything, nudge slightly LEFT, never right.
   ✅ VERIFY THE RIGHT LIST OPENED: the modal title must read **关注 / Following**
   and its rows must show GREY **已关注 / 相互关注** buttons. If the rows show RED
   **关注 / 回关** buttons or the title says **粉丝 / Fans**, you opened the FANS
   list by mistake — close it (click the × or outside the modal) and re-click
   the 关注 count, further LEFT this time. Do not start scanning a fans list.
3. Each row has: avatar (left), username + bio, and a button on the right:
   - **已关注** (grey) = you currently follow them.
   - **相互关注** (grey) = mutual follow.
   - **关注** (red) = you are NOT following (red means "tap to follow").
4. To **unfollow** an account, click its 已关注 / 相互关注 button. It may take
   TWO clicks (the first highlights it red, the second confirms); when it
   turns into a red **关注** button the unfollow is done — move on. NEVER
   click a red 关注 button, that re-follows them.
5. To see more accounts, scroll DOWN over the centre of the modal (the
   account rows). You usually don't need to set scroll_x/scroll_y — the
   runner scrolls a sensible point inside a centred modal by default.
6. Accounts you already unfollowed stay in the list showing red 关注; skip
   them on later passes.
7. **Opening an account's profile + getting back.** Clicking an account's
   avatar/name opens their profile in a **NEW browser TAB** (the Following-list
   tab stays open underneath). Douyin profile pages have **no on-page back
   arrow** — do NOT hunt for one at the top-left, and do NOT click the browser
   toolbar (it's at the screen's top edge, which is blocked). To return to the
   Following list, **CLOSE the profile tab** with `key: ctrl+w` — this closes
   the current tab and returns you to the previous tab where the Following list
   is still open. Then continue with the next account.
8. **To BLOCK (拉黑) an account:** on their profile, click the **···** (more)
   button near the **分享主页** button to open a dropdown, then click **拉黑**
   and confirm in the dialog. The dropdown is hover-dismissed, so act on it
   promptly. (Blocking also removes them from your following list.)

## App: Douyin (douyin.com) — the video feed (like / favorite / comment / follow)

The 推荐 (Recommended) and 关注 (Following) feeds are a full-screen, one-video-
at-a-time player. Key facts:

- **Advancing to the next video: use `key: down` (ArrowDown).** This is the
  reliable way to move to the next item — the mouse wheel often does NOT reach
  the player and leaves you stuck on the same video. (This is the one place to
  prefer the keyboard over the mouse on Douyin.) Press it again to keep going.
- **Live streams** (marked 直播中, with an 进入直播间 button) are NOT regular
  videos and have no like/favorite/comment rail — `key: down` past them until a
  normal video appears.
- A normal video has a vertical **action rail on the RIGHT edge**, top to
  bottom: the creator avatar (often with a red **+** = follow), **点赞** (heart
  = like), **评论** (speech bubble = comment, shows a count), **收藏** (a star
  ★ = favorite/bookmark), **分享** (share). The icon turns highlighted/coloured
  once activated.
  - **Favorite** → click the **收藏** star. Done when it highlights (yellow).
  - **Like** → click the **点赞** heart. Done when it turns red.
  - **Follow the creator** → click the red **+** on the avatar (it disappears
    once followed). These are all toggles — one click; verify the icon changed,
    then move on (don't click again).
- **Comment:** click the **评论** (speech-bubble) icon to open the comment
  panel on the right, click the "说点什么..." input box at the bottom of that
  panel, `type` your comment, then click the **发送 / 发布** (send) button (or
  the comment won't post). Verify your comment appears in the list.
