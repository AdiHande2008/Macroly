import json
import re
import hashlib
import base64
from google import genai
from google.genai import types
import streamlit as st
from prompts import SUMMARY_REQUEST_PROMPT, WELCOME_MESSAGE_TEMPLATE, SYSTEM_PROMPT
from twilio.rest import Client as TwilioClient

# -----------------------------------------------------------------------------
# 1. PAGE CONFIGURATION & LOGO ASSETS
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Macroly • Intelligent Nutrition & Wellness",
    page_icon="assets/logo_icon_96.png",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data
def load_logo_base64(path):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return ""


LOGO_LOCKUP_B64 = load_logo_base64("assets/brand_lockup.png")
LOGO_ICON_B64 = load_logo_base64("assets/logo_icon_96.png")

# -----------------------------------------------------------------------------
# 2. SECRETS & CLIENTS
# -----------------------------------------------------------------------------
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
TWILIO_ACCOUNT_SID = st.secrets["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = st.secrets["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_FROM = st.secrets["TWILIO_WHATSAPP_FROM"]
TWILIO_CONTENT_SID = st.secrets["TWILIO_CONTENT_SID"]


@st.cache_resource
def get_gemini_client():
    return genai.Client(api_key=GEMINI_API_KEY)


@st.cache_resource
def get_twilio_client():
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


gemini_client = get_gemini_client()
twilio_client = get_twilio_client()

MODEL_NAME = "gemini-3.5-flash-lite"


# -----------------------------------------------------------------------------
# 3. BACKEND & NUTRITION ENGINE
# -----------------------------------------------------------------------------
def ask_gemini(parts):
    try:
        return st.session_state.chat.send_message(parts).text
    except Exception as error:
        return f"Sorry, something went wrong: {error}"


def clean_whatsapp_text(text):
    if not text:
        return "No nutrition summary available."
    text = " ".join(text.split())
    return text[:1500] + "..." if len(text) > 1500 else text


def send_whatsapp(to_number, user_name, summary):
    try:
        content_variables = json.dumps(
            {"1": user_name, "2": clean_whatsapp_text(summary)}, ensure_ascii=False
        )
        message = twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:{to_number}",
            content_sid=TWILIO_CONTENT_SID,
            content_variables=content_variables,
        )
        return True, message.sid
    except Exception as error:
        return False, str(error)


def parse_macro_num(val_str):
    if not val_str:
        return 0.0
    val_str = str(val_str).replace("~", "").replace("kcal", "").replace("g", "").strip()
    if "–" in val_str:
        parts = val_str.split("–")
    elif "-" in val_str:
        parts = val_str.split("-")
    else:
        parts = [val_str]
    nums = []
    for p in parts:
        clean = re.sub(r"[^\d.]", "", p)
        if clean:
            try:
                nums.append(float(clean))
            except ValueError:
                pass
    return sum(nums) / len(nums) if nums else 0.0


def parse_meal_response(text):
    if not text or not isinstance(text, str):
        return None

    has_cal = bool(re.search(r'Calories:\s*~?[0-9]+', text, re.IGNORECASE))
    has_total = bool(re.search(r'TOTAL:', text, re.IGNORECASE))
    has_macros = bool(re.search(r'Protein:\s*~?[0-9]+', text, re.IGNORECASE))
    is_summary = (
        "Daily Summary" in text
        or "FitBuddy Daily Summary" in text
        or "Macroly Daily Summary" in text
    )

    if not (has_cal or (has_total and has_macros)) or is_summary:
        return None

    data = {}

    # Calories
    cal_m = re.search(
        r'(?:TOTAL:[\s\S]*?)?Calories:\s*~?([0-9]+(?:[\s–\-]*(?:to)?[0-9]+)?)\s*kcal',
        text,
        re.IGNORECASE,
    )
    if not cal_m:
        cal_m = re.search(
            r'Calories:\s*~?([0-9]+(?:[\s–\-]*(?:to)?[0-9]+)?)', text, re.IGNORECASE
        )
    if cal_m:
        data['calories'] = cal_m.group(1).strip()

    # Protein
    prot_m = re.search(
        r'Protein:\s*~?([0-9]+(?:[\s–\-]*(?:to)?[0-9]+)?)\s*g', text, re.IGNORECASE
    )
    if prot_m:
        data['protein'] = prot_m.group(1).strip()

    # Carbs
    carb_m = re.search(
        r'Carbs:\s*~?([0-9]+(?:[\s–\-]*(?:to)?[0-9]+)?)\s*g', text, re.IGNORECASE
    )
    if carb_m:
        data['carbs'] = carb_m.group(1).strip()

    # Fat
    fat_m = re.search(
        r'Fat:\s*~?([0-9]+(?:[\s–\-]*(?:to)?[0-9]+)?)\s*g', text, re.IGNORECASE
    )
    if fat_m:
        data['fat'] = fat_m.group(1).strip()

    # Fiber
    fib_m = re.search(
        r'Fiber:\s*~?([0-9]+(?:[\s–\-]*(?:to)?[0-9]+)?)\s*g', text, re.IGNORECASE
    )
    if fib_m:
        data['fiber'] = fib_m.group(1).strip()

    # Confidence
    conf_m = re.search(r'Confidence:\s*([^\n\r]+)', text, re.IGNORECASE)
    if conf_m:
        data['confidence'] = conf_m.group(1).strip().strip('*_`')
    else:
        data['confidence'] = "Moderate"

    # Uncertainty
    unc_m = re.search(
        r'(?:Biggest uncertainty|Uncertainty):\s*([^\n\r]+)', text, re.IGNORECASE
    )
    if unc_m:
        data['uncertainty'] = unc_m.group(1).strip().strip('*_`')

    # Trainer tip / Coach's Take
    tip_m = re.search(
        r'(?:TRAINER TIP|Coach\'s Take|Coach Take):\s*([\s\S]*?)(?=\n\s*(?:[A-Z\?]|Was|Did|How|\Z))',
        text,
        re.IGNORECASE,
    )
    if tip_m:
        data['trainer_tip'] = tip_m.group(1).strip()

    # Food items from WHAT I SEE
    see_m = re.search(r'WHAT I SEE\s*([\s\S]*?)(?=\n\s*[🔥\w]|\Z)', text, re.IGNORECASE)
    if see_m:
        items = [
            line.strip().lstrip('•-* ').strip()
            for line in see_m.group(1).strip().splitlines()
            if line.strip()
        ]
        data['foods'] = [item for item in items if item]

    # Clarification question
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if lines and lines[-1].endswith('?'):
        data['question'] = lines[-1]

    return data


def calculate_daily_totals(messages):
    totals = {
        "calories": 0.0,
        "protein": 0.0,
        "carbs": 0.0,
        "fat": 0.0,
        "fiber": 0.0,
        "meal_count": 0,
    }
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("kind") == "text":
            data = parse_meal_response(msg.get("content", ""))
            if data and data.get("calories"):
                totals["calories"] += parse_macro_num(data.get("calories"))
                totals["protein"] += parse_macro_num(data.get("protein", "0"))
                totals["carbs"] += parse_macro_num(data.get("carbs", "0"))
                totals["fat"] += parse_macro_num(data.get("fat", "0"))
                totals["fiber"] += parse_macro_num(data.get("fiber", "0"))
                totals["meal_count"] += 1
    return totals


# -----------------------------------------------------------------------------
# 4. BRIGHT PREMIUM WELLNESS DESIGN SYSTEM & CSS
# -----------------------------------------------------------------------------
def inject_custom_css():
    st.html(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

        :root {
            --bg-base: #F7FAF7;
            --bg-sidebar: #FFFFFF;
            --bg-surface: #FFFFFF;
            --bg-surface-elevated: #F7FAF7;
            --bg-surface-hover: #F0F6F2;
            --bg-mint: #E5F3E9;
            --bg-lime: #E9F5DB;
            --border-subtle: #E2EAE3;
            --border-mint: #B7E4C7;
            --border-focus: #198754;
            --text-primary: #253B30;
            --text-secondary: #66756B;
            --text-muted: #8C9E93;
            --accent-primary: #198754;
            --accent-hover: #157347;
            --accent-secondary: #B7E4C7;
            --accent-muted: rgba(25, 135, 84, 0.08);
            --accent-border: rgba(25, 135, 84, 0.22);
            --accent-orange: #F4A261;
            --semantic-warning-bg: #FFF7ED;
            --semantic-warning-border: #FDBA74;
            --semantic-warning-text: #9A3412;
            --semantic-danger-bg: #FEF2F2;
            --semantic-danger-border: #FECACA;
            --semantic-danger-text: #B91C1C;
            --shadow-card: 0 1px 3px rgba(37, 59, 48, 0.04), 0 1px 2px rgba(37, 59, 48, 0.02);
            --shadow-hover: 0 4px 12px rgba(37, 59, 48, 0.06);
        }

        /* Full App Canvas Reset */
        html, body, [class*="css"] {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
            letter-spacing: -0.01em;
        }

        .stApp {
            background-color: var(--bg-base) !important;
            background-image: none !important;
            color: var(--text-primary) !important;
        }

        header[data-testid="stHeader"] {
            background: rgba(247, 250, 247, 0.88) !important;
            backdrop-filter: blur(8px) !important;
            border-bottom: 1px solid var(--border-subtle) !important;
        }

        /* Sidebar Styling */
        [data-testid="stSidebar"] {
            background-color: var(--bg-sidebar) !important;
            border-right: 1px solid var(--border-subtle) !important;
            box-shadow: 1px 0 3px rgba(37, 59, 48, 0.02) !important;
        }

        [data-testid="stSidebarContent"] {
            padding: 1.25rem 1rem !important;
            background-color: var(--bg-sidebar) !important;
        }

        /* Streamlit Form Input Overrides */
        .stTextInput > div > div > input, 
        .stSelectbox > div > div,
        .stNumberInput > div > div > input {
            background-color: var(--bg-surface) !important;
            border: 1px solid var(--border-subtle) !important;
            border-radius: 8px !important;
            color: var(--text-primary) !important;
            font-size: 0.88rem !important;
            transition: border-color 0.15s ease, box-shadow 0.15s ease !important;
            box-shadow: 0 1px 2px rgba(37, 59, 48, 0.02) !important;
        }

        .stTextInput > div > div > input:focus, 
        .stSelectbox > div > div:focus-within,
        .stNumberInput > div > div > input:focus {
            border-color: var(--accent-primary) !important;
            box-shadow: 0 0 0 2px rgba(25, 135, 84, 0.15) !important;
        }

        .stTextInput label, .stSelectbox label, .stNumberInput label {
            color: var(--text-secondary) !important;
            font-weight: 500 !important;
            font-size: 0.78rem !important;
            text-transform: uppercase !important;
            letter-spacing: 0.04em !important;
            margin-bottom: 0.35rem !important;
        }

        /* Buttons */
        .stButton > button {
            background: var(--bg-surface) !important;
            color: var(--text-primary) !important;
            border: 1px solid var(--border-subtle) !important;
            border-radius: 8px !important;
            font-weight: 500 !important;
            font-size: 0.84rem !important;
            padding: 0.45rem 0.95rem !important;
            transition: all 0.15s ease !important;
            box-shadow: var(--shadow-card) !important;
        }

        .stButton > button:hover {
            background: var(--bg-mint) !important;
            border-color: var(--border-mint) !important;
            color: var(--accent-primary) !important;
            transform: none !important;
        }

        button[kind="primaryFormSubmit"], 
        button[data-testid="baseButton-primary"] {
            background: var(--accent-primary) !important;
            color: #FFFFFF !important;
            font-weight: 600 !important;
            border: 1px solid var(--accent-primary) !important;
            box-shadow: 0 2px 4px rgba(25, 135, 84, 0.2) !important;
        }

        button[kind="primaryFormSubmit"]:hover, 
        button[data-testid="baseButton-primary"]:hover {
            background: var(--accent-hover) !important;
            color: #FFFFFF !important;
            border-color: var(--accent-hover) !important;
            transform: none !important;
            box-shadow: 0 3px 6px rgba(25, 135, 84, 0.28) !important;
        }

        /* Bottom Container & Fixed Bar */
        [data-testid="stBottom"],
        [data-testid="stBottom"] > div,
        [data-testid="stBottomBlockContainer"],
        .stChatFloatingInputContainer {
            background-color: var(--bg-base) !important;
            background: var(--bg-base) !important;
        }

        footer {
            display: none !important;
        }

        /* Chat Input Component */
        [data-testid="stChatInput"] {
            background-color: var(--bg-surface) !important;
            background: #FFFFFF !important;
            border: 1px solid var(--border-subtle) !important;
            border-radius: 10px !important;
            box-shadow: 0 4px 14px rgba(37, 59, 48, 0.06) !important;
            padding: 0.25rem 0.5rem !important;
        }

        [data-testid="stChatInput"] > div {
            background-color: transparent !important;
            background: transparent !important;
            border: none !important;
        }

        [data-testid="stChatInput"]:focus-within {
            border-color: var(--accent-primary) !important;
            box-shadow: 0 0 0 2px rgba(25, 135, 84, 0.15) !important;
        }

        [data-testid="stChatInput"] textarea {
            background-color: transparent !important;
            background: transparent !important;
            color: var(--text-primary) !important;
            font-size: 0.9rem !important;
        }

        [data-testid="stChatInput"] textarea::placeholder {
            color: var(--text-muted) !important;
        }

        [data-testid="stChatInput"] button {
            color: var(--accent-primary) !important;
            background: transparent !important;
            border: none !important;
        }

        /* File Uploader Dropzone */
        [data-testid="stFileUploaderDropzone"] {
            background: var(--bg-surface) !important;
            border: 1.5px dashed var(--border-mint) !important;
            border-radius: 10px !important;
            color: var(--text-secondary) !important;
            padding: 1.1rem !important;
            transition: border-color 0.2s ease, background 0.2s ease !important;
        }

        [data-testid="stFileUploaderDropzone"]:hover {
            border-color: var(--accent-primary) !important;
            background: var(--bg-mint) !important;
        }

        [data-testid="stCameraInput"] {
            border-radius: 10px !important;
            overflow: hidden !important;
        }

        /* Expanders */
        [data-testid="stExpander"] {
            background: var(--bg-surface) !important;
            border: 1px solid var(--border-subtle) !important;
            border-radius: 8px !important;
            box-shadow: var(--shadow-card) !important;
        }

        [data-testid="stExpander"] details {
            border: none !important;
        }

        [data-testid="stExpander"] summary {
            color: var(--text-secondary) !important;
            font-size: 0.84rem !important;
            font-weight: 500 !important;
        }

        /* Chat Messages Container */
        .stChatMessage {
            background-color: var(--bg-surface) !important;
            border: 1px solid var(--border-subtle) !important;
            border-radius: 10px !important;
            box-shadow: var(--shadow-card) !important;
            color: var(--text-primary) !important;
            margin-bottom: 0.75rem !important;
        }

        [data-testid="stChatMessageContent"] {
            color: var(--text-primary) !important;
        }

        /* -------------------------------------------------------------
           Macroly Custom Components System (Fresh Wellness Theme)
           ------------------------------------------------------------- */
        .ms-sidebar-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 1.15rem;
            padding-bottom: 0.85rem;
            border-bottom: 1px solid var(--border-subtle);
        }

        .ms-brand-group {
            display: flex;
            align-items: center;
            gap: 0.6rem;
        }

        .ms-sidebar-brand-img {
            height: 32px;
            width: auto;
            max-width: 165px;
            object-fit: contain;
            display: block;
        }

        .ms-logo-icon {
            width: 28px;
            height: 28px;
            background: var(--bg-mint);
            border: 1px solid var(--border-mint);
            border-radius: 7px;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .ms-brand-name {
            font-size: 1.08rem;
            font-weight: 700;
            color: var(--text-primary);
            letter-spacing: -0.02em;
        }

        .ms-status-chip {
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            font-size: 0.72rem;
            font-weight: 500;
            color: var(--accent-primary);
            background: var(--bg-mint);
            border: 1px solid var(--border-mint);
            padding: 0.2rem 0.55rem;
            border-radius: 6px;
        }

        .ms-status-dot {
            width: 6px;
            height: 6px;
            background-color: var(--accent-primary);
            border-radius: 50%;
        }

        .ms-card {
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: 10px;
            padding: 1.1rem;
            margin-bottom: 0.85rem;
            box-shadow: var(--shadow-card);
        }

        .ms-card-header {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            margin-bottom: 0.75rem;
        }

        .ms-card-title {
            font-size: 0.72rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-muted);
        }

        .ms-card-subtitle {
            font-size: 0.76rem;
            color: var(--text-secondary);
        }

        .ms-profile-card {
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: 10px;
            padding: 0.85rem 1rem;
            margin-bottom: 0.85rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.6rem;
            box-shadow: var(--shadow-card);
        }

        .ms-profile-left {
            display: flex;
            align-items: center;
            gap: 0.65rem;
        }

        .ms-profile-avatar {
            width: 32px;
            height: 32px;
            background: var(--bg-mint);
            border: 1px solid var(--border-mint);
            color: var(--accent-primary);
            font-size: 0.82rem;
            font-weight: 700;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .ms-profile-name {
            font-size: 0.88rem;
            font-weight: 600;
            color: var(--text-primary);
            line-height: 1.2;
        }

        .ms-profile-phone {
            font-size: 0.72rem;
            color: var(--text-muted);
        }

        .ms-profile-tag {
            font-size: 0.68rem;
            font-weight: 600;
            background: var(--bg-lime);
            border: 1px solid var(--border-subtle);
            color: var(--text-primary);
            padding: 0.2rem 0.55rem;
            border-radius: 5px;
            white-space: nowrap;
        }

        /* Progress Components */
        .ms-progress-group {
            margin-bottom: 0.75rem;
        }

        .ms-progress-meta {
            display: flex;
            justify-content: space-between;
            font-size: 0.78rem;
            margin-bottom: 0.3rem;
            color: var(--text-secondary);
        }

        .ms-progress-val {
            font-weight: 600;
            color: var(--text-primary);
        }

        .ms-progress-track {
            background: #EAEFEA;
            height: 5px;
            border-radius: 4px;
            overflow: hidden;
        }

        .ms-progress-fill {
            background: var(--accent-primary);
            height: 100%;
            border-radius: 4px;
        }

        .ms-progress-fill-cal {
            background: var(--accent-orange);
            height: 100%;
            border-radius: 4px;
        }

        .ms-macro-micro-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 0.5rem;
            border-top: 1px solid var(--border-subtle);
            padding-top: 0.7rem;
            margin-top: 0.75rem;
        }

        .ms-macro-micro-item {
            text-align: center;
        }

        .ms-macro-micro-label {
            font-size: 0.68rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        .ms-macro-micro-val {
            font-size: 0.88rem;
            font-weight: 600;
            color: var(--text-primary);
            margin-top: 0.15rem;
        }

        /* WhatsApp Sync Section (Pale green surface) */
        .ms-whatsapp-box {
            background: var(--bg-mint);
            border: 1px solid var(--border-mint);
            border-radius: 10px;
            padding: 1.05rem;
            margin-bottom: 0.85rem;
        }

        .ms-whatsapp-title-row {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            font-size: 0.84rem;
            font-weight: 600;
            color: var(--accent-primary);
            margin-bottom: 0.25rem;
        }

        .ms-whatsapp-desc {
            font-size: 0.76rem;
            color: var(--text-secondary);
            line-height: 1.45;
        }

        /* Main Dashboard Header */
        .ms-top-header {
            margin-bottom: 1.15rem;
            padding-bottom: 0.95rem;
            border-bottom: 1px solid var(--border-subtle);
        }

        .ms-top-title-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 0.6rem;
            margin-bottom: 0.35rem;
        }

        .ms-page-title {
            font-size: 1.45rem;
            font-weight: 700;
            letter-spacing: -0.025em;
            color: var(--text-primary);
            margin: 0;
            line-height: 1.2;
        }

        .ms-page-desc {
            font-size: 0.86rem;
            color: var(--text-secondary);
            margin: 0;
            line-height: 1.45;
        }

        /* Daily Overview Metrics Bar (White cards with elegant green progress) */
        .ms-overview-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 0.75rem;
            margin-bottom: 1.15rem;
        }

        @media (max-width: 900px) {
            .ms-overview-grid {
                grid-template-columns: repeat(2, 1fr);
            }
        }

        .ms-metric-card {
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: 10px;
            padding: 0.95rem 1.1rem;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            box-shadow: var(--shadow-card);
            transition: box-shadow 0.15s ease, border-color 0.15s ease;
        }

        .ms-metric-card:hover {
            box-shadow: var(--shadow-hover);
            border-color: var(--border-mint);
        }

        .ms-metric-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.4rem;
        }

        .ms-metric-label {
            font-size: 0.72rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-muted);
        }

        .ms-metric-val {
            font-size: 1.4rem;
            font-weight: 700;
            color: var(--text-primary);
            letter-spacing: -0.02em;
            line-height: 1.15;
            margin-bottom: 0.25rem;
        }

        .ms-metric-unit {
            font-size: 0.8rem;
            font-weight: 500;
            color: var(--text-secondary);
        }

        .ms-metric-sub {
            font-size: 0.74rem;
            color: var(--text-secondary);
            margin-top: 0.35rem;
        }

        .ms-split-val {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 0.25rem;
        }

        .ms-split-num {
            font-size: 1.15rem;
            font-weight: 700;
            color: var(--text-primary);
        }

        .ms-split-lbl {
            font-size: 0.72rem;
            color: var(--text-muted);
            margin-left: 0.2rem;
        }

        /* Food Photo Upload Area (Soft mint background with green dashed border) */
        .ms-upload-container {
            background: var(--bg-mint);
            border: 1.5px dashed var(--border-mint);
            border-radius: 12px;
            padding: 1.15rem 1.35rem 0.85rem 1.35rem;
            margin-bottom: 1.15rem;
            box-shadow: var(--shadow-card);
        }

        .ms-upload-header {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 0.75rem;
        }

        .ms-upload-icon-circle {
            width: 34px;
            height: 34px;
            background: #FFFFFF;
            border: 1px solid var(--border-mint);
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .ms-upload-title {
            font-size: 0.95rem;
            font-weight: 600;
            color: var(--text-primary);
            line-height: 1.2;
        }

        .ms-upload-sub {
            font-size: 0.78rem;
            color: var(--text-secondary);
        }

        /* Meal Analysis Result Card */
        .ms-meal-card {
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: 12px;
            padding: 1.25rem;
            margin: 0.75rem 0;
            box-shadow: var(--shadow-card);
        }

        .ms-meal-top-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 0.85rem;
            border-bottom: 1px solid var(--border-subtle);
            margin-bottom: 1rem;
            flex-wrap: wrap;
            gap: 0.5rem;
        }

        .ms-meal-title-badge {
            font-size: 0.74rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            gap: 0.45rem;
        }

        .ms-conf-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            font-size: 0.72rem;
            font-weight: 500;
            padding: 0.2rem 0.6rem;
            border-radius: 6px;
        }

        .ms-conf-high {
            background: var(--bg-mint);
            border: 1px solid var(--border-mint);
            color: var(--accent-primary);
        }

        .ms-conf-moderate {
            background: var(--semantic-warning-bg);
            border: 1px solid var(--semantic-warning-border);
            color: var(--semantic-warning-text);
        }

        .ms-conf-low {
            background: var(--semantic-danger-bg);
            border: 1px solid var(--semantic-danger-border);
            color: var(--semantic-danger-text);
        }

        .ms-calorie-banner {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--bg-mint);
            border: 1px solid var(--border-mint);
            border-radius: 8px;
            padding: 0.85rem 1.15rem;
            margin-bottom: 0.85rem;
        }

        .ms-cal-label {
            font-size: 0.72rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-secondary);
        }

        .ms-cal-val {
            font-size: 1.65rem;
            font-weight: 700;
            color: var(--text-primary);
            line-height: 1;
        }

        .ms-cal-unit {
            font-size: 0.86rem;
            font-weight: 500;
            color: var(--text-secondary);
        }

        .ms-macro-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 0.5rem;
            margin-bottom: 0.85rem;
        }

        .ms-macro-box {
            background: var(--bg-base);
            border: 1px solid var(--border-subtle);
            border-radius: 8px;
            padding: 0.7rem 0.85rem;
        }

        .ms-macro-box-title {
            font-size: 0.68rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        .ms-macro-box-num {
            font-size: 1.05rem;
            font-weight: 600;
            color: var(--text-primary);
            margin-top: 0.15rem;
        }

        .ms-tags-wrapper {
            display: flex;
            flex-wrap: wrap;
            gap: 0.4rem;
            margin-top: 0.4rem;
        }

        .ms-food-tag {
            background: var(--bg-lime);
            border: 1px solid var(--border-subtle);
            color: var(--text-primary);
            font-size: 0.76rem;
            padding: 0.25rem 0.65rem;
            border-radius: 6px;
        }

        .ms-callout {
            border-radius: 8px;
            padding: 0.75rem 0.95rem;
            margin-top: 0.65rem;
            font-size: 0.84rem;
            line-height: 1.45;
        }

        .ms-callout-uncertainty {
            background: var(--semantic-warning-bg);
            border: 1px solid var(--semantic-warning-border);
            color: var(--semantic-warning-text);
        }

        .ms-callout-coach {
            background: var(--bg-mint);
            border: 1px solid var(--border-mint);
            border-left: 3px solid var(--accent-primary);
            color: #14532D;
        }

        .ms-callout-title {
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.2rem;
            display: flex;
            align-items: center;
            gap: 0.35rem;
        }

        .ms-callout-question {
            background: var(--bg-base);
            border: 1px dashed var(--border-mint);
            border-radius: 8px;
            padding: 0.75rem 0.95rem;
            margin-top: 0.65rem;
        }

        .ms-q-title {
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-muted);
            margin-bottom: 0.25rem;
        }

        .ms-q-text {
            font-size: 0.88rem;
            font-weight: 500;
            color: var(--text-primary);
            line-height: 1.4;
        }

        .ms-q-hint {
            font-size: 0.72rem;
            color: var(--text-muted);
            margin-top: 0.25rem;
        }

        /* Empty State */
        .ms-empty-state {
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: 12px;
            padding: 2.25rem 1.5rem;
            text-align: center;
            margin: 1.25rem 0;
            box-shadow: var(--shadow-card);
        }

        .ms-empty-icon {
            width: 44px;
            height: 44px;
            background: var(--bg-mint);
            border: 1px solid var(--border-mint);
            border-radius: 10px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 0.85rem;
        }

        .ms-empty-title {
            font-size: 1.05rem;
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 0.35rem;
        }

        .ms-empty-desc {
            font-size: 0.84rem;
            color: var(--text-secondary);
            max-width: 440px;
            margin: 0 auto 1.15rem auto;
            line-height: 1.45;
        }

        .ms-empty-tags {
            display: flex;
            flex-wrap: wrap;
            justify-content: center;
            gap: 0.45rem;
        }

        .ms-tag {
            background: var(--bg-mint);
            border: 1px solid var(--border-mint);
            color: var(--text-primary);
            font-size: 0.74rem;
            padding: 0.25rem 0.65rem;
            border-radius: 6px;
        }

        /* Onboarding */
        .ms-onboarding-card {
            max-width: 480px;
            margin: 2.5rem auto;
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: 14px;
            padding: 2.2rem;
            box-shadow: 0 4px 20px rgba(37, 59, 48, 0.07);
        }

        .ms-onboarding-tag {
            font-size: 0.7rem;
            font-weight: 600;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: var(--accent-primary);
            margin-bottom: 0.4rem;
        }

        .ms-onboarding-title {
            font-size: 1.55rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            color: var(--text-primary);
            margin-bottom: 0.4rem;
            line-height: 1.2;
        }

        .ms-onboarding-sub {
            font-size: 0.88rem;
            color: var(--text-secondary);
            line-height: 1.5;
            margin-bottom: 1.35rem;
        }
        </style>
        """
    )


# -----------------------------------------------------------------------------
# 5. UI COMPONENT RENDERERS
# -----------------------------------------------------------------------------
def render_meal_html(data):
    cal = data.get('calories', '0')
    prot = data.get('protein', '0')
    carb = data.get('carbs', '0')
    fat = data.get('fat', '0')
    fib = data.get('fiber', '0')

    conf = data.get('confidence', 'Moderate')
    conf_class = 'ms-conf-moderate'
    conf_dot = '#F4A261'
    if 'high' in conf.lower():
        conf_class = 'ms-conf-high'
        conf_dot = '#198754'
    elif 'low' in conf.lower() or 'poor' in conf.lower():
        conf_class = 'ms-conf-low'
        conf_dot = '#E63946'

    foods_html = ''
    if data.get('foods'):
        tags_html = ''.join(
            [f'<span class="ms-food-tag">{f}</span>' for f in data['foods']]
        )
        foods_html = f'''<div style="margin-top: 0.75rem;">
            <div style="font-size: 0.68rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em;">Identified Food Items</div>
            <div class="ms-tags-wrapper">{tags_html}</div>
        </div>'''

    uncertainty_html = ''
    if data.get('uncertainty'):
        uncertainty_html = f'''<div class="ms-callout ms-callout-uncertainty">
            <div class="ms-callout-title">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                Key Assumptions & Uncertainty
            </div>
            <div>{data['uncertainty']}</div>
        </div>'''

    coach_html = ''
    if data.get('trainer_tip'):
        coach_html = f'''<div class="ms-callout ms-callout-coach">
            <div class="ms-callout-title">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                Coach Macroly's Take
            </div>
            <div>{data['trainer_tip']}</div>
        </div>'''

    question_html = ''
    if data.get('question'):
        question_html = f'''<div class="ms-callout-question">
            <div class="ms-q-title">Coach Asks</div>
            <div class="ms-q-text">{data['question']}</div>
            <div class="ms-q-hint">Tip: Reply in the chat composer below to refine this estimate.</div>
        </div>'''

    html = f'''<div class="ms-meal-card">
        <div class="ms-meal-top-row">
            <div class="ms-meal-title-badge">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#198754" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>
                <span>Estimated Nutrition Breakdown</span>
            </div>
            <div class="ms-conf-badge {conf_class}">
                <span style="width:6px; height:6px; border-radius:50%; background:{conf_dot}; display:inline-block;"></span>
                <span>{conf.capitalize()} Confidence</span>
            </div>
        </div>
        <div class="ms-calorie-banner">
            <div>
                <div class="ms-cal-label">Total Calories</div>
                <div style="font-size: 0.74rem; color: var(--text-secondary); margin-top: 0.15rem;">Visual portion approximation</div>
            </div>
            <div>
                <span class="ms-cal-val">~{cal}</span> <span class="ms-cal-unit">kcal</span>
            </div>
        </div>
        <div class="ms-macro-grid">
            <div class="ms-macro-box">
                <div class="ms-macro-box-title">Protein</div>
                <div class="ms-macro-box-num">~{prot}g</div>
            </div>
            <div class="ms-macro-box">
                <div class="ms-macro-box-title">Carbs</div>
                <div class="ms-macro-box-num">~{carb}g</div>
            </div>
            <div class="ms-macro-box">
                <div class="ms-macro-box-title">Fat</div>
                <div class="ms-macro-box-num">~{fat}g</div>
            </div>
            <div class="ms-macro-box">
                <div class="ms-macro-box-title">Fiber</div>
                <div class="ms-macro-box-num">~{fib}g</div>
            </div>
        </div>
        {foods_html}
        {uncertainty_html}
        {coach_html}
        {question_html}
    </div>'''
    return html


def render_message(message):
    avatar = "assets/logo_icon_96.png" if message["role"] == "assistant" else None
    with st.chat_message(message["role"], avatar=avatar):
        if message["kind"] == "text":
            content = message["content"]
            meal_data = parse_meal_response(content)
            if meal_data and meal_data.get("calories"):
                st.html(render_meal_html(meal_data))
                with st.expander("Detailed Analysis & Transcript", expanded=False):
                    st.markdown(content)
            elif (
                "Daily Summary" in content
                or "FitBuddy Daily Summary" in content
                or "Macroly Daily Summary" in content
            ):
                st.html(
                    f"""<div class="ms-card" style="border-left: 3px solid var(--accent-primary);">
                        <div style="font-size:0.72rem; font-weight:600; letter-spacing:0.05em; color:var(--accent-primary); text-transform:uppercase; margin-bottom:0.4rem;">
                            Daily Nutrition Briefing
                        </div>
                        <div style="line-height:1.55; font-size:0.9rem; color:var(--text-primary); white-space:pre-wrap;">{content}</div>
                    </div>"""
                )
            elif content.startswith("Sorry, something went wrong"):
                st.html(
                    f"""<div class="ms-card" style="border-left: 3px solid var(--semantic-danger-border); background: var(--semantic-danger-bg);">
                        <div style="font-weight:600; color:var(--semantic-danger-text); font-size:0.86rem; margin-bottom:0.25rem;">Analysis Notice</div>
                        <div style="font-size:0.84rem; color:var(--text-secondary);">{content}</div>
                    </div>"""
                )
            else:
                st.write(content)
        elif message["kind"] == "image":
            st.html(
                """<div style="font-size:0.72rem; font-weight:600; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.04em; margin-bottom:0.35rem;">
                    Logged Plate Photo
                </div>"""
            )
            st.image(message["content"], use_container_width=True)


def add_message(role, kind, content):
    st.session_state.messages.append({"role": role, "kind": kind, "content": content})
    render_message(st.session_state.messages[-1])


# -----------------------------------------------------------------------------
# 6. ONBOARDING FLOW
# -----------------------------------------------------------------------------
inject_custom_css()

if "onboarded" not in st.session_state:
    st.html(
        """<div class="ms-onboarding-card">
            <div class="ms-onboarding-tag">Macroly Setup</div>
            <div class="ms-onboarding-title">Your nutrition, simplified.</div>
            <div class="ms-onboarding-sub">Configure your profile and fitness goal to get personalized calorie and macro recommendations.</div>
        </div>"""
    )

    onboard_col1, onboard_col2, onboard_col3 = st.columns([1, 2, 1])
    with onboard_col2:
        with st.form("onboarding_form"):
            name = st.text_input("Full Name", placeholder="e.g. Aditya")
            whatsapp_number = st.text_input(
                "WhatsApp Number (with country code)",
                placeholder="+91XXXXXXXXXX",
                help="Your daily nutrition summaries will be sent directly to this number.",
            )
            goal = st.selectbox(
                "Primary Goal",
                [
                    "Muscle Gain",
                    "Fat Loss",
                    "Body Recomposition",
                    "Maintenance & Health",
                ],
                index=0,
            )
            submitted = st.form_submit_button(
                "Get Started with Macroly", use_container_width=True
            )

        if submitted:
            if not name.strip() or not whatsapp_number.strip():
                st.error("Please enter both your name and WhatsApp number to begin.")
            else:
                goal_targets = {
                    "Muscle Gain": {
                        "calories": 2400,
                        "protein": 140,
                        "carbs": 260,
                        "fat": 70,
                        "fiber": 30,
                    },
                    "Fat Loss": {
                        "calories": 1800,
                        "protein": 130,
                        "carbs": 170,
                        "fat": 55,
                        "fiber": 30,
                    },
                    "Body Recomposition": {
                        "calories": 2100,
                        "protein": 140,
                        "carbs": 220,
                        "fat": 65,
                        "fiber": 30,
                    },
                    "Maintenance & Health": {
                        "calories": 2000,
                        "protein": 110,
                        "carbs": 230,
                        "fat": 65,
                        "fiber": 28,
                    },
                }

                st.session_state.name = name.strip()
                st.session_state.whatsapp_number = whatsapp_number.strip()
                st.session_state.goal = goal
                st.session_state.targets = goal_targets.get(
                    goal,
                    {
                        "calories": 2000,
                        "protein": 130,
                        "carbs": 220,
                        "fat": 65,
                        "fiber": 28,
                    },
                )
                st.session_state.chat = gemini_client.chats.create(
                    model=MODEL_NAME,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT
                    ),
                )
                st.session_state.messages = []
                st.session_state.processed_hashes = set()
                st.session_state.onboarded = True
                st.rerun()

    st.stop()


# -----------------------------------------------------------------------------
# 7. MAIN APPLICATION STATE & CALCULATIONS
# -----------------------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []
if "processed_hashes" not in st.session_state:
    st.session_state.processed_hashes = set()
if "goal" not in st.session_state:
    st.session_state.goal = "Muscle Gain"
if "targets" not in st.session_state:
    st.session_state.targets = {
        "calories": 2200,
        "protein": 130,
        "carbs": 240,
        "fat": 65,
        "fiber": 28,
    }

daily_totals = calculate_daily_totals(st.session_state.messages)
targets = st.session_state.targets

cal_pct = min(100, int((daily_totals['calories'] / max(1, targets['calories'])) * 100))
pro_pct = min(100, int((daily_totals['protein'] / max(1, targets['protein'])) * 100))
remaining_cal = max(0, int(targets['calories'] - daily_totals['calories']))
initial_letter = (
    st.session_state.name[0].upper() if st.session_state.get("name") else "U"
)


# -----------------------------------------------------------------------------
# 8. SIDEBAR: BRIGHT CLEAN WELLNESS DESIGN
# -----------------------------------------------------------------------------
with st.sidebar:
    # Macroly Brand Header
    st.html(
        f"""<div class="ms-sidebar-header">
            <div class="ms-brand-group">
                <img src="data:image/png;base64,{LOGO_LOCKUP_B64}" alt="Macroly" class="ms-sidebar-brand-img" />
            </div>
            <div class="ms-status-chip">
                <span class="ms-status-dot"></span>
                <span>Online</span>
            </div>
        </div>"""
    )

    # Compact User Profile Card
    st.html(
        f"""<div class="ms-profile-card">
            <div class="ms-profile-left">
                <div class="ms-profile-avatar">{initial_letter}</div>
                <div>
                    <div class="ms-profile-name">{st.session_state.name}</div>
                    <div class="ms-profile-phone">{st.session_state.whatsapp_number}</div>
                </div>
            </div>
            <div class="ms-profile-tag">{st.session_state.goal}</div>
        </div>"""
    )

    # Today's Progress Card
    st.html(
        f"""<div class="ms-card">
            <div class="ms-card-header">
                <span class="ms-card-title">Today's Intake</span>
                <span class="ms-card-subtitle">{remaining_cal} kcal left</span>
            </div>
            <div class="ms-progress-group">
                <div class="ms-progress-meta">
                    <span>Calories</span>
                    <span class="ms-progress-val">{int(daily_totals['calories'])} / {targets['calories']} kcal</span>
                </div>
                <div class="ms-progress-track">
                    <div class="ms-progress-fill-cal" style="width: {cal_pct}%;"></div>
                </div>
            </div>
            <div class="ms-progress-group">
                <div class="ms-progress-meta">
                    <span>Protein</span>
                    <span class="ms-progress-val">{int(daily_totals['protein'])} / {targets['protein']}g</span>
                </div>
                <div class="ms-progress-track">
                    <div class="ms-progress-fill" style="width: {pro_pct}%;"></div>
                </div>
            </div>
            <div class="ms-macro-micro-grid">
                <div class="ms-macro-micro-item">
                    <div class="ms-macro-micro-label">Carbs</div>
                    <div class="ms-macro-micro-val">{int(daily_totals['carbs'])}g</div>
                </div>
                <div class="ms-macro-micro-item">
                    <div class="ms-macro-micro-label">Fat</div>
                    <div class="ms-macro-micro-val">{int(daily_totals['fat'])}g</div>
                </div>
                <div class="ms-macro-micro-item">
                    <div class="ms-macro-micro-label">Fiber</div>
                    <div class="ms-macro-micro-val">{int(daily_totals['fiber'])}g</div>
                </div>
            </div>
        </div>"""
    )

    # WhatsApp Sync Card (Soft Mint Surface)
    st.html(
        """<div class="ms-whatsapp-box">
            <div class="ms-whatsapp-title-row">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#198754" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
                <span>WhatsApp Briefing</span>
            </div>
            <div class="ms-whatsapp-desc">Sync today's meals, calories, and coach observations directly to your registered number.</div>
        </div>"""
    )

    send_disabled = len(st.session_state.messages) <= 1
    if st.button(
        "Send Details to WhatsApp",
        disabled=send_disabled,
        use_container_width=True,
        key="btn_sidebar_whatsapp",
    ):
        with st.spinner("Compiling summary..."):
            summary = ask_gemini([SUMMARY_REQUEST_PROMPT])
        success, info = send_whatsapp(
            st.session_state.whatsapp_number, st.session_state.name, summary
        )
        if success:
            st.success("Summary sent via WhatsApp.")
            st.session_state.last_summary = summary
        else:
            st.error(f"Dispatch failed: {info}")
            st.session_state.last_summary = summary

    # Target Adjuster Expander
    with st.expander("Adjust Daily Targets", expanded=False):
        new_cal = st.number_input(
            "Target Calories (kcal)",
            value=targets['calories'],
            step=50,
            key="edit_target_cal",
        )
        new_pro = st.number_input(
            "Target Protein (g)",
            value=targets['protein'],
            step=5,
            key="edit_target_pro",
        )
        if st.button("Save Targets", use_container_width=True, key="save_targets_btn"):
            st.session_state.targets['calories'] = int(new_cal)
            st.session_state.targets['protein'] = int(new_pro)
            st.rerun()

    # Reset Option
    if st.button("Reset Session / New Day", use_container_width=True, key="btn_reset_chat"):
        st.session_state.messages = []
        st.session_state.processed_hashes = set()
        st.session_state.chat = gemini_client.chats.create(
            model=MODEL_NAME,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )
        st.rerun()


# -----------------------------------------------------------------------------
# 9. MAIN CONTENT: HEADER & DAILY OVERVIEW
# -----------------------------------------------------------------------------

# Top Header
st.html(
    f"""<div class="ms-top-header">
        <div class="ms-top-title-row">
            <div style="display: flex; align-items: center; gap: 0.85rem;">
                <img src="data:image/png;base64,{LOGO_ICON_B64}" alt="Macroly Emblem" style="width: 44px; height: 44px; object-fit: contain; flex-shrink: 0;" />
                <div>
                    <h1 class="ms-page-title" style="margin: 0 0 0.15rem 0;">Your nutrition, simplified.</h1>
                    <p class="ms-page-desc" style="margin: 0;">Visual food tracking, macro estimation, and honest guidance designed for daily consistency.</p>
                </div>
            </div>
            <div class="ms-status-chip">
                <span class="ms-status-dot"></span>
                <span>Coach Macroly Active</span>
            </div>
        </div>
    </div>"""
)

# Daily Overview Metric Grid (White cards with elegant green progress bars)
st.html(
    f"""<div class="ms-overview-grid">
        <div class="ms-metric-card">
            <div class="ms-metric-header">
                <span class="ms-metric-label">Calories</span>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#F4A261" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>
            </div>
            <div class="ms-metric-val">{int(daily_totals['calories'])} <span class="ms-metric-unit">/ {targets['calories']} kcal</span></div>
            <div class="ms-progress-track">
                <div class="ms-progress-fill-cal" style="width: {cal_pct}%;"></div>
            </div>
            <div class="ms-metric-sub">{remaining_cal} kcal remaining</div>
        </div>
        <div class="ms-metric-card">
            <div class="ms-metric-header">
                <span class="ms-metric-label">Protein</span>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#198754" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6.5 6.5 11 11M21 21l-1-1M3 3l1 1M18 22l4-4M2 6l4-4M3 10l7-7M14 21l7-7"/></svg>
            </div>
            <div class="ms-metric-val">{int(daily_totals['protein'])}g <span class="ms-metric-unit">/ {targets['protein']}g</span></div>
            <div class="ms-progress-track">
                <div class="ms-progress-fill" style="width: {pro_pct}%;"></div>
            </div>
            <div class="ms-metric-sub">{pro_pct}% of daily target</div>
        </div>
        <div class="ms-metric-card">
            <div class="ms-metric-header">
                <span class="ms-metric-label">Carbs & Fat</span>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#66756B" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22a7 7 0 0 0 7-7c0-2-1-3.9-3-5.5s-3.5-4-4-6.5c-.5 2.5-2 4.9-4 6.5C6 11.1 5 13 5 15a7 7 0 0 0 7 7z"/></svg>
            </div>
            <div class="ms-split-val">
                <div><span class="ms-split-num">{int(daily_totals['carbs'])}g</span> <span class="ms-split-lbl">Carbs</span></div>
                <div style="border-left:1px solid var(--border-subtle); height:22px;"></div>
                <div><span class="ms-split-num">{int(daily_totals['fat'])}g</span> <span class="ms-split-lbl">Fat</span></div>
            </div>
            <div class="ms-metric-sub">Logged from {daily_totals['meal_count']} meals</div>
        </div>
        <div class="ms-metric-card">
            <div class="ms-metric-header">
                <span class="ms-metric-label">Fiber & Meals</span>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#198754" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 20h10M10 20c5.5-2.5.8-6.4 3-10M9.5 9.4c1.1.8 1.8 2.2 2.3 3.7-2 .4-3.5.4-4.8-.3-1.2-.6-2.3-1.9-3-4.2 2.8-.5 4.4 0 5.5.8zM14.1 6a7 7 0 0 0-1.1 4c1.9-.1 3.3-.6 4.3-1.4 1-1 1.6-2.3 1.7-4.6-2.7.1-4 1-4.9 2z"/></svg>
            </div>
            <div class="ms-metric-val">{int(daily_totals['fiber'])}g <span class="ms-metric-unit">Fiber</span></div>
            <div class="ms-metric-sub">{daily_totals['meal_count']} logged entries today</div>
        </div>
    </div>"""
)


# -----------------------------------------------------------------------------
# 10. FOOD PHOTO UPLOAD COMPONENT (Soft Mint Surface)
# -----------------------------------------------------------------------------
st.html(
    """<div class="ms-upload-container">
        <div class="ms-upload-header">
            <div class="ms-upload-icon-circle">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#198754" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/></svg>
            </div>
            <div>
                <div class="ms-upload-title">Log Meal with Visual Recognition</div>
                <div class="ms-upload-sub">Upload a plate photo or snap live to identify foods, portions, and macros</div>
            </div>
        </div>
    </div>"""
)

upload_col1, upload_col2 = st.columns([1.1, 0.9])

with upload_col1:
    uploaded_file = st.file_uploader(
        "Upload meal image",
        type=["jpg", "jpeg", "png"],
        key="scanner_upload",
        label_visibility="collapsed",
        help="Upload JPG or PNG photo under 10MB",
    )

with upload_col2:
    with st.expander("Or Capture Live with Camera", expanded=False):
        camera_file = st.camera_input(
            "Take photo", key="scanner_camera", label_visibility="collapsed"
        )

active_file = uploaded_file or camera_file
if active_file is not None:
    photo_bytes = active_file.getvalue()
    photo_hash = hashlib.md5(photo_bytes).hexdigest()
    if photo_hash not in st.session_state.processed_hashes:
        st.session_state.processed_hashes.add(photo_hash)
        add_message("user", "image", photo_bytes)
        parts = [
            types.Part.from_bytes(data=photo_bytes, mime_type=active_file.type),
            "Analyze this meal. Identify the food items, estimate portion sizes, calories, and macros with confidence level and uncertainties.",
        ]
        with st.spinner("Analyzing plate composition and estimating portions..."):
            answer = ask_gemini(parts)
        add_message("assistant", "text", answer)
        st.rerun()


# -----------------------------------------------------------------------------
# 11. MEAL STREAM & CONVERSATION
# -----------------------------------------------------------------------------
if not st.session_state.messages:
    add_message(
        "assistant",
        "text",
        WELCOME_MESSAGE_TEMPLATE.format(name=st.session_state.name),
    )
else:
    for message in st.session_state.messages:
        render_message(message)

# Empty State (when 0 meals logged)
if daily_totals['meal_count'] == 0 and len(st.session_state.messages) <= 1:
    st.html(
        f"""<div class="ms-empty-state">
            <div class="ms-empty-icon" style="background: transparent; border: none; padding: 0;">
                <img src="data:image/png;base64,{LOGO_ICON_B64}" alt="Macroly" style="width: 44px; height: 44px; object-fit: contain;" />
            </div>
            <div class="ms-empty-title">Ready for your first meal</div>
            <div class="ms-empty-desc">Upload a photo above or ask Coach Macroly about your meal to start calculating calories and macros.</div>
            <div class="ms-empty-tags">
                <span class="ms-tag">Visual portion estimation</span>
                <span class="ms-tag">Confidence scoring</span>
                <span class="ms-tag">WhatsApp summary</span>
            </div>
        </div>"""
    )


# -----------------------------------------------------------------------------
# 12. QUICK ACTIONS & CHAT COMPOSER
# -----------------------------------------------------------------------------
def handle_quick_query(prompt_text):
    add_message("user", "text", prompt_text)
    with st.spinner("Coach is calculating..."):
        answer = ask_gemini([prompt_text])
    add_message("assistant", "text", answer)
    st.rerun()


st.html("""<div style="font-size:0.72rem; font-weight:600; text-transform:uppercase; letter-spacing:0.04em; color:var(--text-muted); margin-top:0.75rem; margin-bottom:0.35rem;">Suggested Queries</div>""")

chip_cols = st.columns(4)
with chip_cols[0]:
    if st.button("High-Protein Snacks", use_container_width=True, key="chip_snack"):
        handle_quick_query("Suggest 3 quick, high-protein evening snacks.")
with chip_cols[1]:
    if st.button("Boiled Eggs Macros", use_container_width=True, key="chip_eggs"):
        handle_quick_query(
            "What are the calories and protein in 3 boiled whole eggs vs 3 egg whites?"
        )
with chip_cols[2]:
    if st.button("Rice Portion Guide", use_container_width=True, key="chip_rice"):
        handle_quick_query(
            "How do I visually estimate 1 cup of cooked rice without a weighing scale?"
        )
with chip_cols[3]:
    if st.button("Review Today's Goals", use_container_width=True, key="chip_goals"):
        handle_quick_query(
            "How am I doing on my calories and protein today based on what I've logged?"
        )

# Fixed bottom chat input with photo attachment support
user_input = st.chat_input(
    "Ask Coach Macroly, clarify meal details, or attach a photo...",
    accept_file=True,
    file_type=["jpg", "jpeg", "png"],
)

if user_input:
    photo = user_input.files[0] if user_input.files else None
    text = user_input.text
    parts = []

    if photo is not None:
        photo_bytes = photo.getvalue()
        add_message("user", "image", photo_bytes)
        parts.append(types.Part.from_bytes(data=photo_bytes, mime_type=photo.type))
    if text:
        add_message("user", "text", text)
        parts.append(text)
    elif photo is not None:
        parts.append(
            "What is this meal? Give me the calories, macros, and confidence breakdown."
        )

    with st.spinner("Analyzing with Coach Macroly..."):
        answer = ask_gemini(parts)
    add_message("assistant", "text", answer)
    st.rerun()
