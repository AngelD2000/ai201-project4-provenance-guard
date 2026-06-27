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
- Rate liimiting
    * Assuming just people in the US let's do 5k TP 3s


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
