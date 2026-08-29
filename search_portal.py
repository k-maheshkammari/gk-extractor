import os
import re
import time
import streamlit as st
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        st.error("⚠️ Supabase credentials missing in .env file.")
        return None
    return create_client(SUPABASE_URL, SUPABASE_KEY)

st.set_page_config(
    page_title="GK & GS Smart Practice Portal",
    page_icon="✨",
    layout="wide"
)

# 1. సెషన్ స్టేట్ ఇనీషియలైజేషన్
if "theme_color" not in st.session_state:
    st.session_state["theme_color"] = "Green"
if "dark_mode" not in st.session_state:
    st.session_state["dark_mode"] = False
if "visible_count" not in st.session_state:
    st.session_state["visible_count"] = 10
if "search_query" not in st.session_state:
    st.session_state["search_query"] = ""
if "active_exam_filter" not in st.session_state:
    st.session_state["active_exam_filter"] = "All Exams"

# 2. థీమ్ కలర్ పాలెట్ (VocabCraft Style)
PALETTES = {
    "Green": {"primary": "#10b981", "light_bg": "#f0fdf4", "border": "#a7f3d0", "btn_text": "#ffffff"},
    "Blue": {"primary": "#0ea5e9", "light_bg": "#f0f9ff", "border": "#bae6fd", "btn_text": "#ffffff"},
    "Yellow": {"primary": "#f59e0b", "light_bg": "#fffbeb", "border": "#fde68a", "btn_text": "#ffffff"},
    "Orange": {"primary": "#f97316", "light_bg": "#fff7ed", "border": "#fed7aa", "btn_text": "#ffffff"},
    "Pink": {"primary": "#ec4899", "light_bg": "#fdf2f8", "border": "#fbcfe8", "btn_text": "#ffffff"},
}

active_palette = PALETTES[st.session_state["theme_color"]]
is_dark = st.session_state["dark_mode"]

# డైనమిక్ బ్యాక్‌గ్రౌండ్ & టెక్స్ట్ రంగులు
bg_color = "#0f172a" if is_dark else "#f8fafc"
card_bg = "#1e293b" if is_dark else "#ffffff"
text_color = "#f1f5f9" if is_dark else "#1e293b"
subtext_color = "#94a3b8" if is_dark else "#64748b"
card_border = "#334155" if is_dark else "#e2e8f0"
option_bg = "#334155" if is_dark else "#f1f5f9"

# 3. కస్టమ్ CSS ఇంజెక్షన్
st.markdown(f"""
<style>
    /* App Background */
    .stApp {{
        background-color: {bg_color};
        color: {text_color};
        font-family: 'Segoe UI', Roboto, sans-serif;
    }}
    
    /* Hide Default Header & Margins for Clean Look */
    header[data-testid="stHeader"] {{
        background: transparent;
    }}
    
    /* Top Navigation Bar */
    .top-navbar {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 10px 0 20px 0;
        border-bottom: 1px solid {card_border};
        margin-bottom: 25px;
    }}
    
    /* Card Design */
    .q-card {{
        background-color: {card_bg};
        border: 1px solid {card_border};
        border-radius: 18px;
        padding: 22px;
        margin-bottom: 20px;
        box-shadow: 0 4px 15px rgba(0, 0, 0, {"0.2" if is_dark else "0.04"});
    }}
    
    /* Question Title */
    .q-text {{
        font-size: 17px;
        font-weight: 700;
        color: {text_color};
        line-height: 1.5;
        margin-bottom: 14px;
    }}
    
    /* Badges */
    .badge {{
        display: inline-block;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 12px;
        font-weight: 600;
        margin-right: 6px;
        margin-bottom: 6px;
    }}
    .badge-state {{ background: rgba(14, 165, 233, 0.15); color: #38bdf8; border: 1px solid rgba(14, 165, 233, 0.3); }}
    .badge-exam {{ background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }}
    .badge-date {{ background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }}
    .badge-shift {{ background: rgba(236, 72, 153, 0.15); color: #f472b6; border: 1px solid rgba(236, 72, 153, 0.3); }}

    /* Options Styling */
    .opt-box {{
        background-color: {option_bg};
        border: 1px solid {card_border};
        border-radius: 12px;
        padding: 10px 14px;
        margin: 8px 0;
        font-size: 14px;
        color: {text_color};
    }}
    .opt-key {{
        font-weight: 700;
        color: {active_palette['primary']};
        margin-right: 8px;
    }}
    
    /* Found Banner */
    .found-pill {{
        display: inline-block;
        background-color: {active_palette['light_bg'] if not is_dark else '#1e293b'};
        color: {active_palette['primary']};
        border: 1px solid {active_palette['border'] if not is_dark else '#334155'};
        padding: 8px 18px;
        border-radius: 30px;
        font-weight: 700;
        font-size: 14px;
        margin-bottom: 20px;
    }}
    
    /* Bottom padding for fixed chat input */
    .main-content {{
        padding-bottom: 90px;
    }}
</style>
""", unsafe_allow_html=True)

# 4. ఎగ్జామ్ స్ట్రక్చర్ (Central Boards & State Exams)
EXAM_CATEGORIES = {
    "Central Exams": {
        "SSC": ["All SSC Exams", "SSC CGL", "SSC CHSL", "SSC MTS", "SSC CPO", "SSC GD"],
        "RRB": ["All Railway Exams", "RRB NTPC", "RRB Group D", "RRB ALP", "RRB JE"],
        "UPSC": ["All UPSC Exams", "UPSC CSE", "UPSC CDS", "UPSC NDA", "UPSC CAPF"],
        "Banking": ["All Banking Exams", "SBI PO", "SBI Clerk", "IBPS PO", "IBPS Clerk"]
    },
    "State Exams": {
        "Telangana": ["All Telangana Exams", "TSPSC Group 1", "TSPSC Group 2", "TSPSC Group 4", "TS Police SI", "TS Police Constable"],
        "Andhra Pradesh": ["All AP Exams", "APPSC Group 1", "APPSC Group 2", "AP Police SI", "AP Police Constable"],
        "Tamil Nadu": ["All Tamil Nadu Exams", "TNPSC Group 1", "TNPSC Group 2", "TNPSC Group 4"],
        "Karnataka": ["All Karnataka Exams", "KPSC KAS", "Karnataka Police SI"]
    }
}

# 5. డేటాబేస్ సెర్చ్ ఫంక్షన్
def search_questions(search_term: str, exam_filter: str, limit: int = 10):
    try:
        client = get_supabase_client()
        if not client:
            return [], 0
        
        response = client.rpc(
            "search_gk_portal",
            {
                "search_term": search_term.strip().lower(),
                "exam_filter": exam_filter if exam_filter else "All Exams",
                "page_limit": limit,
                "page_offset": 0
            }
        ).execute()
        
        data = response.data or []
        total_count = data[0]["total_count"] if data else 0
        return data, total_count
    except Exception as e:
        st.error(f"Search Error: {e}")
        return [], 0

# 6. టాప్ బార్ (Title + VocabCraft Themes + Filter Popover + Dark Mode)
c_head, c_filter, c_theme, c_mode = st.columns([4.5, 2, 2.5, 1])

with c_head:
    st.markdown(f"<h2 style='margin:0; color:{active_palette['primary']}; font-weight:800;'>🎯 GK Portal</h2>", unsafe_allow_html=True)
    st.caption("VocabCraft Edition • Smart Multilingual Search")

with c_filter:
    with st.popover(f"🎛️ Filter: {st.session_state['active_exam_filter'][:14]}...", use_container_width=True):
        st.markdown("**Select Board / State Exam**")
        cat_type = st.radio("Category", ["All Exams", "Central Exams", "State Exams"], horizontal=True)
        
        selected_filter_val = "All Exams"
        if cat_type == "Central Exams":
            board = st.selectbox("Select Board", list(EXAM_CATEGORIES["Central Exams"].keys()))
            sub_exam = st.selectbox("Select Exam", EXAM_CATEGORIES["Central Exams"][board])
            selected_filter_val = board if "All" in sub_exam else sub_exam
        elif cat_type == "State Exams":
            st_name = st.selectbox("Select State", list(EXAM_CATEGORIES["State Exams"].keys()))
            st_exam = st.selectbox("Select Exam", EXAM_CATEGORIES["State Exams"][st_name])
            selected_filter_val = st_name if "All" in st_exam else st_exam
            
        if st.button("Apply Filter", use_container_width=True, type="primary"):
            st.session_state["active_exam_filter"] = selected_filter_val
            st.session_state["visible_count"] = 10
            st.rerun()

with c_theme:
    selected_theme = st.selectbox(
        "Theme Color", 
        list(PALETTES.keys()), 
        index=list(PALETTES.keys()).index(st.session_state["theme_color"]),
        label_visibility="collapsed"
    )
    if selected_theme != st.session_state["theme_color"]:
        st.session_state["theme_color"] = selected_theme
        st.rerun()

with c_mode:
    mode_toggle = st.toggle("🌙 Dark", value=st.session_state["dark_mode"])
    if mode_toggle != st.session_state["dark_mode"]:
        st.session_state["dark_mode"] = mode_toggle
        st.rerun()

st.divider()

# 7. డేటా ఫెచింగ్
questions, total_found = search_questions(
    search_term=st.session_state["search_query"],
    exam_filter=st.session_state["active_exam_filter"],
    limit=st.session_state["visible_count"]
)

# టోటల్ క్వశ్చన్స్ బ్యానర్
search_label = f" for '{st.session_state['search_query']}'" if st.session_state['search_query'] else ""
filter_label = f" • {st.session_state['active_exam_filter']}" if st.session_state['active_exam_filter'] != "All Exams" else ""
st.markdown(
    f'<div class="found-pill">✨ Total Questions Found: {total_found}{search_label}{filter_label}</div>', 
    unsafe_allow_html=True
)

# 8. ప్రశ్నల ప్రదర్శన (Scrollable Container)
st.markdown('<div class="main-content">', unsafe_allow_html=True)

if not questions:
    st.info("💡 ఎలాంటి ప్రశ్నలు కనుగొనబడలేదు. కింద ఉన్న సెర్చ్ బార్‌లో పదం టైప్ చేయండి.")
else:
    for idx, item in enumerate(questions):
        q_data = item.get("question", {}) or {}
        opts_data = item.get("options", {}) or {}
        expl_data = item.get("explanation", {}) or {}
        ans = item.get("correct_answer", "")
        
        exam_name = item.get("exam_name", "General Exam")
        state_name = item.get("state", "Central") or "Central"
        date_str = item.get("date", "") or "PYQ"
        shift_str = item.get("shift", "")

        with st.container():
            st.markdown(f"""
            <div class="q-card">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:8px;">
                    <div class="q-text">Q{idx+1}. {q_data.get('en', '')}</div>
                    <div>
                        <span class="badge badge-state">📍 {state_name}</span>
                        <span class="badge badge-exam">🏛️ {exam_name}</span>
                        <span class="badge badge-date">📅 {date_str}</span>
                        {f'<span class="badge badge-shift">⏰ {shift_str}</span>' if shift_str else ''}
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            t_en, t_te, t_hi = st.tabs(["🇬🇧 English", "🇮🇳 తెలుగు", "🇮🇳 हिंदी"])
            with t_en:
                en_opts = opts_data.get("en", {})
                for k in sorted(en_opts.keys()):
                    st.markdown(f'<div class="opt-box"><span class="opt-key">({k})</span> {en_opts[k]}</div>', unsafe_allow_html=True)
            with t_te:
                st.markdown(f"**{q_data.get('te', '')}**")
                te_opts = opts_data.get("te", {})
                for k in sorted(te_opts.keys()):
                    st.markdown(f'<div class="opt-box"><span class="opt-key">({k})</span> {te_opts[k]}</div>', unsafe_allow_html=True)
            with t_hi:
                st.markdown(f"**{q_data.get('hi', '')}**")
                hi_opts = opts_data.get("hi", {})
                for k in sorted(hi_opts.keys()):
                    st.markdown(f'<div class="opt-box"><span class="opt-key">({k})</span> {hi_opts[k]}</div>', unsafe_allow_html=True)

            with st.expander("💡 View Answer & Explanation"):
                st.success(f"✅ **Correct Answer: Option ({ans})**")
                e_tab1, e_tab2, e_tab3 = st.tabs(["English Expl.", "తెలుగు వివరణ", "हिंदी व्याख्या"])
                with e_tab1:
                    st.write(expl_data.get("en", "No explanation available."))
                with e_tab2:
                    st.write(expl_data.get("te", "వివరణ అందుబాటులో లేదు."))
                with e_tab3:
                    st.write(expl_data.get("hi", "व्याख्या उपलब्ध नहीं है।"))

# 9. లోడ్‌మోర్ బటన్ (Load More 10 by 10)
if len(questions) < total_found:
    st.markdown("<br>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns([2, 3, 2])
    with c2:
        if st.button(f"🔽 Load More ({len(questions)} of {total_found})", use_container_width=True):
            st.session_state["visible_count"] += 10
            st.rerun()

st.markdown('</div>', unsafe_allow_html=True)

# 10. Gemini AI Pinned Bottom Search Bar (Fixed at the very bottom)
user_prompt = st.chat_input("Ask or search anything (e.g. Sardar Patel, Article 21, Sabarmati)...")
if user_prompt:
    st.session_state["search_query"] = user_prompt.strip()
    st.session_state["visible_count"] = 10
    st.rerun()