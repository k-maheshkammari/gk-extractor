import os
import re
import json
import io
import time
from datetime import datetime
from difflib import SequenceMatcher
import streamlit as st
import pandas as pd
from dotenv import load_dotenv
from google import genai
from google.genai import types
from supabase import create_client, Client
from pypdf import PdfReader, PdfWriter

load_dotenv()

# 1. API Keys Initialization (1 to 15 Keys Support)
gemini_keys_raw = []
for i in range(1, 16):
    val = os.getenv(f"GEMINI_API_KEY_{i}")
    if val and val not in gemini_keys_raw:
        gemini_keys_raw.append(val)

if os.getenv("GEMINI_API_KEY") and os.getenv("GEMINI_API_KEY") not in gemini_keys_raw:
    gemini_keys_raw.append(os.getenv("GEMINI_API_KEY"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        st.error(f"Supabase Connection Error: {e}")

DAILY_LIMIT_PER_KEY = 20

if "key_status" not in st.session_state:
    st.session_state["key_status"] = {
        i: {"exhausted": False, "calls_made": 0, "last_tested": None} 
        for i in range(len(gemini_keys_raw))
    }

if "round_robin_idx" not in st.session_state:
    st.session_state["round_robin_idx"] = 0

def get_key_display(idx):
    k = gemini_keys_raw[idx]
    return f"Key {idx + 1} (...{k[-6:]})"

def test_single_key(idx):
    k = gemini_keys_raw[idx]
    try:
        client = genai.Client(api_key=k)
        client.models.generate_content(model="gemini-3.6-flash", contents="OK")
        st.session_state["key_status"][idx]["exhausted"] = False
        return True
    except Exception as err:
        err_msg = str(err)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
            st.session_state["key_status"][idx]["exhausted"] = True
            st.session_state["key_status"][idx]["calls_made"] = DAILY_LIMIT_PER_KEY
        return False

def get_next_available_client():
    active_indices = [
        i for i in range(len(gemini_keys_raw)) 
        if not st.session_state["key_status"][i]["exhausted"]
    ]
    if not active_indices:
        return None, None, None
    
    current_idx = active_indices[st.session_state["round_robin_idx"] % len(active_indices)]
    st.session_state["round_robin_idx"] += 1
    selected_key = gemini_keys_raw[current_idx]
    
    return genai.Client(api_key=selected_key), current_idx, get_key_display(current_idx)

# 2. UI Layout
st.set_page_config(page_title="Smart GK Extractor & Live Monitor", layout="wide")
st.title("📚 Smart GK Extractor & Auto-Translator (En | Te | Hi)")

# Sidebar Monitor
st.sidebar.header("📊 Live Quota & Capacity Monitor")
active_keys_count = sum(1 for v in st.session_state["key_status"].values() if not v["exhausted"])
total_keys_count = len(gemini_keys_raw)

if st.sidebar.button("🔄 Check & Refresh All Keys", use_container_width=True):
    with st.sidebar.status("Testing all keys..."):
        for i in range(total_keys_count):
            test_single_key(i)
    st.rerun()

st.sidebar.markdown(f"**🟢 Active Keys:** `{active_keys_count} / {total_keys_count}`")

chunk_size = 2

total_calls_left = 0
for i in range(total_keys_count):
    if not st.session_state["key_status"][i]["exhausted"]:
        calls_used = st.session_state["key_status"][i]["calls_made"]
        total_calls_left += max(0, DAILY_LIMIT_PER_KEY - calls_used)

st.sidebar.info(f"⚡ **Estimated Calls Left:** ~`{total_calls_left}`")

with st.sidebar.expander("🔑 Individual Key Status", expanded=False):
    for i in range(total_keys_count):
        status_info = st.session_state["key_status"][i]
        key_name = get_key_display(i)
        if status_info["exhausted"]:
            st.markdown(f"❌ **{key_name}**: `QUOTA EXHAUSTED`")
        else:
            calls_left = DAILY_LIMIT_PER_KEY - status_info["calls_made"]
            st.markdown(f"✅ **{key_name}**: `{max(0, calls_left)}/{DAILY_LIMIT_PER_KEY} left`")

st.sidebar.divider()

# 3. Filename & Title Metadata Extractor
def parse_metadata_from_name(name_str):
    name = (name_str or "").lower()
    exam = "General Exam"
    if "ssc" in name:
        exam = "SSC CGL" if "cgl" in name else "SSC"
    elif "tspsc" in name:
        exam = "TSPSC Group 1" if "group-1" in name or "group 1" in name else "TSPSC"
    elif "uppsc" in name:
        exam = "UPPSC"
    elif "upsc" in name:
        exam = "UPSC"
    elif "appsc" in name:
        exam = "APPSC"
    elif "rrb" in name or "railway" in name:
        exam = "RRB NTPC"
    
    state = "Central"
    if "tspsc" in name or "telangana" in name:
        state = "Telangana"
    elif "uppsc" in name or "up" in name:
        state = "Uttar Pradesh"
    elif "appsc" in name or "ap" in name:
        state = "Andhra Pradesh"

    date_match = re.search(r'(\d{4}[-_]\d{2}[-_]\d{2})|(\d{1,2}(?:st|nd|rd|th)?[-_][a-z]{3}[-_]\d{4})', name)
    date = date_match.group(0).replace("_", "-") if date_match else ""

    shift_match = re.search(r'shift[-_\s]?\d+', name)
    shift = shift_match.group(0).replace("-", " ").title() if shift_match else ""

    return exam, state, date, shift

# 4. Universal Smart Deduplication Engine
BOILERPLATE_PATTERNS = [
    r'read\s+(the\s+)?(below|following)\s+statements?',
    r'mark\s+the\s+following\s+statements?',
    r'which\s+of\s+the\s+following(\s+statements?)?(\s+regarding[a-z\s]+)?(\s+is|\s+are|\s+correctly)?\s*(correct|true|false|reflects|evolution)?',
    r'assertion\s*\([a-z]\):?',
    r'reason\s*\([a-z]\):?',
    r'mark\s+the\s+correct\s+option[s]?',
    r'choose\s+the\s+correct\s+(option|answer|code)',
    r'consider\s+the\s+following\s+(statements?|pairs?)?',
    r'select\s+the\s+correct\s+answer',
    r'match\s+list\s*[-–]?\s*[i|1]\s+with\s+list\s*[-–]?\s*[i|2]',
    r'క్రింది\s*(వాక్యాలను|ప్రకటనలను)\s*పరిశీలించండి',
    r'సరైన\s*(సమాధానాన్ని|ఎంపికను)\s*ఎంచుకోండి'
]

GENERIC_OPTION_WORDS = {
    'both', 'and', 'true', 'false', 'correct', 'explanation', 'not', 'neither',
    'only', 'one', 'two', 'three', 'four', 'statement', 'statements', 'option', 'is', 'are', 'reason', 'assertion'
}

def clean_boilerplate_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.lower()
    for pattern in BOILERPLATE_PATTERNS:
        cleaned = re.sub(pattern, ' ', cleaned, flags=re.IGNORECASE)
    return cleaned

def extract_core_entity_tokens(text: str) -> set:
    if not text:
        return set()
    cleaned = clean_boilerplate_text(text)
    words = re.findall(r'[a-zA-Z0-9\u0C00-\u0C7F]{3,}', cleaned)
    return {w for w in words if w not in GENERIC_OPTION_WORDS}

def extract_substantive_option_tokens(options_dict: dict) -> set:
    if not isinstance(options_dict, dict):
        return set()
    en_opts = options_dict.get("en", options_dict)
    if not isinstance(en_opts, dict):
        return set()
    combined_opts = " ".join(str(v) for v in en_opts.values()).lower()
    words = re.findall(r'[a-zA-Z0-9]{3,}', combined_opts)
    return {w for w in words if w not in GENERIC_OPTION_WORDS}

def are_same_question(q1_item, q2_item) -> bool:
    opt_tokens1 = extract_substantive_option_tokens(q1_item.get("options", {}))
    opt_tokens2 = extract_substantive_option_tokens(q2_item.get("options", {}))
    
    if len(opt_tokens1) >= 3 and len(opt_tokens2) >= 3:
        opt_inter = len(opt_tokens1 & opt_tokens2)
        opt_union = len(opt_tokens1 | opt_tokens2)
        if (opt_inter / opt_union) >= 0.70:
            return True

    q1_en = q1_item.get("question", {}).get("en", "")
    q2_en = q2_item.get("question", {}).get("en", "")
    t1 = extract_core_entity_tokens(q1_en)
    t2 = extract_core_entity_tokens(q2_en)
    
    if t1 and t2:
        intersection = len(t1 & t2)
        union = len(t1 | t2)
        if union > 0 and (intersection / union) >= 0.65 and intersection >= 2:
            return True
            
        c1 = re.sub(r'\s+', ' ', clean_boilerplate_text(q1_en)).strip()
        c2 = re.sub(r'\s+', ' ', clean_boilerplate_text(q2_en)).strip()
        if len(c1) > 20 and len(c2) > 20:
            if SequenceMatcher(None, c1, c2).ratio() >= 0.78:
                return True

    q1_te = q1_item.get("question", {}).get("te", "")
    q2_te = q2_item.get("question", {}).get("te", "")
    if q1_te and q2_te:
        t_te1 = extract_core_entity_tokens(q1_te)
        t_te2 = extract_core_entity_tokens(q2_te)
        if t_te1 and t_te2:
            inter_te = len(t_te1 & t_te2)
            union_te = len(t_te1 | t_te2)
            if union_te > 0 and (inter_te / union_te) >= 0.65 and inter_te >= 2:
                return True
                
    return False

def get_question_completeness_score(item) -> int:
    q_len = len(item.get("question", {}).get("en", ""))
    exp_len = len(item.get("explanation", {}).get("en", ""))
    opts_count = len(item.get("options", {}).get("en", {}))
    return q_len + exp_len + (opts_count * 50)

def deduplicate_and_merge_questions(question_list):
    unique_questions = []
    for new_item in question_list:
        matched_idx = -1
        for idx, existing_item in enumerate(unique_questions):
            if are_same_question(new_item, existing_item):
                matched_idx = idx
                break
        
        if matched_idx == -1:
            unique_questions.append(new_item)
        else:
            if get_question_completeness_score(new_item) > get_question_completeness_score(unique_questions[matched_idx]):
                unique_questions[matched_idx] = new_item
                
    return unique_questions

def clean_json_response(raw_text):
    text = raw_text.strip()
    match = re.search(r'\[\s*\{.*\}\s*\]', text, re.DOTALL)
    if match:
        return match.group(0)
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'^```\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()

# 5. High-Recall Multi-Language System Prompt
system_instruction = """
You are an expert Indian competitive exam analyzer and multilingual parser (UPSC, SSC CGL, TSPSC, APPSC, RRB).

CORE EXTRACTION RULES:
1. STRICTLY IGNORE:
   - Pure Math / Quantitative Aptitude (Algebra, Geometry, Trigonometry, Arithmetic word problems).
   - Pure Non-Verbal & Logical Reasoning puzzles (Number/Letter Series, Blood Relations, Coding-Decoding, Syllogisms).
   - Pure English Grammar & Comprehension passages.

2. EXTRACT 100% OF ALL GENERAL KNOWLEDGE / GENERAL STUDIES (GS) QUESTIONS:
   - Indian Polity & Constitution, History & National Movement, Geography, Economy, Science & Tech, Environment, Current Affairs.
   - ALL Formats MUST be extracted: Assertion & Reason (A&R), Match List-I & II, Statements (1 only, 2 only, Both, Neither), Chronology.
   - CRITICAL: If a question started previously and finishes later, RECONSTRUCT IT COMPLETELY with all options and assertions.

LANGUAGE RULES:
- VERBATIM EXTRACTION: If a language (English, Telugu, or Hindi) is already present, extract it 100% word-for-word exactly as printed. Do NOT re-phrase or re-write.
- ACADEMIC TRANSLATION: For missing languages, provide natural academic translations (Telugu Academy style / NCERT standard).
- All questions MUST contain all 3 language keys ("en", "te", "hi") for question, options, and explanation.

OUTPUT FORMAT: Strict valid JSON array of objects without markdown backticks.
[
  {
    "question": {"en": "...", "te": "...", "hi": "..."},
    "options": {
      "en": {"A": "...", "B": "...", "C": "...", "D": "..."},
      "te": {"A": "...", "B": "...", "C": "...", "D": "..."},
      "hi": {"A": "...", "B": "...", "C": "...", "D": "..."}
    },
    "correct_answer": "A",
    "meta_tags": ["Broad Subject", "Main Topic", "Sub-Topic", "Specific Entity"],
    "explanation": {"en": "...", "te": "...", "hi": "..."}
  }
]
"""

# 6. Fault-Tolerant AI Engine (gemini-3.6-flash)
def generate_with_smart_fallback(file_part, prompt_text, live_status_box, max_retries=12):
    for attempt in range(max_retries):
        current_client, key_idx, key_name = get_next_available_client()
        if not current_client:
            raise Exception("అన్ని API కీలలో ఈ రోజుకి డైలీ కోటా పూర్తయింది.")
        
        live_status_box.markdown(f"🔑 **Currently Running:** `{key_name}` | 🔄 **Attempt:** `{attempt + 1}`")
        
        try:
            response = current_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=[file_part, prompt_text],
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    temperature=0.0
                )
            )
            st.session_state["key_status"][key_idx]["calls_made"] += 1
            return response.text.strip(), key_name
            
        except Exception as err:
            err_msg = str(err)
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                st.session_state["key_status"][key_idx]["exhausted"] = True
                st.session_state["key_status"][key_idx]["calls_made"] = DAILY_LIMIT_PER_KEY
                st.warning(f"⚠️ `{key_name}` కోటా తాకింది! వేరే కీకి స్విచ్ అవుతున్నాం...")
                time.sleep(1)
            elif "503" in err_msg or "UNAVAILABLE" in err_msg or "500" in err_msg or "504" in err_msg:
                wait_time = min(3 * (attempt + 1), 10)
                st.warning(f"⏳ గూగుల్ సర్వర్ బిజీగా ఉంది (503). {wait_time} సెకన్లు ఆగి మళ్లీ ప్రయత్నిస్తున్నాం...")
                time.sleep(wait_time)
            else:
                st.warning(f"⚠️ నెట్‌వర్క్ రీట్రై: {err_msg[:80]}...")
                time.sleep(2)
                
    raise Exception("గరిష్ట రీట్రైల తర్వాత కూడా సర్వర్ రెస్పాన్స్ ఇవ్వలేదు.")

# 7. Dual Input Interface (Upload vs Direct Paste)
tab_upload, tab_paste = st.tabs(["📄 Upload File (PDF / TXT)", "📋 Direct Paste Text (Ultra-Fast)"])

detected_meta = {"exam": "", "state": "", "date": "", "shift": ""}

with tab_upload:
    uploaded_file = st.file_uploader("Upload Question Paper (PDF or TXT)", type=["pdf", "txt"])
    if uploaded_file:
        e, s, d, sh = parse_metadata_from_name(uploaded_file.name)
        detected_meta = {"exam": e, "state": s, "date": d, "shift": sh}

with tab_paste:
    st.caption("⚡ PDF నుండి కాపీ చేసిన లేదా AI తో కన్వర్ట్ చేసిన టెక్స్ట్‌ను నేరుగా ఇక్కడ పేస్ట్ చేయండి.")
    paper_title = st.text_input("Paper Name / Reference (Optional)", placeholder="e.g. SSC_CGL_2024_09_12_Shift1")
    pasted_text_input = st.text_area("Paste Question Paper Text Here", height=240, placeholder="Paste questions text here (Ctrl + V)...")
    if paper_title:
        e, s, d, sh = parse_metadata_from_name(paper_title)
        detected_meta = {"exam": e, "state": s, "date": d, "shift": sh}

# Sidebar Metadata Inputs (Auto-filled & Editable)
st.sidebar.header("📝 Exam Metadata")
manual_exam_name = st.sidebar.text_input("Exam Name", value=detected_meta["exam"])
manual_state = st.sidebar.text_input("State", value=detected_meta["state"])
manual_date = st.sidebar.text_input("Date", value=detected_meta["date"])
manual_shift = st.sidebar.text_input("Shift", value=detected_meta["shift"])

# 8. Execution Pipeline
st.markdown("<br>", unsafe_allow_html=True)
btn_col1, btn_col2 = st.columns([3, 7])

with btn_col1:
    start_process = st.button("🚀 Smart Extract & Process (En, Te, Hi)", type="primary", use_container_width=True)

if start_process:
    if active_keys_count == 0:
        st.error("అన్ని API కీలు కోటా తాకాయి. దయచేసి కాసేపటి తర్వాత ప్రయత్నించండి.")
    else:
        raw_extracted_questions = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        live_status_box = st.empty()

        try:
            # SCENARIO A: Direct Pasted Text Execution
            if pasted_text_input and pasted_text_input.strip():
                clean_raw_text = pasted_text_input.strip()
                status_text.markdown("⏳ **Processing Pasted Text with AI...**")
                
                # Split large text into ~4500 character safe chunks to protect output token limit
                chunk_len = 4500
                text_slices = [clean_raw_text[i:i+chunk_len] for i in range(0, len(clean_raw_text), chunk_len)]
                total_slices = len(text_slices)

                for s_idx, t_chunk in enumerate(text_slices):
                    status_text.markdown(f"⏳ **Processing Text Batch {s_idx + 1} of {total_slices}...**")
                    file_part = types.Part.from_bytes(data=t_chunk.encode("utf-8"), mime_type="text/plain")
                    raw_resp, used_key_name = generate_with_smart_fallback(
                        file_part=file_part,
                        prompt_text="Extract all GK questions following verbatim and academic translation rules.",
                        live_status_box=live_status_box
                    )
                    clean_json_str = clean_json_response(raw_resp)
                    if clean_json_str:
                        try:
                            parsed_q = json.loads(clean_json_str)
                            if isinstance(parsed_q, list):
                                raw_extracted_questions.extend(parsed_q)
                        except Exception as parse_err:
                            st.warning(f"JSON Parse Warning in Batch {s_idx + 1}: {parse_err}")

                    progress_bar.progress((s_idx + 1) / total_slices)
                    time.sleep(0.4)

            # SCENARIO B: Uploaded File Execution (PDF / TXT)
            elif uploaded_file is not None:
                if uploaded_file.name.endswith(".pdf"):
                    pdf_bytes = uploaded_file.read()
                    pdf_reader = PdfReader(io.BytesIO(pdf_bytes))
                    total_pages = len(pdf_reader.pages)

                    page_slices = []
                    curr = 0
                    while curr < total_pages:
                        nxt = min(curr + 2, total_pages)
                        page_slices.append((curr, nxt))
                        if nxt == total_pages:
                            break
                        curr += 1
                        
                    total_chunks = len(page_slices)

                    for chunk_idx, (slice_start, slice_end) in enumerate(page_slices):
                        status_text.markdown(f"⏳ **Processing Pages {slice_start + 1} to {slice_end} of {total_pages}** (Batch {chunk_idx + 1}/{total_chunks})...")

                        writer = PdfWriter()
                        for p in range(slice_start, slice_end):
                            if p < len(pdf_reader.pages):
                                writer.add_page(pdf_reader.pages[p])
                        
                        chunk_stream = io.BytesIO()
                        writer.write(chunk_stream)
                        chunk_bytes = chunk_stream.getvalue()

                        file_part = types.Part.from_bytes(data=chunk_bytes, mime_type="application/pdf")

                        raw_resp, used_key_name = generate_with_smart_fallback(
                            file_part=file_part,
                            prompt_text="Extract all GK questions following verbatim, boundary reconstruction, and translation rules.",
                            live_status_box=live_status_box
                        )

                        clean_json_str = clean_json_response(raw_resp)
                        if clean_json_str:
                            try:
                                chunk_questions = json.loads(clean_json_str)
                                if isinstance(chunk_questions, list):
                                    raw_extracted_questions.extend(chunk_questions)
                            except Exception as parse_err:
                                st.warning(f"JSON Parse Warning in Batch {chunk_idx + 1}: {parse_err}")

                        progress_bar.progress((chunk_idx + 1) / total_chunks)
                        time.sleep(0.5)

                else:
                    text_content = uploaded_file.read().decode("utf-8")
                    file_part = types.Part.from_bytes(data=text_content.encode("utf-8"), mime_type="text/plain")
                    raw_resp, used_key_name = generate_with_smart_fallback(
                        file_part=file_part,
                        prompt_text="Extract all GK questions following verbatim and translation rules.",
                        live_status_box=live_status_box
                    )
                    clean_json_str = clean_json_response(raw_resp)
                    raw_extracted_questions = json.loads(clean_json_str)
                    progress_bar.progress(1.0)
            else:
                st.warning("⚠️ దయచేసి ఒక PDF/TXT ఫైల్‌ను అప్‌లోడ్ చేయండి లేదా బాక్స్‌లో టెక్స్ట్‌ను పేస్ట్ చేయండి.")

            if raw_extracted_questions:
                final_unique_questions = deduplicate_and_merge_questions(raw_extracted_questions)
                st.session_state["extracted_questions"] = final_unique_questions
                status_text.empty()
                live_status_box.empty()
                st.success(f"🎉 100% Extraction Successful! Total Unique GK Questions Extracted: {len(final_unique_questions)}")

        except Exception as e:
            st.error(f"Error during AI processing: {e}")

# 9. Multilingual Preview & Supabase Save
if "extracted_questions" in st.session_state and st.session_state["extracted_questions"]:
    questions_data = st.session_state["extracted_questions"]

    st.subheader(f"📊 Extracted Questions ({len(questions_data)} GK Questions Total)")

    tab_preview, tab_raw = st.tabs(["📋 Multilingual Preview (Tab View)", "📄 JSON Output"])

    with tab_preview:
        for idx, q in enumerate(questions_data):
            q_en = q.get("question", {}).get("en", "")
            with st.expander(f"Q{idx+1}: {q_en[:90]}..."):
                lang_tab1, lang_tab2, lang_tab3 = st.tabs(["🇬🇧 English", "🇮🇳 Telugu (తెలుగు)", "🇮🇳 Hindi (हिंदी)"])
                
                with lang_tab1:
                    st.markdown(f"**Question:** {q.get('question', {}).get('en')}")
                    st.write("**Options:**", q.get("options", {}).get("en"))
                    st.info(f"**Explanation:** {q.get('explanation', {}).get('en')}")

                with lang_tab2:
                    st.markdown(f"**ప్రశ్న:** {q.get('question', {}).get('te')}")
                    st.write("**ఆప్షన్లు:**", q.get("options", {}).get("te"))
                    st.info(f"**వివరణ:** {q.get('explanation', {}).get('te')}")

                with lang_tab3:
                    st.markdown(f"**प्रश्न:** {q.get('question', {}).get('hi')}")
                    st.write("**विकल्प:**", q.get("options", {}).get("hi"))
                    st.info(f"**व्याख्या:** {q.get('explanation', {}).get('hi')}")

                st.write(f"**Correct Answer:** `{q.get('correct_answer')}`")
                st.write("**Meta Tags:**", ", ".join(q.get("meta_tags", [])))

    with tab_raw:
        st.json(questions_data)

    st.divider()
    if st.button("💾 Save All Questions to Supabase Database", type="primary", use_container_width=True):
        if not supabase:
            st.error("Supabase client not initialized.")
        else:
            with st.spinner("Saving all 3-language questions to Supabase..."):
                rows_to_insert = []
                for q in questions_data:
                    rows_to_insert.append({
                        "exam_name": manual_exam_name if manual_exam_name else None,
                        "state": manual_state if manual_state else None,
                        "date": manual_date if manual_date else None,
                        "shift": manual_shift if manual_shift else None,
                        "question": q.get("question"),
                        "options": q.get("options"),
                        "correct_answer": q.get("correct_answer"),
                        "meta_tags": q.get("meta_tags"),
                        "explanation": q.get("explanation")
                    })

                try:
                    batch_size = 50
                    for i in range(0, len(rows_to_insert), batch_size):
                        batch = rows_to_insert[i:i + batch_size]
                        supabase.table("gk_questions").insert(batch).execute()
                    
                    st.success(f"🎉 Success! All {len(rows_to_insert)} GK Questions have been saved to Supabase in 3 languages!")
                except Exception as err:
                    st.error(f"Database insert error: {err}")