/**
 * Cloudflare Worker port of the FastAPI bridge in `bridge/server.py`.
 *
 * Same OpenAI-compat surface as Google's Gemini endpoint:
 *   POST /v1beta/openai/chat/completions
 *
 * Auth: bearer token from `Authorization: Bearer pc_…`. Looked up against the
 * D1 `tokens` table. Atomic SQL UPDATE checks + bumps the daily counter in
 * one round-trip; if the UPDATE matches zero rows, we distinguish "no such
 * token" from "quota exhausted" with a follow-up SELECT.
 *
 * Forwarding: re-signs with the operator's GEMINI_API_KEY (Worker secret),
 * forwards to Google, streams the response back unchanged.
 *
 * Logs: one JSON line per request via console.log — visible in `wrangler tail`
 * for live debug and persistable to R2 via Logpush.
 */

interface Env {
    DB: D1Database;
    GEMINI_API_KEY: string;
}

const UPSTREAM =
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions";

interface TokenRow {
    label: string;
    max_calls_per_day: number | null;
    calls_today: number;
    day: string | null;
}

function logLine(record: Record<string, unknown>): void {
    console.log(
        JSON.stringify({
            ts: new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"),
            ...record,
        }),
    );
}

function utcToday(): string {
    return new Date().toISOString().slice(0, 10);
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

        // --- 2. Atomic quota check + counter bump ---
        // The CASE expression resets calls_today to 0 (well, "1 after the
        // bump") whenever the stored `day` differs from today — first call of
        // a new UTC day rolls the counter automatically. The WHERE clause
        // refuses the UPDATE when today's count would exceed the cap, so
        // RETURNING gives us NULL on both "no such token" and "quota
        // exhausted" — we disambiguate below with a SELECT.
        const today = utcToday();
        const now = utcNow();
        const updated = await env.DB.prepare(
            `
            UPDATE tokens
            SET calls_today = CASE WHEN day = ?1 THEN calls_today + 1 ELSE 1 END,
                day = ?1,
                calls_total = calls_total + 1,
                last_call_at = ?2
            WHERE token = ?3
              AND (
                max_calls_per_day IS NULL
                OR max_calls_per_day = 0
                OR (CASE WHEN day = ?1 THEN calls_today ELSE 0 END) < max_calls_per_day
              )
            RETURNING label, max_calls_per_day, calls_today
            `,
        )
            .bind(today, now, token)
            .first<TokenRow>();

        if (!updated) {
            // Either token unknown OR quota exhausted. Disambiguate.
            const check = await env.DB.prepare(
                "SELECT label, max_calls_per_day, calls_today, day FROM tokens WHERE token = ?1",
            )
                .bind(token)
                .first<TokenRow>();
            if (!check) {
                logLine({
                    event: "reject",
                    reason: "bad_token",
                    token_prefix: token.slice(0, 8),
                    remote,
                });
                return new Response("invalid bearer token", { status: 401 });
            }
            logLine({
                event: "reject",
                reason: "quota_exceeded",
                label: check.label,
                cap: check.max_calls_per_day,
                calls_today: check.calls_today,
                remote,
            });
            return new Response(
                `daily quota of ${check.max_calls_per_day} calls exhausted for token '${check.label}'. Resets at UTC midnight.`,
                { status: 429 },
            );
        }

        const { label, max_calls_per_day: cap, calls_today: callsToday } = updated;

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

        logLine({
            event: "forward",
            label,
            model,
            status: upstreamResp.status,
            remote,
            latency_ms,
            req_bytes: body.byteLength,
            resp_bytes: respBody.byteLength,
            calls_today: callsToday,
            cap,
        });

        return new Response(respBody, {
            status: upstreamResp.status,
            headers: {
                "content-type":
                    upstreamResp.headers.get("content-type") ?? "application/json",
            },
        });
    },
} satisfies ExportedHandler<Env>;
