import os
import re
import json
import io
import time
import traceback
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import streamlit as st
import pandas as pd
from dotenv import load_dotenv, find_dotenv
from google import genai
from google.genai import types
from supabase import create_client, Client
from pypdf import PdfReader

# 1. Environment Loading (.env & Streamlit Secrets)
load_dotenv(find_dotenv(), override=True)

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

# Google Daily Reset: US/Pacific Timezone (12:30 PM IST)
def get_google_quota_date():
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        except Exception:
            pass
    return datetime.now(timezone(timedelta(hours=-7))).strftime("%Y-%m-%d")

def get_key_display(idx):
    k = gemini_keys_raw[idx]
    return f"Key {idx + 1} (...{k[-6:]})"

# 2. 100% Centralized Cloud Persistence (No Local Files)
def fetch_cloud_telemetry():
    google_today = get_google_quota_date()
    status_map = {}
    tokens = {
        "last_input_tokens": 0,
        "last_output_tokens": 0,
        "last_total_tokens": 0,
        "session_total_tokens": 0
    }
    
    if supabase:
        try:
            res = supabase.table("key_quota_tracker").select("*").execute()
            rows = res.data or []
            existing = {r["key_index"]: r for r in rows}

            tot_sess_tokens = 0
            for i in range(len(gemini_keys_raw)):
                k_name = get_key_display(i)
                if i in existing and existing[i].get("quota_date") == google_today:
                    row = existing[i]
                    status_map[i] = {
                        "calls_made": row.get("calls_made", 0),
                        "cooldown_until": row.get("cooldown_until", 0.0),
                        "exhausted": row.get("calls_made", 0) >= DAILY_LIMIT_PER_KEY
                    }
                    tot_sess_tokens += row.get("session_total_tokens", 0)
                    if row.get("last_total_tokens", 0) > tokens["last_total_tokens"]:
                        tokens["last_input_tokens"] = row.get("last_input_tokens", 0)
                        tokens["last_output_tokens"] = row.get("last_output_tokens", 0)
                        tokens["last_total_tokens"] = row.get("last_total_tokens", 0)
                else:
                    # New day or first run: Initialize in Supabase
                    supabase.table("key_quota_tracker").upsert({
                        "key_index": i,
                        "key_name": k_name,
                        "quota_date": google_today,
                        "calls_made": 0,
                        "cooldown_until": 0.0,
                        "last_input_tokens": 0,
                        "last_output_tokens": 0,
                        "last_total_tokens": 0,
                        "session_total_tokens": 0
                    }).execute()
                    status_map[i] = {"calls_made": 0, "cooldown_until": 0.0, "exhausted": False}
            tokens["session_total_tokens"] = tot_sess_tokens
            return status_map, tokens
        except Exception as err:
            st.sidebar.error(f"Supabase Sync Notice: {err}")

    for i in range(len(gemini_keys_raw)):
        status_map[i] = {"calls_made": 0, "cooldown_until": 0.0, "exhausted": False}
    return status_map, tokens

def push_cloud_update(idx, calls_made, cooldown_until=0.0, inp_t=0, out_t=0, tot_t=0):
    google_today = get_google_quota_date()
    k_name = get_key_display(idx)
    payload = {
        "key_index": idx,
        "key_name": k_name,
        "quota_date": google_today,
        "calls_made": calls_made,
        "cooldown_until": cooldown_until
    }
    if tot_t > 0:
        payload["last_input_tokens"] = inp_t
        payload["last_output_tokens"] = out_t
        payload["last_total_tokens"] = tot_t
        cur_sess = st.session_state["token_metrics"]["session_total_tokens"] + tot_t
        payload["session_total_tokens"] = cur_sess

    if supabase:
        try:
            supabase.table("key_quota_tracker").upsert(payload).execute()
        except Exception as err:
            st.error(f"Cloud Quota Update Failed: {err}")

# Load live telemetry from Supabase Cloud on every render
cloud_status, cloud_tokens = fetch_cloud_telemetry()
st.session_state["key_status"] = cloud_status
st.session_state["token_metrics"] = cloud_tokens

if "audit_logs" not in st.session_state:
    st.session_state["audit_logs"] = []

if "parsed_input_questions" not in st.session_state:
    st.session_state["parsed_input_questions"] = []

if "enriched_questions" not in st.session_state:
    st.session_state["enriched_questions"] = []

if "visible_count" not in st.session_state:
    st.session_state["visible_count"] = 10

def get_current_waterfall_client():
    now = time.time()
    for idx in range(len(gemini_keys_raw)):
        status = st.session_state["key_status"][idx]
        is_exhausted = status["calls_made"] >= DAILY_LIMIT_PER_KEY
        is_cooling = status.get("cooldown_until", 0.0) > now
        if not is_exhausted and not is_cooling:
            selected_key = gemini_keys_raw[idx]
            return genai.Client(api_key=selected_key), idx, get_key_display(idx)
    return None, None, None

# 3. UI Layout & Sidebar Live Monitor
st.set_page_config(page_title="SSC GK 25-Batch Engine", layout="wide")
st.title("⚡ SSC GK Waterfall Batch Engine (Cloud Synced)")

st.sidebar.header("📊 Live Supabase API & Quota Monitor")

active_client, active_idx, active_name = get_current_waterfall_client()
if active_name:
    calls_done = st.session_state["key_status"][active_idx]["calls_made"]
    st.sidebar.success(f"👉 **Running Key:** `{active_name}`")
    st.sidebar.progress(calls_done / DAILY_LIMIT_PER_KEY, text=f"Key Progress: {calls_done}/{DAILY_LIMIT_PER_KEY} Calls")
else:
    st.sidebar.error("❌ All Keys currently cooling down or daily limits reached.")

st.sidebar.subheader("🎯 Live Cloud Token Tracker")
t_met = st.session_state["token_metrics"]
col_t1, col_t2 = st.sidebar.columns(2)
with col_t1:
    st.metric("Last Output Tokens", f"{t_met['last_output_tokens']:,}")
with col_t2:
    st.metric("Last Total Tokens", f"{t_met['last_total_tokens']:,}")

st.sidebar.caption(f"Input: `{t_met['last_input_tokens']:,}` | Session Total: `{t_met['session_total_tokens']:,}`")

st.sidebar.divider()
st.sidebar.subheader("🔑 All Keys Status")
now_t = time.time()
for i in range(len(gemini_keys_raw)):
    status_info = st.session_state["key_status"][i]
    k_name = get_key_display(i)
    c_used = status_info["calls_made"]
    cooling_left = int(status_info.get("cooldown_until", 0.0) - now_t)

    if status_info["exhausted"] or c_used >= DAILY_LIMIT_PER_KEY:
        st.sidebar.markdown(f"❌ **{k_name}**: `EXHAUSTED` ({c_used}/{DAILY_LIMIT_PER_KEY})")
    elif cooling_left > 0:
        st.sidebar.markdown(f"⏳ **{k_name}**: `COOLING` ({cooling_left}s left) ({c_used}/{DAILY_LIMIT_PER_KEY})")
    elif i == active_idx:
        st.sidebar.markdown(f"🟢 **{k_name}**: `ACTIVE RUNNER` ({c_used}/{DAILY_LIMIT_PER_KEY})")
    else:
        st.sidebar.markdown(f"⚪ **{k_name}**: `STANDBY` ({c_used}/{DAILY_LIMIT_PER_KEY})")

# 4. Helpers & Parser
def clean_json_response(raw_text):
    text = raw_text.strip()
    match = re.search(r'\[\s*\{.*\}\s*\]', text, re.DOTALL)
    if match:
        return match.group(0)
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'^```\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()

def parse_incoming_input(text: str) -> list:
    clean_text = text.strip()
    if not clean_text:
        return []

    parsed_raw = []
    if clean_text.startswith("[") and clean_text.endswith("]"):
        try:
            data = json.loads(clean_text)
            if isinstance(data, list):
                parsed_raw = data
        except Exception:
            pass

    if not parsed_raw:
        if re.search(r'\n\s*[-*_]{3,}\s*(?:\n|$)', clean_text):
            blocks = re.split(r'\n\s*[-*_]{3,}\s*(?:\n|$)', clean_text)
        else:
            split_pattern = r'(?=(?:\n|^)\s*(?:Q\.?\s*No[\.\:\s]*\d+|\bQ\.?\s*\d+[\.\:\)]|\bQuestion\s*(?:\d+|[\:\(])|\d{1,3}[\.\)]\s+[A-Z\u0900-\u097F\u0C00-\u0C7F]))'
            blocks = re.split(split_pattern, clean_text, flags=re.IGNORECASE)

        valid_blocks = [b.strip() for b in blocks if b.strip() and len(b.strip()) > 15]
        for b in valid_blocks:
            ans_match = re.search(r'(?:Correct\s*(?:Answer|Option)|Ans(?:wer)?)[\s\:\-\=]+[\(\[]?\s*([A-Da-d1-4])\s*[\)\]]?', b, re.IGNORECASE)
            detected_ans = ans_match.group(1).upper() if ans_match else "A"
            if detected_ans in ["1", "2", "3", "4"]:
                detected_ans = {"1": "A", "2": "B", "3": "C", "4": "D"}[detected_ans]

            parsed_raw.append({
                "raw_block": b,
                "correct_answer": detected_ans
            })

    deduped = []
    seen_hashes = set()
    for item in parsed_raw:
        sample_str = item.get("raw_block") or str(item.get("question", ""))
        normalized_str = re.sub(r'\s+', ' ', sample_str.lower()).strip()
        if normalized_str and normalized_str not in seen_hashes:
            seen_hashes.add(normalized_str)
            deduped.append(item)

    for idx, item in enumerate(deduped):
        item["id"] = idx + 1

    return deduped

def parse_metadata_from_name(name_str):
    name = (name_str or "").lower()
    exam = "SSC CGL" if "cgl" in name else ("SSC CHSL" if "chsl" in name else ("SSC MTS" if "mts" in name else "SSC"))
    state = "Central"
    date_match = re.search(r'(\d{4}[-_]\d{2}[-_]\d{2})|(\d{1,2}(?:st|nd|rd|th)?[-_][a-z]{3}[-_]\d{4})', name)
    date = date_match.group(0).replace("_", "-") if date_match else ""
    shift_match = re.search(r'shift[-_\s]?\d+', name)
    shift = shift_match.group(0).replace("-", " ").title() if shift_match else ""
    return exam, state, date, shift

batch25_system_prompt = """
You are an expert Indian Competitive Exam Solution Engineer (NCERT & Telugu Academy standard).

TASK:
You are provided with a batch of up to 25 clean GK questions.
For EVERY question, you must return:
1. "id": Matching the exact ID provided.
2. "meta_tags": Exactly 4 hierarchical levels:
   [Level 1: "Broad Subject", Level 2: "Main Topic", Level 3: "Sub-Topic", Level 4: "Specific Concept/Entity"]
3. "explanation": Comprehensive, standard academic explanation in English, Telugu, and Hindi.
4. "question" & "options": If the input question was in raw text form, format them into 3 languages ("en", "te", "hi"). If already structured in 3 languages, mirror them verbatim.
5. "correct_answer": Verify or confirm the correct option ("A", "B", "C", or "D").

STRICT RULE:
Return ONLY a valid JSON array of objects without markdown backticks.

OUTPUT FORMAT:
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
    "meta_tags": ["Broad Subject", "Main Topic", "Sub-Topic", "Specific Concept"],
    "explanation": {"en": "...", "te": "...", "hi": "..."}
  }
]
"""

# 5. Non-Destructive Processing Engine with Complete Diagnostic Tracing
def process_full_batch(questions_list, live_box, max_retries=15):
    payload = json.dumps(questions_list, ensure_ascii=False)
    file_part = types.Part.from_bytes(data=payload.encode("utf-8"), mime_type="text/plain")
    
    last_error_diagnostic = "ఎలాంటి కాల్ జరగలేదు లేదా ప్రారంభంలోనే కీలు అందుబాటులో లేవు."

    for attempt in range(max_retries):
        client, key_idx, key_name = get_current_waterfall_client()
        if not client:
            cool_times = [st.session_state["key_status"][i]["cooldown_until"] - time.time() for i in range(len(gemini_keys_raw)) if not st.session_state["key_status"][i]["exhausted"]]
            wait_sec = max(int(min(cool_times)), 2) if cool_times else 10
            last_error_diagnostic = f"అన్ని కీలు కూల్‌డౌన్‌లో లేదా డైలీ లిమిట్ రీచ్ అయ్యాయి (Attempt {attempt+1}/{max_retries})."
            live_box.warning(f"⏳ అన్ని కీలు కూల్‌డౌన్‌లో ఉన్నాయి. {wait_sec} సెకన్లు వేచిచూస్తున్నాం...")
            time.sleep(wait_sec)
            continue

        live_box.markdown(f"🔑 **Running:** `{key_name}` | 🔄 **Batch Size:** `{len(questions_list)} Qs` | Attempt `{attempt+1}`")

        try:
            start_t = time.time()
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=[file_part, f"Enrich all {len(questions_list)} questions with 4-level meta-tags, 3-language explanations, and strict JSON format."],
                config=types.GenerateContentConfig(
                    system_instruction=batch25_system_prompt,
                    response_mime_type="application/json",
                    temperature=0.0
                )
            )
            duration = round(time.time() - start_t, 2)
            
            # Extract Tokens
            usage = response.usage_metadata
            inp_tok = getattr(usage, "prompt_token_count", 0)
            out_tok = getattr(usage, "candidates_token_count", 0)
            tot_tok = getattr(usage, "total_token_count", 0)

            # Update Supabase Cloud with new call count and token metrics
            new_calls = st.session_state["key_status"][key_idx]["calls_made"] + 1
            push_cloud_update(key_idx, new_calls, cooldown_until=0.0, inp_t=inp_tok, out_t=out_tok, tot_t=tot_tok)

            finish_reason = ""
            if response.candidates and len(response.candidates) > 0:
                finish_reason = str(response.candidates[0].finish_reason)

            if "MAX_TOKENS" in finish_reason:
                st.warning(f"⚠️ {key_name} వద్ద టోకెన్లు సరిపోలేదు. విభజిస్తున్నాం...")
                mid = len(questions_list) // 2
                batch_a = process_full_batch(questions_list[:mid], live_box, max_retries)
                batch_b = process_full_batch(questions_list[mid:], live_box, max_retries)
                
                merged = []
                seen_ids = set()
                for q in (batch_a + batch_b):
                    qid = q.get("id")
                    if qid not in seen_ids:
                        seen_ids.add(qid)
                        merged.append(q)
                return merged

            clean_str = clean_json_response(response.text)
            if not clean_str:
                raise ValueError(f"గూగుల్ నుండి రెస్పాన్స్ టెక్స్ట్ రాలేదు (Empty Text). Response Candidates: {response.candidates}")

            result = json.loads(clean_str)
            if isinstance(result, list) and len(result) > 0:
                st.session_state["audit_logs"].append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "key": key_name,
                    "questions": len(questions_list),
                    "tokens": out_tok,
                    "duration": f"{duration}s",
                    "status": "SUCCESS"
                })
                return result
            else:
                raise ValueError(f"రెస్పాన్స్ JSON లిస్ట్ రూపంలో రాలేదు: {str(result)[:250]}")

        except Exception as err:
            err_msg = str(err)
            tb_str = traceback.format_exc()
            last_error_diagnostic = f"Key: {key_name} | Attempt: {attempt+1}\nError: {err_msg}\n\nFull Traceback:\n{tb_str}"
            
            # Print clearly in VS Code Console
            print("\n" + "="*70)
            print(f"🚨 [ATTEMPT {attempt+1} FAILED] ON KEY: {key_name}")
            print(f"ERROR: {err_msg}")
            traceback.print_exc()
            print("="*70 + "\n")

            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                cool_expire = time.time() + 60.0
                current_c = st.session_state["key_status"][key_idx]["calls_made"]
                push_cloud_update(key_idx, current_c, cooldown_until=cool_expire)
                live_box.warning(f"⏳ `{key_name}` వద్ద తాత్కాలిక నిమిషాల రద్దీ (429 Limit). 60 సెకన్ల కూల్‌డౌన్ విధించి, తర్వాతి కీకి బదిలీ చేస్తున్నాం...")
                time.sleep(2)
            elif "503" in err_msg or "UNAVAILABLE" in err_msg:
                wait_t = min(3 * (attempt + 1), 12)
                live_box.warning(f"⏳ సర్వర్ బిజీ (503). {wait_t} సెకన్లు వేచిచూస్తున్నాం...")
                time.sleep(wait_t)
            else:
                live_box.warning(f"⚠️ ఎర్రర్ ({key_name}): {err_msg[:90]}... రీట్రై అవుతోంది...")
                time.sleep(2)

    # 15 Attempts ఫెయిల్ అయితే అసలైన కారణంతో ఎక్సెప్షన్ రేయిజ్ చేయబడుతుంది
    raise Exception(f"25 ప్రశ్నల ప్రాసెసింగ్ 15 అటెంప్ట్‌ల తర్వాత కూడా పూర్తి కాలేదు.\n\nచివరి ఎర్రర్ డయాగ్నస్టిక్ వివరాలు:\n{last_error_diagnostic}")

# 6. UI Tabs
tab_text, tab_file, tab_search = st.tabs(["📋 Direct Paste GK Questions", "📄 Upload File (TXT/PDF)", "🔍 Search Database"])
detected_meta = {"exam": "SSC CGL", "state": "Central", "date": "", "shift": ""}

with tab_text:
    paper_title = st.text_input("Paper Title / Shift", placeholder="e.g. SSC_CGL_2024_09_12_Shift1")
    pasted_input = st.text_area("Paste GK Questions (Text or JSON)", height=240, placeholder="Paste 25 clean questions here...")
    if paper_title:
        e, s, d, sh = parse_metadata_from_name(paper_title)
        detected_meta = {"exam": e, "state": s, "date": d, "shift": sh}

with tab_file:
    uploaded_file = st.file_uploader("Upload Question File", type=["txt", "pdf"])
    if uploaded_file:
        e, s, d, sh = parse_metadata_from_name(uploaded_file.name)
        detected_meta = {"exam": e, "state": s, "date": d, "shift": sh}

# --- TAB 3: SMART SEARCH PORTAL ---
with tab_search:
    st.subheader("🎯 Explore & Practice GK Database")
    s_col1, s_col2 = st.columns([3, 1])
    with s_col1:
        search_kw = st.text_input("Search Questions (e.g. Chabahar, Bonalu, Article 249, Sati, UCC)", placeholder="Type a keyword and press Enter...")
    with s_col2:
        exam_filt = st.selectbox("Exam Filter", ["All Exams", "SSC CGL", "SSC CHSL", "SSC MTS", "Central"])

    if st.button("🔎 Search Questions", use_container_width=True) or search_kw:
        if not supabase:
            st.error("Supabase client not connected.")
        else:
            with st.spinner("Searching database..."):
                try:
                    kw = search_kw.strip().lower()
                    query = supabase.table("gk_questions").select("*")
                    if exam_filt != "All Exams":
                        query = query.ilike("exam_name", f"%{exam_filt}%")
                    
                    if kw:
                        query = query.or_(f"question->>en.ilike.%{kw}%,question->>te.ilike.%{kw}%,question->>hi.ilike.%{kw}%")

                    res = query.order("created_at", desc=True).limit(st.session_state["visible_count"]).execute()
                    questions = res.data or []

                    if not questions:
                        st.info("💡 ఎలాంటి ప్రశ్నలు కనుగొనబడలేదు.")
                    else:
                        st.success(f"✨ Found {len(questions)} Questions matching your search:")
                        for idx, q in enumerate(questions):
                            q_data = q.get("question", {}) or {}
                            opts_data = q.get("options", {}) or {}
                            expl_data = q.get("explanation", {}) or {}
                            
                            with st.container():
                                st.markdown(f"**Q{idx+1}. {q_data.get('en', '')}**")
                                st.caption(f"📍 {q.get('state','Central')} | 🏛️ {q.get('exam_name','SSC')} | 📅 {q.get('date','')} {q.get('shift','')}")
                                
                                l_tab1, l_tab2, l_tab3 = st.tabs(["🇬🇧 English", "🇮🇳 తెలుగు", "🇮🇳 हिंदी"])
                                with l_tab1:
                                    en_opts = opts_data.get("en", {})
                                    for k in sorted(en_opts.keys()):
                                        st.write(f"**({k})** {en_opts[k]}")
                                with l_tab2:
                                    st.markdown(f"**ప్రశ్న:** {q_data.get('te', '')}")
                                    te_opts = opts_data.get("te", {})
                                    for k in sorted(te_opts.keys()):
                                        st.write(f"**({k})** {te_opts[k]}")
                                with l_tab3:
                                    st.markdown(f"**प्रश्न:** {q_data.get('hi', '')}")
                                    hi_opts = opts_data.get("hi", {})
                                    for k in sorted(hi_opts.keys()):
                                        st.write(f"**({k})** {hi_opts[k]}")

                                with st.expander("💡 View Answer & Detailed Explanation"):
                                    st.success(f"✅ Correct Answer: Option ({q.get('correct_answer')})")
                                    st.markdown(f"**English:** {expl_data.get('en', '')}")
                                    st.markdown(f"**తెలుగు:** {expl_data.get('te', '')}")
                                    st.markdown(f"**हिंदी:** {expl_data.get('hi', '')}")
                                st.divider()
                except Exception as err:
                    st.error(f"Search Error: {err}")

# Sidebar Metadata
st.sidebar.divider()
st.sidebar.subheader("📝 Paper Details")
manual_exam = st.sidebar.text_input("Exam Name", value=detected_meta["exam"])
manual_state = st.sidebar.text_input("State", value=detected_meta["state"])
manual_date = st.sidebar.text_input("Date", value=detected_meta["date"])
manual_shift = st.sidebar.text_input("Shift", value=detected_meta["shift"])

# 7. Gatekeeper
col1, col2 = st.columns([1, 1])
with col1:
    btn_parse = st.button("🔍 1. Verify & Lock Clean Questions", type="primary", use_container_width=True)

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
        st.warning("⚠️ దయచేసి ప్రశ్నలను పేస్ట్ చేయండి లేదా ఫైల్ అప్‌లోడ్ చేయండి.")
    else:
        parsed_items = parse_incoming_input(raw_content)
        st.session_state["parsed_input_questions"] = parsed_items
        st.session_state["enriched_questions"] = []
        st.rerun()

# 8. Enrichment Trigger with Transparent Error Display
if st.session_state["parsed_input_questions"]:
    parsed_list = st.session_state["parsed_input_questions"]
    total_q = len(parsed_list)

    st.success(f"🎯 **Locked Unique Questions: `{total_q}`** (Zero Duplicates Verified)")

    with col2:
        btn_start_enrich = st.button(f"🚀 2. Run Waterfall Batch Enrichment ({total_q} Questions)", type="primary", use_container_width=True)

    if btn_start_enrich:
        status_box = st.empty()
        live_box = st.empty()
        status_box.markdown(f"⏳ **Processing All {total_q} Questions via Waterfall Engine...**")
        start_time = time.time()

        try:
            enriched_results = process_full_batch(parsed_list, live_box)
            final_data = []
            seen_final_ids = set()
            for idx, item in enumerate(enriched_results):
                orig_item = parsed_list[idx] if idx < len(parsed_list) else {}
                qid = idx + 1
                if qid not in seen_final_ids:
                    seen_final_ids.add(qid)
                    final_data.append({
                        "id": qid,
                        "question": item.get("question") or orig_item.get("question"),
                        "options": item.get("options") or orig_item.get("options"),
                        "correct_answer": item.get("correct_answer") or orig_item.get("correct_answer"),
                        "meta_tags": item.get("meta_tags"),
                        "explanation": item.get("explanation")
                    })

            elapsed = round(time.time() - start_time, 2)
            st.session_state["enriched_questions"] = final_data
            status_box.empty()
            live_box.empty()
            st.success(f"🎉 Complete! All {len(final_data)} Questions Enriched in {elapsed}s.")
            st.rerun()
        except Exception as e:
            status_box.empty()
            live_box.empty()
            st.error("🚨 Processing Failed! అసలైన ఎర్రర్ వివరాలు కింద ఉన్నాయి:")
            st.code(str(e), language="text")
            print("\n" + "#"*70)
            print("🚨 UNCAUGHT PIPELINE EXCEPTION:")
            traceback.print_exc()
            print("#"*70 + "\n")

# 9. Multilingual Review & Supabase Direct Ingestion
if st.session_state["enriched_questions"]:
    data = st.session_state["enriched_questions"]
    st.subheader(f"📊 Ready for Database ({len(data)} GK Questions)")

    tab_prev, tab_json, tab_audit = st.tabs(["📋 Multilingual Preview", "📄 Raw JSON", "⏱️ Live Audit Log"])

    with tab_prev:
        for idx, q in enumerate(data):
            q_en = (q.get("question") or {}).get("en", "")
            with st.expander(f"Q{idx+1}: {str(q_en)[:95]}..."):
                l1, l2, l3 = st.tabs(["🇬🇧 English", "🇮🇳 Telugu", "🇮🇳 Hindi"])
                with l1:
                    st.markdown(f"**Question:** {(q.get('question') or {}).get('en')}")
                    st.write("**Options:**", (q.get("options") or {}).get("en"))
                    st.info(f"**Explanation:** {(q.get('explanation') or {}).get('en')}")
                with l2:
                    st.markdown(f"**ప్రశ్న:** {(q.get('question') or {}).get('te')}")
                    st.write("**ఆప్షన్లు:**", (q.get("options") or {}).get("te"))
                    st.info(f"**వివరణ:** {(q.get('explanation') or {}).get('te')}")
                with l3:
                    st.markdown(f"**प्रश्न:** {(q.get('question') or {}).get('hi')}")
                    st.write("**विकल्प:**", (q.get("options") or {}).get("hi"))
                    st.info(f"**व्याख्या:** {(q.get('explanation') or {}).get('hi')}")

                st.write(f"**Correct Answer:** `{q.get('correct_answer')}`")
                st.write("**4-Level Meta-Tags:**", q.get("meta_tags", []))

    with tab_json:
        st.json(data)

    with tab_audit:
        if st.session_state["audit_logs"]:
            st.dataframe(pd.DataFrame(st.session_state["audit_logs"]), use_container_width=True)

    st.divider()
    if st.button("💾 Push All Questions to Supabase Database", type="primary", use_container_width=True):
        if not supabase:
            st.error("Supabase client is not connected.")
        else:
            with st.spinner("Writing directly to Supabase..."):
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

                for attempt in range(1, 4):
                    try:
                        for i in range(0, len(rows), 25):
                            batch = rows[i:i + 25]
                            supabase.table("gk_questions").insert(batch).execute()
                        st.success(f"🎉 Success! All {len(rows)} Unique Questions pushed to Supabase.")
                        break
                    except Exception as err:
                        if attempt < 3:
                            time.sleep(2)
                        else:
                            st.error(f"Database error: {err}")