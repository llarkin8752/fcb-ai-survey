import streamlit as st
import random
import time
import json
import io
import os
import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
from datetime import datetime
import anthropic

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="FCB AI Aptitude Survey",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding-top: 2rem; padding-bottom: 5rem; max-width: 780px; }

.progress-container {
    position: fixed; bottom: 0; left: 0; right: 0;
    background: white; border-top: 1px solid #e5e7eb;
    padding: 14px 32px; z-index: 9999;
    display: flex; align-items: center; gap: 16px;
    box-shadow: 0 -2px 12px rgba(0,0,0,0.06);
}
.progress-bar-bg { flex: 1; height: 10px; background: #e5e7eb; border-radius: 99px; overflow: hidden; }
.progress-bar-fill {
    height: 100%;
    background: linear-gradient(90deg, #A6192E, #c94060);
    border-radius: 99px; transition: width 0.5s ease;
}
.progress-label { font-size: 13px; font-weight: 700; color: #A6192E; min-width: 44px; text-align: right; }
.progress-step  { font-size: 12px; color: #6b7280; white-space: nowrap; }

.section-header {
    background: linear-gradient(135deg, #A6192E 0%, #c94060 100%);
    color: white; border-radius: 14px;
    padding: 1.6rem 2rem; margin-bottom: 1.6rem; text-align: center;
}
.section-header h2 { margin: 0; font-size: 1.5rem; font-weight: 700; }
.section-header p  { margin: 0.4rem 0 0; font-size: 0.93rem; opacity: 0.88; }

.survey-card {
    background: white; border-radius: 14px;
    border: 1px solid #e5e7eb;
    padding: 1.6rem 2rem; margin-bottom: 1.2rem;
    box-shadow: 0 2px 8px rgba(0,0,0,0.05);
    color: #374151;
}

.phase-badge {
    display: inline-block;
    background: #f3f4f6; color: #374151;
    border-radius: 8px; padding: 6px 14px;
    font-size: 0.82rem; font-weight: 600;
    margin-bottom: 1rem;
}

.chat-msg-user {
    background: #f3f4f6; border-radius: 12px 12px 4px 12px;
    padding: 0.75rem 1rem; margin: 0.6rem 0; margin-left: 2.5rem;
    font-size: 0.9rem; line-height: 1.5;
}
.chat-msg-ai {
    background: #fdf2f4; border: 1px solid #f3c4cc;
    border-radius: 12px 12px 12px 4px;
    padding: 0.75rem 1rem; margin: 0.6rem 0; margin-right: 2.5rem;
    font-size: 0.9rem; line-height: 1.5;
}

.simplified-card {
    background: #fffbeb; border-radius: 12px;
    border: 2px solid #f59e0b;
    padding: 1.2rem 1.6rem; margin-top: 0.8rem;
}

.raffle-box {
    background: linear-gradient(135deg, #fffbeb, #fef3c7);
    border: 2px solid #f59e0b; border-radius: 16px;
    padding: 1.8rem 2rem; text-align: center; margin-top: 1.5rem;
}
.raffle-box h3 { color: #92400e; margin: 0 0 0.4rem; }

div[data-testid="stButton"] > button {
    background: white; color: #374151;
    border: 1px solid #d1d5db; border-radius: 10px;
    padding: 0.65rem 2rem; font-weight: 600; font-size: 1rem;
    transition: opacity 0.2s; width: 100%;
}
div[data-testid="stButton"] > button:hover { opacity: 0.85; }
div[data-testid="stButton"] > button:disabled { opacity: 0.4; }
</style>
""", unsafe_allow_html=True)


# ── Google Sheets ─────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEET_HEADERS = [
    "participant_id", "submitted_at", "major", "academic_year",
    "completion_status",
    "likert_1", "likert_2", "likert_3", "likert_4", "likert_5", "likert_avg",
    "mc_questions", "mc_answers", "mc_e_flags", "mc_score", "mc_total", "mc_time_sec",
    "s1_key", "s1_initial_response", "s1_chat_turns", "s1_chat_history",
    "s1_final_response", "s1_ai_score_json", "s1_time_sec",
    "raffle_email",
]

# FIX 1: Added ttl=3600 so credentials refresh every hour instead of expiring silently
@st.cache_resource(ttl=3600)
def _get_gspread_client():
    """Cache the auth client with a 1-hour TTL to prevent token expiry failures."""
    creds_info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_sheet():
    """Always return a fresh worksheet reference so header changes are picked up immediately."""
    try:
        gc = _get_gspread_client()
        sh = gc.open_by_key(st.secrets["GSHEET_KEY"])
        try:
            ws = sh.worksheet("responses")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title="responses", rows=2000, cols=30)
        if not ws.row_values(1):
            ws.append_row(SHEET_HEADERS, value_input_option='RAW')
        return ws
    except Exception as e:
        st.error(f"⚠️ Google Sheets connection failed: {e}")
        # FIX 2: Store last error in session state for debugging
        st.session_state["last_sheet_error"] = str(e)
        return None


# ── Progressive save helpers ──────────────────────────────────────────────────

def _find_row(ws):
    """Return the 1-based sheet row index for the current session, or None."""
    try:
        all_ids = ws.col_values(1)
        target = st.session_state.session_id
        for i, pid in enumerate(reversed(all_ids)):
            # FIX 3: Strip whitespace and compare as strings to handle Sheets number formatting
            if str(pid).strip() == str(target).strip():
                return len(all_ids) - i
    except Exception as e:
        st.session_state["last_find_row_error"] = str(e)
    return None


def _update_col(ws, row_idx, col_name, value):
    """Update a single named column in an existing row."""
    try:
        header = ws.row_values(1)
        if col_name not in header:
            return
        col_idx = header.index(col_name) + 1
        ws.update_cell(row_idx, col_idx, str(value))
    except Exception as e:
        st.session_state["last_update_error"] = f"{col_name}: {e}"


def save_initial(major, year):
    """
    STAGE 1 — Called on Page 1 (About You) when respondent confirms FCB status.
    Creates a new row with participant_id, timestamp, major, year, and
    completion_status = 'started'. All other columns are left blank.
    """
    # FIX 4: Guard against duplicate rows if user clicks Back then Continue again
    if st.session_state.get("initial_saved"):
        return

    ws = get_sheet()
    if ws is None:
        return
    try:
        header = ws.row_values(1)
        blank_row = [""] * len(header)
        # FIX 5: Prefix session ID with "S_" so Google Sheets stores it as text not a number
        blank_row[header.index("participant_id")]    = st.session_state.session_id
        blank_row[header.index("submitted_at")]      = datetime.now().isoformat()
        blank_row[header.index("major")]             = major
        blank_row[header.index("academic_year")]     = year
        blank_row[header.index("completion_status")] = "started"
        # FIX 6: Use value_input_option='RAW' so session_id is never converted to a number
        ws.append_row(blank_row, value_input_option='RAW')
        st.session_state["initial_saved"] = True
        st.session_state["last_sheet_error"] = None
    except Exception as e:
        st.session_state["last_sheet_error"] = f"save_initial failed: {e}"


def save_likert():
    """
    STAGE 2a — Called after Page 2 (Likert).
    Updates likert columns and advances completion_status to 'likert_complete'.
    """
    ws = get_sheet()
    if ws is None:
        return
    row_idx = _find_row(ws)
    if not row_idx:
        st.session_state["last_sheet_error"] = "save_likert: could not find row for session_id"
        return
    try:
        likert_vals = [st.session_state.likert.get(i, 0) for i in range(5)]
        likert_avg  = round(sum(likert_vals) / 5, 2) if all(likert_vals) else ""
        for i, val in enumerate(likert_vals):
            _update_col(ws, row_idx, f"likert_{i+1}", val)
        _update_col(ws, row_idx, "likert_avg", likert_avg)
        _update_col(ws, row_idx, "completion_status", "likert_complete")
    except Exception as e:
        st.session_state["last_sheet_error"] = f"save_likert failed: {e}"


def save_mc():
    """
    STAGE 2b — Called after Page 3 (MC quiz).
    Updates MC columns (including mc_e_flags) and advances completion_status to 'mc_complete'.
    mc_answers stores only A/B/C/D; mc_e_flags stores {question_key: 0|1}.
    """
    ws = get_sheet()
    if ws is None:
        return
    row_idx = _find_row(ws)
    if not row_idx:
        st.session_state["last_sheet_error"] = "save_mc: could not find row for session_id"
        return
    try:
        mc_pool    = st.session_state.mc_pool
        mc_answers = st.session_state.mc_answers
        mc_e_flags = st.session_state.mc_e_flags

        def effective_answer(q):
            k = q["key"]
            if mc_e_flags.get(k, 0) == 1 and k in SIMPLIFIED_QUESTIONS:
                return st.session_state.mc_clarify_answers.get(k)
            return mc_answers.get(k)

        mc_score = sum(
            1 for q in mc_pool
            if effective_answer(q) == q["answer"]
        )
        mc_time = round(time.time() - st.session_state.mc_start, 1) if st.session_state.mc_start else ""

        clean_answers = {}
        for q in mc_pool:
            k = q["key"]
            if mc_e_flags.get(k, 0) == 1 and k in SIMPLIFIED_QUESTIONS:
                clean_answers[k] = st.session_state.mc_clarify_answers.get(k, "")
            else:
                clean_answers[k] = mc_answers.get(k, "")

        full_e_flags = {q["key"]: mc_e_flags.get(q["key"], 0) for q in mc_pool}

        _update_col(ws, row_idx, "mc_questions", json.dumps([q["key"] for q in mc_pool]))
        _update_col(ws, row_idx, "mc_answers",   json.dumps(clean_answers))
        _update_col(ws, row_idx, "mc_e_flags",   json.dumps(full_e_flags))
        _update_col(ws, row_idx, "mc_score",     mc_score)
        _update_col(ws, row_idx, "mc_total",     len(mc_pool))
        _update_col(ws, row_idx, "mc_time_sec",  mc_time)
        _update_col(ws, row_idx, "completion_status", "mc_complete")
    except Exception as e:
        st.session_state["last_sheet_error"] = f"save_mc failed: {e}"


def save_scenario(scenario_key, is_last):
    """
    STAGE 2c — Called after the single scenario is submitted.
    All scenario data is written to s1_* columns.
    """
    ws = get_sheet()
    if ws is None:
        return
    row_idx = _find_row(ws)
    if not row_idx:
        st.session_state["last_sheet_error"] = "save_scenario: could not find row for session_id"
        return
    try:
        k = scenario_key
        chat_hist  = st.session_state.scenario_chat.get(k, [])
        user_turns = len([m for m in chat_hist if m["role"] == "user"])
        _update_col(ws, row_idx, "s1_key",              k)
        _update_col(ws, row_idx, "s1_initial_response", st.session_state.scenario_initial.get(k, ""))
        _update_col(ws, row_idx, "s1_chat_turns",       user_turns)
        _update_col(ws, row_idx, "s1_chat_history",     json.dumps(chat_hist))
        _update_col(ws, row_idx, "s1_final_response",   st.session_state.scenario_final.get(k, ""))
        _update_col(ws, row_idx, "s1_ai_score_json",    json.dumps(st.session_state.scenario_scores.get(k, {})))
        _update_col(ws, row_idx, "s1_time_sec",         st.session_state.scenario_times.get(k, ""))
        _update_col(ws, row_idx, "completion_status",   "submitted")
    except Exception as e:
        st.session_state["last_sheet_error"] = f"save_scenario failed: {e}"


def save_to_sheet():
    """
    STAGE 3 — Final save on full submission (kept for compatibility).
    Ensures completion_status is marked 'submitted'.
    """
    ws = get_sheet()
    if ws is None:
        return False
    try:
        row_idx = _find_row(ws)
        if row_idx:
            _update_col(ws, row_idx, "completion_status", "submitted")
        return True
    except Exception as e:
        st.error(f"Save failed: {e}")
        return False


def update_raffle_email(email):
    """Updates the raffle_email column for the current session row."""
    ws = get_sheet()
    if ws is None:
        return
    try:
        row_idx = _find_row(ws)
        if row_idx:
            _update_col(ws, row_idx, "raffle_email", email)
    except Exception as e:
        st.session_state["last_sheet_error"] = f"update_raffle_email failed: {e}"


# ── Anthropic helpers ─────────────────────────────────────────────────────────
def _anthropic_client():
    api_key = st.secrets.get("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
    return anthropic.Anthropic(api_key=api_key)


def score_scenario(scenario_key, final_response):
    scenario = next(s for s in ALL_SCENARIOS if s["key"] == scenario_key)
    user_msg = (
        f"Scenario context: {scenario['context']}\n\n"
        f"Task prompt: {scenario['prompt']}\n\n"
        f"Student's final response:\n{final_response}"
    )
    try:
        msg = _anthropic_client().messages.create(
            # FIX 7: Corrected model string — was "claude-sonnet-4-6" which is invalid
            model="claude-sonnet-4-20250514",
            max_tokens=700,
            system=SCORING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = msg.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        st.session_state["last_anthropic_error"] = f"score_scenario: {e}"
        return {"error": str(e), "total": 0, "summary": "Scoring unavailable."}


def chat_with_claude(scenario_key, user_message):
    scenario = next(s for s in ALL_SCENARIOS if s["key"] == scenario_key)
    history = st.session_state.scenario_chat.get(scenario_key, [])
    system = f"""You are an AI discussion partner helping a business student think through a real-world scenario as part of an academic survey.

Scenario context: {scenario['context']}
Task: {scenario['prompt']}

The student has already written an initial response. Your role is Socratic — ask probing questions, challenge their assumptions, push them to think deeper. Do NOT write their answer for them or just validate what they said. Be concise (3-5 sentences). Professional and encouraging tone."""

    try:
        msg = _anthropic_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system,
            messages=history + [{"role": "user", "content": user_message}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        st.session_state["last_anthropic_error"] = f"chat_with_claude: {e}"
        return f"(Error: {e})"


# ── Static data ───────────────────────────────────────────────────────────────
MAJORS = [
    "Accountancy (BSBA)", "Finance (BSBA)", "Financial Services (BSBA)",
    "General Business (BSBA)", "Information Systems (BSBA)",
    "International Business (BA)", "Management (BSBA)", "Marketing (BSBA)",
    "Real Estate (BSBA)", "BMACC (Accountancy BSBA/MS)",
    "Online Degree Completion (BSBA)", "BMGBD (B.A./M.S.)", "Business Minor",
]

LIKERT_QUESTIONS = [
    "I feel confident using AI tools (e.g., ChatGPT, Claude) to complete business tasks.",
    "I understand how to write effective prompts that get useful responses from AI assistants.",
    "I can identify when an AI-generated response might be inaccurate or biased.",
    "I am aware of ethical and privacy concerns when using AI in a professional setting.",
    "I believe my SDSU coursework has adequately prepared me to use AI in my future career.",
]
LIKERT_LABELS = ["Strongly\nDisagree", "Disagree", "Neutral", "Agree", "Strongly\nAgree"]


# ── Simplified fallback questions ─────────────────────────────────────────────
SIMPLIFIED_QUESTIONS = {
    "pe_improve_output": {
        "q": "Your AI-generated report feels too vague. What is the best way to get a more useful result?",
        "options": {
            "A": "Change a technical setting to make the output more random.",
            "B": "Rewrite your instructions to be more specific about what you need.",
            "C": "Ask it to write more words.",
            "D": "Keep trying the same request until it improves.",
        },
        "answer": "B",
    },
    "custom_gpt_advanced": {
        "q": "What is the safest way to set up a Custom GPT for use in a business?",
        "options": {
            "A": "Permanently train it on your company's private data.",
            "B": "Turn up its creativity settings for better results.",
            "C": "Remove its guidelines so it can answer more freely.",
            "D": "Connect it to approved company sources with clear rules and human review.",
        },
        "answer": "D",
    },
    "llm_distribution_shift": {
        "q": "An AI works great during testing but struggles in real workplace situations. Why might this happen?",
        "options": {
            "A": "It was only tested on familiar situations, so new ones throw it off.",
            "B": "It forgot what it learned during testing.",
            "C": "It changes how it behaves depending on who is using it.",
            "D": "It needs more creative tasks to perform well.",
        },
        "answer": "A",
    },
    "data_privacy_reidentification": {
        "q": "You removed all names from customer conversations before using AI. Are there still privacy risks?",
        "options": {
            "A": "No — removing names is enough to protect privacy.",
            "B": "Yes — the AI may still figure out who someone is based on other details like their role or location.",
            "C": "No — AI cannot store or remember conversation data.",
            "D": "Yes — but only if the conversations mention financial information.",
        },
        "answer": "B",
    },
    "hallucination_cutoff": {
        "q": "An AI gives a detailed explanation of something that happened after it was last updated. What most likely explains this?",
        "options": {
            "A": "It has a live connection to current news.",
            "B": "It remembered and stored that future event in advance.",
            "C": "It made up a convincing-sounding answer based on patterns it already knew.",
            "D": "It was smart enough to figure out what must have happened on its own.",
        },
        "answer": "C",
    },
    "bias_detection_student": {
        "q": "An AI trained mostly on city data is used to predict what rural customers want. What is the biggest risk?",
        "options": {
            "A": "It will respond more slowly in rural areas.",
            "B": "Its predictions may not apply well to rural customers because it learned mostly from city patterns.",
            "C": "It will need extra memory to handle geographic differences.",
            "D": "It will produce longer answers than necessary.",
        },
        "answer": "B",
    },
    "bias_detection_demographic": {
        "q": "AI recommends ignoring customers aged 18-34 because they seem harder to satisfy. How should you respond?",
        "options": {
            "A": "Accept it — younger customers are commonly known to be less satisfied.",
            "B": "Question it — the data the AI learned from may have been skewed, so check other sources first.",
            "C": "Verify it with just one other source, then use the recommendation.",
            "D": "Flag it as a concern but still share the recommendation with your team.",
        },
        "answer": "B",
    },
}

MC_BANK = [
    # ── Prompt Engineering ──────────────────────────────────────────────────
    {
        "key": "pe_purpose", "domain": "Prompt Engineering", "difficulty": "Easy",
        "q": "Which of the following best describes the purpose of prompt engineering?",
        "options": {
            "A": "To retrain the language model using new datasets.",
            "B": "To design and structure inputs that guide the AI toward more accurate and relevant outputs.",
            "C": "To modify the internal neural network architecture.",
            "D": "To increase the computational speed of the AI system.",
        },
        "answer": "B",
    },
    {
        "key": "pe_improve_output", "domain": "Prompt Engineering", "difficulty": "Medium",
        "q": (
            "You are using a language model to generate a client-facing market analysis report. "
            "The output is grammatically correct but overly generic and lacks industry-specific data. "
            "What is the MOST effective prompt-engineering strategy to improve the result?"
        ),
        "options": {
            "A": "Increase the temperature setting to make the output more creative.",
            "B": "Rewrite the prompt to include industry context, target audience, required metrics, and formatting constraints.",
            "C": "Ask the model to make the response longer.",
            "D": "Regenerate the same prompt multiple times until a better answer appears.",
        },
        "answer": "B",
    },
    # ── Custom GPTs ─────────────────────────────────────────────────────────
    {
        "key": "custom_gpt_basic", "domain": "Custom GPTs", "difficulty": "Medium",
        "q": "Which of the following best describes a responsible approach when deploying a Custom GPT for business use?",
        "options": {
            "A": "Fine-tune foundation weights with proprietary data permanently.",
            "B": "Increase temperature and token limits.",
            "C": "Remove persona constraints.",
            "D": "Use retrieval-augmented generation with controlled sources and oversight.",
        },
        "answer": "D",
    },
    {
        "key": "custom_gpt_advanced", "domain": "Custom GPTs", "difficulty": "Hard",
        "q": (
            "Which best reflects a technically sound and governance-aware approach when deploying "
            "a Custom GPT built with domain-specific knowledge files?"
        ),
        "options": {
            "A": "Fine-tuning the base model weights directly on proprietary data.",
            "B": "Expanding temperature and token limits to improve creativity.",
            "C": "Removing persona constraints so the model can generalize more flexibly.",
            "D": "Using RAG with controlled knowledge sources, system instructions, output constraints, and human oversight.",
        },
        "answer": "D",
    },
    # ── AI Literacy ─────────────────────────────────────────────────────────
    {
        "key": "llm_mechanism", "domain": "AI Literacy", "difficulty": "Easy",
        "q": "Which of the following statements best describes a Large Language Model (LLM)?",
        "options": {
            "A": "It generates text by analyzing and summarizing web content.",
            "B": "It generates text by predicting the next word based on prior context.",
            "C": "It translates text into multiple languages simultaneously.",
            "D": "It uses pre-defined templates to fill in blanks.",
        },
        "answer": "B",
    },
    {
        "key": "llm_distribution_shift", "domain": "AI Literacy", "difficulty": "Hard",
        "q": (
            "An AI system performs well in training evaluations but produces unreliable outputs "
            "when used in a new professional setting. Which explanation best reflects strong AI literacy?"
        ),
        "options": {
            "A": "The model encountered inputs outside its training distribution.",
            "B": "The model forgot previously learned information.",
            "C": "The model intentionally adapts its behavior to new users.",
            "D": "The model requires more creativity to function properly.",
        },
        "answer": "A",
    },
    # ── Data Privacy ────────────────────────────────────────────────────────
    {
        "key": "data_privacy_vendor", "domain": "Data Privacy", "difficulty": "Medium",
        "q": (
            "You are an operations analyst using AI to review vendor performance reports. "
            "What is the BEST privacy-protective action?"
        ),
        "options": {
            "A": "Confirm vendor data sharing is authorized first.",
            "B": "Upload reports with vendor contract details.",
            "C": "Upload reports from only selected vendors.",
            "D": "Ask the AI system to delete reports afterward.",
        },
        "answer": "A",
    },
    {
        "key": "data_privacy_reidentification", "domain": "Data Privacy", "difficulty": "Hard",
        "q": (
            "You are using AI to analyze customer support conversations. "
            "Even after removing names, what privacy risk may still exist?"
        ),
        "options": {
            "A": "The AI system may generate slower responses.",
            "B": "The AI system may identify individuals using indirect data.",
            "C": "The AI system may require additional processing time.",
            "D": "The AI system may reduce analysis accuracy.",
        },
        "answer": "B",
    },
    # ── Ethical Risk Awareness ──────────────────────────────────────────────
    {
        "key": "ethical_risk_encryption", "domain": "Ethical Risk Awareness", "difficulty": "Hard",
        "q": "Sending personal information to cloud-based generative AI tools has few privacy concerns.",
        "options": {
            "A": "True — information is encrypted during transmission using sophisticated algorithms.",
            "B": "True — generative AI tools are black-box systems and cannot reproduce personal information.",
            "C": "False — generative AI tools may use inputs for training and could reproduce personal information in future outputs.",
            "D": "False — quantum computing can easily decipher encrypted data.",
        },
        "answer": "C",
    },
    {
        "key": "ethical_risk_bias_promotion", "domain": "Ethical Risk Awareness", "difficulty": "Hard",
        "q": (
            "You are using AI to recommend employees for promotion based on performance data. "
            "What ethical risk should be evaluated FIRST?"
        ),
        "options": {
            "A": "The AI system may rely on patterns influenced by past organizational bias.",
            "B": "The AI system may require additional employee performance information.",
            "C": "The AI system may generate recommendations faster than manual review.",
            "D": "The AI system may produce promotion rankings that vary across uses.",
        },
        "answer": "A",
    },
    # ── Task Suitability ────────────────────────────────────────────────────
    {
        "key": "task_suitability_basic", "domain": "Task Suitability", "difficulty": "Easy",
        "q": "Which of these tasks would be INAPPROPRIATE for AI in a business setting?",
        "options": {
            "A": "Drafting a client specification sheet.",
            "B": "Generating initial ideas for a social media campaign brief.",
            "C": "Summarizing long internal meeting transcripts.",
            "D": "Providing full legal advice on a binding contract.",
        },
        "answer": "D",
    },
    {
        "key": "task_suitability_leadership", "domain": "Task Suitability", "difficulty": "Medium",
        "q": (
            "You are preparing a quarterly performance update for senior leadership. "
            "Which use of AI would be LEAST appropriate?"
        ),
        "options": {
            "A": "Using AI to summarize internal meeting notes before drafting the report.",
            "B": "Using AI to generate a first draft of slides that you will review and edit.",
            "C": "Using AI to analyze raw financial data and provide final investment recommendations.",
            "D": "Using AI to suggest alternative phrasing for key takeaways in the report.",
        },
        "answer": "C",
    },
    # ── Human vs. AI Boundary ───────────────────────────────────────────────
    {
        "key": "human_boundary_basic", "domain": "Human vs. AI Boundary", "difficulty": "Medium",
        "q": "Which task is most appropriate to delegate primarily to an AI system?",
        "options": {
            "A": "Delivering individualized performance feedback to an employee.",
            "B": "Generating an initial draft of a routine business report.",
            "C": "Resolving an interpersonal conflict between coworkers.",
            "D": "Making the final decision on a job candidate.",
        },
        "answer": "B",
    },
    {
        "key": "human_boundary_pm", "domain": "Human vs. AI Boundary", "difficulty": "Medium",
        "q": (
            "A project manager is deciding what to delegate to an AI assistant during a busy launch week. "
            "Which task is most appropriate to delegate?"
        ),
        "options": {
            "A": "Conducting a one-on-one performance review about long-term career goals.",
            "B": "Creating an initial summary of customer feedback from surveys and support tickets.",
            "C": "Mediating a disagreement between two team members over a missed deadline.",
            "D": "Making the final hiring decision after reviewing candidate interview notes.",
        },
        "answer": "B",
    },
    # ── Hallucination Recognition ────────────────────────────────────────────
    {
        "key": "hallucination_student", "domain": "Hallucination Recognition", "difficulty": "Medium",
        "q": (
            "As a student using an LLM to gather information for an assignment, "
            "how should you approach the information it provides?"
        ),
        "options": {
            "A": "LLM answers are always more trustworthy than internet sources — use them without verification.",
            "B": "LLM answers are generally more trustworthy, but should still be verified with reliable sources.",
            "C": "LLM answers are not necessarily more trustworthy and should be cross-checked with credible references.",
            "D": "LLM answers are less trustworthy than internet sources because they rely on outdated information.",
        },
        "answer": "C",
    },
    {
        "key": "hallucination_cutoff", "domain": "Hallucination Recognition", "difficulty": "Hard",
        "q": (
            "An AI system generates a detailed explanation of an event that occurred after its "
            "training cutoff date. What is the best interpretation?"
        ),
        "options": {
            "A": "The model has real-time news access.",
            "B": "The model stores future events.",
            "C": "The model is generating a plausible pattern-based response.",
            "D": "The model reasoned independently.",
        },
        "answer": "C",
    },
    # ── Bias Detection ──────────────────────────────────────────────────────
    {
        "key": "bias_detection_student", "domain": "Bias Detection", "difficulty": "Medium",
        "q": (
            "An AI model trained primarily on data from urban markets is used to predict consumer "
            "preferences for a rural product launch. What is the PRIMARY concern?"
        ),
        "options": {
            "A": "The model will run slower due to geographic complexity.",
            "B": "The model's predictions may reflect urban patterns and underperform for rural audiences.",
            "C": "The model will require a larger token limit to process regional differences.",
            "D": "The model may generate longer outputs than needed for the campaign.",
        },
        "answer": "B",
    },
    {
        "key": "bias_detection_demographic", "domain": "Bias Detection", "difficulty": "Hard",
        "q": (
            "The AI produces this insight: \"Customers aged 18–34 are significantly more dissatisfied "
            "than older groups, suggesting the company should deprioritize this demographic.\" "
            "How should you approach this recommendation?"
        ),
        "options": {
            "A": "Accept it after confirming online that younger consumers are generally harder to satisfy.",
            "B": "Critically evaluate whether the training data overrepresents certain groups, and consult additional sources.",
            "C": "Cross-reference with one other source; if it agrees, incorporate with confidence.",
            "D": "Flag the framing as oversimplified, but still present the AI's recommendation to your team.",
        },
        "answer": "B",
    },
]

# ── All scenarios ─────────────────────────────────────────────────────────────
ALL_SCENARIOS = [
    {
        "key": "scenario_marketing",
        "title": "Marketing Strategy Scenario",
        "domain": "Prompt Engineering · Bias Detection · Task Suitability",
        "context": (
            "You are a marketing coordinator at a mid-sized retail company. Your manager asks you "
            "to use an AI assistant to develop a targeted social media campaign for a new product "
            "launch aimed at Gen Z consumers. The campaign needs to feel authentic and culturally "
            "relevant, and the final content will be approved by your team before publishing."
        ),
        "prompt": (
            "Describe step-by-step how you would use an AI tool to help build this campaign. "
            "What specific instructions (prompts) would you give the AI? What outputs would you "
            "expect, and how would you evaluate whether the AI's suggestions are appropriate, "
            "unbiased, and suitable for your target audience before presenting them to your manager?"
        ),
    },
    {
        "key": "scenario_finance",
        "title": "Financial Analysis Scenario",
        "domain": "Task Suitability · Hallucination Recognition · Ethical Risk",
        "context": (
            "You are a junior financial analyst at an investment firm. A senior partner asks you "
            "to quickly analyze a client's financial portfolio and produce a one-page summary "
            "report by end of day. The portfolio contains sensitive client data. You are considering "
            "using an AI tool to speed up the analysis process."
        ),
        "prompt": (
            "Walk through your decision-making process. Which parts of this task would you use AI "
            "for, and which parts would you handle yourself and why? What risks or limitations "
            "would you be aware of when using AI in this context, and how would you verify the "
            "accuracy of the AI's output before presenting it to the senior partner?"
        ),
    },
]

SCORING_SYSTEM_PROMPT = """You are an academic research assistant scoring SDSU business student responses to AI aptitude scenarios.
Score the response on exactly 5 dimensions. For each, give a score 0-4 and a one-sentence justification.
Return ONLY valid JSON — no markdown, no explanation outside the JSON:

{
  "prompt_engineering": {"score": 0, "note": "..."},
  "task_suitability": {"score": 0, "note": "..."},
  "hallucination_recognition": {"score": 0, "note": "..."},
  "ethical_risk_awareness": {"score": 0, "note": "..."},
  "business_reasoning": {"score": 0, "note": "..."},
  "total": 0,
  "summary": "One sentence overall assessment."
}

Scoring rubric per dimension (0-4):
  0 = No evidence of understanding
  1 = Vague or superficial awareness
  2 = Basic understanding, lacks specificity or depth
  3 = Solid, specific, business-appropriate understanding
  4 = Sophisticated and nuanced; demonstrates professional-level AI literacy

total = sum of all five scores (max 20).
Be fair and consistent. This is for peer-reviewed academic research."""

MAX_CHAT_TURNS = 5


# ── Session state ─────────────────────────────────────────────────────────────
def init_state():
    defaults = {
        "page": 0,
        # FIX 8: Prefix session_id with "S_" so Google Sheets never misreads it as a number
        "session_id": f"S_{int(time.time())}_{random.randint(1000, 9999)}",
        "year": None,
        "major": None,
        "likert": {},
        "mc_pool": [],
        "mc_answers": {},
        "mc_e_flags": {},
        "mc_clarify_answers": {},
        "mc_start": None,
        "assigned_scenario": None,
        "scenario_phase": {},
        "scenario_start": None,
        "scenario_initial": {},
        "scenario_chat": {},
        "scenario_final": {},
        "scenario_scores": {},
        "scenario_times": {},
        "chat_input_reset": 0,
        "raffle_email": "",
        "raffle_submitted": False,
        "initial_saved": False,
        # Debug error tracking
        "last_sheet_error": None,
        "last_find_row_error": None,
        "last_update_error": None,
        "last_anthropic_error": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


init_state()

# ── TEMPORARY DEBUG PANEL — Remove before final deployment ───────────────────
with st.sidebar:
    st.markdown("### 🔧 Connection Test")
    if st.button("Test Connections"):
        # Test 1: GCP secrets
        try:
            creds_info = dict(st.secrets["gcp_service_account"])
            st.success(f"✅ GCP secrets loaded\n`{creds_info.get('client_email')}`")
        except Exception as e:
            st.error(f"❌ GCP secrets missing: {e}")

        # Test 2: Sheet key
        try:
            key = st.secrets["GSHEET_KEY"]
            st.success(f"✅ Sheet key: `{key}`")
        except Exception as e:
            st.error(f"❌ GSHEET_KEY missing: {e}")

        # Test 3: Sheet connection + test write
        try:
            ws = get_sheet()
            if ws:
                st.success(f"✅ Sheet connected! Rows: {ws.row_count}")
                try:
                    ws.append_row(["TEST_DEBUG", "delete_me"], value_input_option='RAW')
                    st.success("✅ Test write succeeded — check your sheet for a TEST_DEBUG row!")
                except Exception as e:
                    st.error(f"❌ Write failed: {e}")
            else:
                st.error("❌ get_sheet() returned None")
        except Exception as e:
            st.error(f"❌ Sheet connection failed: {e}")

        # Test 4: Anthropic key
        try:
            ak = st.secrets["ANTHROPIC_API_KEY"]
            st.success(f"✅ Anthropic key: `{ak[:12]}...`")
        except Exception as e:
            st.error(f"❌ Anthropic key missing: {e}")

    # Show any recent errors from session state
    st.markdown("---")
    st.markdown("### 🪲 Last Known Errors")
    for err_key in ["last_sheet_error", "last_find_row_error", "last_update_error", "last_anthropic_error"]:
        val = st.session_state.get(err_key)
        if val:
            st.error(f"**{err_key}:** {val}")
        else:
            st.success(f"**{err_key}:** None")

    st.markdown("---")
    st.caption(f"Session ID: `{st.session_state.session_id}`")
    st.caption(f"initial_saved: `{st.session_state.get('initial_saved')}`")
# ── END DEBUG PANEL ───────────────────────────────────────────────────────────

PAGE_NAMES = ["Welcome", "About You", "Self-Perception", "AI Knowledge Quiz", "Scenario Intro", "AI Scenario", "Complete"]
TOTAL_PAGES = len(PAGE_NAMES) - 1


def show_progress():
    pct = int((st.session_state.page / TOTAL_PAGES) * 100)
    name = PAGE_NAMES[st.session_state.page]
    st.markdown(f"""
    <div class="progress-container">
      <span class="progress-step">Step {st.session_state.page + 1} of {TOTAL_PAGES + 1}: {name}</span>
      <div class="progress-bar-bg">
        <div class="progress-bar-fill" style="width:{pct}%"></div>
      </div>
      <span class="progress-label">{pct}%</span>
    </div>
    """, unsafe_allow_html=True)


def next_page():
    st.session_state.page += 1
    st.rerun()


def prev_page():
    st.session_state.page -= 1
    st.rerun()


def wc_display(text):
    count = len(text.split()) if text.strip() else 0
    color = "#16a34a" if count >= 80 else "#ca8a04" if count >= 40 else "#dc2626"
    st.markdown(f'<p style="font-size:0.82rem;color:{color};margin-top:4px">{count} words</p>',
                unsafe_allow_html=True)
    return count


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 0 — Welcome + Consent
# ═══════════════════════════════════════════════════════════════════════════════
if st.session_state.page == 0:

    st.markdown("""
    <div class="section-header">
      <h2>FCB AI Aptitude Study</h2>
      <p>Fowler College of Business · San Diego State University</p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="survey-card">
      <h3 style="color:#A6192E;margin-top:0">Why This Research Matters</h3>
      <p>Artificial intelligence is rapidly transforming every corner of the business world —
      but are business students actually prepared for it? This study explores two questions:</p>
      <ul>
        <li>How <strong>confident</strong> do FCB students feel using AI in professional contexts?</li>
        <li>How <strong>capable</strong> are they when it comes to real business AI tasks?</li>
      </ul>
      <p>Your honest responses will directly inform how SDSU prepares students for an AI-driven workforce.</p>
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:1.2rem 0">
      <p style="font-size:0.9rem;color:#6b7280;margin:0">
        <strong>~10 minutes</strong> &nbsp;·&nbsp;
        <strong>Fully anonymous</strong> — no names collected &nbsp;·&nbsp;
        Optional entry to win a <strong>$25 BetterBuzz Gift Card</strong>
      </p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="survey-card" style="background:#f9f1f2;border-color:#f3c4cc">
      <h4 style="color:#A6192E;margin-top:0">What to Expect</h4>
      <p style="margin:0;font-size:0.92rem">
        <strong>Part 1 —</strong> Quick background questions<br>
        <strong>Part 2 —</strong> Rate your own AI confidence (Likert scale)<br>
        <strong>Part 3 —</strong> AI knowledge quiz (multiple choice)<br>
        <strong>Part 4 —</strong> One real-world scenario with live AI interaction
      </p>
    </div>
    """, unsafe_allow_html=True)

    consent = st.checkbox(
        "I understand this study is voluntary and anonymous. "
        "I consent to my responses being used for academic research in accordance with SDSU IRB guidelines.",
        key="consent_check",
    )

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        if st.button("Start the Survey", disabled=not consent):
            next_page()

    show_progress()


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 1 — About You
# ═══════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == 1:

    st.markdown("""
    <div class="section-header">
      <h2>About You</h2>
      <p>Quick background questions — takes under a minute</p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("**Are you currently enrolled as a student in the Fowler College of Business at SDSU?**")
    is_fcb = st.radio("", ["Yes — I am an FCB student", "No — I am not an FCB student"],
                      index=None, key="fcb_radio", label_visibility="collapsed")

    if is_fcb == "No — I am not an FCB student":
        st.error(
            "This survey is open to Fowler College of Business students only.  \n"
            "Thank you for your interest in our research!"
        )
        show_progress()
        st.stop()

    if is_fcb == "Yes — I am an FCB student":
        st.markdown("**What is your current academic year?**")
        year = st.radio("", ["Freshman", "Sophomore", "Junior", "Senior"],
                        index=None, key="year_radio", label_visibility="collapsed", horizontal=True)

        st.markdown("**What is your major or program?**")
        major = st.selectbox("", ["— Select your major —"] + MAJORS,
                             key="major_select", label_visibility="collapsed")

        can_continue = year is not None and major != "— Select your major —"
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            if st.button("Continue", disabled=not can_continue):
                st.session_state.year = year
                st.session_state.major = major
                if st.session_state.assigned_scenario is None:
                    st.session_state.assigned_scenario = random.choice(
                        [s["key"] for s in ALL_SCENARIOS]
                    )
                save_initial(major, year)
                next_page()

        if not can_continue:
            st.caption("Please complete both fields to continue.")

    show_progress()


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 2 — Likert Self-Perception
# ═══════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == 2:

    st.markdown("""
    <div class="section-header">
      <h2>AI Self-Perception</h2>
      <p>Rate your agreement with each statement — be honest, there are no right or wrong answers</p>
    </div>
    """, unsafe_allow_html=True)

    all_answered = True
    for i, question in enumerate(LIKERT_QUESTIONS):
        current = st.session_state.likert.get(i)
        st.markdown(f"**{i + 1}.** {question}")
        st.markdown("")
        cols = st.columns(5)
        for j, label in enumerate(LIKERT_LABELS):
            val = j + 1
            with cols[j]:
                is_selected = (current == val)
                st.markdown(
                    f'<div style="text-align:center;font-size:0.72rem;color:#6b7280;margin-bottom:4px">'
                    f'{label}</div>', unsafe_allow_html=True
                )
                if st.button(
                    "✓" if is_selected else str(val),
                    key=f"lk_{i}_{val}",
                    use_container_width=True,
                ):
                    st.session_state.likert[i] = val
                    st.rerun()
        if i not in st.session_state.likert:
            all_answered = False
        st.markdown("---")

    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if st.button("Back"):
            prev_page()
    with col3:
        if st.button("Continue", disabled=not all_answered):
            save_likert()
            next_page()

    if not all_answered:
        st.caption(f"Answered {len(st.session_state.likert)} of {len(LIKERT_QUESTIONS)} — please respond to all.")

    show_progress()


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 3 — Multiple Choice (stratified by domain)
# ═══════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == 3:

    if not st.session_state.mc_pool:
        domains = {}
        for q in MC_BANK:
            domains.setdefault(q["domain"], []).append(q)
        pool = []
        keys = list(domains.keys())
        random.shuffle(keys)
        for domain_key in keys:
            if len(pool) >= 5:
                break
            pool.append(random.choice(domains[domain_key]))
        random.shuffle(pool)
        st.session_state.mc_pool = pool
        st.session_state.mc_start = time.time()

    st.markdown("""
    <div class="section-header">
      <h2>AI Knowledge Quiz</h2>
      <p>5 questions · Select the best answer for each · No time limit</p>
    </div>
    """, unsafe_allow_html=True)

    st.caption(
        "If a question uses terms you are unfamiliar with, select "
        "'E. I\'m not sure what\'s being asked' — a simpler version will appear below it (where available)."
    )
    st.markdown("")

    for i, q in enumerate(st.session_state.mc_pool):
        qkey = q["key"]
        has_simplified = qkey in SIMPLIFIED_QUESTIONS
        e_was_clicked = st.session_state.mc_e_flags.get(qkey, 0) == 1

        options = [f"{k}. {v}" for k, v in q["options"].items()]
        clarify_label = "E. I'm not sure what's being asked."
        options_with_e = options + [clarify_label]

        st.markdown(f"**Q{i + 1}.** {q['q']}")

        current_k = st.session_state.mc_answers.get(qkey)
        if e_was_clicked and not current_k:
            current_display = clarify_label
        elif current_k and current_k in q["options"]:
            current_display = f"{current_k}. {q['options'][current_k]}"
        else:
            current_display = None

        idx_val = options_with_e.index(current_display) if current_display in options_with_e else None

        chosen = st.radio(
            label="", options=options_with_e, index=idx_val,
            key=f"mc_{qkey}", label_visibility="collapsed",
        )

        if chosen:
            if chosen == clarify_label:
                st.session_state.mc_e_flags[qkey] = 1
                st.session_state.mc_answers.pop(qkey, None)
            else:
                st.session_state.mc_answers[qkey] = chosen[0]
                if not e_was_clicked:
                    st.session_state.mc_e_flags[qkey] = 0

        if st.session_state.mc_e_flags.get(qkey, 0) == 1 and has_simplified:
            sq = SIMPLIFIED_QUESTIONS[qkey]
            st.markdown(
                '<div class="simplified-card">'
                '<p style="font-size:0.82rem;font-weight:700;color:#92400e;margin:0 0 8px">'
                'Here is a simpler version of the same question:</p></div>',
                unsafe_allow_html=True,
            )
            st.markdown(f"*{sq['q']}*")
            s_opts = [f"{k}. {v}" for k, v in sq["options"].items()]
            cur_s = st.session_state.mc_clarify_answers.get(qkey)
            cur_s_d = f"{cur_s}. {sq['options'][cur_s]}" if cur_s and cur_s in sq["options"] else None
            s_idx = s_opts.index(cur_s_d) if cur_s_d in s_opts else None
            s_ch = st.radio(
                label="", options=s_opts, index=s_idx,
                key=f"mc_simplified_{qkey}", label_visibility="collapsed",
            )
            if s_ch:
                st.session_state.mc_clarify_answers[qkey] = s_ch[0]

        elif st.session_state.mc_e_flags.get(qkey, 0) == 1 and not has_simplified:
            st.warning(
                "No simplified version is available for this question. "
                "Please select your best guess from options A–D above."
            )

        st.markdown("---")

    def mc_answered(qkey):
        e_flag = st.session_state.mc_e_flags.get(qkey, 0)
        abcd = st.session_state.mc_answers.get(qkey)
        if not e_flag:
            return abcd in ["A", "B", "C", "D"]
        if qkey in SIMPLIFIED_QUESTIONS:
            return qkey in st.session_state.mc_clarify_answers
        else:
            return abcd in ["A", "B", "C", "D"]

    all_mc = all(mc_answered(q["key"]) for q in st.session_state.mc_pool)
    answered_mc = sum(1 for q in st.session_state.mc_pool if mc_answered(q["key"]))

    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if st.button("Back"):
            prev_page()
    with col3:
        if st.button("Continue", disabled=not all_mc):
            save_mc()
            next_page()

    if not all_mc:
        st.caption(f"Answered {answered_mc} of {len(st.session_state.mc_pool)} — please answer all questions.")

    show_progress()


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 4 — Scenario Section Breaker
# ═══════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == 4:

    st.markdown("""
    <div class="section-header">
      <h2>Part 4: Real-World AI Scenario</h2>
      <p>Apply your thinking to a realistic business situation</p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="survey-card">
      <h3 style="color:#A6192E;margin-top:0">What You'll Be Doing</h3>
      <p>The final section presents one business scenario. You'll work through three short phases:</p>
      <p>
        <span style="background:#A6192E;color:white;border-radius:50%;padding:2px 9px;font-weight:700;margin-right:8px">1</span>
        <strong>Write your initial response</strong> — Read the scenario and share your thinking on your own first. No AI involved yet.
      </p>
      <p>
        <span style="background:#A6192E;color:white;border-radius:50%;padding:2px 9px;font-weight:700;margin-right:8px">2</span>
        <strong>Discuss with an AI assistant</strong> — An AI will ask Socratic questions to help you pressure-test your ideas. This step is <em>optional</em>.
      </p>
      <p>
        <span style="background:#A6192E;color:white;border-radius:50%;padding:2px 9px;font-weight:700;margin-right:8px">3</span>
        <strong>Write your final response</strong> — Revise or expand your answer based on your reflection. This is what gets scored.
      </p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="survey-card" style="background:#f9f1f2;border-color:#f3c4cc">
      <h4 style="color:#A6192E;margin-top:0">A Few Things to Know</h4>
      <ul style="margin:0;padding-left:1.2rem;font-size:0.92rem;color:#374151;line-height:1.8">
        <li>There are <strong>no trick questions</strong> — we're interested in your reasoning process, not a single correct answer.</li>
        <li>Responses are scored on <strong>depth and business thinking</strong>, not length.</li>
        <li>The AI chat is a <strong>thinking tool</strong>, not a grader — it won't write your answer for you.</li>
      </ul>
    </div>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if st.button("← Back"):
            prev_page()
    with col3:
        if st.button("Begin Scenario →"):
            next_page()

    show_progress()


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 5 — Interactive AI Scenario
# ═══════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == 5:

    assigned_key = st.session_state.assigned_scenario
    if assigned_key is None:
        assigned_key = random.choice([s["key"] for s in ALL_SCENARIOS])
        st.session_state.assigned_scenario = assigned_key

    scenario = next(s for s in ALL_SCENARIOS if s["key"] == assigned_key)
    key = scenario["key"]

    if key not in st.session_state.scenario_phase:
        st.session_state.scenario_phase[key] = "initial"
        st.session_state.scenario_chat[key] = []
        st.session_state.scenario_start = time.time()

    phase = st.session_state.scenario_phase[key]

    phase_labels = {
        "initial": "Step 1 of 3 — Initial Response",
        "chat":    "Step 2 of 3 — AI Discussion",
        "final":   "Step 3 of 3 — Final Response",
    }

    st.markdown(f"""
    <div class="section-header">
      <h2>{scenario['title']}</h2>
      <p>{scenario['domain']}</p>
    </div>
    <div style="text-align:center;margin-bottom:1.4rem">
      <span class="phase-badge">{phase_labels[phase]}</span>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("#### Scenario")
    st.info(scenario["context"])
    st.markdown("#### Your Task")
    st.markdown(scenario["prompt"])
    st.markdown("---")

    if phase == "initial":
        st.markdown("### ✏️ Write Your Initial Response")
        st.caption(
            "Think through this on your own first — no AI help yet. "
            "In the next step you'll be able to discuss your thinking with an AI assistant."
        )

        initial_text = st.text_area(
            label="",
            value=st.session_state.scenario_initial.get(key, ""),
            height=230,
            placeholder="Type your initial response here. Aim for at least 80 words...",
            key=f"initial_{key}",
            label_visibility="collapsed",
        )
        wc = wc_display(initial_text)
        can_advance = wc >= 20

        col1, col2, col3 = st.columns([1, 2, 1])
        with col1:
            if st.button("← Back"):
                prev_page()
                st.rerun()
        with col3:
            if st.button("Lock In & Open AI Chat →", disabled=not can_advance):
                st.session_state.scenario_initial[key] = initial_text
                st.session_state.scenario_phase[key] = "chat"
                st.rerun()

        if not can_advance:
            st.caption("Please write at least a few sentences before continuing.")

    elif phase == "chat":
        st.markdown("### 💬 Discuss with the AI Assistant")

        chat_hist = st.session_state.scenario_chat[key]
        user_turns = len([m for m in chat_hist if m["role"] == "user"])
        turns_left = MAX_CHAT_TURNS - user_turns

        with st.expander("📝 Your initial response", expanded=False):
            st.write(st.session_state.scenario_initial[key])

        st.caption(
            "Use this AI to pressure-test your thinking — ask it questions, push back on ideas, "
            f"or explore angles you missed. **{turns_left} message{'s' if turns_left != 1 else ''} remaining.** "
            "This step is optional; click *Write Final Response* whenever you're ready."
        )

        if not chat_hist:
            st.markdown(
                '<p style="color:#9ca3af;font-style:italic;font-size:0.88rem;margin:1rem 0">'
                'Start a conversation below, or skip straight to your final response.</p>',
                unsafe_allow_html=True,
            )
        for msg in chat_hist:
            if msg["role"] == "user":
                st.markdown(
                    f'<div class="chat-msg-user"><strong>You:</strong> {msg["content"]}</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f'<div class="chat-msg-ai"><strong>AI:</strong> {msg["content"]}</div>',
                    unsafe_allow_html=True,
                )

        if turns_left > 0:
            user_input = st.text_input(
                "Your message",
                key=f"chat_in_{key}_{st.session_state.chat_input_reset}",
                placeholder="Ask a follow-up, challenge an assumption, or explore a different angle...",
                label_visibility="collapsed",
            )
            col_send, col_skip = st.columns([1, 1])
            with col_send:
                if st.button("Send →", disabled=not user_input.strip()):
                    with st.spinner("AI is thinking..."):
                        reply = chat_with_claude(key, user_input.strip())
                    st.session_state.scenario_chat[key].append({"role": "user", "content": user_input.strip()})
                    st.session_state.scenario_chat[key].append({"role": "assistant", "content": reply})
                    st.session_state.chat_input_reset += 1
                    st.rerun()
            with col_skip:
                if st.button("Write Final Response →"):
                    st.session_state.scenario_phase[key] = "final"
                    st.rerun()
        else:
            st.info("You've used all your messages for this scenario.")
            if st.button("Write Final Response →"):
                st.session_state.scenario_phase[key] = "final"
                st.rerun()

    elif phase == "final":
        st.markdown("### ✅ Write Your Final Response")
        st.caption(
            "Based on your initial thinking and any AI discussion, write your best final answer. "
            "You can revise, expand, or keep what you had."
        )

        with st.expander("📋 Review your initial response & AI conversation", expanded=False):
            st.markdown("**Your initial response:**")
            st.write(st.session_state.scenario_initial.get(key, ""))
            chat_hist = st.session_state.scenario_chat.get(key, [])
            if chat_hist:
                st.markdown("**AI conversation:**")
                for msg in chat_hist:
                    prefix = "You" if msg["role"] == "user" else "AI"
                    st.markdown(f"**{prefix}:** {msg['content']}")

        default_final = st.session_state.scenario_final.get(
            key, st.session_state.scenario_initial.get(key, "")
        )
        final_text = st.text_area(
            label="",
            value=default_final,
            height=260,
            placeholder="Write your final response here...",
            key=f"final_{key}",
            label_visibility="collapsed",
        )
        wc = wc_display(final_text)
        can_submit = wc >= 20

        col1, col2, col3 = st.columns([1, 2, 1])
        with col1:
            if st.button("← Back to Chat"):
                st.session_state.scenario_phase[key] = "chat"
                st.rerun()
        with col3:
            if st.button("Submit Survey →", disabled=not can_submit):
                elapsed = round(time.time() - (st.session_state.scenario_start or time.time()), 1)
                st.session_state.scenario_final[key] = final_text
                st.session_state.scenario_times[key] = elapsed

                with st.spinner("Scoring your response..."):
                    scores = score_scenario(key, final_text)
                    st.session_state.scenario_scores[key] = scores

                with st.spinner("Saving your response..."):
                    save_scenario(key, is_last=True)

                next_page()

        if not can_submit:
            st.caption("Please write at least a few sentences to submit.")

    show_progress()


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 6 — Completion + Raffle + Admin Export
# ═══════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == 6:

    st.markdown("""
    <div class="section-header">
      <h2>You're Done!</h2>
      <p>Thank you for contributing to FCB AI research at SDSU</p>
    </div>
    """, unsafe_allow_html=True)

    st.success(
        "Your responses have been recorded successfully. "
        "Your participation helps shape how SDSU prepares students for an AI-driven workforce."
    )

    st.markdown('<div class="raffle-box">', unsafe_allow_html=True)
    st.markdown("### 🎉 Enter to Win a $25 BetterBuzz Gift Card!")
    st.markdown(
        "As a thank-you, we're raffling **BetterBuzz Gift Cards** among all completers. "
        "Enter your SDSU email below — stored separately from your responses, used only for the raffle draw."
    )

    if not st.session_state.raffle_submitted:
        email_input = st.text_input(
            "SDSU Email (optional — leave blank to skip)",
            placeholder="yourname@sdsu.edu",
            key="raffle_email_input",
        )
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            if st.button("Submit Email"):
                if email_input.strip() and "@" in email_input:
                    update_raffle_email(email_input.strip())
                    st.session_state.raffle_email = email_input.strip()
                    st.session_state.raffle_submitted = True
                    st.rerun()
                elif not email_input.strip():
                    st.session_state.raffle_submitted = True
                    st.rerun()
                else:
                    st.warning("Please enter a valid email address, or leave blank to skip.")
    else:
        if st.session_state.raffle_email:
            st.success("You're entered! We'll reach out to winners after data collection closes.")
        else:
            st.info("Raffle entry skipped. Thanks again for participating!")

    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("---")
    st.markdown(
        "<p style='text-align:center;color:#6b7280;font-size:0.88rem'>"
        "Questions about this research? Contact the FCB Research Team at SDSU.<br>"
        "This study is conducted in accordance with SDSU IRB guidelines."
        "</p>",
        unsafe_allow_html=True,
    )

    with st.expander("🔒 Researcher Data Export"):
        try:
            correct_pwd = st.secrets["RESEARCHER_PASSWORD"]
        except Exception:
            correct_pwd = ""

        pwd = st.text_input("Research team password", type="password", key="admin_pwd")

        if pwd and pwd == correct_pwd:
            st.success("Access granted.")
            ws = get_sheet()
            if ws:
                try:
                    records = ws.get_all_records()
                    df = pd.DataFrame(records)
                    st.markdown(f"**Total responses:** {len(df)}")
                    st.dataframe(df, use_container_width=True)
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                        df.to_excel(writer, index=False, sheet_name="Survey Responses")
                    st.download_button(
                        label="⬇️ Download All Responses as Excel",
                        data=buf.getvalue(),
                        file_name=f"fcb_ai_survey_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                except Exception as e:
                    st.error(f"Could not load data: {e}")
        elif pwd:
            st.error("Incorrect password.")

    show_progress()
