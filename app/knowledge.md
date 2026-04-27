# Phantom-Click knowledge base

Procedural tips appended to the system prompt every turn. Add a section here
whenever a task needs a non-obvious step the agent has to know up front.
Keep entries terse — every byte costs tokens.

## Douyin desktop — determining a follower's gender

You can almost never tell gender from the small avatar shown in the follow-list
modal alone. To classify an account in your follow list as male / female:

1. In the 关注 list modal, click the user's NAME or AVATAR (the left side of the
   row, NOT the 已关注 button on the right). This opens that user's profile in
   place of the modal.
2. On the profile, look at:
   - the larger avatar (top of profile),
   - the bio line under the name,
   - the grid of their videos for face shots,
   - any 性别 / 男 / 女 indicator if visible.
3. Decide gender. If unclear, default to "unknown" and skip.
4. To return to the follow-list modal — **PREFER the sidebar route**, it is
   100% reliable:
   - Click `我的` in the LEFT SIDEBAR (around logical `(125, 305)`).
   - Then click the `关注 N` stat at the top of your profile (around
     `(470, 215)`). The modal reopens at row 1.
   - This is more clicks than the in-app back arrow, but the back arrow's
     pixel position varies (sometimes at `(90, 130)`, sometimes hidden
     under a video player) so it often misses. The sidebar is always at
     the same place.

   - **NEVER press `escape`.** Escape on Douyin desktop closes the entire
     follow-list modal AND the profile.
   - **NEVER use `cmd+tab`.** The Douyin window is already frontmost; the
     runner re-raises it every turn.

5. The modal reopens at row 1, so to process all 16 follows you'd re-examine
   the same first rows each cycle. To skip rows you've already examined,
   use your `progress` field — keep a list `examined_names: [name1, name2, ...]`
   and when you reopen the modal, skip any row whose name is in that list.

## Douyin desktop — unfollowing from the profile page

Once on a profile, the unfollow control is the **已关注** button next to the
account name (usually top-right of the user-info card on the profile page).
Click it ONCE — Douyin may show a confirmation popup with a `确定` (confirm)
button; click that too. After confirmation the button switches to a red
`关注` (Follow) state and the user's 关注 count drops by 1.

## Douyin desktop — scrolling within a modal

`scroll` already moves the cursor to the modal's footprint (≈ 55% across the
screen) before dispatching the wheel event, so it scrolls the modal contents
directly. If you need a different anchor, pass `scroll_x` and `scroll_y` in
your action JSON (in OS-logical pixels) — that overrides the default. After
scrolling, the rows that were visible may shift up; re-read the row labels
in the next screenshot before continuing.
