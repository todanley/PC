#!/usr/bin/env python3
"""Comment and favorite 5 低评论 douyin videos."""
from phantom import PhantomInput, PhantomScreen
import subprocess, time
from PIL import Image
import numpy as np

pi = PhantomInput()
ps = PhantomScreen()

subprocess.run(["open", "-a", "抖音"], timeout=5)
time.sleep(3)

IX = 1595
CY = 570
SY = 665

comments = ["太好看了", "绝绝子", "哈哈哈笑死", "太厉害了吧", "好可爱啊"]
done = 0

for a in range(30):
    if done >= 5:
        break
    print(f"\nVideo {a+1} ({done}/5)")
    pi.press_key(49)
    time.sleep(1.5)
    ss = f"/tmp/dy_a{a}.png"
    ps.capture(ss)
    arr = np.array(Image.open(ss).convert("RGB"))
    b = int(np.sum(np.mean(arr[1100:1160, 3100:3320, :], axis=2) > 180))
    print(f"  bright={b}")
    if b > 200:
        print("  SKIP")
        pi.press_key(49)
        time.sleep(0.5)
        pi.press_key(125)
        time.sleep(3)
        continue
    print("  Commenting!")
    pi.click(IX, SY, smooth=True)
    time.sleep(1.5)
    pi.click(IX, CY, smooth=True)
    time.sleep(3)
    pi.click(1400, 1050, smooth=True)
    time.sleep(1)
    print(f"  Typing: {comments[done]}")
    pi.type_text(comments[done], wpm=60)
    time.sleep(1)
    pi.press_key(36)
    time.sleep(2)
    done += 1
    print(f"  Done ({done}/5)")
    pi.press_key(53)
    time.sleep(0.5)
    pi.press_key(49)
    time.sleep(0.5)
    pi.press_key(125)
    time.sleep(3)

print(f"\n=== {done} videos commented and favorited ===")
