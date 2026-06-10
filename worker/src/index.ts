/**
 * Cloudflare Worker port of the FastAPI bridge in `bridge/server.py`.
 *
 * Same OpenAI-compat surface as Google's Gemini endpoint:
 *   POST /v1beta/openai/chat/completions
 *
 * Auth + wallet: bearer token from `Authorization: Bearer pc_…`, looked up in
 * the D1 `tokens` table. Each token is a PREPAID WALLET holding a dollar
 * balance (integer micro-USD). Per request: validate the token, refuse with
 * 402 if its balance is spent, forward to Google, then meter the REAL Gemini
 * cost from the response `usage` block, apply a markup (operator margin), and
 * atomically deduct from the balance. The remaining balance is echoed back in
 * the `X-Quota-Remaining-Usd` header so the client can show it to the user.
 *
 * Forwarding: re-signs with the operator's GEMINI_API_KEY (Worker secret),
 * forwards to Google, returns the response unchanged (plus the quota header).
 *
 * Logs: one JSON line per request via console.log — visible in `wrangler tail`
 * for live debug and persistable to R2 via Logpush.
 */

interface Env {
    DB: D1Database;
    GEMINI_API_KEY: string;
    // GitHub PAT with `repo` scope on the private todanley/PC repo. The
    // /download/LuLuBot.exe handler uses it to fetch the latest release
    // asset server-side so CN users only ever connect to bridge.z1nexusn1.org
    // (which is verified working in China; github.com / objects.githubusercontent.com
    // are not reliably reachable). Store via:
    //   wrangler secret put GH_TOKEN
    GH_TOKEN: string;
    // GitHub Release asset ID for the current LuLuBot.exe. Bumped each
    // build via `wrangler secret put LULUBOT_ASSET_ID`. Looking up the
    // latest release dynamically would also work but locks us into the
    // GitHub API rate limit on every download, which is cheaper to dodge.
    LULUBOT_ASSET_ID: string;
}

const UPSTREAM =
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions";

// ── Pricing + margin ────────────────────────────────────────────────────────
// Per-model list price in micro-USD per 1,000,000 tokens. gemini-3-pro-preview
// is tiered: a cheaper rate for prompts up to ~200k tokens, a higher rate above
// it. VERIFY these against Google's current pricing page before relying on the
// margin — they're a single dated constant block on purpose.
//   (values as of 2026-05; phantom-click sends ~1-2k prompt tokens/turn so the
//    low tier dominates; the high tier is defensive.)
interface Price {
    inLowUusdPerM: number;
    outLowUusdPerM: number;
    inHighUusdPerM: number;
    outHighUusdPerM: number;
    tierSplit: number;
}
// Gemini 3 / 3.1 Pro Preview share the same rates (verified against the
// public pricing page, 2026-05). Both ids are mapped so a lookup never falls
// back unexpectedly; DEFAULT_PRICING also points here.
const GEMINI_3_PRO: Price = {
    inLowUusdPerM: 2_000_000, outLowUusdPerM: 12_000_000, // <= 200k prompt
    inHighUusdPerM: 4_000_000, outHighUusdPerM: 18_000_000, // > 200k prompt
    tierSplit: 200_000,
};
const PRICING: Record<string, Price> = {
    "gemini-3-pro-preview": GEMINI_3_PRO,
    "gemini-3.1-pro-preview": GEMINI_3_PRO,
};
const DEFAULT_PRICING: Price = GEMINI_3_PRO;

// Markup in basis points (/10000). 20000 = 2.0x = ~50% operator margin: a $5
// token covers ~$2.50 of real Gemini. Fold AUD↔USD into this too if selling in
// a different currency than Google bills in.
const MARKUP_BP = 20_000;

interface TokenRow {
    label: string;
    tier: string | null;
    balance_uusd: number;
}

// Real Gemini cost of one call, marked up, in micro-USD. ceil at each step so
// rounding always favors the operator (never under-charges within a tier).
function costMicroUsd(model: string, promptTokens: number, completionTokens: number): number {
    const p = PRICING[model] ?? DEFAULT_PRICING;
    const high = promptTokens > p.tierSplit;
    const inRate = high ? p.inHighUusdPerM : p.inLowUusdPerM;
    const outRate = high ? p.outHighUusdPerM : p.outLowUusdPerM;
    const raw =
        Math.ceil((promptTokens * inRate) / 1_000_000) +
        Math.ceil((completionTokens * outRate) / 1_000_000);
    return Math.ceil((raw * MARKUP_BP) / 10_000);
}

function logLine(record: Record<string, unknown>): void {
    console.log(
        JSON.stringify({
            ts: new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"),
            ...record,
        }),
    );
}

function utcNow(): string {
    return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

export default {
    async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
        const url = new URL(request.url);

        if (url.pathname === "/healthz") {
            return Response.json({ ok: true, worker: true });
        }

        // CN-accessible download proxy for the built LuLuBot.exe.
        // The artifact is published on GitHub Releases (which is unreliable
        // from inside China), so we proxy through Cloudflare's edge — CN
        // users only ever connect to bridge.z1nexusn1.org which is verified
        // working from CN (the bridge itself runs through the same route),
        // and CF fetches from GitHub server-side and streams the response
        // back. Cloudflare's CDN caches the response so repeat downloads
        // hit the edge instead of GitHub.
        //
        // Stable URL for handout: https://bridge.z1nexusn1.org/download/LuLuBot.exe
        // Migrate to R2 once the operator enables R2 on the CF account.
        //
        // GitHub release-asset downloads on PRIVATE repos require auth.
        // Two-step dance: hit the API endpoint with our PAT, GitHub returns
        // a 302 to a short-lived signed objects.githubusercontent.com URL,
        // then we fetch that signed URL withOUT our Authorization header
        // (sending it would make S3 reject the request as overspecified).
        if (url.pathname === "/download/LuLuBot.exe" && request.method === "GET") {
            const apiUrl = `https://api.github.com/repos/todanley/PC/releases/assets/${env.LULUBOT_ASSET_ID}`;
            const step1 = await fetch(apiUrl, {
                headers: {
                    Authorization: `Bearer ${env.GH_TOKEN}`,
                    Accept: "application/octet-stream",
                    "User-Agent": "LuLuBot-bridge",
                },
                redirect: "manual",
            });
            if (step1.status !== 302) {
                logLine({
                    event: "download_step1_unexpected",
                    status: step1.status,
                    asset: env.LULUBOT_ASSET_ID,
                });
                return new Response(`download not available (gh step 1 ${step1.status})`, { status: 502 });
            }
            const signedUrl = step1.headers.get("location");
            if (!signedUrl) {
                return new Response("download not available (no redirect target)", { status: 502 });
            }
            // Fetch the actual binary from the signed URL — no auth.
            // Edge caching DISABLED for now: cache key would be the URL
            // alone, so when we re-point LULUBOT_ASSET_ID at a new build
            // the cached old binary keeps returning until the TTL expires.
            // Pinning a versioned cacheKey would solve that, but until
            // builds are stable enough that we trust them not to ship a
            // bad binary, "always fetch from GitHub" is safer than
            // "cached wrong file for 24h". GitHub's CDN handles the cost.
            const upstream = await fetch(signedUrl);
            const headers = new Headers();
            headers.set("content-type", "application/octet-stream");
            const cl = upstream.headers.get("content-length");
            if (cl) headers.set("content-length", cl);
            headers.set("content-disposition", 'attachment; filename="LuLuBot.exe"');
            headers.set("cache-control", "no-store");
            return new Response(upstream.body, {
                status: upstream.status,
                headers,
            });
        }

        if (
            url.pathname !== "/v1beta/openai/chat/completions" ||
            request.method !== "POST"
        ) {
            return new Response("not found", { status: 404 });
        }

        const t0 = Date.now();
        const remote = request.headers.get("cf-connecting-ip") || "?";

        // --- 1. Auth: bearer token must be in tokens table ---
        const auth = request.headers.get("authorization");
        if (!auth || !auth.toLowerCase().startsWith("bearer ")) {
            logLine({ event: "reject", reason: "no_bearer", remote });
            return new Response("missing bearer token", { status: 401 });
        }
        const token = auth.slice(7).trim();

        // --- 2. Wallet check: token must exist and have balance left ---
        const row = await env.DB.prepare(
            "SELECT label, tier, balance_uusd FROM tokens WHERE token = ?1",
        )
            .bind(token)
            .first<TokenRow>();

        if (!row) {
            logLine({
                event: "reject",
                reason: "bad_token",
                token_prefix: token.slice(0, 8),
                remote,
            });
            return new Response("invalid bearer token", { status: 401 });
        }
        if (row.balance_uusd <= 0) {
            logLine({
                event: "reject",
                reason: "quota_exhausted",
                label: row.label,
                tier: row.tier,
                remote,
            });
            // 402 Payment Required — machine-parseable so the client can map it
            // to a "buy a new token" prompt.
            return new Response(
                JSON.stringify({ error: "quota_exhausted", balance_usd: 0 }),
                { status: 402, headers: { "content-type": "application/json" } },
            );
        }

        const { label, tier, balance_uusd: balanceBefore } = row;
        const now = utcNow();

        // --- 3. Forward to Google ---
        const body = await request.arrayBuffer();
        let model: string | null = null;
        try {
            model = JSON.parse(new TextDecoder().decode(body)).model ?? null;
        } catch {
            /* unparseable body; logged as null model */
        }

        let upstreamResp: Response;
        try {
            upstreamResp = await fetch(UPSTREAM, {
                method: "POST",
                headers: {
                    Authorization: `Bearer ${env.GEMINI_API_KEY}`,
                    "Content-Type": "application/json",
                },
                body,
            });
        } catch (e) {
            const latency_ms = Date.now() - t0;
            logLine({
                event: "upstream_error",
                label,
                model,
                remote,
                latency_ms,
                error: e instanceof Error ? e.message : String(e),
            });
            return new Response(`upstream error: ${e}`, { status: 502 });
        }

        const respBody = await upstreamResp.arrayBuffer();
        const latency_ms = Date.now() - t0;

        // --- 4. Meter real usage and deduct (only on a 2xx body with usage) ---
        let promptTokens = 0;
        let completionTokens = 0;
        let billedOutput = 0;
        let totalTokens = 0;
        let costUusd = 0;
        let remainingUusd = balanceBefore;
        if (upstreamResp.ok) {
            try {
                const j = JSON.parse(new TextDecoder().decode(respBody)) as {
                    usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number };
                };
                const u = j.usage;
                if (u && typeof u.prompt_tokens === "number" && typeof u.completion_tokens === "number") {
                    promptTokens = u.prompt_tokens;
                    completionTokens = u.completion_tokens;
                    totalTokens = typeof u.total_tokens === "number" ? u.total_tokens : 0;
                    // Gemini bills "thinking"/reasoning tokens at the OUTPUT rate
                    // but leaves them OUT of completion_tokens — they only show
                    // up in total_tokens. Charging just completion_tokens would
                    // give reasoning away free (turns the markup negative on
                    // thinking-heavy calls). Bill every non-prompt token at the
                    // output rate.
                    billedOutput = Math.max(completionTokens, totalTokens - promptTokens);
                    costUusd = costMicroUsd(model ?? "", promptTokens, billedOutput);
                }
            } catch {
                /* non-JSON / streamed body: nothing to meter, deduct nothing */
            }
        }
        if (costUusd > 0) {
            // Single atomic UPDATE; SQLite serializes writes so the stored
            // balance stays consistent. MAX(0, …) keeps it non-negative even if
            // the final call slightly overshoots (pre-check only required > 0).
            const after = await env.DB.prepare(
                `
                UPDATE tokens
                SET balance_uusd = MAX(0, balance_uusd - ?2),
                    spent_uusd   = spent_uusd + ?2,
                    calls_total  = calls_total + 1,
                    last_call_at = ?3
                WHERE token = ?1
                RETURNING balance_uusd
                `,
            )
                .bind(token, costUusd, now)
                .first<{ balance_uusd: number }>();
            if (after) remainingUusd = after.balance_uusd;
        }

        logLine({
            event: "forward",
            label,
            tier,
            model,
            status: upstreamResp.status,
            remote,
            latency_ms,
            req_bytes: body.byteLength,
            resp_bytes: respBody.byteLength,
            prompt_tokens: promptTokens,
            completion_tokens: completionTokens,
            total_tokens: totalTokens,
            billed_output: billedOutput,
            cost_uusd: costUusd,
            remaining_uusd: remainingUusd,
        });

        return new Response(respBody, {
            status: upstreamResp.status,
            headers: {
                "content-type":
                    upstreamResp.headers.get("content-type") ?? "application/json",
                "X-Quota-Remaining-Usd": (remainingUusd / 1_000_000).toFixed(6),
            },
        });
    },
} satisfies ExportedHandler<Env>;
