#!/usr/bin/env python3
"""Comment and favorite 10 XHS posts. Records post URLs."""
from phantom import PhantomInput, PhantomScreen
import subprocess, time

pi = PhantomInput()
ps = PhantomScreen()

comments = [
    "太好看了！", "写得真好", "收藏了谢谢分享", "好棒好棒", "学到了！",
    "太实用了吧", "哇这个好厉害", "感谢分享！", "绝了绝了", "mark一下"
]

def get_url():
    return subprocess.run(["osascript", "-e",
        'tell application "Google Chrome" to get URL of active tab of front window'],
        capture_output=True, text=True, timeout=5).stdout.strip()

def go_search():
    subprocess.run(["osascript", "-e",
        'tell application "Google Chrome" to set URL of active tab of front window to "https://www.xiaohongshu.com/search_result?keyword=水豚&type=1"'
    ], timeout=10)
    time.sleep(5)
    pi.press_key(53)  # Close any popups
    time.sleep(1)

# Bring Chrome to front
subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'], timeout=5)
time.sleep(2)

post_urls = []
done = 0

# Post grid positions (logical coords) - 5 columns, 2 rows
# x positions: ~350, 550, 750, 950, 1150 (safely past sidebar)
# y positions: row1 ~350, row2 ~600
grid = [
    (350, 350), (550, 350), (750, 350), (950, 350), (1150, 350),
    (350, 600), (550, 600), (750, 600), (950, 600), (1150, 600),
]

go_search()

for i, (gx, gy) in enumerate(grid):
    if done >= 10:
        break

    print(f"\n=== Post {done+1}/10 ===")

    # Click on post
    pi.click(gx, gy, smooth=True)
    time.sleep(5)

    url = get_url()
    if "/explore/" not in url or "search_result" in url:
        print(f"  Miss ({url[:60]}), trying next")
        pi.press_key(53)
        time.sleep(1)
        continue

    print(f"  Opened: {url[:80]}")
    post_urls.append(url)

    # Comment input "说点什么..." is at bottom of right panel
    # From screenshot: approximately x=830, y=760
    pi.click(830, 760, smooth=True)
    time.sleep(1)

    # Type comment
    comment = comments[done]
    print(f"  Typing: {comment}")
    pi.type_text(comment, wpm=60)
    time.sleep(1)

    # Press Enter to submit (or Cmd+Enter)
    pi.press_key(36)
    time.sleep(2)

    # Click favorite/star icon (⭐ at bottom action bar)
    # From screenshot: star is at approximately x=810, y=800
    pi.click(810, 800, smooth=True)
    time.sleep(1)

    done += 1
    print(f"  Done! ({done}/10)")

    ps.capture(f"/tmp/xhs_done_{done}.png")

    # Close modal
    pi.press_key(53)
    time.sleep(2)

    # If we need a second row, scroll down
    if i == 4:
        pi.click(600, 400)
        time.sleep(0.3)
        for _ in range(8):
            pi.scroll(dy=-4)
            time.sleep(0.1)
        time.sleep(2)

print(f"\n\n=== {done} posts commented and favorited ===")
print("\nPost links (click to see your comments):")
for j, url in enumerate(post_urls, 1):
    print(f"{j}. {url}")
