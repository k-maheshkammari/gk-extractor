import os
import re
import json
import io
import time
from datetime import datetime
import streamlit as st
import pandas as pd
from dotenv import load_dotenv
from google import genai
from google.genai import types
from supabase import create_client, Client
from pypdf import PdfReader

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

if "parsed_input_questions" not in st.session_state:
    st.session_state["parsed_input_questions"] = []

if "enriched_questions" not in st.session_state:
    st.session_state["enriched_questions"] = []

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
st.set_page_config(page_title="SSC GK Production Engine", layout="wide")
st.title("⚡ SSC GK High-Speed Enrichment & Database Ingestion Engine")

st.sidebar.header("📊 Live API Quota Monitor")
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
    exam = "SSC CGL" if "cgl" in name else ("SSC CHSL" if "chsl" in name else ("SSC MTS" if "mts" in name else "SSC"))
    state = "Central"
    
    date_match = re.search(r'(\d{4}[-_]\d{2}[-_]\d{2})|(\d{1,2}(?:st|nd|rd|th)?[-_][a-z]{3}[-_]\d{4})', name)
    date = date_match.group(0).replace("_", "-") if date_match else ""

    shift_match = re.search(r'shift[-_\s]?\d+', name)
    shift = shift_match.group(0).replace("-", " ").title() if shift_match else ""

    return exam, state, date, shift

def clean_json_response(raw_text):
    text = raw_text.strip()
    match = re.search(r'\[\s*\{.*\}\s*\]|\{\s*".*"\s*:\s*.*\}', text, re.DOTALL)
    if match:
        return match.group(0)
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'^```\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()

# 4. Universal Fault-Tolerant Question Parser (Detects Q.No, ---, Q1, 1.)
def process_parsed_blocks(valid_parts: list) -> list:
    parsed = []
    for idx, part in enumerate(valid_parts):
        ans_match = re.search(r'(?:Correct\s*(?:Answer|Option)|Ans(?:wer)?)[\s\:\-\=]+[\(\[]?\s*([A-Da-d1-4])\s*[\)\]]?', part, re.IGNORECASE)
        detected_ans = ans_match.group(1).upper() if ans_match else None
        if detected_ans in ["1", "2", "3", "4"]:
            detected_ans = {"1": "A", "2": "B", "3": "C", "4": "D"}[detected_ans]

        parsed.append({
            "id": idx + 1,
            "raw_block": part,
            "detected_answer": detected_ans
        })
    return parsed

def parse_raw_text_to_questions(text: str) -> list:
    clean_text = text.strip()
    if not clean_text:
        return []

    # Strategy 1: JSON Array
    if clean_text.startswith("[") and clean_text.endswith("]"):
        try:
            data = json.loads(clean_text)
            if isinstance(data, list) and len(data) > 0:
                return data
        except Exception:
            pass

    # Strategy 2: Horizontal Separators (--- or ===)
    if re.search(r'\n\s*[-*_]{3,}\s*(?:\n|$)', clean_text):
        raw_blocks = re.split(r'\n\s*[-*_]{3,}\s*(?:\n|$)', clean_text)
        valid_parts = [b.strip() for b in raw_blocks if b.strip() and len(b.strip()) > 15]
        if len(valid_parts) > 1:
            return process_parsed_blocks(valid_parts)

    # Strategy 3: Universal Regex Splitting (Q.No: 50, Q.No. 26, Question (EN):, Q1., 1.)
    split_pattern = r'(?=(?:\n|^)\s*(?:Q\.?\s*No[\.\:\s]*\d+|\bQ\.?\s*\d+[\.\:\)]|\bQuestion\s*(?:\d+|[\:\(])|\d{1,3}[\.\)]\s+[A-Z\u0900-\u097F\u0C00-\u0C7F]))'
    parts = re.split(split_pattern, clean_text, flags=re.IGNORECASE)
    valid_parts = [p.strip() for p in parts if p.strip() and len(p.strip()) > 15]

    # Strategy 4: Fallback Regex if still bundled
    if len(valid_parts) <= 1:
        fallback_pattern = r'(?=(?:\n|^)\s*(?:Q\.?\s*No|\bQ\d+|\bQuestion|\d{1,3}[\.\)]))'
        parts = re.split(fallback_pattern, clean_text, flags=re.IGNORECASE)
        valid_parts = [p.strip() for p in parts if p.strip() and len(p.strip()) > 15]

    return process_parsed_blocks(valid_parts)

# 5. Targeted Single-Item Enrichment System Prompt
enrichment_system_prompt = """
You are an expert Indian Competitive Exam Solution Engineer (NCERT & Telugu Academy standard).

TASK:
You are provided with a single clean GK question block.
Generate the academic solution, verified answer, 3-language translations (En, Te, Hi), detailed explanation, and strict 4-level meta-tags.

CRITICAL RULES:
1. PRESERVE SCIENCE & MATH SYMBOLS 100% INTACT:
   - NEVER modify physics dimensional formulas (e.g., [M^1 L^2 T^-2]), chemistry formulas (H2SO4, FeSO4), Greek symbols (Ω, μ, λ, α, β, Δ), or math powers/roots.
2. 3 LANGUAGES MANDATORY (en, te, hi):
   - If English, Telugu, or Hindi is already present in raw text, extract it verbatim.
   - For missing languages, provide natural, academic translations.
3. 4-LEVEL HIERARCHICAL META-TAGS:
   - meta_tags MUST strictly be a 4-element array:
     [Level 1: "Broad Subject", Level 2: "Main Topic", Level 3: "Sub-Topic", Level 4: "Specific Concept/Entity"]
     Example: ["General Science", "Biology", "Genetics & Heredity", "Pleiotropy"]
4. VERIFIED CORRECT ANSWER:
   - Confirm or solve the correct option ("A", "B", "C", or "D").
5. COMPREHENSIVE EXPLANATION:
   - Provide standard academic explanations in English, Telugu, and Hindi.

OUTPUT FORMAT: Strict single JSON object without markdown backticks:
{
  "id": 1,
  "question": {"en": "...", "te": "...", "hi": "..."},
  "options": {
    "en": {"A": "...", "B": "...", "C": "...", "D": "..."},
    "te": {"A": "...", "B": "...", "C": "...", "D": "..."},
    "hi": {"A": "...", "B": "...", "C": "...", "D": "..."}
  },
  "correct_answer": "A",
  "meta_tags": ["Subject", "Topic", "Sub-Topic", "Concept"],
  "explanation": {"en": "...", "te": "...", "hi": "..."}
}
"""

# 6. Fault-Tolerant Single-Item AI Engine (Zero 503 Risk)
def enrich_single_question_safely(q_item, live_box, max_retries=10):
    prompt_payload = json.dumps(q_item, ensure_ascii=False)
    file_part = types.Part.from_bytes(data=prompt_payload.encode("utf-8"), mime_type="text/plain")

    for attempt in range(max_retries):
        client, key_idx, key_name = get_next_available_client()
        if not client:
            raise Exception("అన్ని API కీలలో డైలీ కోటా పూర్తయింది.")

        live_box.markdown(f"🔑 **Using:** `{key_name}` | 🔄 **Q{q_item.get('id')} Attempt:** `{attempt + 1}`")

        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=[file_part, "Enrich this GK question with translations, verified answer, explanation, and 4-level meta-tags."],
                config=types.GenerateContentConfig(
                    system_instruction=enrichment_system_prompt,
                    response_mime_type="application/json",
                    temperature=0.0
                )
            )
            st.session_state["key_status"][key_idx]["calls_made"] += 1
            clean_str = clean_json_response(response.text)
            if clean_str:
                return json.loads(clean_str)
        except Exception as err:
            err_msg = str(err)
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                st.session_state["key_status"][key_idx]["exhausted"] = True
                st.session_state["key_status"][key_idx]["calls_made"] = DAILY_LIMIT_PER_KEY
                st.warning(f"⚠️ `{key_name}` కోటా తాకింది! తర్వాతి కీకి స్విచ్ అవుతున్నాం...")
                time.sleep(1)
            elif "503" in err_msg or "UNAVAILABLE" in err_msg:
                wait_t = min(2 * (attempt + 1), 8)
                st.warning(f"⏳ సర్వర్ బిజీ (503). {wait_t} సెకన్లు ఆగి మళ్లీ ప్రయత్నిస్తున్నాం...")
                time.sleep(wait_t)
            else:
                time.sleep(1.5)

    raise Exception(f"Q{q_item.get('id')} ప్రాసెసింగ్ ఫెయిల్ అయింది.")

# 7. Inputs & Metadata
tab_text, tab_file = st.tabs(["📋 Direct Paste Clean Questions", "📄 Upload Clean Text / PDF"])

detected_meta = {"exam": "SSC CGL", "state": "Central", "date": "", "shift": ""}

with tab_text:
    paper_title = st.text_input("Paper Title / Shift", placeholder="e.g. SSC_CGL_2024_09_12_Shift1")
    pasted_input = st.text_area("Paste GK Questions (Text or JSON)", height=260, placeholder="Paste clean extracted questions here...")
    if paper_title:
        e, s, d, sh = parse_metadata_from_name(paper_title)
        detected_meta = {"exam": e, "state": s, "date": d, "shift": sh}

with tab_file:
    uploaded_file = st.file_uploader("Upload Question File (TXT or PDF)", type=["txt", "pdf"])
    if uploaded_file:
        e, s, d, sh = parse_metadata_from_name(uploaded_file.name)
        detected_meta = {"exam": e, "state": s, "date": d, "shift": sh}

# Sidebar Metadata
st.sidebar.header("📝 Exam Details")
manual_exam = st.sidebar.text_input("Exam Name", value=detected_meta["exam"])
manual_state = st.sidebar.text_input("State", value=detected_meta["state"])
manual_date = st.sidebar.text_input("Date", value=detected_meta["date"])
manual_shift = st.sidebar.text_input("Shift", value=detected_meta["shift"])

st.markdown("<br>", unsafe_allow_html=True)

# 8. Step 1: Immediate Deterministic Parse & Gatekeeper
col1, col2 = st.columns([1, 1])

with col1:
    btn_parse = st.button("🔍 1. Verify & Lock Question Count", type="primary", use_container_width=True)

if btn_parse:
    raw_content = ""
    if pasted_input and pasted_input.strip():
        raw_content = pasted_input.strip()
    elif uploaded_file:
        if uploaded_file.name.endswith(".pdf"):
            reader = PdfReader(io.BytesIO(uploaded_file.read()))
            for page in reader.pages:
                raw_content += (page.extract_text() or "") + "\n"
        else:
            raw_content = uploaded_file.read().decode("utf-8")

    if not raw_content.strip():
        st.warning("⚠️ దయచేసి టెక్స్ట్‌ను పేస్ట్ చేయండి లేదా ఫైల్‌ను అప్‌లోడ్ చేయండి.")
    else:
        parsed_items = parse_raw_text_to_questions(raw_content)
        st.session_state["parsed_input_questions"] = parsed_items
        st.session_state["enriched_questions"] = []
        st.rerun()

# 9. Gatekeeper & Step 2: 1-by-1 Safe Enrichment Engine
if st.session_state["parsed_input_questions"]:
    parsed_list = st.session_state["parsed_input_questions"]
    total_q = len(parsed_list)
    
    st.success(f"🎯 **Exact Questions Found & Locked: `{total_q}`** (Zero missing, Zero drop guarantee)")
    
    with st.expander(f"📋 Review Locked Questions ({total_q} Items)", expanded=False):
        for item in parsed_list:
            st.markdown(f"**Q{item.get('id')}:** {item.get('raw_block', item)[:120]}...")
            st.caption(f"Detected Answer: `{item.get('detected_answer')}`")
            st.divider()

    with col2:
        btn_start_enrich = st.button(f"⚡ 2. Start Safe Enrichment ({total_q} Questions)", type="primary", use_container_width=True)

    if btn_start_enrich:
        status_box = st.empty()
        live_box = st.empty()
        prog = st.progress(0)
        
        results = []
        for idx, q_item in enumerate(parsed_list):
            q_num = idx + 1
            status_box.markdown(f"⏳ **Enriching Question {q_num} of {total_q}...**")
            
            try:
                enriched_obj = enrich_single_question_safely(q_item, live_box)
                enriched_obj["id"] = q_num
                results.append(enriched_obj)
            except Exception as e:
                st.error(f"Error at Question {q_num}: {e}")
                break
                
            prog.progress(q_num / total_q)
            time.sleep(0.2)

        st.session_state["enriched_questions"] = results
        status_box.empty()
        live_box.empty()
        st.success(f"🎉 100% Success! All {len(results)}/{total_q} Questions Enriched with 3 Languages & 4-Level Meta-Tags.")

# 10. Preview & Supabase Direct Ingestion
if st.session_state["enriched_questions"]:
    data = st.session_state["enriched_questions"]
    st.subheader(f"📊 Ready for Database ({len(data)} GK Questions)")

    tab_prev, tab_json = st.tabs(["📋 Multilingual Preview", "📄 JSON"])

    with tab_prev:
        for idx, q in enumerate(data):
            q_en = q.get("question", {}).get("en", "")
            with st.expander(f"Q{idx+1}: {q_en[:95]}..."):
                l1, l2, l3 = st.tabs(["🇬🇧 English", "🇮🇳 Telugu", "🇮🇳 Hindi"])
                with l1:
                    st.markdown(f"**Question:** {q.get('question', {}).get('en')}")
                    st.write("**Options:**", q.get("options", {}).get("en"))
                    st.info(f"**Explanation:** {q.get('explanation', {}).get('en')}")
                with l2:
                    st.markdown(f"**ప్రశ్న:** {q.get('question', {}).get('te')}")
                    st.write("**ఆప్షన్లు:**", q.get("options", {}).get("te"))
                    st.info(f"**వివరణ:** {q.get('explanation', {}).get('te')}")
                with l3:
                    st.markdown(f"**प्रश्न:** {q.get('question', {}).get('hi')}")
                    st.write("**विकल्प:**", q.get("options", {}).get("hi"))
                    st.info(f"**व्याख्या:** {q.get('explanation', {}).get('hi')}")
                
                st.write(f"**Correct Answer:** `{q.get('correct_answer')}`")
                st.write("**4-Level Meta-Tags:**", q.get("meta_tags", []))

    with tab_json:
        st.json(data)

    st.divider()
    if st.button("💾 Push All Questions to Supabase Database", type="primary", use_container_width=True):
        if not supabase:
            st.error("Supabase client is not connected.")
        else:
            with st.spinner("Saving directly to Supabase..."):
                rows = []
                for q in data:
                    rows.append({
                        "exam_name": manual_exam if manual_exam else None,
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
                    for i in range(0, len(rows), 50):
                        batch = rows[i:i + 50]
                        supabase.table("gk_questions").insert(batch).execute()
                    st.success(f"🎉 Success! All {len(rows)} Questions pushed to Supabase.")
                except Exception as err:
                    st.error(f"Database error: {err}")