#!/usr/bin/env python3
"""Vision-driven Douyin orchestrator.

For every action: re-focus 抖音, screenshot, ask Gemini, re-focus 抖音, act, save proof.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from phantom import PhantomInput, PhantomScreen, PhantomVision

PROOF_DIR = Path("/tmp/phantom_proofs")
PROOF_DIR.mkdir(exist_ok=True)

pi = PhantomInput()
ps = PhantomScreen()
pv = PhantomVision()

# Detect Retina scale once.
_test = str(PROOF_DIR / "_scale.png")
ps.capture(_test)
try:
    from PIL import Image
    SCALE = Image.open(_test).size[0] / ps.get_screen_size()[0]
except Exception:
    SCALE = 2.0
os.unlink(_test)


def focus_douyin():
    subprocess.run(["osascript", "-e", 'tell application "抖音" to activate'], check=False)
    time.sleep(0.7)


def shot(label: str) -> str:
    p = str(PROOF_DIR / f"{label}.png")
    ps.capture(p)
    return p


def ask(prompt: str, image_path: str) -> dict:
    """Single Gemini call. Returns parsed steps dict."""
    return pv.analyze(image_path, prompt)


def do_steps(steps_resp: dict, label: str):
    """Execute the steps array returned by Gemini."""
    steps = steps_resp.get("steps", [steps_resp])
    for i, action in enumerate(steps):
        act = action.get("action", "error")
        if act in ("done", "error"):
            print(f"  [{label}] vision says: {act} — {action.get('reasoning','')}")
            continue
        if act == "click":
            x = float(action["x"]) / SCALE
            y = float(action["y"]) / SCALE
            print(f"  [{label}] click ({x:.0f},{y:.0f}) — {action.get('reasoning','')[:80]}")
            pi.click(x, y)
        elif act == "type":
            text = action.get("text", "")
            print(f"  [{label}] type: {text[:40]}")
            if any(ord(c) > 127 for c in text):
                pi.paste_text(text)
            else:
                pi.type_text(text)
        elif act == "key":
            keymap = {"enter":36,"return":36,"tab":48,"escape":53,"space":49,
                      "backspace":51,"delete":117,"up":126,"down":125,
                      "left":123,"right":124}
            mod = {"command":55,"cmd":55,"shift":56,"option":58,"alt":58,
                   "control":59,"ctrl":59}
            parts = [p.strip().lower() for p in action.get("key","").split("+")]
            mods = [mod[p] for p in parts if p in mod]
            mk = next((keymap[p] for p in parts if p in keymap), None)
            if mk is not None:
                print(f"  [{label}] key: {action.get('key')}")
                pi.press_key(mk, modifiers=mods or None)
        elif act == "scroll":
            direction = action.get("direction", "down")
            dy = -5 if direction == "down" else 5
            print(f"  [{label}] scroll {direction}")
            pi.scroll(dy=dy)
        time.sleep(0.5)


def vision_step(label: str, prompt: str, settle: float = 1.5):
    """Re-focus → screenshot → vision → re-focus → act → screenshot proof."""
    print(f"\n=== {label} ===")
    print(f"  prompt: {prompt[:100]}")
    focus_douyin()
    before = shot(f"{label}_before")
    resp = ask(prompt, before)
    print(f"  resp: {json.dumps(resp, ensure_ascii=False)[:200]}")
    focus_douyin()       # vision call spawned Chromium → re-focus before clicking
    do_steps(resp, label)
    time.sleep(settle)
    after = shot(f"{label}_after")
    print(f"  proof: {after}")
    return resp


def search_and_follow(query: str, slot: int):
    """Search a query, switch to user tab, follow top 5."""
    # 1. Click the search field
    vision_step(
        f"{slot}_a_click_search",
        "There is a Douyin desktop app window. Click on the search input box at the top of the Douyin window — it has the placeholder text '搜索你想搜的内容'. The search box is the wide input field next to the magnifying-glass / 搜索 button. Coordinates must be on that input field, NOT on the title bar or menu bar above it. Return only one click action.",
    )

    # 2. Paste the query and press Enter (no vision needed — we just typed in the focused field)
    print(f"\n=== {slot}_b_type_and_submit ===")
    focus_douyin()
    pi.paste_text(query)
    time.sleep(0.5)
    pi.press_key(36)  # Enter
    time.sleep(2.5)
    shot(f"{slot}_b_after_search_{query}")

    # 3. Switch to 用户 tab
    vision_step(
        f"{slot}_c_user_tab",
        "We are on a Douyin search results page. Click the tab labeled '用户' (Users) in the search-results category bar near the top — it filters results to show user accounts only. Return one click action.",
        settle=2.0,
    )

    # 4. Follow top 5 accounts
    for n in range(1, 6):
        vision_step(
            f"{slot}_d{n}_follow_acct{n}",
            f"We are on a Douyin search results page filtered to '用户' (users). There is a vertical list of user accounts. Click the '关注' (Follow) button on the row for user #{n} (counting from the top, 1-indexed). The 关注 button is a small button on the right side of each user row. If the button on row #{n} already says '已关注' (already following), pick the next not-yet-followed user below it. Return one click action.",
            settle=1.8,
        )

    # 5. Clear search by clicking the search field again and selecting all + delete
    vision_step(
        f"{slot}_e_back_to_search",
        "Click the search input field again at the top of the Douyin window (placeholder may be replaced with the previous query). One click action.",
    )
    focus_douyin()
    # Cmd+A then Backspace to clear
    pi.press_key(0, [55])  # Cmd+A
    time.sleep(0.3)
    pi.press_key(51)       # Backspace
    time.sleep(0.5)


if __name__ == "__main__":
    queries = sys.argv[1:] or ["成都相亲", "贵阳相亲", "重庆相亲"]
    for idx, q in enumerate(queries, start=1):
        print(f"\n############ {idx}. SEARCH: {q} ############")
        search_and_follow(q, idx)
    print("\nALL DONE")
