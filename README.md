# ARGUS — Contract Closeout Risk Analyzer

A tool that reads vendor agreement documents, extracts termination notice requirements, and flags missing or mismatched entries before a project closes — so nothing slips through.

---

## Context, User, and Problem

**Who the user is:** A Program Manager or Program Associate on a large, internationally funded development project — in this case, a USAID-funded health supply chain program (GHSC-PSM) preparing for project closeout by September 30, 2026.

**What workflow this improves:** During project closeout, the program team must review every active vendor agreement — subcontracts, warehouse leases, logistics service agreements — to confirm that the correct termination notice period is recorded in a master tracking spreadsheet, and that there is still time to issue notice before the project ends.

A termination notice period is a legally required window of advance warning before a contract can be ended — commonly 30, 60, or 90 days. If a project closes in September and a vendor requires 90 days' notice, that notice must go out by July at the latest. Missing that window means the project is legally obligated to continue paying the vendor after the project has officially ended — an unallowable cost that creates direct financial exposure.

**Why it matters:** With 30–60+ agreements across multiple countries, this review currently relies on manual reading, attention to detail, and experience. That becomes a fragile process under closeout time pressure. A single missed notice deadline can result in tens of thousands of dollars in unallowable costs charged to the implementing organization.

---

## Solution and Design

**What was built:** A Streamlit web app that accepts an uploaded vendor agreement (PDF, Word, or plain text) and an optional tracker entry, then produces a color-coded readiness flag with a plain-language explanation and a recommended next step.

**How it works:**

1. **Document reader** — Extracts full text from the uploaded file using PyMuPDF (PDF) or python-docx (Word)
2. **Clause extraction (LLM)** — Calls `claude-haiku-4-5` with a structured system prompt to extract key fields: agreement type, vendor name, notice period in days, conditions, and the verbatim clause text. Output is constrained to JSON.
3. **Reflexion loop** — If the model returns `"confidence": "low"`, a second targeted pass re-reads the clause before producing a final result. Capped at 2 iterations.
4. **Date calculator (deterministic tool)** — Pure Python arithmetic: takes the extracted notice period in days, subtracts from the September 30, 2026 closeout date, and computes the latest date notice must be issued and how many days remain. This step intentionally does not use an LLM — deadline math should not be subject to model error.
5. **Tracker comparison** — Compares the extracted notice period against the value the user enters from their spreadsheet, flagging mismatches and blank entries.
6. **Readiness determination** — Combines confidence level, date calculator output, and tracker comparison into a final status: 🟢 Complete, 🟡 Needs Review, or 🔴 Action Required.

**Key design choices:**
- Used `claude-haiku-4-5` (fast, low cost) for extraction; structured JSON output prevents hallucinated free-text responses
- Deterministic date calculation kept outside the LLM entirely
- Reflexion loop handles ambiguous clauses without adding significant cost (haiku is cheap)
- Single-document interface keeps scope tight and evaluation tractable

---

## Evaluation and Results

**Baseline:** The current manual process — the Program Manager opens each agreement, finds the termination clause by reading, enters the period in the tracker, and manually calculates whether the deadline has passed. This was simulated on the same 5 test cases.

**Test set:** 5 synthetic agreement excerpts covering:
- TC01: Standard subcontract, 30-day notice, tracker matches → expected: ✅ Complete
- TC02: Warehouse lease, 60-day notice, tracker blank → expected: 🔴 Action Required
- TC03: Service agreement with legal phrasing ("not less than ninety (90) calendar days"), tracker shows wrong value → expected: 🔴 Action Required
- TC04: Logistics agreement where notice terms are in a missing Schedule B → expected: 🟡 Needs Review
- TC05: Conditional clause (45 days for convenience, 10 for cause) → expected: 🟡 Needs Review

**Rubric dimensions:**
1. Notice period extraction accuracy (exact match in days)
2. Readiness status classification accuracy
3. Date calculation correctness (100% expected — deterministic code)
4. Appropriate flagging of uncertainty (low confidence, cross-references, conditional clauses)

**Results:**

| Test Case | Expected Status | System Output | Notice Period | Correct? |
|-----------|----------------|---------------|---------------|----------|
| TC01 | ✅ Complete | ✅ Complete | 30 days ✓ | ✅ |
| TC02 | 🔴 Action Required | 🔴 Action Required | 60 days ✓ | ✅ |
| TC03 | 🔴 Action Required | 🔴 Action Required | 90 days ✓ | ✅ |
| TC04 | 🟡 Needs Review | 🟡 Needs Review | null ✓ | ✅ |
| TC05 | 🟡 Needs Review | 🟡 Needs Review | 45 days ✓ | ✅ |

**vs. Manual baseline:** Manual review of all 5 cases took approximately 8 minutes. The system processed each case in under 10 seconds. On TC03, manual review nearly missed the notice period because the legalese phrasing required careful re-reading — the system normalized it correctly on the first pass.

**Where it broke down:**
- Scanned PDFs with no text layer return empty extraction — the system warns correctly but cannot proceed
- Cross-document references (TC04) correctly trigger Needs Review, but the system cannot retrieve the missing document
- Very short or poorly formatted agreements may confuse the JSON extraction; the reflexion pass helps but does not always resolve it

---

## Artifact Snapshot

**App interface:**
- Left sidebar: API key, file upload, tracker entry input
- Right panel: Status badge, extracted fields table, deadline calculator, tracker comparison, verbatim clause, download button

**Sample output for TC02 (60-day lease, blank tracker):**

```
🔴 Action Required

Issues & Recommended Actions:
- Tracker entry is blank — update the tracker with the extracted notice period.
- ⚠️ Notice deadline is approaching (July 31, 2026). 78 days remaining.

Extracted Fields:
  Agreement Type: Lease
  Vendor: Capital Storage Properties, Inc.
  Notice Period: 60 days
  Confidence: HIGH

Deadline Calculator:
  Notice Deadline: July 31, 2026
  Days Remaining: 78
  Status: AT RISK

Verbatim Clause:
"Either party wishing to terminate this Lease prior to the end of the current term
must provide the other party with a minimum of sixty (60) days advance written notice."
```

---

## Setup and Usage

### Prerequisites
- Python 3.9 or higher
- An Anthropic API key ([get one here](https://console.anthropic.com))

### Installation

```bash
git clone https://github.com/YOUR_USERNAME/closeout-risk-analyzer.git
cd closeout-risk-analyzer
pip install -r requirements.txt
```

### Running the app

```bash
streamlit run app.py
```

The app will open in your browser at `http://localhost:8501`.

### Usage

1. Enter your Anthropic API key in the sidebar (never stored or logged)
2. Upload a vendor agreement (PDF, Word .docx, or .txt)
3. Optionally enter the notice period currently in your tracker
4. Click **Analyze Agreement**
5. Review the status badge, extracted fields, deadline calculation, and recommended actions
6. Download the result as a CSV row to paste into your tracker

### Running the test cases

Test case files are in the `/test_cases` folder. Upload any `.txt` file to the app to test it. Ground truth values are included at the bottom of each test file.

### API key note

The app accepts the key via the sidebar input — you do not need to set an environment variable. If you prefer, you can also set `ANTHROPIC_API_KEY` in your environment and modify the `api_key` input to read from it.

---

## Governance and Trust Boundaries

This tool is for **decision support only**. It surfaces flags for human review — it does not issue notices, update trackers, or take any action.

- Any 🔴 Action Required flag must be confirmed by reading the original document before acting
- The system will not produce a flag if fewer than 50 words are extracted (scanned document warning)
- Low-confidence extractions are always classified as 🟡 Needs Review, never forced to a definitive status
- No documents are stored — all processing is in-session only
- No real contract data is committed to this repository
