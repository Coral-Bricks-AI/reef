import type { VercelRequest, VercelResponse } from '@vercel/node'

/**
 * Inlined GP Verdict Gemini logic (no separate module — Vercel doesn't bundle
 * sibling imports). Exported for dev-api-vc-judge.ts.
 */

const reportScoresSchema = {
  type: 'object',
  properties: {
    data_accuracy: { type: 'integer' },
    strategic_moat_analysis: { type: 'integer' },
    financial_rigor: { type: 'integer' },
  },
  required: ['data_accuracy', 'strategic_moat_analysis', 'financial_rigor'],
}

const VC_JUDGE_RESPONSE_SCHEMA = {
  type: 'object',
  properties: {
    summary_table_markdown: { type: 'string' },
    scores: {
      type: 'object',
      properties: {
        coral_bricks: reportScoresSchema,
        chatgpt: reportScoresSchema,
        perplexity: reportScoresSchema,
        grok: reportScoresSchema,
      },
      required: ['coral_bricks', 'chatgpt', 'perplexity', 'grok'],
    },
    narrative_markdown: { type: 'string' },
    final_verdict: { type: 'string' },
  },
  required: [
    'summary_table_markdown',
    'scores',
    'narrative_markdown',
    'final_verdict',
  ],
}

type VcJudgeReportScores = {
  data_accuracy: number
  strategic_moat_analysis: number
  financial_rigor: number
}

type VcJudgeScores = {
  coral_bricks: VcJudgeReportScores
  chatgpt: VcJudgeReportScores
  perplexity: VcJudgeReportScores
  grok: VcJudgeReportScores
}

type VcJudgeVerdictJson = {
  summary_table_markdown: string
  scores: VcJudgeScores
  narrative_markdown: string
  final_verdict: string
}

const VC_JUDGE_SYSTEM_INSTRUCTION_WITH_SEARCH = `You are a Senior VC Analyst assistant with access to Google Search grounding.

Use Google Search whenever it improves your judgment—especially to verify financial metrics, check claims against recent public data and 2026-relevant benchmarks, validate company facts, and source regulatory or market context. Run searches deliberately (each executed search is billed to the API project).

Temporal logic: The user message includes a **Real-world reference instant** (server time). Treat that as "today" for deciding whether dated events in the reports are past, future, or impossible. Do not anchor your reasoning to an old training cutoff (e.g. "as of mid-2024") unless you are directly quoting a report.

**Stated scope vs your paraphrase:** Before calling any source "stale," "outdated," or claiming it "stops at" a certain date, re-read that source for an explicit **time range**, **through / as-of date**, or **coverage window** (e.g. "Jan 2024 through Mar 19, 2026"). If the source states a window that extends to or beyond the reference instant, do not claim the source ends earlier unless you clearly separate (i) what period the *events discussed* emphasize from (ii) what window the source *claims* to cover. Do not contradict the source's own stated bounds.

Your reply to the user must be a single JSON object only (no markdown fences), matching the schema enforced by responseMimeType application/json. Use the string fields for any markdown tables or narrative.`

const VC_JUDGE_SYSTEM_INSTRUCTION_NO_SEARCH = `You are a Senior VC Analyst assistant. You do not have live web search—base your analysis on the four reports in the user message and your training knowledge. Note uncertainty where facts cannot be verified.

Temporal logic: The user message includes a **Real-world reference instant** (server time). Treat that as "today" for deciding whether dated events in the reports are past, future, or impossible. Do not default to phrases like "as of mid-2024" or your knowledge cutoff as if it were the present—use the reference instant instead.

**Stated scope vs your paraphrase:** Before calling any source "stale," "outdated," or claiming it "stops at" a certain date, re-read that source for an explicit **time range**, **through / as-of date**, or **coverage window**. If the source states a window that extends to or beyond the reference instant, do not claim the source ends earlier unless you clearly separate (i) the period the *narrative emphasizes* from (ii) the window the source *claims* to cover. Do not contradict the source's own stated bounds.

Your reply must be a single JSON object only (no markdown fences), matching the schema enforced by responseMimeType application/json. Use the string fields for any markdown tables or narrative.`

/** Weights for a single overall score on the same 1–10 scale as the rubric rows. */
const SCORE_WEIGHTS: Record<keyof VcJudgeReportScores, number> = {
  data_accuracy: 0.35,
  strategic_moat_analysis: 0.35,
  financial_rigor: 0.3,
}

function weightedOverall(r: VcJudgeReportScores): number {
  let sum = 0
  for (const k of Object.keys(SCORE_WEIGHTS) as (keyof VcJudgeReportScores)[]) {
    const v = Number(r[k])
    const w = SCORE_WEIGHTS[k]
    if (Number.isFinite(v) && Number.isFinite(w)) sum += w * v
  }
  return Math.round(sum * 10) / 10
}

function formatScoresMarkdownTable(scores: VcJudgeScores): string {
  const header =
    '| Category | Coral Bricks | ChatGPT | Perplexity | Grok |\n| --- | --- | --- | --- | --- |'
  const row = (label: string, sub: keyof VcJudgeReportScores) =>
    `| ${label} | ${scores.coral_bricks[sub]} | ${scores.chatgpt[sub]} | ${scores.perplexity[sub]} | ${scores.grok[sub]} |`
  const overallRow = `| **Overall (weighted)** | ${weightedOverall(scores.coral_bricks)} | ${weightedOverall(scores.chatgpt)} | ${weightedOverall(scores.perplexity)} | ${weightedOverall(scores.grok)} |`
  const weightsNote =
    '\n\n*Overall = 0.35× data accuracy + 0.35× strategic moat + 0.30× financial rigor (same 1–10 scale).*'
  return [
    header,
    row('Data Accuracy', 'data_accuracy'),
    row('Strategic Moat Analysis', 'strategic_moat_analysis'),
    row('Financial Rigor', 'financial_rigor'),
    overallRow,
  ].join('\n') + weightsNote
}

/** Unescape literal \\n in Gemini JSON strings and fix concatenated table rows */
function normalizeVerdictMarkdown(s: string): string {
  // Some models return literal backslash-n instead of newlines
  let out = s.replace(/\\n/g, '\n')
  // Fix markdown tables where rows are concatenated. We can't replace all | | because the
  // header row may have an empty first cell (| | Coral Bricks |). Only split at row
  // boundaries: before alignment row (| :---) or before data rows (| Tone |, | Timeframe |, etc).
  const rowBoundaryPatterns = [
    /\|\s*\|(\s*:[-:]*)/g, // before alignment row: | | :--- or | | :---:
    /\|\s*\|(\s*Tone\b)/gi,
    /\|\s*\|(\s*Timeframe\b)/gi,
    /\|\s*\|(\s*Key Evidence\b)/gi,
    /\|\s*\|(\s*Conclusion\b)/gi,
  ]
  for (const re of rowBoundaryPatterns) {
    out = out.replace(re, '|\n|$1')
  }
  // Also handle || (no space) between rows
  out = out.replace(/\|\|(\s*:[-:]*)/g, '|\n|$1')
  out = out.replace(/\|\|(\s*Tone\b)/gi, '|\n|$1')
  out = out.replace(/\|\|(\s*Timeframe\b)/gi, '|\n|$1')
  out = out.replace(/\|\|(\s*Key Evidence\b)/gi, '|\n|$1')
  out = out.replace(/\|\|(\s*Conclusion\b)/gi, '|\n|$1')
  return out.trim()
}

function buildVerdictMarkdownFromJson(v: VcJudgeVerdictJson): string {
  const parts: string[] = []
  if (v.summary_table_markdown?.trim())
    parts.push(normalizeVerdictMarkdown(v.summary_table_markdown))
  parts.push('### Scores (1–10)\n\n' + formatScoresMarkdownTable(v.scores))
  if (v.narrative_markdown?.trim())
    parts.push(normalizeVerdictMarkdown(v.narrative_markdown))
  parts.push('### Final verdict\n\n' + normalizeVerdictMarkdown(v.final_verdict?.trim() || ''))
  return parts.join('\n\n')
}

/** Strip accidental markdown fences from model output */
function normalizeModelJsonText(s: string): string {
  let t = s.trim()
  if (t.startsWith('```')) {
    t = t
      .replace(/^```(?:json)?\s*\r?\n?/i, '')
      .replace(/\r?\n?```\s*$/i, '')
      .trim()
  }
  return t
}

/**
 * If the model wrapped JSON in prose or sent one object, extract the first
 * `{ ... }` with string-aware brace matching.
 */
function extractFirstCompleteJsonObject(str: string): string | null {
  const start = str.indexOf('{')
  if (start === -1) return null
  let depth = 0
  let inString = false
  let escape = false
  for (let i = start; i < str.length; i++) {
    const c = str[i]!
    if (inString) {
      if (escape) {
        escape = false
        continue
      }
      if (c === '\\') {
        escape = true
        continue
      }
      if (c === '"') inString = false
      continue
    }
    if (c === '"') {
      inString = true
      continue
    }
    if (c === '{') depth++
    else if (c === '}') {
      depth--
      if (depth === 0) return str.slice(start, i + 1)
    }
  }
  return null
}

function tryParseVcJudgeVerdictJson(innerText: string): VcJudgeVerdictJson | null {
  const normalized = normalizeModelJsonText(innerText)
  const chunks = new Set<string>([normalized])
  const extracted = extractFirstCompleteJsonObject(normalized)
  if (extracted) chunks.add(extracted)

  for (const chunk of chunks) {
    try {
      const parsed = JSON.parse(chunk) as VcJudgeVerdictJson
      if (parsed?.scores && parsed.summary_table_markdown != null) return parsed
    } catch {
      /* try next */
    }
  }
  return null
}

function readMaxOutputTokens(): number {
  const raw = process.env.VC_JUDGE_MAX_OUTPUT_TOKENS?.trim()
  const n = raw ? parseInt(raw, 10) : NaN
  if (Number.isFinite(n) && n >= 256) return Math.min(n, 65536)
  return 16_384
}

export function buildVcJudgeUserPrompt(
  query: string,
  coral: string,
  chatgpt: string,
  perplexity: string,
  grok: string,
): string {
  const now = new Date()
  const isoUtc = now.toISOString()
  const humanUtc = now.toUTCString()

  return `I will provide some investment research reports for the same company.

**Real-world reference instant (treat as "today" for all temporal checks—not your training cutoff):** ${humanUtc} (${isoUtc})

**Research context / query:** ${query}

**Coral Bricks** (indexed knowledge base + RAG):
${coral}

**ChatGPT** (web search):
${chatgpt}

**Perplexity** (web search):
${perplexity}

**Grok** (web search):
${grok}

---

Compare these reports and determine which provides a more actionable investment thesis.

**Temporal / recency discipline (read carefully before scoring):**
- For each source, note any **explicit** stated time range, "through" date, or "as of" line in the text. Your **Timeframe** row and narrative must reflect what each source *actually says* about its window—not an assumption based on where the narrative focuses (e.g. heavy 2024 detail plus a stated range through 2026 means you must not claim the source "ends in late 2024" without that distinction).
- If the user asked for **"recent"** events and a source's facts mostly end before the reference instant but its **stated** coverage includes the present, say it may be **incomplete on post-window events** or **emphasis skewed to an earlier wave**—do not misstate its declared coverage.
- Use Google Search (when enabled) to check for **additional** real-world developments after what each source discusses; frame gaps as "may miss X after [period]" rather than rewriting a source's own stated bounds.

**Required JSON fields:**

1. **summary_table_markdown** — Markdown table: columns Coral Bricks | ChatGPT | Perplexity | Grok; row labels Tone, Timeframe, Key Evidence, Conclusion (first column). **Keep each cell to one tight sentence** where possible (long cells bloat the JSON). Put each table row on its own line (use real newlines, not escaped). **Timeframe** must summarize each source's **stated** coverage window where present, plus what period the substantive claims emphasize.

2. **scores** — For each of coral_bricks, chatgpt, perplexity, grok, integers 1–10 for:
   - data_accuracy (judge plausibility vs credible public knowledge / 2026 benchmarks; flag likely hallucinations)
   - strategic_moat_analysis (defensibility)
   - financial_rigor (multiples and margins)

3. **narrative_markdown** — Qualitative discussion covering (be concise so the full JSON fits in one response):
   - Data accuracy & sourcing (Revenue, EBITDA, Burn—verified vs hallucinated)
   - Strategic depth (Porter, SWOT, defensibility)
   - Risk identification (cap table, concentration, regulatory red flags)
   - Forward-looking bull and bear case (≈5 years)
   **Length:** Target **under ~2,000 characters** (~350 words). Prefer short paragraphs or bullets; avoid long quoted passages.

4. **final_verdict** — One clear paragraph (max ~600 characters): which source a GP would trust most for a Monday morning partner meeting.`
}

export function processVcJudgeGeminiResponse(data: unknown): {
  verdict: string
  scores: VcJudgeScores | null
  verdictJson: VcJudgeVerdictJson | null
  groundingMetadata: unknown
} {
  const d = data as {
    candidates?: Array<{
      content?: { parts?: Array<{ text?: string }> }
      finishReason?: string
      groundingMetadata?: unknown
    }>
  }
  const candidate = d.candidates?.[0]
  const finishReason = candidate?.finishReason ?? ''
  const groundingMetadata = candidate?.groundingMetadata ?? null
  const parts = candidate?.content?.parts
  const rawText = Array.isArray(parts)
    ? parts.map(p => (typeof p?.text === 'string' ? p.text : '')).join('').trim()
    : ''

  if (!rawText) {
    return { verdict: '', scores: null, verdictJson: null, groundingMetadata }
  }

  const parsed = tryParseVcJudgeVerdictJson(rawText)
  if (parsed) {
    return {
      verdict: buildVerdictMarkdownFromJson(parsed),
      scores: parsed.scores,
      verdictJson: parsed,
      groundingMetadata,
    }
  }

  const truncated = finishReason === 'MAX_TOKENS'
  if (truncated) {
    console.warn('VC Judge: finishReason=MAX_TOKENS, tail:', rawText.slice(-240))
  } else {
    console.warn('VC Judge: JSON parse failed, head:', rawText.slice(0, 500))
  }

  const verdict = truncated
    ? '### Judge response was truncated\n\nThe model hit its **output token limit** before finishing valid JSON. **Run the query again.** If this keeps happening, set a higher **VC_JUDGE_MAX_OUTPUT_TOKENS** in server environment (only if your Gemini model supports it).'
    : '### Judge response could not be parsed\n\nThe model returned text that was not valid JSON. **Run the query again.**'

  return {
    verdict,
    scores: null,
    verdictJson: null,
    groundingMetadata,
  }
}

function buildVcJudgeGenerateContentBody(
  userPrompt: string,
  useGoogleSearch: boolean,
) {
  const base = {
    systemInstruction: {
      parts: [
        {
          text: useGoogleSearch
            ? VC_JUDGE_SYSTEM_INSTRUCTION_WITH_SEARCH
            : VC_JUDGE_SYSTEM_INSTRUCTION_NO_SEARCH,
        },
      ],
    },
    contents: [{ role: 'user', parts: [{ text: userPrompt }] }],
    generationConfig: {
      maxOutputTokens: readMaxOutputTokens(),
      temperature: 0.3,
      responseMimeType: 'application/json',
      responseSchema: VC_JUDGE_RESPONSE_SCHEMA,
    },
  }
  if (useGoogleSearch) {
    return { ...base, tools: [{ google_search: {} }] }
  }
  return base
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

export async function fetchGeminiVcJudge(
  apiKey: string,
  model: string,
  userPrompt: string,
): Promise<Response> {
  const preferSearch = process.env.VC_JUDGE_USE_GOOGLE_SEARCH === 'true'
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`

  const post = (useGoogleSearch: boolean) =>
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(
        buildVcJudgeGenerateContentBody(userPrompt, useGoogleSearch),
      ),
    })

  let resp = await post(preferSearch)

  if (resp.status === 429 && preferSearch) {
    await sleep(1500)
    resp = await post(false)
  }

  if (resp.status === 429) {
    await sleep(2500)
    resp = await post(false)
  }

  return resp
}

// ─── Vercel handler ─────────────────────────────────────────────────────────

/**
 * Gemini + optional search can exceed the default ~10s limit → FUNCTION_INVOCATION_FAILED (plain text).
 * Pro/Enterprise: up to 60s (or higher per plan). Hobby is capped at 10s — upgrade or expect timeouts.
 */
export const config = {
  maxDuration: 60,
}

const GOOGLE_API_KEY = process.env.GOOGLE_API_KEY
const MODEL =
  process.env.VC_JUDGE_GEMINI_MODEL?.trim() || 'gemini-3-flash-preview'

/**
 * The GP Verdict: compares Coral Bricks, ChatGPT, Perplexity, and Grok via
 * Gemini (JSON mode). Optional Google Search via VC_JUDGE_USE_GOOGLE_SEARCH=true.
 */
export default async function handler(
  req: VercelRequest,
  res: VercelResponse,
): Promise<void> {
  if (req.method !== 'POST') {
    res.status(405).json({ success: false, error: 'Method not allowed' })
    return
  }

  if (!GOOGLE_API_KEY) {
    res.status(503).json({
      success: false,
      error: 'VC Judge unavailable. GOOGLE_API_KEY not configured.',
    })
    return
  }

  const rawBody = parseJsonBody(req.body)
  const body = rawBody as {
    query?: string
    answers?: {
      coral?: string
      chatgpt?: string
      perplexity?: string
      grok?: string
    }
  }

  const { query, answers } = body
  if (!query || typeof query !== 'string' || !answers || typeof answers !== 'object') {
    res.status(400).json({
      success: false,
      error: 'Missing or invalid "query" and "answers" in request body',
    })
    return
  }

  const coral = answers.coral ?? '(no answer)'
  const chatgpt = answers.chatgpt ?? '(no answer)'
  const perplexity = answers.perplexity ?? '(no answer)'
  const grok = answers.grok ?? '(no answer)'

  const userPrompt = buildVcJudgeUserPrompt(query, coral, chatgpt, perplexity, grok)

  try {
    const resp = await fetchGeminiVcJudge(GOOGLE_API_KEY, MODEL, userPrompt)

    if (!resp.ok) {
      const errBody = await resp.text()
      console.warn('Gemini API error', { status: resp.status, body: errBody })
      const msg =
        resp.status === 429
          ? 'Gemini rate limit (429). Wait briefly and retry. Search grounding uses extra quota—keep VC_JUDGE_USE_GOOGLE_SEARCH unset/false unless you need it.'
          : `Gemini API error: ${resp.status}`
      res.status(resp.status).json({
        success: false,
        error: msg,
      })
      return
    }

    const rawText = await resp.text()
    let data: unknown
    try {
      data = rawText ? JSON.parse(rawText) : null
    } catch {
      console.error('VC Judge: Gemini body not JSON', rawText?.slice(0, 800))
      res.status(502).json({
        success: false,
        error:
          'Gemini returned a non-JSON response. Try VC_JUDGE_GEMINI_MODEL=gemini-2.0-flash or check API logs.',
      })
      return
    }

    const processed = processVcJudgeGeminiResponse(data)

    sendJson(res, 200, {
      success: true,
      verdict: processed.verdict,
      scores: processed.scores,
      verdictJson: processed.verdictJson,
      groundingMetadata: processed.groundingMetadata,
    })
  } catch (err) {
    console.error('VC Judge failed', err)
    if (!res.headersSent) {
      res.status(500).json({
        success: false,
        error: err instanceof Error ? err.message : 'Judge failed',
      })
    }
  }
}

function parseJsonBody(body: unknown): Record<string, unknown> {
  if (body == null) return {}
  if (typeof body === 'string') {
    try {
      const o = JSON.parse(body) as unknown
      return o && typeof o === 'object' && !Array.isArray(o)
        ? (o as Record<string, unknown>)
        : {}
    } catch {
      return {}
    }
  }
  if (Buffer.isBuffer(body)) {
    try {
      const o = JSON.parse(body.toString('utf8')) as unknown
      return o && typeof o === 'object' && !Array.isArray(o)
        ? (o as Record<string, unknown>)
        : {}
    } catch {
      return {}
    }
  }
  if (typeof body === 'object' && !Array.isArray(body)) {
    return body as Record<string, unknown>
  }
  return {}
}

function sendJson(res: VercelResponse, status: number, payload: object): void {
  try {
    const json = JSON.stringify(payload, (_k, v) =>
      typeof v === 'bigint' ? v.toString() : v,
    )
    res.status(status).setHeader('Content-Type', 'application/json').end(json)
  } catch (stringifyErr) {
    console.error('VC Judge: response JSON.stringify failed', stringifyErr)
    if (!res.headersSent) {
      res.status(500).json({
        success: false,
        error: 'Failed to serialize judge response',
      })
    }
  }
}
