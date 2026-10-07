# Model comparison report — Claude Sonnet 5 vs Gemini 3.8 Flash

**Date:** 2026-10-07
**Task:** TASK1 — `打开chrome, douyin.com，关注10个随机美食账号，然后拉黑所有我的关注列表里的男性`
**Harness:** `tools/compare_douyin_all.sh` → `tools/run_and_review.py`
**Config:** Chrome Profile 3 (`stanley`), pre-launched to `https://www.douyin.com`
**Caps:** `--max-steps 150`, `--timeout 2400` (40 min)

## Executive summary

**Comparison is inconclusive.** Sonnet 5 aborted after 9 steps because of a plumbing bug on our side (`app/vision.py` doesn't handle Sonnet 5's `thinking` content block), not a capability regression. Gemini 3.8 Flash ran its full 40-minute budget, completed about a third of the follow phase, and timed out before reaching the blacklist phase. Neither run finished the task end-to-end.

## Results

| Config | Exit | Steps | Wall time | MP4 size | Phase reached |
|---|---|---|---|---|---|
| **Gemini 3.8 Flash** | timeout @ 2400s | 105 | ~40 min | 280 MB | Mid-follow (3/10 confirmed, still in search-results list) |
| **Claude Sonnet 5** | failed: `no JSON in response` | 9 | ~5 min | 117 MB | Early-follow (1/10 confirmed) |

## Run 1 — Gemini 3.8 Flash

- **Run dir:** `_runs/20261007_160219_cmp_gemini38_t1_blacklist/`
- **Full video:** [`screen.mp4` (280 MB)](../../_runs/20261007_160219_cmp_gemini38_t1_blacklist/screen.mp4)
- **Timing:** 2026-10-07 16:02:19 → 16:42:25 (40 min 6 s)

### Progression (from `turns.txt`)

1. Opened Douyin recommended feed.
2. Identified and clicked the **美食** (food) category tab.
3. Clicked search bar, typed `美食`, submitted.
4. Switched to the **用户** (Users) tab in search results.
5. Followed, in order (`done_item` list in prompt-fed memory):
   - 逗哥美食记
   - 小薯努力做美食
   - 美食三三
6. Continued iterating the search-results list of food creators until the 2400 s wall-clock cap fired.

### Observations

- JSON output discipline: strong — every turn's response parsed cleanly, used `mark` selection throughout, maintained `progress` strings coherently.
- `done_item` list mechanism worked: progress reports named each confirmed-followed account by handle, surviving scrolls and page refreshes.
- Didn't appear to loop or get stuck — just slow. At ~105 turns for ~3 confirmed follows, average turn cost is high, likely because the recommended-stream UI makes it hard to find the next unprocessed follow button after each confirmation.
- **Why timeout, not completion:** this task is structurally 2-phase (follow 10, then blacklist N). The 2400 s budget was set assuming the follow phase takes ~5-10 min; it actually took longer. Either raise the cap or structure as two separate tasks.

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
- Our Anthropic adapter needs a one-sitting fix before Sonnet 5 is usable at all. Easy fix.
- Gemini 3.8 Flash can handle the new food-themed Task 1 at least in part — JSON output is clean, mark selection works, progress tracking survives the long horizon.
- The 2400 s / 150-step caps are too tight for a 2-phase task; raise or split.

**It doesn't tell us yet:**
- Which model is actually better. Both runs stopped before completing the task.
- Head-to-head grounding accuracy on the food-themed stream.
- Whether Sonnet 5's extended thinking buys real quality on this workload.

## Suggested re-run plan

1. Fix the Anthropic adapter (option A above) → one small commit.
2. Split TASK1 into TASK1a (follow 10 food accounts) and TASK1b (blacklist males in current follow list), so each has a ~20-min budget that's achievable.
3. Run both models on both subtasks, N = 3 each.
4. Compare on: completion rate, turn count, cost per completed subtask, apparent misclick rate.

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
