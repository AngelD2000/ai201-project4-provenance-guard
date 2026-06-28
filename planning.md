# Planning — Provenance Guard

## 1. Detection Signals


### Signal 1 — Stylometric Heuristics
- **Measures:** Raw text is routed to one of three engines (Essay / Poetry / Short-Form) based on structural rules, then each engine measures three monotonic stylistic features.

  | Engine | Feature | Direction | What it captures |
  | --- | --- | --- | --- |
  | **Essay** | `burstiness_score` | inverted (low = AI) | sentence-length variability |
  |  | `em_dash_density` | direct (high = AI) | em-dash (`—`, U+2014) usage per 100 words |
  |  | `transition_density` | direct (high = AI) | overuse of "however / moreover / furthermore" |
  | **Poetry** | `mean_word_rarity` | inverted (low = AI) | average log-rank of content words via frequency list |
  |  | `cliche_phrase_count` | direct (high = AI) | hits against fixed list of ~30 AI poetry constructions |
  |  | `line_length_variance` | inverted (low = AI) | line-length variability |
  | **Short-Form** | `caps_ratio` | inverted (low = AI) | non-standard capitalization rate |
  |  | `fragment_ratio` | inverted (low = AI) | sentence-fragment frequency |
  |  | `lowercase_start_ratio` | direct (high = AI) | sentences starting with lowercase |

- **Output shape:** A single normalized AI-direction score per submission.
  - Each feature normalized to `[0,1]` via min-max bounds: `clip((x - human_min) / (ai_max - human_min), 0, 1)` (mirrored for inverted features)
  - Per-engine `stylo_score = weighted_mean(normalized_features) ∈ [0,1]`
  - Returns raw feature dict alongside the score for audit logging

- **Range / type:** `stylo_score: float ∈ [0,1]` where higher = more AI-like.

- **Why this signal:** Cheaper than LLM calls, deterministic, and every feature has a one-line audit-log story (e.g., "em-dash density was 4.2 per 100 words, our human-typical ceiling is 1.5, so this feature flagged AI"). Three engines because stylistic tells differ by genre.

- **Calibration note — `lowercase_start_ratio` direction:** Initial assumption was `inverted` (humans use proper case; AI mimics the "lowercase aesthetic" voice). The Kaggle "Celebrity Tweets — Real vs AI-Generated" dataset (n=35 unique tweets after dedup) shows the opposite: humans starting with lowercase = 10%, AI = 67%. The AI imitator *over-applies* the lowercase voice — real celebrities (Billie Eilish, Tyler, Ariana) actually mix cases, while the AI assumes their voice is uniformly lowercase. Direction flipped to `direct`; bounds tuned to `(0.10, 0.67)`. Re-validate against a non-celebrity short-form corpus before relying on this beyond v1.

**Router (text → engine):**
```python
def route(text):
    words = word_count(text)
    lines = non_empty_lines(text)
    mean_line_len = mean(len(l) for l in lines) if lines else 0

    if words <= 500 and len(lines) <= 2:           return "short_form"
    elif len(lines) >= 3 and mean_line_len < 60:   return "poetry"
    else:                                          return "essay"
```

**Per-genre routing sketch:**

```
                       ┌──> [ Poetry Engine ] ──> mean_word_rarity, cliche_phrase_count, line_length_variance
                       │
   [ Raw Text Input ] ─┼──> [ Essay Engine  ] ──> burstiness_score, em_dash_density, transition_density
                       │
                       └──> [ Short Engine  ] ──> caps_ratio, fragment_ratio, lowercase_start_ratio
```


### Signal 2 — LLM as a judge (Groq)
- **Measures:** Semantic coherence, sentiment, voice, and overall flow — the holistic stylistic cues that stylometric heuristics can't capture.
- **Output shape:**
     ```json
     {
          "label": "AI/Human",
          "reasoning": "Why does was this classified as AI/Human",
          "confidence": 0-1
     }
     ```
     Converted into a unified AI-direction score so it can be combined with `stylo_score`:
     ```
     llm_ai_score = confidence          if label == "AI"
                  = 1 - confidence       if label == "Human"
     ```
- **Range / type:** Raw output is JSON; downstream we use `llm_ai_score: float ∈ [0,1]` where higher = more AI-like. Raw fields (label, reasoning, confidence) are kept for the audit log.
- **Why this signal:** Catches sentiment and overall flow that stylometric numbers miss. Complements Signal 1's deterministic backbone — semantic insight on top of statistical patterns.

### Combination strategy
- **How signals combine into one score:**
     Plain average of the two AI-direction scores → stored as the submission's confidence:
     ```
     combined_score = (stylo_score + llm_ai_score) / 2
     ```
     The **label** is decided by a two-part gate over `combined_score` *and* a per-signal directional check:
     ```
     strong_ai    = combined_score > 0.7  AND  stylo_score > 0.5  AND  llm_ai_score > 0.5   → "high-confidence AI"
     strong_human = combined_score < 0.3  AND  stylo_score < 0.5  AND  llm_ai_score < 0.5   → "high-confidence human"
     anything else                                                                            → "uncertain"
     ```
     The `combined_score` gate carries the magnitude check; the per-signal `>0.5 / <0.5` directional check is the guardrail against one strong signal carrying the call when the other is near-zero (a 0.95 + 0.05 case averages to 0.5 — never strong). This rule is *deliberately relaxed* from a strict per-signal `>0.7 / <0.3` cut: that strict version would refuse to call AI text when stylo lands at 0.68 even with the judge at 0.92, which over-punishes near-misses while preserving no extra safety beyond what the directional check already gives us.
- **Weights / formula:**
     Equal weighting — `w_stylo = w_llm = 0.5`. Treating both signals as equally informative is the honest position for v1 (no validation data to justify trusting one over the other). Weights live here as a tuning knob if real submissions show systematic bias.
- **Tie-breaking rules:**
     * Threshold boundaries use strict `>` and `<` — exact-equality at 0.7, 0.5, or 0.3 does NOT pass the bar
     * If `signal_2` (LLM) fails or returns malformed JSON: fall back to `stylo_score` alone and force label = "uncertain" (we can't claim high-confidence with one signal missing)
     * Identical mirrored scores (`stylo_score = 0.6, llm_ai_score = 0.4`) → "uncertain": combined averages to 0.5 (well below 0.7) AND the signals point opposite directions

---

## 2. Uncertainty Representation

> Answer: What does a confidence score of 0.6 *mean* to your system? How do you map raw signal outputs to a calibrated score? What thresholds separate "likely AI" / "uncertain" / "likely human"?

- **Semantic meaning of the score (what does 0.0 / 0.5 / 1.0 represent?):**
  `combined_score` is the average AI-leaning of the two signals on a unified `[0,1]` scale.
  - `0.0` — both signals say "definitely human" at full strength (floor; rarely reached in practice)
  - `0.3` — both signals lean human
  - `0.5` — no net AI signal (could be true ambiguity, or strong opposite signals canceling out)
  - `0.7` — both signals lean AI
  - `1.0` — both signals say "definitely AI" at full strength (ceiling)

  **Important:** the score is the *magnitude* of AI-ness; the *label* is decided by signal agreement (see §1). A 0.6 doesn't unambiguously mean "moderately AI" — it could be two weakly-agreeing signals OR two strongly-disagreeing signals averaging out. The audit log stores both `combined_score` and the individual signal values so the original picture can always be reconstructed.

- **Calibration approach (how raw signal outputs → calibrated score):**
  1. Stylometric features → normalized to `[0,1]` AI-direction via min-max bounds (§1)
  2. Per-engine weighted mean → `stylo_score`
  3. LLM judge `(label, confidence)` → converted to `llm_ai_score`
  4. Plain average of `stylo_score` and `llm_ai_score` → `combined_score`

  Calibration knobs: per-feature `(human_min, ai_max)` bounds and per-engine feature weights. Hand-tuned from sample text for v1; revisited only if real submissions show systematic miscalibration.

- **Thresholds:** Labels come from a two-part rule over `(combined_score, per-signal direction)`, not from `combined_score` alone:
  - **likely AI:** `combined_score > 0.7  AND  stylo_score > 0.5  AND  llm_ai_score > 0.5`
  - **likely human:** `combined_score < 0.3  AND  stylo_score < 0.5  AND  llm_ai_score < 0.5`
  - **uncertain:** anything else — middle-band combined score OR one signal points the wrong way

- **Why these thresholds:**
  - **0.7 / 0.3** are symmetric around 0.5 — they describe the combined-magnitude bar a confident call has to clear
  - **Per-signal `>0.5 / <0.5` directional check** is the guardrail against one strong signal carrying the call: it forces both signals to at least *lean* the same way, so a 0.95 stylo + 0.05 llm averaging to 0.5 can never be labeled strong (and a 0.45 stylo + 0.99 llm can't either — combined gets close but the directional check fails)
  - **Wide 0.4 uncertain band** is deliberately conservative — being slow to claim high-confidence is the responsible default for a system that affects creators. Tighten only with real data
  - **Two-part rule (magnitude + direction)** gives "uncertain" semantic meaning ("signals disagreed" or "no signal was strong") rather than just "score landed mid-range." That's a much crisper story for both audit logs and appeals — a creator can be told *why* they were uncertain, not just *that* they were

---

## 3. Transparency Label Design

> Answer: What exact text will the label show for each of the three cases? Write the variants out now, before building the UI.

The label returned by the `/submit` endpoint is a short canonical string. Consuming platforms decide their own UI prose; the system's contract is just the tag.

### High-confidence AI
> Exact label text: `"high-confidence AI"`

### High-confidence human
> Exact label text: `"high-confidence human"`

### Uncertain
> Exact label text: `"uncertain"`

### Notes
- **Tone/voice guidelines:**
  - The label is a **machine-readable canonical string**, returned exactly as listed above. Lowercase except for the "AI" acronym. No trailing period, no variation
  - Any user-facing copy (banners, tooltips, modal text) is the consuming platform's responsibility — the system does not generate prose
  - The `/submit` response also returns the numeric `confidence` and `attribution` so platforms can render their own UI affordances around the label

- **What the label must NOT say:**
  - ❌ Numeric confidence baked into the string (e.g. `"high-confidence AI (0.87)"`) — `confidence` is a separate field
  - ❌ Internal feature names or signal scores (e.g. `"high-confidence AI: em_dash flagged"`) — those live in the audit log
  - ❌ Verdict-style claims about the writer (e.g. `"AI-written by author"`) — labels describe the text, not the person
  - ❌ Any variant strings ("ai", "High Confidence AI", "AI-high") — exactly the three canonical values above, nothing else. The label is part of the API contract

---

## 4. Appeals Workflow

> Answer: Who can appeal? What info do they provide? What does the system do on receipt — status changes, logs? What does a reviewer see in the appeal queue?

- **Who can submit an appeal:**
  **Only the original author of the submission.** Submissions carry an `author_id` field (provided by the consuming platform at `/submit` time and stored in the `decisions` row); appeals must present a matching `author_id` to be accepted. The consuming platform owns authentication of users → `author_id`; the system enforces that whoever appeals is the same identity that submitted.

- **Required appeal inputs:**
  - `content_id: str` — links back to the original decision record (same value as `submission_id` from /submit's response; renamed at the wire per the graded spec)
  - `creator_reasoning: str` — the creator's explanation, non-empty, free-form text (no minimum length enforced beyond non-empty; platforms can layer their own limits)
  - `author_id: str` (optional) — when supplied, must match the `author_id` stored on the original submission, otherwise the appeal is rejected (403). Implemented as optional so the minimal graded-spec curl still works; supply it in production to enforce author-match

  **Optional but encouraged — evidence attachments:**
  - `evidence: list[Attachment]` — author-provided supporting material such as draft screenshots, version-history captures, or revision-progress timelines that demonstrate the writing was incremental human work. Each attachment is:
    ```
    Attachment {
        filename: str,
        content_type: str,        # e.g. "image/png", "image/jpeg"
        captured_at: ISO-8601 str, # when the screenshot/document was captured
        description: str,         # author's caption — what this is meant to show
        data: base64 str          # the file payload
    }
    ```
  - Per-attachment size cap: 5 MB. Per-appeal cap: 10 attachments. Caps enforced at request validation; oversized → 413
  - The `captured_at` timestamps matter independently of the appeal timestamp — a reviewer is looking for *progression over time*, so timeline evidence is far more useful than a single static screenshot

- **System actions on receipt:**
  - **Lookup:** fetch the original decision from SQLite `decisions` by `submission_id`. If not found → 404
  - **Authorization:** compare submitted `author_id` against the stored `author_id`. Mismatch → 403 Forbidden. Empty reasoning → 400
  - **status change:** the original submission's status flips from `"active"` → `"under_review"` in the `decisions` table
  - **what gets logged:** a new row in SQLite `appeals` containing `submission_id` (FK), `author_id`, `reasoning`, a snapshot of the `original_decision` fields (`combined_score`, `attribution`, `final_label`), `status: "under_review"`, and `timestamp`. Evidence attachments are stored in a related `appeal_evidence` table (one row per attachment) keyed by `appeal_id`, holding `filename`, `content_type`, `captured_at`, `description`, and the file payload (blob or path)
  - **what does NOT change:** the original classification fields (`combined_score`, `stylo_score`, `llm_ai_score`, `attribution`, `final_label`) are immutable — they remain as the system's record of *what we said at decision time*. Resolution only adds a new row in `appeals`; it never edits the original `decisions` row
  - **Response:** HTTP 202 with `{"content_id": "...", "appeal_id": "...", "status": "under_review"}`

- **What the reviewer sees in the queue:**
  Each queued appeal surfaces the full classification trace so the reviewer can re-derive *why* the system landed where it did:
  - **Original text** (raw)
  - **Routing decision** — which engine ran (essay / poetry / short-form)
  - **Per-feature scores** — each stylometric feature's raw value, normalized value, and threshold (so the reviewer can see exactly which features flagged)
  - **Signal scores** — `stylo_score`, `llm_ai_score`, `combined_score`
  - **Agreement flag** — `signals_agreed: bool` and which branch fired (strong agreement, mid-band, or disagreement)
  - **LLM rationale** — the judge's reasoning text from Signal 2
  - **Final label** — the canonical string the user saw
  - **Creator's reasoning** — the appeal text
  - **Evidence attachments** — viewable inline, sorted by `captured_at` so progression-over-time evidence reads as a timeline (drafts → revisions → final)
  - **Timestamps** — submission, appeal submission, and each attachment's `captured_at`

- **Resolution outcomes (overturn / uphold / …):**
  Two terminal outcomes for v1:
  - **Overturn** — reviewer disagrees with system. New row in `appeals` with `resolution: "overturned"`, optional `corrected_label`. Original `decisions` row stays immutable; the overturn is the system-of-record going forward
  - **Uphold** — reviewer agrees with system. New row with `resolution: "upheld"`. Status returns to `"active"`

  Both outcomes carry a reviewer note for the audit trail. A third outcome — explicitly *not* in v1 — is "Needs More Info," which would loop back to the creator; deferred until appeal volume justifies the workflow.

---

## 5. Anticipated Edge Cases

> Answer: At least two *specific* cases your system will handle poorly. Not "inaccurate detection" — concrete scenarios like "a haiku with repeated structure."

### Edge case 1 — Poetry with heavy repetition (refrain, anaphora)
- **Scenario:** A human-written poem that leans on deliberate repetition — refrains like Poe's "nevermore," anaphora like Maya Angelou's "Still I Rise," or any structure where lines, phrases, or images recur intentionally as craft.
- **Why our signals struggle:** The Poetry Engine **triple-fires** on this pattern:
  - `mean_word_rarity` ↓ — repetition shrinks the unique vocabulary, so average word rarity drops
  - `cliche_phrase_count` ↑ — repeated lines often *look* like the formulaic AI constructions in our fixed list
  - `line_length_variance` ↓ — refrains and anaphora hold line lengths close together
  All three Poetry features vote AI, and the LLM-as-judge can be ambivalent on poetic voice. Agreement gate fires → `"high-confidence AI"`. A confidently wrong call on a deliberate human craft technique.
- **Mitigation / acknowledgement:** Document this failure mode openly in transparency docs; encourage appeals on poetry flagged AI; consider (post-v1) a "repetition is craft" detector that softens the rarity penalty when repetition is structurally regular.

### Edge case 2 — Formal prose with heavy em-dash usage (academic / literary essay)
- **Scenario:** Writing that combines heavy em-dash use (a known human stylistic tic — Emily Dickinson, modern literary essayists) with the uniformity that comes from formal registers: academic abstracts, polished essays, or long-form journalism.
- **Why our signals struggle:** Two of the three Essay features fire on this combined pattern:
  - `em_dash_density` ↑ — the writer's stylistic voice triggers our AI flag
  - `burstiness_score` ↓ — formal/academic prose tends toward uniform sentence length
  - `transition_density` ↑ may also fire if the writer uses connective tissue like "however," "moreover," "furthermore"
  When two or three features fire, `stylo_score` lands high; the LLM-as-judge may also lean AI on a "flat" academic voice. Agreement gate can fire on `"high-confidence AI"` even when this is a perfectly human essay.
- **Mitigation / acknowledgement:** Acknowledge that em-dash-as-stylistic-choice predates LLMs and is a known human voice marker. In docs, name this case explicitly. Long-term, em-dash threshold could be raised when paired with bursty sentences (a "literary essayist" mode).

### Edge case 3 — Highly structured human writing (technical manuals, academic abstracts, news copy)
- **Scenario:** Writing whose register *requires* uniformity — technical documentation, academic abstracts, AP-style journalism, translated text, or non-native English where simpler/uniform sentences are the norm.
- **Why our signals struggle:** This is a known blind spot — already flagged in the README under "LLM as a judge → Blind Spot." Both signals fail in the same direction:
  - `burstiness_score` ↓ — uniformity is the *house style*, not a tell
  - `transition_density` ↑ — formal writing leans on transitional connectors
  - LLM-as-judge **also leans AI** because the voice is genuinely flat — the absence of sentiment isn't a bug in technical writing, it's the goal
  Stylometric pipeline votes AI, LLM judge votes AI, agreement gate fires → `"high-confidence AI"`. Possibly the system's worst failure mode because **both signals confidently agree on the wrong answer.**
- **Mitigation / acknowledgement:** Surface this loudly in transparency docs targeted at platforms that host technical/academic content. Consider a content-type hint at submission time (`content_type: "technical" | "creative" | ...`) so platforms can warn users their material may be misclassified. Encourage appeals for this category.

---

## Architecture

> 2–3 sentence narrative describing the submission flow and the appeal flow.

**Narrative:** On `POST /submit`, raw text is routed to one of three stylometric engines (essay / poetry / short-form), scored against three monotonic features per engine, and combined with a Groq LLM-as-judge call into a `combined_score` (plain average) and a `final_label` (decided by agreement gating over individual signals); the full decision trace is persisted in SQLite under the submission's `author_id` and returned as `{attribution, confidence, label}`. On `POST /appeal`, the system verifies the caller's `author_id` matches the original submission, logs the creator's reasoning plus optional evidence attachments (screenshots with `captured_at` timestamps) into the `appeals` and `appeal_evidence` tables, flips the original submission's status to `"under_review"` (without mutating the original decision fields), and returns a 202 acknowledgment.

### Diagram

#### Flow 1 — Submission (`POST /submit`)

```
   ┌──────────┐
   │  Client  │
   └────┬─────┘
        │
        │  POST /submit
        │  body: { "text": "<raw user text>" }
        ▼
   ┌────────────────────┐
   │  Service Layer     │
   │  /submit handler   │
   └────────┬───────────┘
            │
            │  raw_text: str
            ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │              SIGNAL DETECTION PIPELINE                          │
   │                                                                 │
   │   ┌──────────────────────────────┐                              │
   │   │  Signal 1                    │                              │
   │   │  Stylometric Heuristics      │   raw_text: str              │
   │   │  • sentence-length variance  │ ◄─────────────────           │
   │   │  • type-token ratio          │                              │
   │   │  • punctuation density       │                              │
   │   │  • avg sentence complexity   │                              │
   │   └──────────────┬───────────────┘                              │
   │                  │                                              │
   │                  │  stylo_score: float ∈ [0,1]                  │
   │                  │  stylo_features: dict                        │
   │                  ▼                                              │
   │   ┌──────────────────────────────┐                              │
   │   │  Signal 2                    │   raw_text: str              │
   │   │  LLM-as-Judge (Groq)         │ ◄─────────────────           │
   │   │  • semantic coherence        │                              │
   │   │  • stylistic patterns        │                              │
   │   │  • sentiment markers         │                              │
   │   └──────────────┬───────────────┘                              │
   │                  │                                              │
   │                  │  llm_score: float ∈ [0,1]                    │
   │                  │  llm_rationale: str                          │
   │                  ▼                                              │
   │   ┌──────────────────────────────┐                              │
   │   │  Confidence Scorer           │                              │
   │   │  weighted aggregate of       │                              │
   │   │  (stylo_score, llm_score)    │                              │
   │   └──────────────┬───────────────┘                              │
   │                  │                                              │
   │                  │  combined_score: float ∈ [0,1]               │
   │                  │  attribution: "AI" | "human" | "uncertain"   │
   │                  ▼                                              │
   │   ┌──────────────────────────────┐                              │
   │   │  Transparency Labeler        │                              │
   │   │  thresholds → label          │                              │
   │   └──────────────┬───────────────┘                              │
   │                  │                                              │
   │                  │  label: "high-confidence AI"                 │
   │                  │       | "high-confidence human"              │
   │                  │       | "uncertain"                          │
   └──────────────────┼──────────────────────────────────────────────┘
                      │
                      │  decision_record: {
                      │    submission_id: uuid (PK),
                      │    raw_text,
                      │    stylo_score, llm_score,
                      │    combined_score, attribution, label,
                      │    timestamp
                      │  }  → INSERT into SQLite `decisions`
                      ▼
              ┌─────────────────────┐
              │   Audit Log         │
              │   (SQLite)          │
              │   table: decisions  │
              │   PK: submission_id │
              └──────────┬──────────┘
                         │
                         │  submission_id: str (ack)
                         ▼
              ┌─────────────────────────────────┐
              │         HTTP 200 Response       │
              │  {                              │
              │    "submission_id": "...",      │
              │    "attribution":  "...",       │
              │    "confidence":   0.87,        │
              │    "label":        "..."        │
              │  }                              │
              └─────────────────────────────────┘
                       │
                       ▼
                  ┌──────────┐
                  │  Client  │
                  └──────────┘
```

#### Flow 2 — Appeal (`POST /appeal`)

```
   ┌──────────┐
   │  Client  │
   └────┬─────┘
        │
        │  POST /appeal
        │  body: {
        │    "content_id":        "...",
        │    "creator_reasoning": "<creator's text>",
        │    "author_id":         "..." (optional),
        │    "evidence": [                       ◄── optional
        │       {
        │         "filename":     "...",
        │         "content_type": "image/png",
        │         "captured_at":  "ISO-8601",
        │         "description":  "...",
        │         "data":         "<base64>"
        │       },
        │       ...                              (≤ 10 attachments, ≤ 5 MB each)
        │    ]
        │  }
        ▼
   ┌────────────────────┐
   │  Service Layer     │
   │  /appeal handler   │
   └────────┬───────────┘
            │
            │  content_id, creator_reasoning, evidence[], author_id?
            ▼
   ┌────────────────────────────────┐
   │  Lookup original decision      │
   │  SELECT FROM SQLite            │
   │  `decisions` WHERE id = ?      │
   │  404 if not found              │
   └──────────────┬─────────────────┘
                  │
                  │  original_decision: {
                  │    author_id, attribution,
                  │    combined_score, final_label
                  │  }
                  ▼
   ┌────────────────────────────────┐
   │  Authorize + Validate          │
   │  • submitted author_id         │
   │    == original author_id?      │  → no: 403
   │  • reasoning non-empty?        │  → no: 400
   │  • evidence within caps?       │  → no: 413
   └──────────────┬─────────────────┘
                  │
                  │  validated payload
                  ▼
   ┌────────────────────────────────┐
   │  Status Updater                │
   │  decisions.status              │
   │    → "under_review"            │
   │  (other fields untouched)      │
   └──────────────┬─────────────────┘
                  │
                  │  appeal_record: {
                  │    submission_id, author_id, reasoning,
                  │    original_decision snapshot,
                  │    status: "under_review", timestamp
                  │  }
                  │  evidence_records: [
                  │    { appeal_id (FK), filename,
                  │      content_type, captured_at,
                  │      description, payload }
                  │  ]
                  ▼
          ┌─────────────────────────────┐
          │   Audit Log (SQLite)        │
          │   • table: appeals          │
          │     FK: submission_id       │
          │   • table: appeal_evidence  │
          │     FK: appeal_id           │
          └──────────┬──────────────────┘
                     │
                     │  ack
                     ▼
          ┌─────────────────────────────────┐
          │         HTTP 202 Response       │
          │  {                              │
          │    "content_id": "...",         │
          │    "appeal_id":  "...",         │
          │    "status":     "under_review" │
          │  }                              │
          └─────────────────────────────────┘
                   │
                   ▼
              ┌──────────┐
              │  Client  │
              └──────────┘
```

---

## AI Tool Plan

### M3 — Submission endpoint + first signal
- **Spec sections to provide:**
  - §1 → Signal 1 (Stylometric Heuristics), including the router pseudocode, the per-engine feature tables, normalization formula, and per-engine weighted-mean → `stylo_score`
  - Architecture → submission flow diagram (Flow 1) — just up through "Signal 1 → stylo_score"
  - Skip Signal 2 entirely; skip §2 thresholds; skip combination strategy
- **What to ask the AI tool to generate:**
  - A Flask app skeleton with `POST /submit` that accepts `{text, author_id}` and returns a placeholder JSON response
  - Pure-Python implementation of the router and the three engines (essay / poetry / short-form), each with its three monotonic features and the `clip((x - human_min)/(ai_max - human_min), 0, 1)` normalization (mirrored for inverted features)
  - SQLite schema for the `decisions` table with all the fields named in §1/§4 (PK `submission_id`, `author_id`, `raw_text`, `engine_used`, per-feature raw + normalized values, `stylo_score`, `status`, `timestamp`)
  - A `POST /submit` handler that runs the router → engine → writes a row → returns `{submission_id, stylo_score}` (label fields stubbed for now)
- **How to verify the output:**
  - Hand-feed the router 3 sample texts (a paragraph, a haiku, a tweet) — does each route to the right engine?
  - For each engine, hand-feed clearly AI text and clearly human text — do the per-feature scores actually differ? Are they in `[0,1]`?
  - Inspect SQLite directly: does the `decisions` row contain all expected fields and is `author_id` non-null?
  - Run features directly (outside the endpoint) on toy inputs first; only wire to `/submit` once the math is sane

### M4 — Second signal + confidence scoring
- **Spec sections to provide:**
  - §1 → Signal 2 (LLM-as-judge JSON shape + the `llm_ai_score` conversion) and the full Combination Strategy block (plain average + agreement gating + tie-breakers)
  - §2 → all four sub-answers (semantic meaning, calibration, thresholds, why)
  - Architecture → submission flow diagram, full pipeline
- **What to ask the AI tool to generate:**
  - A Groq client wrapper that takes `raw_text`, prompts the model for `{label, reasoning, confidence}`, parses the JSON, and converts to `llm_ai_score`
  - A combiner function: `(stylo_score, llm_ai_score) → (combined_score, signals_agreed, final_label)` using the gating rules from §1
  - Updates to the `decisions` SQLite schema to add `llm_label`, `llm_confidence`, `llm_rationale`, `llm_ai_score`, `combined_score`, `signals_agreed`, `final_label`, `attribution`
  - Updated `POST /submit` handler that runs Signal 1 → Signal 2 → combiner → writes the full decision → returns `{submission_id, attribution, confidence, label}`
  - The LLM-failure fallback path from §1's tie-breaking rules
- **How to verify the output:**
  - Feed 3 clearly-AI samples → expect `combined_score > 0.7` and label `"high-confidence AI"`
  - Feed 3 clearly-human samples (mixed genres) → expect `combined_score < 0.3` and label `"high-confidence human"`
  - Feed deliberately ambiguous text (e.g., a polished but human Wikipedia paragraph) → expect `"uncertain"`
  - Force the LLM call to fail (bad API key) → confirm the system falls back to `stylo_score` alone and forces `"uncertain"`
  - Sanity check: scores should *vary* across inputs, not cluster around 0.5

### M5 — Production layer (labels + appeal endpoint)
- **Spec sections to provide:**
  - §3 → the three canonical label strings and the "must NOT say" guardrails
  - §4 → the full appeals workflow (author_id check, evidence attachments, what gets logged, what does NOT change, reviewer view)
  - Architecture → appeal flow diagram (Flow 2)
- **What to ask the AI tool to generate:**
  - The final label-string emitter (deterministic from the agreement gate) — produces exactly `"high-confidence AI"`, `"high-confidence human"`, or `"uncertain"`
  - SQLite schema for `appeals` and `appeal_evidence` tables matching §4's spec
  - `POST /appeal` handler: lookup by `submission_id` (404), `author_id` check (403), reasoning non-empty (400), evidence-attachment validation (5 MB / 10-count caps → 413), insert appeal + evidence rows, flip submission status to `"under_review"`, return 202
  - A minimal reviewer-queue read endpoint that returns the full decision trace + appeal + evidence (sorted by `captured_at`) for a given `submission_id`
- **How to verify the output:**
  - All three label variants are reachable from test inputs (confirms M4 thresholds wire through to M5 label strings exactly)
  - Submit text → appeal with matching `author_id` + reasoning → returns 202, status is `"under_review"`, appeal row exists in SQLite
  - Same flow with mismatched `author_id` → 403; empty reasoning → 400; bogus `submission_id` → 404
  - Submit appeal with 2 attachments (different `captured_at` timestamps) → both rows in `appeal_evidence`, reviewer endpoint returns them in chronological order
  - Confirm the original `decisions` row is untouched after appeal — only `status` changes, not the score/label fields

---

## Pre-build Checklist

- [ ] Reviewed and revised the three label variants
- [ ] Confirmed thresholds in §2 match the labels in §3
- [ ] Edge cases in §5 are specific, not generic
- [ ] Updated this doc before any stretch features
