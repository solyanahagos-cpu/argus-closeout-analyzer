import streamlit as st
import anthropic
import json
import re
from datetime import date, datetime
import io

# ── Document reading ──────────────────────────────────────────────

def extract_text_from_pdf(file_bytes):
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        return text.strip()
    except Exception as e:
        return ""

def extract_text_from_docx(file_bytes):
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        text = "\n".join([p.text for p in doc.paragraphs])
        return text.strip()
    except Exception as e:
        return ""

def extract_text(uploaded_file):
    file_bytes = uploaded_file.read()
    name = uploaded_file.name.lower()
    if name.endswith(".pdf"):
        return extract_text_from_pdf(file_bytes)
    elif name.endswith(".docx") or name.endswith(".doc"):
        return extract_text_from_docx(file_bytes)
    elif name.endswith(".txt"):
        return file_bytes.decode("utf-8", errors="ignore")
    else:
        return ""

# ── Date calculator tool (deterministic — no LLM) ────────────────

CLOSEOUT_DATE = date(2026, 9, 30)

def calculate_notice_deadline(notice_days: int) -> dict:
    """
    Given a required notice period in days, calculate:
    - The latest date notice must be issued
    - How many days remain from today
    - Whether the deadline is already passed, at risk, or safe
    """
    today = date.today()
    deadline = date(
        CLOSEOUT_DATE.year,
        CLOSEOUT_DATE.month,
        CLOSEOUT_DATE.day
    )
    # Subtract notice days from closeout date
    from datetime import timedelta
    notice_deadline = deadline - timedelta(days=notice_days)
    days_remaining = (notice_deadline - today).days

    if days_remaining < 0:
        status = "OVERDUE"
        status_detail = f"Notice deadline passed {abs(days_remaining)} days ago"
    elif days_remaining <= 30:
        status = "AT RISK"
        status_detail = f"Only {days_remaining} days left to issue notice"
    else:
        status = "ON TRACK"
        status_detail = f"{days_remaining} days remaining to issue notice"

    return {
        "notice_deadline": notice_deadline.strftime("%B %d, %Y"),
        "days_remaining": days_remaining,
        "status": status,
        "status_detail": status_detail,
        "closeout_date": CLOSEOUT_DATE.strftime("%B %d, %Y"),
        "notice_days_required": notice_days,
    }

# ── LLM clause extraction ─────────────────────────────────────────

SYSTEM_PROMPT = """You are a contract review assistant helping a program manager audit vendor agreements before project closeout.

Your job is to extract termination notice information from agreement text and return it as a JSON object.

Return ONLY valid JSON. No explanation, no markdown, no extra text.

The JSON must have exactly these fields:
{
  "agreement_type": "string — e.g. Subcontract, Lease, Service Agreement, Logistics Agreement",
  "vendor_name": "string — vendor or counterparty name, or 'Not specified' if not found",
  "effective_date": "string — contract start date in plain English, or 'Not specified'",
  "expiration_date": "string — contract end date in plain English, or 'Not specified'",
  "notice_period_days": integer or null — the termination notice period in calendar days as a number, null if genuinely cannot be determined,
  "notice_conditions": "string — any conditions on notice (e.g. must be in writing, cure period), or 'None specified'",
  "termination_clause_quote": "string — the verbatim sentence or phrase from the document that contains the termination notice requirement, or 'Not found'",
  "conditional_notice": true or false — true if the notice period varies by reason (e.g. for cause vs for convenience),
  "confidence": "high", "medium", or "low" — your confidence that notice_period_days is correct,
  "confidence_reason": "string — one sentence explaining why confidence is high, medium, or low"
}

Rules:
- Convert all notice periods to calendar days (e.g. '3 months' = 90 days, '2 weeks' = 14 days)
- If you find a conditional clause (different periods for cause vs convenience), set conditional_notice to true and use the LONGER period for notice_period_days
- If the document references an external attachment for notice terms, set confidence to 'low'
- If no termination clause exists at all, set notice_period_days to null and confidence to 'low'
"""

def extract_clause(text: str, api_key: str) -> dict:
    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Extract termination notice information from this agreement text:\n\n{text[:8000]}"
            }
        ]
    )

    raw = message.content[0].text.strip()

    # Strip markdown fences if present
    raw = re.sub(r"```json|```", "", raw).strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {
            "agreement_type": "Unknown",
            "vendor_name": "Unknown",
            "effective_date": "Not specified",
            "expiration_date": "Not specified",
            "notice_period_days": None,
            "notice_conditions": "Parse error",
            "termination_clause_quote": raw[:300],
            "conditional_notice": False,
            "confidence": "low",
            "confidence_reason": "Could not parse structured output from model response."
        }

    return result

# ── Reflexion pass for low-confidence extractions ─────────────────

REFLEXION_PROMPT = """The previous extraction had low confidence. Re-read the following agreement text carefully and focus specifically on finding the termination or cancellation notice requirement.

Look for phrases like:
- "terminate", "termination", "cancellation", "cancel"
- "notice", "prior written notice", "advance notice"
- "days", "months", "weeks"
- "convenience", "cause", "breach"

Return the same JSON format as before. If there is genuinely no termination clause, set notice_period_days to null."""

def reflexion_pass(text: str, first_result: dict, api_key: str) -> dict:
    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Extract termination notice information from this agreement text:\n\n{text[:8000]}"
            },
            {
                "role": "assistant",
                "content": json.dumps(first_result)
            },
            {
                "role": "user",
                "content": REFLEXION_PROMPT + f"\n\nAgreement text again:\n\n{text[:8000]}"
            }
        ]
    )

    raw = message.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return first_result  # Fall back to first result if reflexion fails

# ── Tracker comparison ────────────────────────────────────────────

def compare_to_tracker(extracted_days, tracker_days_str: str) -> dict:
    if not tracker_days_str or tracker_days_str.strip() == "":
        return {
            "tracker_value": "Blank",
            "match": False,
            "mismatch_detail": "Tracker field is empty — no notice period recorded."
        }

    try:
        tracker_days = int(re.search(r'\d+', tracker_days_str).group())
    except (AttributeError, ValueError):
        return {
            "tracker_value": tracker_days_str,
            "match": False,
            "mismatch_detail": f"Tracker value '{tracker_days_str}' could not be parsed as a number of days."
        }

    if extracted_days is None:
        return {
            "tracker_value": f"{tracker_days} days",
            "match": False,
            "mismatch_detail": "Agreement document does not contain a clear termination clause to compare against."
        }

    match = (extracted_days == tracker_days)
    return {
        "tracker_value": f"{tracker_days} days",
        "match": match,
        "mismatch_detail": "" if match else (
            f"Document says {extracted_days} days but tracker records {tracker_days} days."
        )
    }

# ── Readiness determination ───────────────────────────────────────

def determine_readiness(extraction, date_calc, tracker_comparison, confidence):
    issues = []

    # No notice period found
    if extraction.get("notice_period_days") is None:
        return "🔴 Action Required", [
            "No termination notice period could be identified in this document.",
            "Manual review of the original agreement is required before closeout."
        ]

    # Low confidence → Needs Review regardless
    if confidence == "low":
        issues.append("Extraction confidence is low — the notice period may not be reliable.")

    # Conditional clause warning
    if extraction.get("conditional_notice"):
        issues.append("This agreement has conditional notice periods (e.g., different terms for cause vs. convenience). Verify which applies.")

    # Date status
    if date_calc:
        if date_calc["status"] == "OVERDUE":
            issues.append(f"⚠️ Notice deadline has already passed ({date_calc['notice_deadline']}). Escalate immediately.")
        elif date_calc["status"] == "AT RISK":
            issues.append(f"⚠️ Notice deadline is approaching ({date_calc['notice_deadline']}). {date_calc['days_remaining']} days remaining.")

    # Tracker mismatch
    if tracker_comparison and not tracker_comparison["match"] and tracker_comparison["tracker_value"] != "Blank":
        issues.append(f"Tracker mismatch: {tracker_comparison['mismatch_detail']}")
    elif tracker_comparison and tracker_comparison["tracker_value"] == "Blank":
        issues.append("Tracker entry is blank — update the tracker with the extracted notice period.")

    # Determine status
    critical_keywords = ["passed", "Escalate", "OVERDUE"]
    has_critical = any(any(kw in issue for kw in critical_keywords) for issue in issues)

    if has_critical:
        status = "🔴 Action Required"
    elif issues:
        status = "🟡 Needs Review"
    else:
        status = "🟢 Complete"

    if not issues:
        issues = ["All fields are present and consistent. No immediate action required."]

    return status, issues

# ── Streamlit UI ──────────────────────────────────────────────────

st.set_page_config(
    page_title="ARGUS – Contract Closeout Risk Analyzer",
    page_icon="👁️",
    layout="wide"
)

st.title("👁️ ARGUS")
st.markdown("### Contract Closeout Risk Analyzer")
st.markdown(
    "*Named for Argus Panoptes — the all-seeing guardian of Greek mythology. "
    "ARGUS watches your vendor agreements so nothing slips through.*"
)
st.markdown(
    "Upload a vendor agreement to extract its termination notice requirements, "
    "check them against your tracker, and flag any closeout risks. "
    "**Project closeout date: September 30, 2026.**"
)
st.divider()

# ── Sidebar inputs ────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Setup")
    # Try to load from Streamlit secrets first, fall back to manual input
    default_key = ""
    try:
        default_key = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass

    if default_key:
        api_key = default_key
        st.success("API key loaded.")
    else:
        api_key = st.text_input(
            "Anthropic API Key",
            type="password",
            help="Enter your Anthropic API key. It is never stored or logged."
        )

    st.header("📁 Upload Agreement")
    uploaded_file = st.file_uploader(
        "Upload a vendor agreement",
        type=["pdf", "docx", "txt"],
        help="Supported formats: PDF, Word (.docx), or plain text"
    )

    st.header("📊 Tracker Entry (optional)")
    tracker_notice = st.text_input(
        "Notice period in your tracker",
        placeholder="e.g. 30, 60 days, 90",
        help="Enter the notice period currently recorded in your subcontract tracker for this agreement. Leave blank if not yet recorded."
    )

    analyze_btn = st.button("🔍 Analyze Agreement", type="primary", use_container_width=True)

# ── Main panel ────────────────────────────────────────────────────
if not api_key:
    st.info("👈 Enter your Anthropic API key in the sidebar to get started.")
    st.stop()

if not uploaded_file:
    st.info("👈 Upload an agreement document in the sidebar to begin.")

    with st.expander("ℹ️ What does this tool do?"):
        st.markdown("""
This tool helps Program Managers and Associates review vendor agreements during project closeout.

**It will:**
- Extract the termination notice period from your agreement document
- Calculate whether the notice deadline is still achievable before the September 30, 2026 closeout
- Compare the extracted value against what your tracker records
- Flag any gaps, mismatches, or urgent actions needed

**It will NOT:**
- Make final decisions or issue notices on your behalf
- Store or retain your documents
- Replace human review of flagged items

*All flagged items require human confirmation before any action is taken.*
        """)
    st.stop()

if analyze_btn:
    with st.spinner("Reading document..."):
        raw_text = extract_text(uploaded_file)

    if len(raw_text.strip()) < 50:
        st.error(
            "⚠️ This document appears to be scanned or unreadable. "
            "Less than 50 words of text could be extracted. "
            "**Manual review required** — do not rely on automated analysis for this file."
        )
        st.stop()

    st.success(f"✅ Document read successfully — {len(raw_text.split())} words extracted.")

    # ── Extraction ────────────────────────────────────────────────
    with st.spinner("Extracting termination clause..."):
        try:
            extraction = extract_clause(raw_text, api_key)
        except Exception as e:
            st.error(f"API error during extraction: {e}")
            st.stop()

    # ── Reflexion loop if confidence is low ───────────────────────
    if extraction.get("confidence") == "low":
        with st.spinner("Low confidence detected — re-reading clause..."):
            try:
                extraction = reflexion_pass(raw_text, extraction, api_key)
            except Exception as e:
                pass  # Keep first result if reflexion fails

    # ── Date calculation (deterministic) ─────────────────────────
    notice_days = extraction.get("notice_period_days")
    date_calc = calculate_notice_deadline(notice_days) if notice_days else None

    # ── Tracker comparison ────────────────────────────────────────
    tracker_comparison = compare_to_tracker(notice_days, tracker_notice) if tracker_notice.strip() else None

    # ── Readiness determination ───────────────────────────────────
    status, issues = determine_readiness(
        extraction, date_calc, tracker_comparison, extraction.get("confidence", "low")
    )

    # ── Display results ───────────────────────────────────────────
    st.divider()

    # Status badge
    col1, col2 = st.columns([1, 2])
    with col1:
        if "🔴" in status:
            st.error(f"## {status}")
        elif "🟡" in status:
            st.warning(f"## {status}")
        else:
            st.success(f"## {status}")

    with col2:
        st.markdown("### Issues & Recommended Actions")
        for issue in issues:
            st.markdown(f"- {issue}")

    st.divider()

    # Extracted fields table
    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("### 📄 Extracted Agreement Fields")
        fields = {
            "Agreement Type": extraction.get("agreement_type", "—"),
            "Vendor / Counterparty": extraction.get("vendor_name", "—"),
            "Effective Date": extraction.get("effective_date", "—"),
            "Expiration Date": extraction.get("expiration_date", "—"),
            "Notice Period": f"{notice_days} days" if notice_days else "Not found",
            "Notice Conditions": extraction.get("notice_conditions", "—"),
            "Conditional Clause": "Yes ⚠️" if extraction.get("conditional_notice") else "No",
            "Extraction Confidence": extraction.get("confidence", "—").upper(),
        }
        for k, v in fields.items():
            st.markdown(f"**{k}:** {v}")

    with col_b:
        st.markdown("### 📅 Deadline Calculator")
        if date_calc:
            dc = date_calc
            if dc["status"] == "OVERDUE":
                st.error(f"**Notice Deadline:** {dc['notice_deadline']}")
                st.error(f"**Status:** {dc['status_detail']}")
            elif dc["status"] == "AT RISK":
                st.warning(f"**Notice Deadline:** {dc['notice_deadline']}")
                st.warning(f"**Status:** {dc['status_detail']}")
            else:
                st.success(f"**Notice Deadline:** {dc['notice_deadline']}")
                st.success(f"**Status:** {dc['status_detail']}")

            st.markdown(f"**Notice Period Required:** {dc['notice_days_required']} days")
            st.markdown(f"**Project Closeout Date:** {dc['closeout_date']}")
        else:
            st.info("Date calculation not available — notice period could not be extracted.")

        if tracker_comparison:
            st.markdown("### 📊 Tracker Comparison")
            if tracker_comparison["match"]:
                st.success(f"✅ Tracker matches document: **{tracker_comparison['tracker_value']}**")
            else:
                st.error(f"❌ Mismatch: {tracker_comparison['mismatch_detail']}")

    st.divider()

    # Verbatim clause
    st.markdown("### 📝 Verbatim Clause (Evidence)")
    clause = extraction.get("termination_clause_quote", "Not found")
    if clause and clause != "Not found":
        st.info(f'"{clause}"')
    else:
        st.warning("No termination clause was found in this document.")

    if extraction.get("confidence_reason"):
        st.caption(f"Confidence note: {extraction['confidence_reason']}")

    st.divider()

    # Download button
    import csv, io as sio
    output = sio.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "File", "Agreement Type", "Vendor", "Notice Period (days)",
        "Notice Deadline", "Days Remaining", "Deadline Status",
        "Tracker Value", "Tracker Match", "Readiness Status", "Confidence"
    ])
    writer.writerow([
        uploaded_file.name,
        extraction.get("agreement_type", ""),
        extraction.get("vendor_name", ""),
        notice_days or "",
        date_calc["notice_deadline"] if date_calc else "",
        date_calc["days_remaining"] if date_calc else "",
        date_calc["status"] if date_calc else "",
        tracker_comparison["tracker_value"] if tracker_comparison else tracker_notice,
        tracker_comparison["match"] if tracker_comparison else "",
        status.replace("🔴 ", "").replace("🟡 ", "").replace("🟢 ", ""),
        extraction.get("confidence", "")
    ])

    st.download_button(
        label="⬇️ Download Result as CSV",
        data=output.getvalue(),
        file_name=f"closeout_review_{uploaded_file.name.split('.')[0]}.csv",
        mime="text/csv"
    )

    st.caption(
        "⚠️ ARGUS is for decision support only. All flagged items must be confirmed "
        "by a Program Manager or Associate reading the original document before any action is taken. "
        "This tool does not issue notices or take any action on your behalf."
    )
