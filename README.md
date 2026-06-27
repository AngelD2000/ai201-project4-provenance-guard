# ai201-project4-provenance-guard
This project is a backend system that any creative sharing platform could plug into to classify submitted content, score confidence in that classification, surface a transparency label to users, and handle appeals from creators who believe they've been misclassified.

## Functional
- User should be able to submit a text and get back a structured output with 
    - Input: 
        * Text (str)
    - Output: {"attribution": "...", "confidence":"...", "label":"high-confidence AI, high-confidence human, uncertain"}
        * attribution (string for categorization, AI, human, etc)
        * confidence (how confident we are for the attirbution)
        * transparency label (high-confidence AI, high-confidence human, uncertain)
    - Other:
        * Multi-signal detection pipeline
            * **LLM-based classification (Groq)**: ask the model to assess whether text reads as human or AI-generated. Captures semantic and stylistic coherence holistically.
            * **Stylometric heuristics**: measurable statistical properties that differ between human and AI writing — sentence length variance, type-token ratio (vocabulary diversity), punctuation density, or average sentence complexity. AI text tends to be more uniform; human writing is more variable. Computable in pure Python.
        * Confidence score
        * Save everything with an unique id in the audit log - SQLlite

- User should be able to appeal a classification
    - Input: 
        * Creator's reasoning
        * (optional) other evidence like images of progress/timeline, etc
    - Other:
        * Log the reasoning for appeal and the original decision
        * Update content status to "Under Review"

## Non-functional

### Rate limits (POST /submit)

`10 per minute` + `100 per day`, keyed by client IP, in-memory storage.

**Why these numbers:**

*Writer model.* A real user iterating on a draft will paste a paragraph, read the result, edit, resubmit. Pacing of roughly one submission every 5–10 seconds is a realistic upper bound for thoughtful editing. The 10/min ceiling lines up with that — a writer revising a piece across an editing session won't notice the cap, but someone refreshing the same paragraph rapidly will. Over a full day, 100 distinct paragraphs (≈ a chapter's worth of voice-checking) covers a productive workshop session.

*Abuser model.* The smallest meaningful abuse is automation — a script flooding `/submit` to either (a) probe the classifier for blind spots or (b) starve our shared Groq quota for everyone else. At 10/min per IP, sustained automation is capped at 0.17 RPS — orders of magnitude below the model's free-tier ceiling and easy to detect. The 100/day cap also catches the "slow drip" abuser whose per-minute rate stays under any per-minute window — they still hit the wall by lunchtime.

*Why not the previous 5000/3s?* That's ~1666 RPS per IP — well above any realistic writer (no human refreshes a curl loop a thousand times a second), and well above what one IP could be doing for any legitimate reason. It made the limiter a no-op against any real abuser: a script could exhaust the model quota in seconds without ever tripping the gate. The numbers above shift the limiter from cosmetic to load-bearing.

*Why scope only to /submit?* `/submit` calls Groq (real money + shared quota); `/log` is a cheap SQLite read and `/appeal` is insert-only. Limiting the expensive endpoint is the smallest intervention that protects the actual scarce resource.

**Storage:** `memory://` — fine for single-process dev; production should swap to a Redis backend so the limit holds across processes.

**Evidence — 12 rapid POSTs against the live limiter:**

```text
request 1: HTTP 200
request 2: HTTP 200
request 3: HTTP 200
request 4: HTTP 200
request 5: HTTP 200
request 6: HTTP 200
request 7: HTTP 200
request 8: HTTP 200
request 9: HTTP 200
request 10: HTTP 429
request 11: HTTP 429
request 12: HTTP 429
```

(Bucket was at 10 by the 10th call because an earlier /submit had already consumed 1 slot in the per-IP window.) The 429 body:

```json
{
  "detail": "10 per 1 minute",
  "error":  "rate limit exceeded"
}
```


### Audit log (GET /log)

Every `/submit` writes one row to SQLite; `/log` returns those rows as structured JSON, newest-first. Each entry carries every field a reviewer or auditor needs to reconstruct *why* the system landed where it did:

| Field | Type | What it captures |
| --- | --- | --- |
| `timestamp` | ISO-8601 UTC | When the decision was made |
| `content_id` | UUID | The submission's identifier (matches `/submit` response) |
| `creator_id` | string | Author identity (provided by the platform at submit time) |
| `engine` | `essay` \| `poetry` \| `short_form` | Which stylometric engine routed the text |
| `stylo_score` | float ∈ [0,1] | Signal 1 — stylometric heuristics, AI-direction |
| `llm_score` | float ∈ [0,1] \| null | Signal 2 — Groq judge, AI-direction. `null` when the judge call failed (combiner falls back to "uncertain") |
| `confidence` | float ∈ [0,1] | Combined score = mean of the two signals (magnitude of AI-ness) |
| `signals_agreed` | bool | Whether both signals individually leaned the same direction |
| `attribution` | `AI` \| `human` \| `uncertain` | The short verdict |
| `label` | one of three canonical strings | Public transparency label (see `labels.py`) |
| `status` | `active` \| `under_review` | Flipped to `under_review` once `/appeal` is filed |
| `appeal_reasoning` | string \| null | The creator's reasoning from `/appeal`; `null` if never appealed |

The format is JSON — structured at every layer (DB row → `/log` response → consumer parses it). No console scraping, no string parsing.

**Live evidence — 3 submissions across 2 authors, one appealed:**

```json
{
  "entries": [
    {
      "appeal_reasoning": null,
      "attribution": "uncertain",
      "confidence": 0.3220,
      "content_id": "0e86467e-113b-4052-8908-6bcbe000173d",
      "creator_id": "alice",
      "engine": "essay",
      "label": "uncertain",
      "llm_score": 0.200,
      "signals_agreed": false,
      "status": "active",
      "stylo_score": 0.4440,
      "timestamp": "2026-06-27T23:16:49.175777+00:00"
    },
    {
      "appeal_reasoning": null,
      "attribution": "human",
      "confidence": 0.1571,
      "content_id": "626626a3-3ab9-4959-82a6-27a2f328f0d4",
      "creator_id": "bob",
      "engine": "essay",
      "label": "high-confidence human",
      "llm_score": 0.100,
      "signals_agreed": true,
      "status": "active",
      "stylo_score": 0.2142,
      "timestamp": "2026-06-27T23:16:48.509786+00:00"
    },
    {
      "appeal_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.",
      "attribution": "AI",
      "confidence": 0.7386,
      "content_id": "2970c315-3bef-443c-92a9-3a1e7bb2609e",
      "creator_id": "alice",
      "engine": "essay",
      "label": "high-confidence AI",
      "llm_score": 0.800,
      "signals_agreed": true,
      "status": "under_review",
      "stylo_score": 0.6772,
      "timestamp": "2026-06-27T23:16:47.773912+00:00"
    }
  ]
}
```

Three entries cover all three label classes (`high-confidence AI`, `high-confidence human`, `uncertain`), two `creator_id` values, both an `active` and an `under_review` submission, and the appeal_reasoning populated on the appealed row.


## Signals
1. Stylometric heuristics
    - Good because: 
        *  Human authors naturally write with a mix of very short and very long sentences. LLM-generated texts tend to be more uniform.
        * LLM tend to use "-" in sentences a lot, can check for common tokens since input is text based
    - Blind spots: 
        * Unable to capture sentiment 
2. LLM as a judge
    - Good because: 
        * Human writing is more variable and uses more unexpected word choices. LLMs predict the "next most probable token leading to statistically average predictable word choices
        * Human writing is less verbose
        * Able to capture the sentimant in human writing
    - Blind Spot: 
        * Unable to classify highly structured human writing (like technical manuals or academic abstracts) correctly -> lacks the sentimant

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          PROVENANCE GUARD SYSTEM                            │
└─────────────────────────────────────────────────────────────────────────────┘

   ┌──────────┐
   │   USER   │
   └────┬─────┘
        │
        │ submits text
        ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │                          SERVICE LAYER                                  │
   │  ┌────────────────────────┐         ┌────────────────────────┐          │
   │  │ /attribution endpoint  │         │   /appeal endpoint     │          │
   │  └───────────┬────────────┘         └───────────┬────────────┘          │
   └──────────────┼──────────────────────────────────┼───────────────────────┘
                  │                                  │
                  ▼                                  ▼
   ┌──────────────────────────────────┐   ┌──────────────────────────────────┐
   │   SIGNAL DETECTION PIPELINE      │   │       APPEAL HANDLER             │
   │                                  │   │                                  │
   │  ┌────────────────────────────┐  │   │  ┌────────────────────────────┐  │
   │  │  1. Stylometric Heuristics │  │   │  │ Log creator reasoning      │  │
   │  │     • sentence len variance│  │   │  │ + original decision        │  │
   │  │     • type-token ratio     │  │   │  └────────────┬───────────────┘  │
   │  │     • punctuation density  │  │   │               ▼                  │
   │  │     • avg complexity       │  │   │  ┌────────────────────────────┐  │
   │  └────────────┬───────────────┘  │   │  │ Update status:             │  │
   │               ▼                  │   │  │   "Under Review"           │  │
   │  ┌────────────────────────────┐  │   │  └────────────────────────────┘  │
   │  │  2. LLM-as-Judge (Groq)    │  │   │                                  │
   │  │     • sentiment            │  │   │           (no return)            │
   │  │     • grammar / style      │  │   └──────────────────────────────────┘
   │  └────────────┬───────────────┘  │
   │               ▼                  │
   │  ┌────────────────────────────┐  │
   │  │  Aggregate → Confidence    │  │
   │  └────────────┬───────────────┘  │
   └───────────────┼──────────────────┘
                   ▼
   ┌─────────────────────────────────────────────┐
   │              RESPONSE                       │
   │  {                                          │
   │    "attribution": "AI | human | ...",       │
   │    "confidence":  0.0 – 1.0,                │
   │    "label":       "high-confidence AI"      │
   │                 | "high-confidence human"   │
   │                 | "uncertain"               │
   │  }                                          │
   └──────────────────┬──────────────────────────┘
                      ▼
              ┌───────────────┐
              │ Transparency  │
              │ label shown   │
              │   in UI       │
              └───────┬───────┘
                      │
            ┌─────────┴──────────┐
            │ Disagrees?         │
            │ Hit "Appeal" btn ──┼──► back to /appeal endpoint
            └────────────────────┘

   ┌─────────────────────────────────────────────────────────────────────────┐
   │ NON-FUNCTIONAL: Rate limit ≈ 5k TP / 3s (US-scale assumption)           │
   └─────────────────────────────────────────────────────────────────────────┘
```

## Example

### Workflow for text
userInput -> Service [ attribution endpoint] --> Signal Detection Pipeline [ LLM as a judge on sentiment and grammer style ---> Stylometric heuristics with sentence length variance, vocab, etc] -> Service response with (attribution, confidence, transparency label) -> display the transparency label 


#### IF user wants to appeal label
userInput -> Service [ attribution endpoint] --> Signal Detection Pipeline [ Stylometric heuristics with sentence length variance, vocab, etc ---> LLM as a judge on sentiment and grammer style ] -> Service response with (attribution, confidence, transparency label) -> display the transparency label 

userInput -> Service [ appeal endpoint ] --> log reasoning for appeal with original decision --> update "Under Review" in UI -> doesn't return anything


### False Positive
When system misclassifies human writer's word
* Display == uncertain maybe with .54 confidence is AI
* Creator would hit a button to appeal 
    - UI button for a "appeal classification" -> UI text for human to enter reasoning -> api call to backend with reasoning -> log reasoning and original label -> display "Under Review" in UI
