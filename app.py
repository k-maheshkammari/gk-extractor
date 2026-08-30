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

if "raw_scanned_questions" not in st.session_state:
    st.session_state["raw_scanned_questions"] = []

if "extracted_questions" not in st.session_state:
    st.session_state["extracted_questions"] = []

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

# 2. UI Layout & Sidebar Monitor
st.set_page_config(page_title="Industrial GK Extractor & Live Monitor", layout="wide")
st.title("📚 Industrial GK Extractor & Auto-Translator (En | Te | Hi)")

st.sidebar.header("📊 Live Quota & Capacity Monitor")
active_keys_count = sum(1 for v in st.session_state["key_status"].values() if not v["exhausted"])
total_keys_count = len(gemini_keys_raw)

if st.sidebar.button("🔄 Check & Refresh All Keys", use_container_width=True):
    with st.sidebar.status("Testing all keys..."):
        for i in range(total_keys_count):
            test_single_key(i)
    st.rerun()

st.sidebar.markdown(f"**🟢 Active Keys:** `{active_keys_count} / {total_keys_count}`")

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

# 3. Metadata Extractor
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

def clean_json_response(raw_text):
    text = raw_text.strip()
    match = re.search(r'\[\s*\{.*\}\s*\]', text, re.DOTALL)
    if match:
        return match.group(0)
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'^```\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()

# 4. Stage 1 Deduplication & Entity Normalizer
BOILERPLATE_PATTERNS = [
    r'which\s+of\s+the\s+following(\s+statements?)?(\s+regarding[a-z\s]+)?(\s+is|\s+are|\s+correctly)?\s*(correct|true|false|reflects|evolution)?',
    r'read\s+(the\s+)?(below|following)\s+statements?',
    r'fill\s+in\s+the\s+blanks?(\s*:\s*)?',
    r'assertion\s*\([a-z]\):?',
    r'reason\s*\([a-z]\):?',
    r'choose\s+the\s+correct\s+(option|answer|code)',
    r'select\s+the\s+correct\s+answer',
    r'match\s+list\s*[-–]?\s*[i|1]\s+with\s+list\s*[-–]?\s*[i|2]',
    r'निम्नलिखित\s+में\s+से\s+कौन\s+सा\s+कथन\s+सही\s+है\??',
    r'रिक्त\s+स्थान\s+भरें\s*:\s*',
    r'नीचे\s+अभिकथन\s*\(a\)\s+और\s+कारण\s*\(r\)\s+दिए\s+गए\s+हैं\।?',
    r'క్రింది\s*(వాక్యాలను|ప్రకటనలను)\s*పరిశీలించండి',
    r'సరైన\s*(సమాధానాన్ని|ఎంపికను)\s*ఎంచుకోండి'
]

GENERIC_WORDS = {
    'both', 'and', 'true', 'false', 'correct', 'explanation', 'not', 'neither',
    'only', 'one', 'two', 'three', 'four', 'statement', 'statements', 'option', 
    'options', 'following', 'regarding', 'refers', 'which', 'what', 'when', 'where'
}

def extract_tokens(text: str) -> set:
    if not text:
        return set()
    cleaned = text.lower()
    for p in BOILERPLATE_PATTERNS:
        cleaned = re.sub(p, ' ', cleaned, flags=re.IGNORECASE)
    words = re.findall(r'[a-zA-Z0-9\u0900-\u097F\u0C00-\u0C7F]{3,}', cleaned)
    return {w for w in words if w not in GENERIC_WORDS}

def are_raw_questions_duplicate(q1: dict, q2: dict) -> bool:
    t1 = extract_tokens(q1.get("raw_question", ""))
    t2 = extract_tokens(q2.get("raw_question", ""))
    
    if t1 and t2:
        inter = len(t1 & t2)
        union = len(t1 | t2)
        if union > 0 and (inter / union) >= 0.55 and inter >= 2:
            return True
            
    opts1_str = " ".join(str(v) for v in q1.get("raw_options", {}).values())
    opts2_str = " ".join(str(v) for v in q2.get("raw_options", {}).values())
    opt_t1 = extract_tokens(opts1_str)
    opt_t2 = extract_tokens(opts2_str)
    
    if len(opt_t1) >= 3 and len(opt_t2) >= 3:
        opt_inter = len(opt_t1 & opt_t2)
        opt_union = len(opt_t1 | opt_t2)
        if opt_union > 0 and (opt_inter / opt_union) >= 0.60:
            return True
            
    return False

def get_raw_completeness(q: dict) -> int:
    q_len = len(q.get("raw_question", ""))
    opts_len = sum(len(str(v)) for v in q.get("raw_options", {}).values())
    ans_score = 50 if q.get("detected_answer") else 0
    return q_len + opts_len + ans_score

def deduplicate_raw_scanned_list(raw_list: list) -> list:
    unique_list = []
    for item in raw_list:
        matched_idx = -1
        for idx, existing in enumerate(unique_list):
            if are_raw_questions_duplicate(item, existing):
                matched_idx = idx
                break
        if matched_idx == -1:
            unique_list.append(item)
        else:
            if get_raw_completeness(item) > get_raw_completeness(unique_list[matched_idx]):
                unique_list[matched_idx] = item
    return unique_list

# 5. System Prompts (Two-Stage Architecture)
stage1_system_prompt = """
You are a High-Recall Indian Competitive Exam Question Filter and Boundary Detector.
TASK: Extract ONLY General Knowledge (GK) / General Studies (GS) questions from the given input.

STRICT FILTER RULES:
1. IGNORE completely: Pure Math/Quantitative Aptitude (Trigonometry, Algebra, Arithmetic calculation problems), Pure Logical Reasoning / Number / Letter Series puzzles, Pure English Grammar / Comprehension passages.
2. EXTRACT 100% GK/GS: History, Polity & Constitution, Geography, Economy, General Science (Physics, Chemistry, Biology), Environment, Art & Culture, Current Affairs.
3. ALL QUESTION TYPES MUST BE EXTRACTED: Statement 1 & 2, Assertion & Reason (A&R), Match List-I & II, Chronology, Fill in the Blanks.
4. PRESERVE SCIENCE & FORMULAS 100% INTACT: Formulas ([M^1 L^2 T^-2], H2SO4, FeSO4), Greek symbols (Ω, μ, λ, α, β), superscripts/subscripts. Do NOT drop brackets or equations.
5. Keep the raw_question and raw_options verbatim as printed (including multilingual text if both English & Hindi/Telugu are in the source).

OUTPUT FORMAT: Strict JSON array of objects without markdown backticks:
[
  {
    "raw_question": "...",
    "raw_options": {"A": "...", "B": "...", "C": "...", "D": "..."},
    "detected_answer": "A" // or null if not indicated
  }
]
"""

stage2_system_prompt = """
You are an expert Multilingual Translator & Academic Exam Solution Generator (NCERT / Telugu Academy Standard).
TASK: Take the provided GK questions and enrich them with full 3-language translations, verified correct answer, comprehensive explanation, and meta tags.

RULES:
1. PRESERVE SCIENCE & MATH SYMBOLS 100%: Keep physics units ([M^1 L^2 T^-2]), chemical formulas (H2SO4, FeSO4), Greek symbols (Ω, μ, λ) 100% mathematically accurate across all languages.
2. VERBATIM & ACADEMIC TRANSLATION: If a language is already present, keep it verbatim. For missing languages, generate standard academic translations.
3. SOLVE/CONFIRM CORRECT ANSWER: Determine the verified correct answer ("A", "B", "C", or "D").
4. COMPREHENSIVE EXPLANATION: Write detailed, accurate explanations in English, Telugu, and Hindi.
5. All 3 language keys ("en", "te", "hi") are MANDATORY for question, options, and explanation.

OUTPUT FORMAT: Strict JSON array of objects without markdown backticks:
[
  {
    "id": 1,
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

# 6. Fault-Tolerant AI Engine
def generate_with_smart_fallback(file_part, prompt_text, sys_instruction, live_status_box, max_retries=12):
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
                    system_instruction=sys_instruction,
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

# 7. Dual Input Interface
tab_upload, tab_paste = st.tabs(["📄 Upload File (PDF / TXT)", "📋 Direct Paste Text (Ultra-Fast)"])

detected_meta = {"exam": "", "state": "", "date": "", "shift": ""}

with tab_upload:
    uploaded_file = st.file_uploader("Upload Question Paper (PDF or TXT)", type=["pdf", "txt"])
    if uploaded_file:
        e, s, d, sh = parse_metadata_from_name(uploaded_file.name)
        detected_meta = {"exam": e, "state": s, "date": d, "shift": sh}

with tab_paste:
    st.caption("⚡ PDF నుండి కాపీ చేసిన టెక్స్ట్‌ను నేరుగా ఇక్కడ పేస్ట్ చేయండి.")
    paper_title = st.text_input("Paper Name / Reference (Optional)", placeholder="e.g. SSC_CGL_2024_09_12_Shift1")
    pasted_text_input = st.text_area("Paste Question Paper Text Here", height=220, placeholder="Paste questions text here (Ctrl + V)...")
    if paper_title:
        e, s, d, sh = parse_metadata_from_name(paper_title)
        detected_meta = {"exam": e, "state": s, "date": d, "shift": sh}

# Sidebar Metadata
st.sidebar.header("📝 Exam Metadata")
manual_exam_name = st.sidebar.text_input("Exam Name", value=detected_meta["exam"])
manual_state = st.sidebar.text_input("State", value=detected_meta["state"])
manual_date = st.sidebar.text_input("Date", value=detected_meta["date"])
manual_shift = st.sidebar.text_input("Shift", value=detected_meta["shift"])

st.markdown("<br>", unsafe_allow_html=True)

# 8. STAGE 1: Universal Scan & Deduplicated Extraction (Gatekeeper)
col_step1, col_step2 = st.columns([1, 1])

with col_step1:
    btn_scan = st.button("🔍 1. Scan & Identify Raw GK Questions", type="primary", use_container_width=True)

if btn_scan:
    if active_keys_count == 0:
        st.error("అన్ని API కీలు కోటా తాకాయి. దయచేసి కాసేపటి తర్వాత ప్రయత్నించండి.")
    else:
        st.session_state["raw_scanned_questions"] = []
        st.session_state["extracted_questions"] = []
        
        status_box = st.empty()
        live_box = st.empty()
        progress_bar = st.progress(0)
        
        try:
            scanned_raw = []
            
            # SCENARIO A: Direct Pasted Text
            if pasted_text_input and pasted_text_input.strip():
                raw_text = pasted_text_input.strip()
                status_box.markdown("⏳ **Scanning pasted text for GK questions...**")
                
                chunk_len = 5000
                slices = [raw_text[i:i+chunk_len] for i in range(0, len(raw_text), chunk_len)]
                for idx, sl in enumerate(slices):
                    file_part = types.Part.from_bytes(data=sl.encode("utf-8"), mime_type="text/plain")
                    resp, _ = generate_with_smart_fallback(
                        file_part=file_part,
                        prompt_text="Extract raw GK questions matching system rules.",
                        sys_instruction=stage1_system_prompt,
                        live_status_box=live_box
                    )
                    clean_str = clean_json_response(resp)
                    if clean_str:
                        parsed = json.loads(clean_str)
                        if isinstance(parsed, list):
                            scanned_raw.extend(parsed)
                    progress_bar.progress((idx + 1) / len(slices))
                    
            # SCENARIO B: Uploaded PDF / TXT
            elif uploaded_file is not None:
                if uploaded_file.name.endswith(".pdf"):
                    pdf_bytes = uploaded_file.read()
                    pdf_reader = PdfReader(io.BytesIO(pdf_bytes))
                    total_pages = len(pdf_reader.pages)
                    
                    # 1. Try Digital Text Stream Extraction First (Zero Page Split Loss)
                    full_digital_text = ""
                    for p in pdf_reader.pages:
                        t = p.extract_text() or ""
                        full_digital_text += t + "\n"
                        
                    if len(full_digital_text.strip()) > 300:
                        status_box.markdown("⚡ **Digital PDF Detected: Processing continuous text stream...**")
                        chunk_len = 5500
                        slices = [full_digital_text[i:i+chunk_len] for i in range(0, len(full_digital_text), chunk_len)]
                        for idx, sl in enumerate(slices):
                            file_part = types.Part.from_bytes(data=sl.encode("utf-8"), mime_type="text/plain")
                            resp, _ = generate_with_smart_fallback(
                                file_part=file_part,
                                prompt_text="Extract raw GK questions matching system rules.",
                                sys_instruction=stage1_system_prompt,
                                live_status_box=live_box
                            )
                            clean_str = clean_json_response(resp)
                            if clean_str:
                                parsed = json.loads(clean_str)
                                if isinstance(parsed, list):
                                    scanned_raw.extend(parsed)
                            progress_bar.progress((idx + 1) / len(slices))
                    else:
                        # 2. Fallback to Byte Slices for Scanned / Image-heavy PDFs
                        status_box.markdown(f"📄 **Scanning {total_pages} PDF Pages...**")
                        page_slices = []
                        curr = 0
                        while curr < total_pages:
                            nxt = min(curr + 3, total_pages)
                            page_slices.append((curr, nxt))
                            if nxt == total_pages:
                                break
                            curr += 2  # Safe overlap for boundary reconstruction
                            
                        for c_idx, (p_start, p_end) in enumerate(page_slices):
                            status_box.markdown(f"⏳ **Scanning Pages {p_start+1} to {p_end} of {total_pages}...**")
                            writer = PdfWriter()
                            for p in range(p_start, p_end):
                                writer.add_page(pdf_reader.pages[p])
                            stream = io.BytesIO()
                            writer.write(stream)
                            
                            file_part = types.Part.from_bytes(data=stream.getvalue(), mime_type="application/pdf")
                            resp, _ = generate_with_smart_fallback(
                                file_part=file_part,
                                prompt_text="Extract raw GK questions matching system rules.",
                                sys_instruction=stage1_system_prompt,
                                live_status_box=live_box
                            )
                            clean_str = clean_json_response(resp)
                            if clean_str:
                                parsed = json.loads(clean_str)
                                if isinstance(parsed, list):
                                    scanned_raw.extend(parsed)
                            progress_bar.progress((c_idx + 1) / len(page_slices))
                else:
                    txt = uploaded_file.read().decode("utf-8")
                    file_part = types.Part.from_bytes(data=txt.encode("utf-8"), mime_type="text/plain")
                    resp, _ = generate_with_smart_fallback(
                        file_part=file_part,
                        prompt_text="Extract raw GK questions matching system rules.",
                        sys_instruction=stage1_system_prompt,
                        live_status_box=live_box
                    )
                    clean_str = clean_json_response(resp)
                    scanned_raw = json.loads(clean_str)
                    progress_bar.progress(1.0)
            else:
                st.warning("⚠️ దయచేసి ఒక PDF/TXT ఫైల్‌ను అప్‌లోడ్ చేయండి లేదా టెక్స్ట్‌ను పేస్ట్ చేయండి.")

            # Stage 1 Industrial Deduplication & ID Re-indexing
            unique_scanned = deduplicate_raw_scanned_list(scanned_raw)
            for i, item in enumerate(unique_scanned):
                item["id"] = i + 1

            st.session_state["raw_scanned_questions"] = unique_scanned
            status_box.empty()
            live_box.empty()
            st.rerun()

        except Exception as err:
            st.error(f"Stage 1 Scanning Error: {err}")

# Display Stage 1 Results & Confirmation Gatekeeper
if st.session_state["raw_scanned_questions"]:
    raw_list = st.session_state["raw_scanned_questions"]
    st.success(f"🎯 **GK Questions Found: `{len(raw_list)}`** (Deduplicated & Verified. Review list below before proceeding)")
    
    with st.expander(f"📋 View Identified Raw Questions List ({len(raw_list)} items)", expanded=False):
        for item in raw_list:
            q_txt = item.get("raw_question", "")
            opts = item.get("raw_options", {})
            st.markdown(f"**Q{item.get('id')}:** {q_txt}")
            st.caption(f"Options: {opts} | Detected Ans: `{item.get('detected_answer')}`")
            st.divider()

    with col_step2:
        btn_enrich = st.button(f"⚡ 2. Confirm & Generate Multilingual ({len(raw_list)} Questions)", type="primary", use_container_width=True)

    # 9. STAGE 2: Deterministic Python Slicing & Micro-Batch Enrichment
    if btn_enrich:
        status_box2 = st.empty()
        live_box2 = st.empty()
        prog2 = st.progress(0)
        
        enriched_results = []
        batch_size = 3  # Safe micro-batch: 3 questions per call to protect token limit & 503 errors
        total_items = len(raw_list)
        
        batches = [raw_list[i:i+batch_size] for i in range(0, total_items, batch_size)]
        total_batches = len(batches)
        
        try:
            for b_idx, current_batch in enumerate(batches):
                start_q = b_idx * batch_size + 1
                end_q = min((b_idx + 1) * batch_size, total_items)
                status_box2.markdown(f"⏳ **Enriching Questions {start_q} to {end_q} of {total_items}** (Batch {b_idx + 1}/{total_batches})...")
                
                batch_payload = json.dumps(current_batch, ensure_ascii=False)
                file_part = types.Part.from_bytes(data=batch_payload.encode("utf-8"), mime_type="text/plain")
                
                resp, _ = generate_with_smart_fallback(
                    file_part=file_part,
                    prompt_text="Translate (En, Te, Hi), solve/verify, and enrich these questions in strict JSON format.",
                    sys_instruction=stage2_system_prompt,
                    live_status_box=live_box2
                )
                
                clean_str = clean_json_response(resp)
                if clean_str:
                    batch_out = json.loads(clean_str)
                    if isinstance(batch_out, list):
                        enriched_results.extend(batch_out)
                        
                prog2.progress((b_idx + 1) / total_batches)
                time.sleep(0.3)
                
            st.session_state["extracted_questions"] = enriched_results
            status_box2.empty()
            live_box2.empty()
            st.success(f"🎉 100% Multilingual Generation Complete! All {len(enriched_results)} Questions Enriched.")

        except Exception as e:
            st.error(f"Stage 2 Enrichment Error: {e}")

# 10. Multilingual Preview & Direct Supabase Batch Save
if st.session_state["extracted_questions"]:
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
            with st.spinner("Saving all questions to Supabase..."):
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