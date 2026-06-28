# ai201-project4-provenance-guard
This project is a backend system that any creative sharing platform could plug into to classify submitted content, score confidence in that classification, surface a transparency label to users, and handle appeals from creators who believe they've been misclassified.

## Quickstart

### 1. Set up

```bash
git clone https://github.com/AngelD2000/ai201-project4-provenance-guard.git
cd ai201-project4-provenance-guard

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Add your Groq API key

Get a free key (no credit card) at https://console.groq.com, then create a `.env` file in the repo root:

```bash
echo "GROQ_API_KEY=gsk_your_key_here" > .env
```

(The `.env` file is gitignored — your key never gets committed.)

### 3. Run the server

```bash
python -m flask --app app run
```

The server boots on `http://127.0.0.1:5000`. Two useful URLs:

- **`http://127.0.0.1:5000/`** — single-page demo UI (submit form + live audit log + appeal flow)
- **`http://127.0.0.1:5000/log`** — raw JSON audit log

### 4. Run the tests

```bash
python -m pytest tests/
```

114 tests covering the combiner gating logic, both signals, the SQLite layer, the appeal flow, and the rate limiter.

### Hitting the API directly

```bash
# Classify a piece of text
curl -s -X POST http://127.0.0.1:5000/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "ok so the ramen was actually pretty bad", "author_id": "alice"}' \
  | python -m json.tool

# Read the audit log (scope to one author with ?author_id=alice)
curl -s http://127.0.0.1:5000/log | python -m json.tool

# Appeal a decision — paste the submission_id from the /submit response above
# into the content_id field below (the two names refer to the same identifier;
# /submit returns it as submission_id, /appeal takes it as content_id per the
# graded spec).
curl -s -X POST http://127.0.0.1:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "PASTE-SUBMISSION-ID", "creator_reasoning": "I wrote this myself."}' \
  | python -m json.tool
```

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
        * Update content status to "under_review"

## Non-functional

### Rate limits (POST /submit)

`10 per minute` + `100 per day`, keyed by client IP, in-memory storage.

**Why these numbers:**

*Writer model.* A real user iterating on a draft will paste a paragraph, read the result, edit, resubmit. Pacing of roughly one submission every 5–10 seconds is a realistic upper bound for thoughtful editing. The 10/min ceiling lines up with that — a writer revising a piece across an editing session won't notice the cap, but someone refreshing the same paragraph rapidly will. Over a full day, 100 distinct paragraphs (≈ a chapter's worth of voice-checking) covers a productive workshop session.

*Abuser model.* The smallest meaningful abuse is automation — a script flooding `/submit` to either (a) probe the classifier for blind spots or (b) starve our shared Groq quota for everyone else. At 10/min per IP, sustained automation is capped at 0.17 RPS — orders of magnitude below the model's free-tier ceiling and easy to detect. The 100/day cap also catches the "slow drip" abuser whose per-minute rate stays under any per-minute window — they still hit the wall by lunchtime.

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


### Appeal policy (POST /appeal)

Anyone can appeal a decision — not just the original creator — but the system caps community appeals to prevent a coordinated mob from flipping a label through sheer volume.

| Appellant | Allowed? | Notes |
|---|---|---|
| Original creator (`author_id` matches the decision) | **Always** | Never capped. Their voice is privileged. |
| Third party with an identity (`author_id` ≠ decision's `author_id`) | Up to cap | One appeal per `(content_id, author_id)` — same person can't pile on (409 on duplicate). |
| Anonymous third party (no `author_id` supplied) | Up to cap | Counts toward the cap; can't be deduped (no identity), so a single bad actor *could* fill the cap by themselves — that's the price of accepting anonymous appeals. |
| Either third-party flavor past cap | **429** | "This submission has already received N community appeals" |

**Cap:** **5 third-party appeals per content_id** (`_MAX_THIRD_PARTY_APPEALS` in `app.py`). Small enough that a coordinated brigade can't easily push a label change through sheer volume; large enough that genuine community concern surfaces and reaches a reviewer. The creator can still appeal after the cap is hit — their appeal counts as creator-appeal, not third-party, and is never capped.

**Why a cap at all?** Without one, "appeal" turns into a mob-vote button. A creator whose work draws unwanted attention could be drowned in 50 appeals from a single brigading thread, and the audit log would lose all signal: every entry on every popular piece would read `under_review`. With a cap, the reviewer queue stays meaningful — appeals on a piece mean a meaningful slice of the community flagged it, not that someone organized a pile-on.

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

## Confidence scoring

The two signals combine into a single `combined_score ∈ [0, 1]`:

```
combined_score = (stylo_score + llm_ai_score) / 2
```

The label is decided by a two-part gate over magnitude AND direction:

| | rule | label |
|---|---|---|
| Strong-AI | `combined > 0.7` AND `stylo > 0.5` AND `llm_ai > 0.5` | `"high-confidence AI"` |
| Strong-human | `combined < 0.3` AND `stylo < 0.5` AND `llm_ai < 0.5` | `"high-confidence human"` |
| Anything else | one signal points the wrong way OR neither cleared the bar | `"uncertain"` |

The magnitude gate (`combined > 0.7 / < 0.3`) does the heavy lifting; the per-signal `>0.5 / <0.5` directional check prevents one strong signal from carrying the call when the other points the wrong way (a `stylo=0.95 + llm=0.05` pair averages to 0.5 — never strong).

**How the scores were validated:** `tests/test_combiner.py` covers every gate branch including strict-boundary cases (exactly 0.7, 0.5, 0.3 do NOT pass), the LLM-failure fallback, and the directional guardrail. Live samples are dumped in the Audit log section above:

- AI paragraph → `stylo=0.68`, `llm=0.80`, `combined=0.74` → `high-confidence AI`
- Ramen review (human) → `stylo=0.21`, `llm=0.10`, `combined=0.16` → `high-confidence human`
- Lightly edited AI → `stylo=0.44`, `llm=0.20`, `combined=0.32` → `uncertain`

Three distinct outcomes across three inputs that should land in three different label classes confirms the thresholds actually separate them. Full spec lives in [planning.md §1–2](planning.md); the implementation is `signals/combiner.py`.

## Known limitations

The system's worst failure mode is **highly structured human writing** — technical documentation, academic abstracts, AP-style journalism, translated text, or non-native English where simpler/uniform sentences are the norm. Both signals fail in the same direction and produce a confidently-wrong call.

Why this is uniquely bad:

- `burstiness_score` ↓ — uniform sentence length is the *house style* in formal/technical writing, not a tell of AI
- `transition_density` ↑ — formal writing leans on connectors like "however," "moreover," "furthermore"
- The Groq judge **also leans AI** because the voice is genuinely flat — the absence of sentiment isn't a bug in technical writing, it's the goal

Stylometric pipeline votes AI, LLM judge votes AI, agreement gate fires → `"high-confidence AI"` on a perfectly human technical document. Worse than other failure modes because both signals AGREE on the wrong answer — there's nothing in the audit log to flag the disagreement, so a reviewer reading the trace would see two confident votes and rubber-stamp it.

Mitigations considered but not built: a `content_type` hint at submission time so platforms hosting technical content can warn users their material may be misclassified, or an asymmetric weighting if real data shows a systematic direction of error. Two more edge cases (poetry with refrains; formal prose with em-dashes as a stylistic tic) are written up in [planning.md §5](planning.md).

## Spec reflection

The most significant divergence from `planning.md` was the **strong-AI / strong-human gating rule**.

**What the spec said:** §1 defined a strict per-signal threshold:

```
strong_ai    = stylo_score > 0.7 AND llm_ai_score > 0.7  → "high-confidence AI"
strong_human = stylo_score < 0.3 AND llm_ai_score < 0.3  → "high-confidence human"
```

§2 paraphrased this as *"equivalently: combined_score > 0.7 AND signals agreed strongly"* — but the two formulations aren't actually equivalent. They diverge exactly when one signal is just shy of the bar while the other is well past it.

**What broke:** a real AI paragraph (the "Artificial intelligence represents a transformative paradigm shift…" sample) landed at `stylo=0.677` and `llm=0.80`. Both signals clearly point AI; `combined=0.74` is squarely in "leans AI" territory. But stylo missed the strict `>0.7` bar by 0.023, so the strict rule returned `"uncertain"`. The same near-miss kept happening whenever AI text didn't use literal em-dashes — `em_dash_density` is one of three equally-weighted essay features, so its absence (raw value 0.0, normalized 0.0) capped stylo at ~0.67 even when the other two features were screaming AI.

**What I changed:** relaxed the gate to a two-part rule, magnitude + direction:

```
strong_ai = combined > 0.7 AND stylo > 0.5 AND llm_ai > 0.5
```

The combined-magnitude gate does the heavy lifting; the per-signal `>0.5 / <0.5` directional check preserves the original architectural protection ("never call high-confidence when one signal points the wrong way"). A `stylo=0.45 + llm=0.97` pair still fails — `combined=0.71` clears magnitude but stylo points human, so the directional check vetoes it. I also down-tuned `em_dash_density` weight from `1/3 → 0.15` because absence isn't strong evidence of human; only its *presence* is strong evidence of AI.

**Why this counts as a real divergence, not just a fix:** the strict rule was a deliberate design choice in §1 ("we never call something high-confidence on the back of one strong signal alone"). Relaxing it admits that 0.7 was a chosen-without-data threshold and that a near-miss on a single signal shouldn't disqualify a clearly-aligned pair. I updated both `planning.md` §1 and §2 to remove the inconsistency rather than paper over it. The relaxed rule is enforced by 13 tests in `tests/test_combiner.py` — including explicit cases that would behave differently under the strict rule.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          PROVENANCE GUARD SYSTEM                            │
└─────────────────────────────────────────────────────────────────────────────┘

                                  ┌──────────┐
                                  │   USER   │
                                  └────┬─────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              │ POST /submit           │ GET /log               │ POST /appeal
              ▼                        ▼                        ▼
   ┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
   │  /submit endpoint    │  │   /log endpoint      │  │   /appeal endpoint   │
   └──────────┬───────────┘  └──────────┬───────────┘  └──────────┬───────────┘
              │                         │                         │
              ▼                         │                         ▼
   ┌──────────────────────────┐         │              ┌──────────────────────┐
   │ SIGNAL PIPELINE          │         │              │   APPEAL HANDLER     │
   │  1. Stylometric (3 eng.) │         │              │  • log reasoning     │
   │  2. LLM judge (Groq)     │         │              │    + original snap   │
   │  3. Combiner →           │         │              │  • flip decision    │
   │     {attribution, conf,  │         │              │    status → review   │
   │      label}              │         │              │                      │
   └──────────┬───────────────┘         │              └──────────┬───────────┘
              │ write                   │ read                    │ write
              ▼                         ▼                         ▼
   ┌───────────────────────────────────────────────────────────────────────┐
   │                       AUDIT LOG (SQLite)                              │
   │  tables: decisions · appeals · appeal_evidence                        │
   │  /submit appends a row; /appeal flips status + adds appeal/evidence;  │
   │  /log scans newest-first, joins latest appeal, scoped by author_id    │
   └────────────────────────────────┬──────────────────────────────────────┘
                                    │
                                    ▼
                       ┌──────────────────────────┐
                       │      JSON RESPONSE       │
                       │                          │
                       │  /submit → {submission_  │
                       │    id, attribution,      │
                       │    confidence, label}    │
                       │                          │
                       │  /log → {entries:[       │
                       │    {content_id, scores,  │
                       │     attribution, label,  │
                       │     status, appeal_      │
                       │     reasoning, ...}]}    │
                       │                          │
                       │  /appeal → {content_id,  │
                       │    appeal_id,            │
                       │    status:"under_review"}│
                       └──────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────────────┐
  │ NON-FUNCTIONAL: /submit rate-limited to 10/min + 100/day per IP         │
  │                 (in-memory; see "Rate limits" section for rationale)    │
  │                 Body size capped at 75 MB; /submit text capped at 100KB │
  └─────────────────────────────────────────────────────────────────────────┘
```

## Example

### Workflow for text
userInput -> Service [ attribution endpoint] --> Signal Detection Pipeline [ LLM as a judge on sentiment and grammer style ---> Stylometric heuristics with sentence length variance, vocab, etc] -> Service response with (attribution, confidence, transparency label) -> display the transparency label 


#### IF user wants to appeal label
userInput -> Service [ attribution endpoint] --> Signal Detection Pipeline [ Stylometric heuristics with sentence length variance, vocab, etc ---> LLM as a judge on sentiment and grammer style ] -> Service response with (attribution, confidence, transparency label) -> display the transparency label 

userInput -> Service [ appeal endpoint ] --> log reasoning for appeal with original decision --> update "under_review" in UI -> doesn't return anything


### False Positive
When system misclassifies human writer's word
* Display == uncertain maybe with .54 confidence is AI
* Creator would hit a button to appeal 
    - UI button for a "appeal classification" -> UI text for human to enter reasoning -> api call to backend with reasoning -> log reasoning and original label -> display "under_review" in UI

## AI Usage

I used Claude Code as a pair-programming assistant throughout the build. Two instances worth documenting in detail — both cases where my judgment overrode what the model produced.

### 1. Stylometric feature direction flipped against the spec

**What I asked Claude to generate:** the full Signal 1 implementation (`signals/stylometric.py`) matching `planning.md` §1 — the three engines (essay / poetry / short-form), the per-feature monotonic bounds, the min-max normalization with the inverted variant for low-is-AI features.

**What Claude produced:** a clean implementation matching the spec table exactly. `lowercase_start_ratio` was implemented as **inverted (low = AI)** because the spec said so — the reasoning in planning.md was "humans use proper case; AI mimics the 'lowercase aesthetic' voice."

**What I overrode and why:** I pulled the Kaggle "Celebrity Tweets — Real vs AI-Generated" dataset and counted the actual lowercase-start ratios across the 35 deduped tweets. Result: humans = **10%**, AI = **67%**. The direction was **backwards** — the AI imitator over-applies the lowercase voice, while real celebrities (Billie Eilish, Tyler the Creator, Ariana Grande) mix cases naturally. I flipped the feature to *direct* (high = AI), tuned the bounds to `(0.10, 0.67)`, and added a calibration paragraph to `planning.md` §1 documenting the divergence. Takeaway: when an AI implements a spec without questioning it, surprising-but-empirically-true findings get coded the wrong way around. Catch at calibration time, not in production.

### 2. Combiner gating rule relaxed against the spec

**What I asked Claude to generate:** the combiner function per `planning.md` §1's strict per-signal gating rule (`stylo > 0.7 AND llm > 0.7 → "high-confidence AI"`).

**What Claude produced:** a strict-rule combiner with all 12 spec-conformance tests passing — including the strict-boundary tie-breakers (exactly 0.7 fails because of strict `>`). It worked correctly to spec.

**What I overrode and why:** during live testing, a clearly-AI paragraph hit `stylo=0.68 + llm=0.80 → combined=0.74` and got labeled `"uncertain"` because stylo missed the strict bar by 0.023. I noticed planning.md §2 paraphrased the rule as *"equivalently: combined_score > 0.7 AND signals agreed strongly"* — but the two formulations aren't actually equivalent. I asked Claude to walk me through the divergence and was offered two options: clarify the spec doc, or relax the rule. Claude initially leaned toward keeping the strict rule as the architectural protection; I overrode that because the directional check (`stylo > 0.5 AND llm > 0.5`) preserves the same one-strong-signal-can't-carry-it guarantee without punishing near-misses. Full story is written up in the [Spec reflection](#spec-reflection) section above. Took ~30 minutes of back-and-forth to get spec doc, code, and tests all consistent again.
