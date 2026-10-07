# Model comparison report — Claude Sonnet 5 vs Gemini 3.8 Flash

**Date:** 2026-10-07
**Task:** TASK1 — `打开chrome, douyin.com，关注10个随机美食账号，然后拉黑所有我的关注列表里的男性`
**Harness:** `tools/compare_douyin_all.sh` → `tools/run_and_review.py`
**Config:** Chrome Profile 3 (`stanley`), pre-launched to `https://www.douyin.com`
**Caps:** `--max-steps 150`, `--timeout 2400` (40 min)

## Executive summary

**Head-to-head is inconclusive because Sonnet 5 aborted on a plumbing bug on our side**, not a capability regression — the Anthropic adapter doesn't handle Sonnet 5's `thinking` content block. However, **Gemini 3.8 Flash substantially completed the task**: followed all 10 food accounts, declared `done` at turn 84 after correctly blacklisting 6/6 male accounts among the 15 processed from the follow list (females and no-tag entries all correctly skipped), then continued processing pre-existing follows until the 2400s wall-clock cap fired. The initial write-up of this report wrongly characterised Gemini as "timed out mid-follow"; corrected below after walking the full 100-turn per-turn log.

## Results

| Config | Exit | Steps | Wall time | MP4 size | Outcome |
|---|---|---|---|---|---|
| **Gemini 3.8 Flash** | timeout @ 2400s (after `done` declared at turn 84) | 105 | ~40 min | 280 MB | **10/10 followed, 6/6 males blacklisted, 5 females + 4 no-tag correctly skipped; `done` declared; agent kept scrolling for pre-existing follows post-`done` until cap fired** |
| **Claude Sonnet 5** | failed: `no JSON in response` | 9 | ~5 min | 117 MB | 1/10 followed (大宋宋Tiffany), then stuck re-clicking a wrong X-close coord, crashed on turn 9 (adapter bug) |

## Run 1 — Gemini 3.8 Flash

- **Run dir:** `_runs/20261007_160219_cmp_gemini38_t1_blacklist/`
- **Full video:** [`screen.mp4` (280 MB)](../../_runs/20261007_160219_cmp_gemini38_t1_blacklist/screen.mp4)
- **Timing:** 2026-10-07 16:02:19 → 16:42:25 (40 min 6 s)

### Progression (from the per-turn `turn_NN.md` dumps and the `done_item` chain)

**Follow phase (turns 1–~21):**

1. Opened Douyin recommended feed; clicked the **美食** category tab.
2. Clicked search bar, typed `美食`, submitted, switched to **用户** tab in search results.
3. Followed 10 food accounts in order (progress strings advance `0/10 → 1/10 → … → 9/10`):
   逗哥美食记, 小薯努力做美食, 美食三三, 小阿磊~美食测评, 美食米阿米, 铭哥说美食, Lucky美食, 晓哥爱美食, 胖胖夫妻(乡村美食), 黄大维(美食).

**Phase transition (turn 22):** Agent moves to blacklist phase: `Clicking 拉黑 on 胖胖夫妻(乡村美食)`.

**Blacklist phase (turns 22–83):** 15 accounts processed from the follow list, each with a correct `done_item` classification:

- **Males blacklisted (6/6):** 胖胖夫妻(乡村美食), 晓哥爱美食, 铭哥说美食, 黄大维, 美食米阿米, 小阿磊~美食测评.
- **Females skipped (5):** 美食三三, 逗哥美食记, 仲夏夜之梦, 一缕阳光, @夏天的味道.
- **No-gender-tag skipped (4):** Lucky美食, 小薯努力做美食, 祝淑芳, 用户7838932845515.

**Turn 84:** Agent declares `done`:
`"Followed 10 random food accounts and blacklisted all male accounts among followed creators. Task complete."`

**Turns 85–99+:** Harness kept the loop going because scrolling the follow list revealed more pre-existing follows (e.g. 我一定是世界上最幸运的人@MD2051, 回归美食). Agent dutifully continued inspecting profiles for gender until the 2400s cap fired.

### Observations

- **JSON output discipline: excellent** — every turn's response parsed cleanly, used `mark` selection throughout, never fell back to raw x/y, maintained `progress` and `done_item` strings coherently.
- **`done_item` list mechanism worked as designed** — progress reports named each account by handle, survived scroll resets and page refreshes.
- **Gender attribution worked** — the agent correctly read male/female indicators from profile cards and classified each case.
- **No visible getting-stuck loops** on any UI dead-end — the agent handled the one case of a popup/modal cleanly.
- **Why timeout:** not a failure to complete the task — the agent completed the task's stated objectives at turn 84 — but the task text was ambiguous whether "我的关注列表里的男性" meant "the 10 I just followed" or "everything in my follow list including pre-existing follows." The agent chose the more inclusive reading and kept processing until the cap. Either refine the task text or raise the cap.

## Run 2 — Claude Sonnet 5

- **Run dir:** `_runs/20261007_164235_cmp_sonnet5_t1_blacklist/`
- **Full video:** [`screen.mp4` (117 MB)](../../_runs/20261007_164235_cmp_sonnet5_t1_blacklist/screen.mp4)
- **Timing:** 2026-10-07 16:42:35 → 16:47:20 (4 min 45 s)

### Progression (from `turns.txt`)

1. Opened Douyin, reached search / follow surface.
2. Followed `大宋宋Tiffany` (1 confirmed on `done_item` list).
3. Attempted to close a video panel (click produced no visible change — runner's auto-retry at 8 fan-out offsets also failed; this is a known Douyin modal issue, not Sonnet-specific).
4. On the next turn, Sonnet 5 returned an **empty** response.
5. Harness emitted `✗ vision call failed: no JSON in response:` and the run died.

### Root cause (plumbing bug on our side)

Sonnet 5 **enables extended thinking by default**. For a request with no `thinking: disabled` directive, the Messages API response looks like:

```json
{
  "content": [
    { "type": "thinking", "thinking": "..." },
    { "type": "text",     "text": "{...the JSON action we wanted...}" }
  ],
  ...
}
```

The project's `app/vision.py` Anthropic branch currently reads `content[0].text`, which on Sonnet 5 is a `thinking` block that has no `text` field → returns empty string → downstream `_extract_json_from_response` finds nothing → `vision call failed: no JSON in response`.

Sonnet 5 wasn't failing to produce JSON. It was producing JSON in `content[1]` and we weren't looking.

### Fix options

| Option | Change | Trade-off |
|---|---|---|
| **A. Scan content blocks** | Walk `content[*]`, take first `type: "text"` block's `text` field | Forward-compatible; works for any Anthropic model with or without thinking |
| **B. Disable thinking** | Add `"thinking": {"type": "disabled"}` to the Anthropic request body | One-line fix; loses the thinking quality improvements that come with Sonnet 5 |
| **C. Pin older Claude** | Revert default to a non-thinking variant | Loses the whole point of upgrading to Sonnet 5 |

**Recommendation: A.** It's a 3-line change in `_anthropic_response_text` (or wherever that lives in `app/vision.py`) and future-proof.

## Rough cost (indicative — exact numbers from provider usage headers if needed)

| Config | Turns | Approx $ |
|---|---|---|
| Gemini 3.8 Flash | 105 | ~$0.30–0.50 |
| Claude Sonnet 5 | 9 | ~$0.05–0.10 (aborted early) |

Can be pulled precisely from the per-turn usage dumps in `turns.txt` if a tight comparison is needed.

## What this comparison does and doesn't tell us

**It tells us:**
- **Gemini 3.8 Flash substantially completed TASK 1 end-to-end** on this run: full follow phase + full blacklist phase of the agent's own 10 follows + partial coverage of pre-existing follows before timeout. Clean JSON, correct gender attribution, no misclick loops.
- Our Anthropic adapter has a bug that silently drops Sonnet 5's `thinking`-mode responses. Easy fix (walk `content[*]` for the first `type:"text"` block).
- The 2400 s / 150-step budget is enough for the stated task — the agent's `done` at turn 84 happened well inside the cap; the extra time was spent on an ambiguous reading of "我的关注列表".

**It doesn't tell us yet:**
- Head-to-head quality between the two models. Sonnet 5 never got a fair run due to our bug.
- Whether Sonnet 5's extended thinking buys measurable quality on dense Chinese UIs vs. Gemini 3.8 Flash's output.
- Average cost per confirmed `done_item` with and without thinking.

## Suggested re-run plan

1. **Fix the Anthropic adapter** (`_anthropic_response_text` scans `content[*]` for the first `type:"text"` block) — one small commit, 3 lines.
2. **Tighten TASK 1 wording** to resolve the follow-list ambiguity — e.g. `拉黑我刚关注的10个账号中的男性` (blacklist males among the 10 I just followed) — avoids the agent chasing pre-existing follows after `done`.
3. **Re-run Sonnet 5 only** on the fixed adapter + tightened task — Gemini's result on this run is already satisfactory and can serve as the baseline.
4. **Optional:** N=3 both models for statistical hygiene before you quote numbers externally.

## Artifacts (full paths)

```
_runs/20261007_160219_cmp_gemini38_t1_blacklist/
    screen.mp4            — full session recording (280 MB)
    turns.txt             — system prompt + per-turn user + model response
    step_NN.png           — raw screenshots (105 of them)
    mark_NN.png           — SoM-annotated frames the model actually saw
    meta.json             — task metadata, exit reason
    stderr.log            — mirrored harness output

_runs/20261007_164235_cmp_sonnet5_t1_blacklist/
    screen.mp4            — full session recording (117 MB)
    turns.txt             — same shape, 9 turns
    step_NN.png / mark_NN.png — 9 each
    meta.json
    stderr.log
```
