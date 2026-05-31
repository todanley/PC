# Foundational research — Vision-based computer-use agents (2026-05-29)

109 subagents, 27 sources fetched, 129 claims extracted, 25 verified (23 confirmed, 2 refuted).

## Top-line synthesis

The field opened in **2017** with World of Bits (Shi/Karpathy/Fan/Hernandez/Liang, ICML 2017) — first RL platform for low-level keyboard/mouse on real cached web. Pixel-only became empirically viable in **2023** with Google's **Pix2Act** beating human crowdworkers on MiniWob++ (96.2 vs 94.3) using only screenshots and no DOM. The **2024** SeeAct paper crystallized the field's central diagnosis: GPT-4V completes 51.1% of Mind2Web tasks with oracle grounding — so **action grounding, not planning, is the dominant bottleneck**, and naive Set-of-Mark prompting does NOT transfer cleanly to dense webpage screenshots.

The field then split into two complementary directions:
- **Modular pipelines**: OmniParser (Aug 2024) lifted GPT-4V on ScreenSpot from 16.2% → 73.0% via YOLOv8 + BLIP-2 (Florence-2 in v2) + OCR — direct empirical proof that explicit element parsing dramatically beats raw-pixel grounding.
- **Native end-to-end vision agents**: UGround / SeeAct-V (Oct 2024, OSU NLP) trained on 10M GUI elements over 1.3M+ screenshots; UI-TARS (Jan 2025, ByteDance) beat Claude computer-use 24.6 vs 22.0 on OSWorld@50 steps and GPT-4o 46.6 vs 34.5 on AndroidWorld using only screenshots + human-like input.

Anthropic shipped public-beta computer-use for Claude 3.5 Sonnet on **Oct 22, 2024** — the first frontier-lab commercial vision-based agent — at 14.9% OSWorld screenshot-only (22.0% at 50 steps), nearly double the 7.8% next-best AI.

## Confirmed findings (23, summarized)

### Field history (canonical lineage)
1. **GUI agents — 4-capability framework**: perception / reasoning / planning / acting (Nguyen et al. survey, ACL 2025 Findings). Perception splits into accessibility-based, HTML/DOM-based, pure-visual, hybrid. — https://arxiv.org/abs/2412.13501

2. **World of Bits (ICML 2017)** — first vision-based RL web platform. Shi, Karpathy, Fan, Hernandez, Liang. MiniWoB / FormWoB / QAWoB. — https://proceedings.mlr.press/v70/shi17a/shi17a.pdf

3. **Pix2Act (Shaw et al., NeurIPS 2023)** — Pix2Struct + behavioral cloning + tree-search trajectories. Outperformed humans on MiniWob++ (96.2 vs 94.3). DOM explicitly NOT provided. Established pixel-only is viable. — https://arxiv.org/abs/2306.00245

4. **SeeAct (Zheng et al., ICML 2024)** — "GPT-4V(ision) is a Generalist Web Agent, if Grounded." 51.1% Mind2Web with oracle grounding. Identified naive SoM as ineffective on rich webpages due to "severe hallucination." — https://arxiv.org/abs/2401.01614

5. **SoM (Yang et al., Microsoft, Oct 2023)** — foundational visual-prompting paper. SEEM/SAM-produced marks + GPT-4V zero-shot beats fully-finetuned PolyFormer on RefCOCOg RES (75.6 vs 67.2 mIoU). Caveat: depends materially on segmenter — oracle masks push to 90.1. — https://arxiv.org/abs/2310.11441

### Architectures

6. **OmniParser (Microsoft, Aug 2024)** — modular vision pipeline. YOLOv8 region detector + BLIP-2 (v2: Florence-2) icon captioner + OCR. Lifted GPT-4V ScreenSpot 16.2% → 73.0%. Direct evidence that explicit element parsing beats raw-pixel grounding. — https://arxiv.org/html/2408.00203v1

7. **UGround / SeeAct-V (OSU NLP, Oct 2024)** — pure-visual, rejects HTML/a11y trees as "noise, incompleteness, computational overhead." Trained on 10M GUI elements / 1.3M+ screenshots with slight LLaVA adaptation. — https://arxiv.org/abs/2410.05243

8. **Anthropic computer use (Oct 22 2024)** — first frontier-lab commercial. Claude 3.5 Sonnet. Launch OSWorld 14.9% screenshot-only / 22.0% at 50 steps, vs 7.8% next-best AI. — https://www.anthropic.com/news/3-5-models-and-computer-use

9. **UI-TARS (ByteDance, Jan 2025)** — canonical open-weights native-vision agent. OSWorld@50 24.6 (beats Claude 22.0), AndroidWorld 46.6 (beats GPT-4o 34.5), ScreenSpot Pro 38.1 (SOTA at launch). End-to-end — no commercial wrappers, no DOM. — https://arxiv.org/abs/2501.12326

10. **UI-TARS System-1/System-2 + Reflection Tuning** — trajectories interleave "thoughts" before actions. Two annotated data types: Error Correction (annotators label corrections) + Post-Reflection (simulate recovery). Trained via DPO. Online trace bootstrapping across hundreds of VMs. — https://arxiv.org/html/2501.12326v1

11. **Grounding is the universal bottleneck** — OmniParser, SeeAct, "From Grounding to Planning" (arxiv 2409.01927), and UGround all independently converge on this diagnosis. — https://arxiv.org/abs/2409.01927

## Refuted claims (2)

- ❌ "WoB used crowdworker demos with cached HTTP, establishing the human-demo paradigm later inherited by Mind2Web/WebArena" — 1-2 vote, the specific narrative isn't supported.
- ❌ "UGround outperforms prior visual grounders by up to 20pp across six benchmarks" — 1-2 vote, the specific 20pp number isn't sourced as quoted.

## Time-sensitivity caveats

- All absolute SOTA numbers are **launch snapshots**, not current. UI-TARS-2 (Sept 2025) supersedes UI-TARS-1's OSWorld 24.6 with 47.5. By 2026 Claude Opus 4.6 is ~72% on OSWorld-Verified per xlang.ai/llm-stats.com.
- UI-TARS's "beats Claude computer-use" comparison was run by ByteDance, not Anthropic — first-party benchmarking caveat.
- Anthropic's "7.8% next-best AI system" is unnamed/vendor-reported.
- OmniParser architecture above is v1 (BLIP-2). v2 uses Florence-2.
- SoM's RefCOCOg 75.6 mIoU is segmenter-dependent.

## Open questions (NOT covered by this pass)

- Concrete action-space taxonomy across modern agents (normalized 0-1000 vs raw pixel, Operator's exact action schema, click/double-click/drag/scroll/key vs composite open_app/navigate)
- Safety / visual-prompt-injection landscape (ASCII tags in webpages, action-confirmation patterns, sandboxing, audit, account takeover, Greshake-style indirect injection adapted for screens)
- Closed-source frontier: Operator architecture, Adept ACT lineage, Multion, Imbue, Google Project Mariner. Reproductions and reverse-engineering attempts.
- On-device / small-specialist-model frontiers (sub-7B GUI grounders trained on huge synthetic action datasets, latent action tokens, world-models, agentic RL via OSWorld training loops, GRPO for GUI)
- Methodological critiques of benchmarks (OSWorld vs OSWorld-Verified discrepancies, Mind2Web contamination, ScreenSpot vs Pro vs V2 difficulty calibration)

## Top reading list (priority order)

1. SoM (Yang et al. 2023) — visual prompting foundational paper. https://arxiv.org/abs/2310.11441
2. SeeAct (Zheng et al. 2024) — grounding-is-the-bottleneck diagnosis. https://arxiv.org/abs/2401.01614
3. OmniParser (Lu et al. 2024) — modular vision pipeline. https://arxiv.org/html/2408.00203v1
4. UI-TARS (Qin et al. 2025) — canonical open-weights end-to-end agent. https://arxiv.org/abs/2501.12326
5. UGround / SeeAct-V (Gou et al. 2024) — pure-visual paradigm + dataset recipe. https://arxiv.org/abs/2410.05243
6. Anthropic launch announcement (Oct 2024) — first commercial frontier deployment. https://www.anthropic.com/news/3-5-models-and-computer-use
7. World of Bits (Shi et al. 2017) — field origin point. https://proceedings.mlr.press/v70/shi17a/shi17a.pdf
8. Pix2Act (Shaw et al. 2023) — pixel-only viability proof. https://arxiv.org/abs/2306.00245
9. GUI Agents survey (Nguyen et al. 2024) — analytical frame. https://arxiv.org/abs/2412.13501
10. ScreenSpot-Pro (April 2025) — small-target failure benchmark. https://arxiv.org/html/2504.07981v1
