# Stack-specific research — Phantom-Click (2026-05-29)

108 subagents, 26 sources fetched, 121 claims extracted, 25 verified (22 confirmed, 3 refuted).

## Top-line synthesis

Hand-rolled OCR + OpenCV-contour pipelines for screen parsing are being obsoleted by **hybrid UIA + trained-vision** systems. The single most directly transplantable architecture for our `app/som.py` is **Microsoft UFO² (April 2025)**: UIA-first → OmniParser-vision-second → IoU-fused dedup. Pure-vision parsers like **OmniParser V2** (YOLOv8 + Florence-2, Feb 2025) reach 39.5% on ScreenSpot-Pro and add fine-grained small-icon detection + interactability prediction — exactly the functions our OCR/contour passes approximate by hand.

For long-running list-traversal jobs, **browser-use's behavioral-loop detector** is directly portable: 20-step window, page-state fingerprint (URL + DOM text + element count), exempt `wait`/`done`/`go_back`, escalating-nudge injection at >=5/>=8/>=12 repeats.

## Confirmed findings (22)

### Architecture / SoM / grounding

1. **OmniParser V2 — drop-in for `_detect_text` + `_detect_icons`.** YOLOv8 icon detector + fine-tuned Florence-2 captioner. 39.5% on ScreenSpot-Pro (vs GPT-4o standalone 0.8%). Released Feb 2025, explicit improvements in "more fine-grained/small icon detection" and "prediction of whether each screen element is interactable". Pure-vision — no DOM dependency, compatible with our detection-free posture.
   - https://github.com/microsoft/OmniParser
   - https://huggingface.co/microsoft/OmniParser-v2.0
   - https://www.microsoft.com/en-us/research/articles/omniparser-v2-turning-any-llm-into-a-computer-use-agent/
   - https://arxiv.org/abs/2408.00203

2. **UFO² hybrid UIA + vision control-detection pipeline.** Enumerate UIA tree first → augment with OmniParser vision for non-UIA surfaces (canvas, web custom controls) → IoU-fuse (~10%) to dedup. Addresses Douyin's custom-rendered components that UIA alone misses. Separates HostAgent (task decomposition) from AppAgents — directly maps to our Douyin-web vs Douyin-native split.
   - https://arxiv.org/abs/2504.14603
   - https://github.com/microsoft/UFO
   - https://microsoft.github.io/UFO/ufo2/overview/

3. **OSCAR pattern — A11y-derived SoM with precise numerical coordinates.** Uses native window API to extract A11y tree, derives bounding boxes, layers semantic ID/label captions as SoM prompts. Validates what `app/som.py` does today. The OS Agents survey (ACL 2025 Oral) categorizes this as "dual grounding", the strongest of three grounding regimes.
   - https://arxiv.org/abs/2410.18963
   - https://arxiv.org/html/2508.04482v1

4. **Chrome 138+ native UIA + `--force-renderer-accessibility=complete`.** Chromium exposes IAccessible/MSAA, IAccessible2, and UIA, with documented IA2↔UIA mapping via shared IDs. Means Douyin-web elements become first-class UIA nodes in the same tree walk we already do for native. Preserves no-CDP / no-JS-injection posture.
   - https://chromium.googlesource.com/chromium/src/+/main/docs/accessibility/overview.md
   - https://chromium.googlesource.com/chromium/src/+/refs/heads/main/docs/accessibility/browser/ia2_to_uia.md
   - https://developer.chrome.com/blog/windows-uia-support-update

### Grounding models (self-hosted alternatives)

5. **UI-TARS / UI-TARS-2 (ByteDance).** Sets open-source bar: UI-TARS-72B 24.6 on OSWorld@50 steps (vs Claude 22.0); UI-TARS-1.5 61.6% on ScreenSpot-Pro (vs Claude 3.7's 27.7%). UI-TARS-2 unifies perception/reasoning/action/memory with multi-turn RL and data flywheel. Built on Qwen-2-VL, ~50B tokens continuous training. Self-hostable fallback when remote VLMs misfire on Chinese-language Douyin UIs.
   - https://arxiv.org/html/2501.12326v1
   - https://arxiv.org/abs/2509.02544
   - https://github.com/bytedance/UI-TARS

6. **UI-TARS error-correction + post-reflection DPO recipe.** Annotators pinpoint mistakes in agent traces and label corrective actions (Error Correction); separately simulate recovery steps after errors (Post-Reflection). Paired samples drive DPO. **Transplant without retraining**: harvest our Douyin failure traces (list reset, ghost click, captcha interrupt) and inject as few-shot recovery exemplars in the planner prompt.
   - https://arxiv.org/html/2501.12326v1
   - https://promptlayer.com/models/ui-tars-7b-dpo

7. **ShowUI (CVPR 2025) — 75.1% zero-shot ScreenSpot, 2B model, 256K data.** UI-Connected-Graph visual-token-selection scheme: 33% token reduction, 1.4x speedup vs Qwen2-VL-2B. Motivates a preprocessing step that crops uniform background regions and collapses repeated cells before sending SoM screenshots to remote VLMs. NOTE: 75.1% is on ScreenSpot, not ScreenSpot-Pro — on the latter, performance drops to ~7-28%.
   - https://arxiv.org/abs/2411.17465
   - https://github.com/showlab/ShowUI
   - https://openaccess.thecvf.com/content/CVPR2025/papers/Lin_ShowUI_One_Vision-Language-Action_Model_for_GUI_Visual_Agent_CVPR_2025_paper.pdf

### Grounding strategy

8. **ScreenSpot-Pro reveals catastrophic small-target failure.** Best baseline OS-Atlas-7B 18.9%, UGround-7B 16.5%, GPT-4o/Qwen2-VL-7B <2%. **Cure: ScreenSeekeR cascaded crop-then-ground.** Strong planner nominates coarse region → crop+resize → run SoM/grounding on crop. Same grounder lifts from 18.9% → 48.1% **with no extra training** (29.2pp absolute lift). Direct prescription for `app/som.py`: for small-tag queries (gender badges, verified marks), use planner-guided zoom before the SoM pass.
   - https://arxiv.org/pdf/2504.07981
   - https://arxiv.org/html/2504.07981v1
   - https://gui-agent.github.io/grounding-leaderboard/

### Long-running / list-traversal patterns

9. **browser-use behavioral-loop detector.** `loop_detection_window: int = 20`, `loop_detection_enabled: bool = True` by default. Records non-exempt actions, fingerprints page state by `url + dom_text + element_count`, exempts `{'wait', 'done', 'go_back'}`. `LoopDetector.get_nudge_message` escalates at max_repetition_count >=5, >=8, >=12 plus separate page-stagnation nudge. Injected as `UserMessage` into context, not raised as exception — does not abort the batch. Directly portable to `app/runner.py` for our block-all / unfollow-all loops.
   - https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py
   - https://github.com/browser-use/browser-use/blob/main/browser_use/agent/views.py
   - https://github.com/browser-use/browser-use/blob/main/browser_use/agent/message_manager/service.py

10. **browser-use `max_history_items` truncation pattern.** Keeps first item (task intent) + omitted-items marker + most recent N-1 items, asserts N>5. Exactly what we need when done-list grows past 100s of items in a block-all run.
    - https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py
    - https://github.com/browser-use/browser-use/blob/main/browser_use/agent/views.py

### Survey / taxonomy

11. **OS Agents survey (ACL 2025 Oral) — canonical taxonomy.** Three grounding types: (1) visual (SoM + OCR + GUI element detection via ICONNet, Grounding DINO); (2) semantic (HTML/DOM linking); (3) dual (AppAgent: labeled screenshots + XML element details). Dual grounding empirically strongest. **Grounding DINO** is the named open-vocabulary detector to consider slotting beside YOLO/OmniParser for icon recall augmentation.
    - https://arxiv.org/html/2508.04482v1

## Refuted claims (3)

- ❌ "UI-TARS-2 achieves 47.5 on OSWorld and 50.6 on WindowsAgentArena" — 1-2 vote, paper does NOT report these exact numbers. The architectural framing (perception+reasoning+action+memory unified, multi-turn RL, data flywheel) holds, but don't cite these scores.
- ❌ "UI-TARS uses Set-of-Mark by drawing markers" — 0-3 vote. UI-TARS is a native end-to-end grounder, NOT a SoM consumer. Adopting its approach means **replacing** SoM with a trained grounder, not improving the marks.
- ❌ "OmniParser V2 plugs into Anthropic Computer Use via OmniTool" — 0-3 vote. Real integration requires more than a single wrapper.

## Open questions (NOT covered by this pass — need follow-up research)

- Slider/jigsaw CAPTCHA: which 2024-2026 OSS solvers actually work against Geetest v4 / NetEase Yidun / Douyin sliders, and what behavioral signals do those providers use against `pyautogui`-style synthetic drags?
- Human-like mouse motion: which Bezier / Fitts-tuned implementations evade Akamai Bot Manager / PerimeterX / Cloudflare Turnstile? Minimum jitter/latency budget on top of OS-level events?
- Pre-action stability detection: across browser-use, Stagehand, Skyvern — what concrete signals deliver best precision/recall for "page settled"? Latency budgets, false-positive rates?
- Color-first / small-tag chain-of-thought: are there published prompt patterns showing VLMs reliably reading pixel-level color/shape (gender badges, verified checks)? Needs a targeted pass on Aria-UI, Magma-UI, Aguvis.

## Time-sensitivity caveats

- ScreenSpot-Pro 18.9% reflects April 2025 SOTA — the leaderboard moved fast: UI-TARS-1.5 61.6%, GTA1-7B 55.7%, GPT-5.2 86.3% by late 2025. The value of the 18.9% figure is showing the **gap** and the cascade-zoom recipe that closes it.
- OmniParser V2's 39.5% ScreenSpot-Pro is pipeline+LLM (with GPT-4o), not parser-alone.
- ShowUI 75.1% is ScreenSpot, NOT ScreenSpot-Pro (where it's ~7-28%). Do not conflate.

## Top 5 actionable next steps, ranked

1. **Pilot OmniParser V2 as a parallel SoM source in `app/som.py`.** Add `_detect_omniparser(image_bgr)` returning labeled boxes; A/B against the current OCR+contour merge on Douyin captures. Cost: GPU or hosted endpoint, plus a few hundred ms/turn. Risk: model adds a new dependency, increases bundle size — keep behind a `PHANTOM_SOM_OMNIPARSER` env gate like UIA.
2. **Port browser-use's loop detector to `app/runner.py`.** Add `_LoopDetector` class with 20-step window + page-state fingerprint + exempt action set + escalating nudge injection. Directly addresses g3's captcha-retry burn through the budget. Cost: ~150 LOC.
3. **Add cascade-zoom for small-tag classification.** When the model emits a gender-badge / verified-check query, crop a ~200x200 region around the candidate and re-run SoM/grounding on the crop before the next decision. ScreenSeekeR-style. Cost: 1 extra screenshot/turn, ~+100ms.
4. **Enable `--force-renderer-accessibility=complete` in `tools/run_and_review.py`** (already partially done — verify `--force-renderer-accessibility` is in `_launch_chrome` args). Try `=complete` mode to maximize Chrome's UIA exposure for Douyin-web.
5. **Add an error-correction few-shot library.** Harvest 10-20 historic failure traces from `_runs/` (list reset, ghost click, captcha interrupt), summarize each as a "wrong move → correction" pair, inject into SYSTEM_PROMPT after PROGRESS FIELD. UI-TARS-DPO-recipe without the training.

## Source quality summary

26 unique sources fetched: 16 primary (papers, official repos, official docs), 8 secondary (analysis blogs, third-party implementations), 2 unreliable/forum.
