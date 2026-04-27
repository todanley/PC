# Phantom-Click knowledge base

Procedural tips appended to the system prompt every turn. The user gives a
one-line natural-language task; everything else needed to complete it on a
specific app lives here. Keep entries terse — every byte is a token.

## App launch (any task)

If the task names an app and that app isn't visible:
- macOS: press `cmd+space`, type the app's native name (e.g. `抖音` not
  "Douyin" — Spotlight matches the bundled app, not a website), press
  `enter`. Wait one turn for the window to render.
- The runner already keeps the target app frontmost via `PHANTOM_FOCUS_APP`,
  so once it's launched you don't need to re-focus it.

## Douyin desktop — overall navigation

Douyin's layout shifts between three states; recognise which you're in
before clicking:

1. **Home feed / playing video** (full-screen video, right-side action rail
   with heart, comment, save, share). Sidebar on the very left has tabs
   `精选 / 推荐 / AI / 关注 / 朋友 / 我的 / 直播 / 放映厅 / 短剧`.
2. **Profile page** (your own or a creator's). Top has avatar, name, stats
   row `关注 N | 粉丝 N | 获赞 N`, then tabs `作品 / 推荐 / 喜欢 / 收藏 / …`.
   Top-left has a small `<` back-arrow button (only on someone-else's
   profile, NOT on your own).
3. **Follow-list modal** (overlay window). Title `关注 (N) | 粉丝 (N)`, a
   search bar, then rows of `avatar | name | bio | 已关注 button`. The
   modal occupies roughly the middle-right of the screen with the rest of
   the page dimmed behind it.

## Douyin desktop — opening your follow list

From the home feed: click the `我的` sidebar item → on your profile click
the `关注 N` text in the stats row. The follow-list modal opens at row 1.

## Douyin desktop — determining a follower's gender

You almost always need to enter the user's profile — the small avatar in
the modal isn't enough.

1. In the modal, click the row's avatar OR name (the LEFT side of the
   row, not the `已关注` button on the right). This opens the user's
   profile in place of the modal.
2. On the profile, look at:
   - the larger avatar (top of profile),
   - the bio under the name,
   - the grid of their videos for face shots,
   - any `性别 / 男 / 女` indicator if visible.
3. Decide gender. If unclear, default to `unknown` and skip — the user
   asked for males specifically.

## Douyin desktop — unfollowing on a profile

The unfollow control is the `已关注` button next to the user's name on the
profile (typically top-right of the user-info card). Click it ONCE; Douyin
may pop a `确定` (confirm) dialog — click that too. After confirming, the
button switches to a red `+ 关注` (Follow) state and your `关注 N` count
drops by 1 (verifiable when you return to your own profile).

## Douyin desktop — returning from a profile to the follow modal

PREFER the sidebar route — it works regardless of which profile/layout
you're on:

1. Click `我的` in the LEFT sidebar (re-localize from the current
   screenshot — its position shifts; the icon is consistent).
2. On your profile, click the `关注 N` text in the stats row.

This always works. The in-app `<` back arrow is fragile — it isn't shown
on every layout, and its position varies. Only use it if you can clearly
see it in the current screenshot.

The follow modal reopens at row 1, so you'll re-see rows you already
processed. Track them in `progress.examined_names: [list of names]` and
SKIP any row whose name is in that list before clicking into a new one.

## Douyin desktop — scrolling within a modal

`scroll` action already moves the cursor to the modal's footprint
(≈ 55%/55% of the screen) before dispatching the wheel event, so it
scrolls the modal contents. After scrolling, rows that were visible may
shift up; re-read row labels from the new screenshot.

## Douyin desktop — `已关注` is a toggle

Clicking `已关注` once unfollows. Clicking it again would re-follow. Apply
the count rule from the system prompt's TOGGLE-BUTTON section: read the
profile's `粉丝/关注` count if visible, or just trust that one click is
enough and move on (do NOT re-click).
