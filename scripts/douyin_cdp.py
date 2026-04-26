#!/usr/bin/env python3
"""Drive Douyin desktop (Electron) via CDP WebSocket — no vision, no pixels.

Prereq: Douyin must be launched with --remote-debugging-port=9222.
  /Applications/抖音.app/Contents/MacOS/抖音 --remote-debugging-port=9222 &

Usage:
  python3 scripts/douyin_cdp.py 成都相亲 贵阳相亲 重庆相亲
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import websockets

CDP_HTTP = "http://127.0.0.1:9222"
PROOF_DIR = Path("/tmp/phantom_proofs_cdp")
PROOF_DIR.mkdir(exist_ok=True)


def get_page_ws() -> tuple[str, str]:
    targets = json.loads(urlopen(f"{CDP_HTTP}/json").read())
    for t in targets:
        if t.get("type") == "page" and "douyin.com" in t.get("url", ""):
            return t["webSocketDebuggerUrl"], t["url"]
    raise RuntimeError("No douyin.com page target found")


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.id = 0

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.id += 1
        msg = {"id": self.id, "method": method, "params": params or {}}
        await self.ws.send(json.dumps(msg))
        # Drain until we get our reply id
        while True:
            raw = await self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == self.id:
                if "error" in data:
                    raise RuntimeError(f"{method} error: {data['error']}")
                return data.get("result", {})

    async def eval(self, expression: str, await_promise: bool = True, return_by_value: bool = True) -> dict:
        result = await self.send("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": return_by_value,
            "timeout": 15000,
        })
        if result.get("exceptionDetails"):
            raise RuntimeError(f"JS exception: {result['exceptionDetails'].get('text')} :: {result['exceptionDetails'].get('exception',{}).get('description','')}")
        return result.get("result", {}).get("value")

    async def screenshot(self, label: str) -> str:
        # via Page.captureScreenshot (CDP-native, no need for screen)
        result = await self.send("Page.captureScreenshot", {"format": "png"})
        import base64
        path = str(PROOF_DIR / f"{label}.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(result["data"]))
        return path

    async def click_at(self, x: float, y: float):
        """Real browser-level mouse click via CDP Input domain (passes React's isTrusted check)."""
        common = {"x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1}
        await self.send("Input.dispatchMouseEvent", {**common, "type": "mouseMoved", "buttons": 0, "clickCount": 0})
        await self.send("Input.dispatchMouseEvent", {**common, "type": "mousePressed"})
        await self.send("Input.dispatchMouseEvent", {**common, "type": "mouseReleased"})


async def search_and_follow(cdp: CDP, query: str, slot: int):
    print(f"\n############ {slot}. SEARCH: {query} ############")

    # 1. Navigate to search results URL directly (most reliable)
    search_url = f"https://www.douyin.com/search/{query}?type=user"
    print(f"  navigate: {search_url}")
    await cdp.send("Page.navigate", {"url": search_url})

    # 2. Poll until at least one 关注 button is rendered (max 20s)
    deadline = time.time() + 20
    follow_targets = []
    while time.time() < deadline:
        follow_targets = await cdp.eval("""
        (() => {
          const all = Array.from(document.querySelectorAll('button'));
          const candidates = all.filter(el => (el.innerText || '').trim() === '关注');
          const visible = candidates.filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          }).sort((a, b) => {
            const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
            if (Math.abs(ra.top - rb.top) > 10) return ra.top - rb.top;
            return ra.left - rb.left;
          });
          return visible.slice(0, 10).map((el, i) => {
            const r = el.getBoundingClientRect();
            let card = el;
            for (let j = 0; j < 6 && card.parentElement; j++) card = card.parentElement;
            const nameEl = card.querySelector('[class*="title"], [class*="name"], h3, h4');
            const name = nameEl ? (nameEl.innerText || '').trim().split('\\n')[0].slice(0, 40) : '?';
            return { idx: i, x: r.left + r.width/2, y: r.top + r.height/2, name };
          });
        })()
        """)
        if follow_targets:
            break
        await asyncio.sleep(0.7)

    await cdp.screenshot(f"{slot}_a_user_results")

    print(f"  found {len(follow_targets)} '关注' buttons")
    for t in follow_targets[:8]:
        print(f"    [{t['idx']}] @({t['x']:.0f},{t['y']:.0f}) name={t['name']}")

    # 3. Follow top 5 — real CDP mouse clicks at button center
    followed = []
    for i in range(5):
        # Re-query each iteration: locate the i-th '关注' (skipping 已关注), get center coords + name
        info = await cdp.eval("""
        (() => {
          const buttons = Array.from(document.querySelectorAll('button'))
            .filter(el => (el.innerText||'').trim() === '关注')
            .filter(el => { const r = el.getBoundingClientRect(); return r.width>0 && r.height>0; })
            .sort((a,b) => {
              const ra=a.getBoundingClientRect(), rb=b.getBoundingClientRect();
              if (Math.abs(ra.top-rb.top)>10) return ra.top-rb.top;
              return ra.left-rb.left;
            });
          if (buttons.length === 0) return { ok:false, reason:'no 关注 left' };
          const target = buttons[0];  // always the first remaining
          target.scrollIntoView({ behavior:'instant', block:'center' });
          // Read again post-scroll
          const r = target.getBoundingClientRect();
          let card = target;
          for (let j=0; j<6 && card.parentElement; j++) card = card.parentElement;
          const candidates = card.querySelectorAll('a span, h1, h2, h3, h4, div');
          let name = '?';
          for (const c of candidates) {
            const t = (c.innerText||'').trim();
            if (t && t.length>1 && t.length<40 && !['关注','已关注','发过相关视频','认证徽章','心理健康指导师','私信'].includes(t)) {
              name = t.split('\\n')[0]; break;
            }
          }
          return { ok:true, x: r.left + r.width/2, y: r.top + r.height/2, name };
        })()
        """)
        if not info or not info.get("ok"):
            print(f"  follow {i+1}: {info}")
            followed.append(info)
            break

        print(f"  follow {i+1}: clicking '{info['name']}' at ({info['x']:.0f},{info['y']:.0f})")
        await cdp.click_at(info["x"], info["y"])
        await asyncio.sleep(1.6)

        # Verify state after click
        verify = await cdp.eval(f"""
        (() => {{
          const all = Array.from(document.querySelectorAll('button'));
          const followed = all.filter(el => {{
            const t = (el.innerText||'').trim();
            return t === '已关注' || t === '互相关注';
          }}).length;
          return {{ followedCount: followed }};
        }})()
        """)
        print(f"    -> followed-state buttons now: {verify['followedCount']}")
        followed.append({**info, "verify": verify})
        await cdp.screenshot(f"{slot}_b{i+1}_after_follow")

    return followed


async def main(queries: list[str]):
    ws_url, page_url = get_page_ws()
    print(f"connecting: {ws_url}")
    print(f"current page: {page_url}")
    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        cdp = CDP(ws)
        await cdp.send("Page.enable")
        await cdp.send("Runtime.enable")
        results = {}
        for idx, q in enumerate(queries, start=1):
            results[q] = await search_and_follow(cdp, q, idx)
        print("\n############ SUMMARY ############")
        for q, r in results.items():
            print(f"{q}:")
            for f in r:
                print(f"  - {f}")


if __name__ == "__main__":
    queries = sys.argv[1:] or ["成都相亲", "贵阳相亲", "重庆相亲"]
    asyncio.run(main(queries))
