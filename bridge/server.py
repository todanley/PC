"""Local HTTP bridge that lets mainland-CN clients reach Gemini through the
operator's API key + quota.

Shape: drop-in for `generativelanguage.googleapis.com/v1beta/openai/chat/completions`.
Auth: bearer token issued via `python -m bridge.issue_token <label>`.
Quota: each token has a `max_calls_per_day` cap; counters live in tokens.json
       and reset at UTC midnight. Requests beyond the cap return 429.
Forwarding: rewrites the inbound `Authorization` header to the operator's
GEMINI_API_KEY before calling Google.

Run with `./run-bridge.sh` — it also starts a Cloudflare tunnel so the
endpoint is reachable from CN without router setup.
"""
import asyncio
import json
import os
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response

BRIDGE_DIR = Path(__file__).resolve().parent
TOKENS_FILE = BRIDGE_DIR / "tokens.json"
ACCESS_LOG = BRIDGE_DIR / "access.log"

UPSTREAM = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
UPSTREAM_TIMEOUT_S = 600  # match phantom-click's urlopen timeout

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY not set — the bridge needs the operator's Gemini key "
        "to forward requests upstream."
    )

# Per-IP rate limit: how many requests per IP per window.
# Defaults give a single CN user plenty of headroom for a long task
# (phantom-click bursts ~1 req/s during a fast run) while stopping a single
# abuser from draining the daily pool in minutes.
RATE_LIMIT_PER_IP = int(os.environ.get("BRIDGE_RATE_LIMIT_PER_IP", "30"))
RATE_LIMIT_WINDOW_S = int(os.environ.get("BRIDGE_RATE_LIMIT_WINDOW_S", "60"))

# Single asyncio.Lock guards every read-modify-write of tokens.json. The file
# is small (<10KB even with hundreds of tokens) so we just rewrite the whole
# thing atomically on each mutation — simpler than a real DB and recovers
# cleanly from a mid-write crash.
_TOKENS_LOCK = asyncio.Lock()

# Per-IP sliding window. Each value is a deque of monotonic timestamps of
# recent requests; we evict entries older than the window when we check. The
# whole dict is single-event-loop so no extra lock needed beyond what async
# gives us. NOT persisted — restarting the bridge clears counters, which is
# fine (window is short enough that this isn't exploitable).
_IP_HITS: dict[str, deque[float]] = {}


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _load_tokens_sync() -> dict[str, dict]:
    if not TOKENS_FILE.exists():
        return {}
    try:
        return json.loads(TOKENS_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def _save_tokens_atomic(tokens: dict) -> None:
    # tmp + rename so a crash mid-write can't corrupt the file.
    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir=BRIDGE_DIR, prefix=".tokens.", suffix=".tmp",
        delete=False, encoding="utf-8",
    )
    try:
        json.dump(tokens, tmp, indent=2, ensure_ascii=False)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, TOKENS_FILE)
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _log(record: dict) -> None:
    record["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = json.dumps(record, ensure_ascii=False)
    print(line, flush=True)
    with ACCESS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


app = FastAPI(title="phantom-click gemini bridge")

# Single shared httpx client — connection pooling matters when the same CN
# client fires 10+ turns in a single phantom-click run.
_HTTP = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_S, http2=True)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "tokens_loaded": len(_load_tokens_sync())}


@app.post("/v1beta/openai/chat/completions")
async def forward(request: Request, authorization: str | None = Header(None)):
    t0 = time.monotonic()
    # Behind Cloudflare Tunnel: trust CF-Connecting-IP for the real client
    # IP. Falling back to request.client.host would always show 127.0.0.1.
    # If you ever run the bridge open to the public internet without CF, drop
    # this fallback — it'd let any client spoof its IP via that header.
    remote = (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "?")
    )

    # Per-IP rate limit: slide the window, evict old hits, count what's left.
    now = time.monotonic()
    hits = _IP_HITS.setdefault(remote, deque())
    cutoff = now - RATE_LIMIT_WINDOW_S
    while hits and hits[0] < cutoff:
        hits.popleft()
    if len(hits) >= RATE_LIMIT_PER_IP:
        _log({"event": "reject", "reason": "rate_limited",
              "remote": remote, "hits": len(hits),
              "limit": RATE_LIMIT_PER_IP, "window_s": RATE_LIMIT_WINDOW_S})
        raise HTTPException(
            429,
            f"rate limit: max {RATE_LIMIT_PER_IP} requests per "
            f"{RATE_LIMIT_WINDOW_S}s per IP.",
        )
    hits.append(now)

    # Auth: client sends `Authorization: Bearer <bridge-token>` — NOT the
    # Gemini key. The bridge looks it up against tokens.json and swaps in
    # the real key before forwarding.
    if not authorization or not authorization.lower().startswith("bearer "):
        _log({"event": "reject", "reason": "no_bearer", "remote": remote})
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(None, 1)[1].strip()

    # Quota check + counter bump, atomically under the lock. We bump BEFORE
    # the upstream call so a flood of concurrent requests can't all observe
    # the same "still under cap" snapshot and slip through together.
    async with _TOKENS_LOCK:
        tokens = _load_tokens_sync()
        entry = tokens.get(token)
        if not entry:
            _log({"event": "reject", "reason": "bad_token",
                  "token_prefix": token[:8], "remote": remote})
            raise HTTPException(401, "invalid bearer token")
        label = entry.get("label", "?")
        cap = entry.get("max_calls_per_day")
        today = _utc_today()
        if entry.get("day") != today:
            entry["day"] = today
            entry["calls_today"] = 0
        # cap=None means unlimited; cap=0 means hard-disabled.
        if isinstance(cap, int) and entry["calls_today"] >= cap:
            _log({"event": "reject", "reason": "quota_exceeded",
                  "label": label, "cap": cap,
                  "calls_today": entry["calls_today"], "remote": remote})
            raise HTTPException(
                429,
                f"daily quota of {cap} calls exhausted for token '{label}'. "
                "Resets at UTC midnight.",
            )
        entry["calls_today"] = entry.get("calls_today", 0) + 1
        # Track lifetime total too — handy for billing reconciliation.
        entry["calls_total"] = entry.get("calls_total", 0) + 1
        entry["last_call_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _save_tokens_atomic(tokens)
        calls_today_after = entry["calls_today"]

    body_bytes = await request.body()

    # Best-effort: pull the model name out of the body for the log line.
    model = None
    try:
        model = json.loads(body_bytes).get("model")
    except Exception:
        pass

    headers = {
        "Authorization": f"Bearer {GEMINI_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        upstream_resp = await _HTTP.post(UPSTREAM, content=body_bytes, headers=headers)
    except httpx.HTTPError as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        _log({"event": "upstream_error", "label": label, "model": model,
              "remote": remote, "latency_ms": latency_ms, "error": str(e)})
        raise HTTPException(502, f"upstream error: {e}")

    latency_ms = int((time.monotonic() - t0) * 1000)
    _log({
        "event": "forward",
        "label": label,
        "model": model,
        "status": upstream_resp.status_code,
        "remote": remote,
        "latency_ms": latency_ms,
        "req_bytes": len(body_bytes),
        "resp_bytes": len(upstream_resp.content),
        "calls_today": calls_today_after,
        "cap": cap,
    })

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type", "application/json"),
    )
