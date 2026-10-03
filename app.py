import json
import re
import hashlib
from google import genai
from google.genai import types
import streamlit as st
from prompts import SUMMARY_REQUEST_PROMPT, WELCOME_MESSAGE_TEMPLATE, SYSTEM_PROMPT
from twilio.rest import Client as TwilioClient

# -----------------------------------------------------------------------------
# 1. PAGE CONFIGURATION
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="MacroSnap • AI Food & Fitness Coach",
    page_icon="🥗",
    layout="wide",
    initial_sidebar_state="expanded",
)

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
    text = " ".join(text.split())  # collapse whitespace/newlines
    return text[:1500] + "..." if len(text) > 1500 else text


def send_whatsapp(to_number, user_name, summary):
    # Content template expects {{1}} = name, {{2}} = summary.
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
        or "MacroSnap Daily Summary" in text
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

    # Clarification question (last line if ending with question mark)
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
# 4. CUSTOM DARK GLASSMORPHISM STYLING
# -----------------------------------------------------------------------------
def inject_custom_css():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

        :root {
            --bg-obsidian: #080b11;
            --bg-glass: rgba(18, 26, 42, 0.65);
            --bg-glass-hover: rgba(24, 34, 54, 0.85);
            --border-glass: rgba(255, 255, 255, 0.08);
            --border-glass-light: rgba(255, 255, 255, 0.14);
            --accent-green: #10b981;
            --accent-green-glow: rgba(16, 185, 129, 0.2);
            --accent-cyan: #06b6d4;
            --accent-orange: #f97316;
            --accent-amber: #eab308;
            --accent-rose: #f43f5e;
            --accent-purple: #8b5cf6;
            --text-main: #f8fafc;
            --text-sub: #94a3b8;
            --text-muted: #64748b;
        }

        /* Full App Canvas */
        .stApp {
            background-color: var(--bg-obsidian) !important;
            background-image: 
                radial-gradient(circle at 10% 12%, rgba(16, 185, 129, 0.08) 0%, transparent 40%),
                radial-gradient(circle at 90% 15%, rgba(6, 182, 212, 0.08) 0%, transparent 40%),
                radial-gradient(circle at 50% 85%, rgba(139, 92, 246, 0.06) 0%, transparent 50%) !important;
            background-attachment: fixed !important;
            color: var(--text-main) !important;
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
        }

        header[data-testid="stHeader"] {
            background: transparent !important;
        }

        /* Sidebar Styling */
        [data-testid="stSidebar"] {
            background-color: rgba(11, 16, 26, 0.9) !important;
            backdrop-filter: blur(24px) !important;
            -webkit-backdrop-filter: blur(24px) !important;
            border-right: 1px solid var(--border-glass) !important;
        }

        [data-testid="stSidebarContent"] {
            padding: 1.25rem 1rem !important;
        }

        /* Streamlit Input & Widget Styling */
        .stTextInput > div > div > input, 
        .stSelectbox > div > div,
        .stNumberInput > div > div > input {
            background-color: rgba(15, 23, 42, 0.65) !important;
            border: 1px solid var(--border-glass) !important;
            border-radius: 12px !important;
            color: #f8fafc !important;
            font-size: 0.92rem !important;
            transition: all 0.2s ease !important;
        }

        .stTextInput > div > div > input:focus, 
        .stSelectbox > div > div:focus-within {
            border-color: rgba(16, 185, 129, 0.6) !important;
            box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.2) !important;
        }

        .stTextInput label, .stSelectbox label, .stNumberInput label {
            color: var(--text-sub) !important;
            font-weight: 600 !important;
            font-size: 0.84rem !important;
        }

        /* Streamlit Buttons */
        .stButton > button {
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.15) 0%, rgba(6, 182, 212, 0.15) 100%) !important;
            color: #f8fafc !important;
            border: 1px solid rgba(16, 185, 129, 0.3) !important;
            border-radius: 12px !important;
            font-weight: 600 !important;
            font-size: 0.9rem !important;
            padding: 0.55rem 1.15rem !important;
            transition: all 0.25s ease !important;
            box-shadow: 0 4px 14px rgba(0, 0, 0, 0.25) !important;
        }

        .stButton > button:hover {
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.3) 0%, rgba(6, 182, 212, 0.3) 100%) !important;
            border-color: rgba(16, 185, 129, 0.6) !important;
            transform: translateY(-2px) !important;
            box-shadow: 0 6px 20px rgba(16, 185, 129, 0.25) !important;
        }

        button[kind="primaryFormSubmit"], 
        button[data-testid="baseButton-primary"] {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%) !important;
            color: #ffffff !important;
            border: none !important;
            box-shadow: 0 4px 18px rgba(16, 185, 129, 0.35) !important;
        }

        button[kind="primaryFormSubmit"]:hover, 
        button[data-testid="baseButton-primary"]:hover {
            background: linear-gradient(135deg, #059669 0%, #047857 100%) !important;
            transform: translateY(-2px) !important;
            box-shadow: 0 6px 24px rgba(16, 185, 129, 0.5) !important;
        }

        /* Chat Input Styling */
        [data-testid="stChatInput"] {
            background-color: rgba(15, 23, 42, 0.88) !important;
            backdrop-filter: blur(20px) !important;
            -webkit-backdrop-filter: blur(20px) !important;
            border: 1px solid var(--border-glass-light) !important;
            border-radius: 20px !important;
            box-shadow: 0 10px 35px rgba(0, 0, 0, 0.5) !important;
            padding: 0.35rem 0.5rem !important;
        }

        [data-testid="stChatInput"]:focus-within {
            border-color: rgba(16, 185, 129, 0.55) !important;
            box-shadow: 0 10px 35px rgba(16, 185, 129, 0.15) !important;
        }

        [data-testid="stChatInput"] textarea {
            color: #f8fafc !important;
            font-family: inherit !important;
        }

        /* File Uploader Dropzone */
        [data-testid="stFileUploaderDropzone"] {
            background: rgba(15, 23, 42, 0.45) !important;
            border: 1.5px dashed rgba(16, 185, 129, 0.35) !important;
            border-radius: 16px !important;
            color: #e2e8f0 !important;
            padding: 1.25rem !important;
            transition: all 0.25s ease !important;
        }

        [data-testid="stFileUploaderDropzone"]:hover {
            border-color: #10b981 !important;
            background: rgba(16, 185, 129, 0.08) !important;
        }

        /* Camera Input container */
        [data-testid="stCameraInput"] {
            border-radius: 16px !important;
            overflow: hidden !important;
        }

        /* Glassmorphism Containers */
        .glass-card {
            background: var(--bg-glass);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid var(--border-glass);
            box-shadow: 0 10px 30px -5px rgba(0, 0, 0, 0.45), 0 0 0 1px rgba(255, 255, 255, 0.03) inset;
            border-radius: 18px;
            padding: 1.35rem;
            margin-bottom: 1.25rem;
            transition: all 0.25s ease;
        }

        .glass-card:hover {
            border-color: var(--border-glass-light);
        }

        /* Hero Header */
        .hero-header {
            background: linear-gradient(135deg, rgba(20, 30, 48, 0.75) 0%, rgba(13, 20, 33, 0.85) 100%);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid rgba(255, 255, 255, 0.09);
            border-radius: 20px;
            padding: 1.5rem 1.85rem;
            margin-bottom: 1.35rem;
            position: relative;
            overflow: hidden;
            box-shadow: 0 12px 35px -5px rgba(0, 0, 0, 0.45);
        }

        .hero-header::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 2px;
            background: linear-gradient(90deg, #10b981 0%, #06b6d4 50%, #8b5cf6 100%);
        }

        .hero-top-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.6rem;
            flex-wrap: wrap;
            gap: 0.75rem;
        }

        .brand-badge-group {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .brand-title {
            font-size: 1.7rem;
            font-weight: 800;
            letter-spacing: -0.03em;
            background: linear-gradient(135deg, #f8fafc 0%, #cbd5e1 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            display: flex;
            align-items: center;
            gap: 0.35rem;
        }

        .brand-tag {
            font-size: 0.7rem;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            background: rgba(16, 185, 129, 0.12);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.3);
            padding: 0.22rem 0.6rem;
            border-radius: 999px;
        }

        .status-indicator {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            font-size: 0.8rem;
            font-weight: 600;
            color: #94a3b8;
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.06);
            padding: 0.3rem 0.75rem;
            border-radius: 999px;
        }

        .pulse-dot {
            width: 8px;
            height: 8px;
            background-color: #10b981;
            border-radius: 50%;
            box-shadow: 0 0 0 rgba(16, 185, 129, 0.7);
            animation: pulse-dot 2s infinite;
        }

        @keyframes pulse-dot {
            0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
            100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        .hero-headline {
            font-size: 1.3rem;
            font-weight: 700;
            color: #f1f5f9;
            margin-bottom: 0.25rem;
            line-height: 1.3;
        }

        .hero-subtext {
            font-size: 0.9rem;
            color: #94a3b8;
            line-height: 1.45;
        }

        /* Centerpiece Meal Scanner */
        .scanner-card {
            background: linear-gradient(145deg, rgba(20, 30, 48, 0.65) 0%, rgba(13, 20, 33, 0.75) 100%);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1.5px dashed rgba(16, 185, 129, 0.35);
            border-radius: 20px;
            padding: 1.5rem 1.25rem;
            text-align: center;
            margin-bottom: 1.25rem;
            transition: all 0.3s ease;
        }

        .scanner-card:hover {
            border-color: rgba(16, 185, 129, 0.6);
            box-shadow: 0 10px 30px rgba(16, 185, 129, 0.12);
        }

        .scanner-icon {
            font-size: 2.2rem;
            margin-bottom: 0.4rem;
            display: inline-block;
            filter: drop-shadow(0 4px 10px rgba(16, 185, 129, 0.3));
        }

        .scanner-title {
            font-size: 1.15rem;
            font-weight: 700;
            color: #f8fafc;
            margin-bottom: 0.2rem;
        }

        .scanner-subtitle {
            font-size: 0.85rem;
            color: #94a3b8;
            margin-bottom: 0.75rem;
        }

        /* Daily Dashboard Metric Grid */
        .daily-dashboard-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 0.85rem;
            margin-bottom: 1.25rem;
        }

        .metric-glass-card {
            background: rgba(18, 26, 42, 0.65);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid var(--border-glass);
            border-radius: 16px;
            padding: 0.95rem 1.1rem;
            transition: all 0.2s ease;
        }

        .metric-glass-card:hover {
            transform: translateY(-2px);
            border-color: var(--border-glass-light);
        }

        .metric-card-cal { border-top: 3px solid #f97316; }
        .metric-card-pro { border-top: 3px solid #06b6d4; }
        .metric-card-carb { border-top: 3px solid #eab308; }
        .metric-card-fat { border-top: 3px solid #f43f5e; }
        .metric-card-fib { border-top: 3px solid #10b981; }

        .metric-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.35rem;
        }

        .metric-title {
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: #94a3b8;
        }

        .metric-value-row {
            display: flex;
            align-items: baseline;
            gap: 0.3rem;
            margin-bottom: 0.45rem;
        }

        .metric-value {
            font-size: 1.25rem;
            font-weight: 800;
            color: #f8fafc;
            letter-spacing: -0.02em;
        }

        .metric-target {
            font-size: 0.78rem;
            color: #64748b;
            font-weight: 500;
        }

        .progress-track {
            background: rgba(255, 255, 255, 0.06);
            height: 6px;
            border-radius: 999px;
            overflow: hidden;
            position: relative;
        }

        .progress-fill-cal { background: linear-gradient(90deg, #f97316, #fb923c); height: 100%; border-radius: 999px; }
        .progress-fill-pro { background: linear-gradient(90deg, #06b6d4, #38bdf8); height: 100%; border-radius: 999px; }
        .progress-fill-carb { background: linear-gradient(90deg, #eab308, #fde047); height: 100%; border-radius: 999px; }
        .progress-fill-fat { background: linear-gradient(90deg, #f43f5e, #fb7185); height: 100%; border-radius: 999px; }
        .progress-fill-fib { background: linear-gradient(90deg, #10b981, #4ade80); height: 100%; border-radius: 999px; }

        /* Meal Result Card */
        .meal-result-card {
            background: linear-gradient(145deg, rgba(20, 29, 46, 0.8) 0%, rgba(13, 20, 33, 0.9) 100%);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            box-shadow: 0 15px 35px -5px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.04) inset;
            border-radius: 20px;
            padding: 1.5rem;
            margin: 0.75rem 0;
        }

        .meal-result-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.15rem;
            flex-wrap: wrap;
            gap: 0.5rem;
        }

        .meal-badge {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.75rem;
            font-weight: 700;
            letter-spacing: 0.07em;
            color: #cbd5e1;
            text-transform: uppercase;
        }

        .confidence-pill {
            font-size: 0.75rem;
            font-weight: 700;
            padding: 0.28rem 0.75rem;
            border-radius: 999px;
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
        }

        .conf-high {
            background: rgba(16, 185, 129, 0.12);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.3);
        }

        .conf-moderate {
            background: rgba(234, 179, 8, 0.12);
            color: #facc15;
            border: 1px solid rgba(234, 179, 8, 0.3);
        }

        .conf-low {
            background: rgba(244, 63, 94, 0.12);
            color: #fb7185;
            border: 1px solid rgba(244, 63, 94, 0.3);
        }

        .calorie-hero {
            background: rgba(249, 115, 22, 0.08);
            border: 1px solid rgba(249, 115, 22, 0.25);
            border-radius: 16px;
            padding: 1.15rem;
            text-align: center;
            margin-bottom: 1.15rem;
        }

        .calorie-label {
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.08em;
            color: #fb923c;
            text-transform: uppercase;
            margin-bottom: 0.2rem;
        }

        .calorie-value {
            font-size: 2.1rem;
            font-weight: 800;
            color: #fff7ed;
            letter-spacing: -0.02em;
            line-height: 1.1;
        }

        .calorie-unit {
            font-size: 1.05rem;
            font-weight: 600;
            color: #fb923c;
        }

        .calorie-sub {
            font-size: 0.78rem;
            color: #94a3b8;
            margin-top: 0.3rem;
        }

        .macro-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 0.7rem;
            margin-bottom: 1.15rem;
        }

        @media (max-width: 640px) {
            .macro-grid {
                grid-template-columns: repeat(2, 1fr);
            }
        }

        .macro-stat-card {
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 14px;
            padding: 0.8rem;
            display: flex;
            align-items: center;
            gap: 0.6rem;
        }

        .macro-icon {
            font-size: 1.25rem;
        }

        .macro-title {
            font-size: 0.68rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            color: #94a3b8;
        }

        .macro-num {
            font-size: 1.05rem;
            font-weight: 700;
            color: #f8fafc;
        }

        .macro-protein .macro-num { color: #38bdf8; }
        .macro-carbs .macro-num { color: #facc15; }
        .macro-fat .macro-num { color: #fb7185; }
        .macro-fiber .macro-num { color: #34d399; }

        .section-container {
            margin-bottom: 0.9rem;
        }

        .section-title {
            font-size: 0.74rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            color: #94a3b8;
            margin-bottom: 0.45rem;
            text-transform: uppercase;
        }

        .food-chips-wrapper {
            display: flex;
            flex-wrap: wrap;
            gap: 0.45rem;
        }

        .food-chip {
            background: rgba(15, 23, 42, 0.7);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 999px;
            padding: 0.32rem 0.75rem;
            font-size: 0.82rem;
            color: #e2e8f0;
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
        }

        .insight-card {
            border-radius: 14px;
            padding: 0.95rem 1.1rem;
            margin-bottom: 0.8rem;
        }

        .uncertainty-card {
            background: rgba(234, 179, 8, 0.06);
            border: 1px solid rgba(234, 179, 8, 0.2);
        }

        .uncertainty-card .insight-header {
            color: #facc15;
        }

        .coach-card {
            background: rgba(16, 185, 129, 0.06);
            border: 1px solid rgba(16, 185, 129, 0.2);
        }

        .coach-card .insight-header {
            color: #34d399;
        }

        .insight-header {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            font-size: 0.74rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            margin-bottom: 0.3rem;
            text-transform: uppercase;
        }

        .insight-body {
            font-size: 0.88rem;
            color: #cbd5e1;
            line-height: 1.45;
        }

        .clarification-card {
            background: rgba(6, 182, 212, 0.07);
            border: 1px dashed rgba(6, 182, 212, 0.35);
            border-radius: 14px;
            padding: 0.95rem 1.1rem;
            margin-top: 0.75rem;
        }

        .clarification-header {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            font-size: 0.74rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            color: #38bdf8;
            margin-bottom: 0.25rem;
        }

        .clarification-question {
            font-size: 0.95rem;
            font-weight: 600;
            color: #f0f9ff;
            line-height: 1.4;
            margin-bottom: 0.25rem;
        }

        .clarification-hint {
            font-size: 0.76rem;
            color: #94a3b8;
        }

        /* Image Display in Chat */
        .user-meal-img-container {
            background: rgba(15, 23, 42, 0.65);
            border: 1px solid rgba(255, 255, 255, 0.09);
            border-radius: 16px;
            padding: 0.5rem;
            margin: 0.4rem 0;
            max-width: 480px;
        }

        .user-meal-img-badge {
            font-size: 0.7rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            color: #94a3b8;
            margin-bottom: 0.35rem;
            text-transform: uppercase;
        }

        /* WhatsApp CTA Card */
        .whatsapp-cta-card {
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.08) 0%, rgba(5, 150, 105, 0.12) 100%);
            border: 1px solid rgba(16, 185, 129, 0.25);
            border-radius: 16px;
            padding: 1.15rem;
            margin: 0.85rem 0;
        }

        .whatsapp-badge {
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.07em;
            color: #34d399;
            margin-bottom: 0.3rem;
        }

        .whatsapp-title {
            font-size: 1rem;
            font-weight: 700;
            color: #f8fafc;
            margin-bottom: 0.25rem;
        }

        .whatsapp-desc {
            font-size: 0.8rem;
            color: #94a3b8;
            line-height: 1.4;
            margin-bottom: 0.75rem;
        }

        /* Empty State */
        .empty-state-card {
            background: rgba(18, 26, 42, 0.45);
            border: 1px dashed rgba(255, 255, 255, 0.1);
            border-radius: 20px;
            padding: 2.25rem 1.5rem;
            text-align: center;
            margin: 1.5rem 0;
        }

        .empty-icon {
            font-size: 2.8rem;
            margin-bottom: 0.6rem;
        }

        .empty-title {
            font-size: 1.25rem;
            font-weight: 700;
            color: #f8fafc;
            margin-bottom: 0.3rem;
        }

        .empty-subtitle {
            font-size: 0.88rem;
            color: #94a3b8;
            max-width: 480px;
            margin: 0 auto 1.15rem auto;
            line-height: 1.5;
        }

        .empty-features {
            display: flex;
            flex-wrap: wrap;
            justify-content: center;
            gap: 0.45rem;
        }

        .feat-pill {
            background: rgba(15, 23, 42, 0.65);
            border: 1px solid rgba(255, 255, 255, 0.07);
            color: #cbd5e1;
            font-size: 0.76rem;
            font-weight: 500;
            padding: 0.3rem 0.7rem;
            border-radius: 999px;
        }

        /* User Profile & Sidebar Details */
        .user-profile-badge {
            background: rgba(15, 23, 42, 0.7);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 14px;
            padding: 0.85rem;
            margin-bottom: 1rem;
        }

        .user-name-row {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            font-size: 0.95rem;
            font-weight: 700;
            color: #f8fafc;
        }

        .user-phone-row {
            font-size: 0.76rem;
            color: #94a3b8;
            margin-top: 0.2rem;
        }

        .user-goal-pill {
            display: inline-block;
            font-size: 0.7rem;
            font-weight: 600;
            background: rgba(6, 182, 212, 0.12);
            color: #38bdf8;
            border: 1px solid rgba(6, 182, 212, 0.25);
            padding: 0.18rem 0.55rem;
            border-radius: 999px;
            margin-top: 0.4rem;
        }

        .philosophy-banner {
            text-align: center;
            font-size: 0.68rem;
            font-weight: 700;
            letter-spacing: 0.1em;
            color: #64748b;
            padding: 0.75rem 0.5rem;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            margin-top: 1.25rem;
        }

        /* Onboarding Center Container */
        .onboarding-wrapper {
            max-width: 540px;
            margin: 2rem auto;
            background: linear-gradient(145deg, rgba(20, 30, 48, 0.8) 0%, rgba(13, 20, 33, 0.9) 100%);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 24px;
            padding: 2.25rem;
            box-shadow: 0 20px 45px -10px rgba(0, 0, 0, 0.6);
        }

        .onboarding-badge {
            font-size: 0.75rem;
            font-weight: 800;
            letter-spacing: 0.1em;
            color: #10b981;
            text-transform: uppercase;
            margin-bottom: 0.5rem;
        }

        .onboarding-title {
            font-size: 1.8rem;
            font-weight: 800;
            letter-spacing: -0.02em;
            color: #f8fafc;
            line-height: 1.2;
            margin-bottom: 0.5rem;
        }

        .onboarding-sub {
            font-size: 0.92rem;
            color: #94a3b8;
            line-height: 1.5;
            margin-bottom: 1.5rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
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
    conf_class = 'conf-moderate'
    conf_icon = '🟡'
    if 'high' in conf.lower():
        conf_class = 'conf-high'
        conf_icon = '🟢'
    elif 'low' in conf.lower() or 'poor' in conf.lower():
        conf_class = 'conf-low'
        conf_icon = '🔴'

    foods_html = ''
    if data.get('foods'):
        food_items_html = ''.join(
            [
                f'<div class="food-chip"><span>🥗</span><span>{f}</span></div>'
                for f in data['foods']
            ]
        )
        foods_html = f'''
        <div class="section-container">
            <div class="section-title">🍽️ DETECTED FOOD ITEMS</div>
            <div class="food-chips-wrapper">{food_items_html}</div>
        </div>'''

    uncertainty_html = ''
    if data.get('uncertainty'):
        uncertainty_html = f'''
        <div class="insight-card uncertainty-card">
            <div class="insight-header">
                <span>🔍</span>
                <span>UNCERTAINTY & ASSUMPTIONS</span>
            </div>
            <div class="insight-body">{data['uncertainty']}</div>
        </div>'''

    coach_html = ''
    if data.get('trainer_tip'):
        coach_html = f'''
        <div class="insight-card coach-card">
            <div class="insight-header">
                <span>🏋️</span>
                <span>COACH'S TAKE</span>
            </div>
            <div class="insight-body">{data['trainer_tip']}</div>
        </div>'''

    question_html = ''
    if data.get('question'):
        question_html = f'''
        <div class="clarification-card">
            <div class="clarification-header">
                <span>💬</span>
                <span>COACH ASKS:</span>
            </div>
            <div class="clarification-question">{data['question']}</div>
            <div class="clarification-hint">💡 Reply in the chat bar below to refine this estimate!</div>
        </div>'''

    html = f'''
    <div class="meal-result-card">
        <div class="meal-result-header">
            <div class="meal-badge">
                <span style="width:7px; height:7px; border-radius:50%; background:#10b981; display:inline-block;"></span>
                <span>ESTIMATED MEAL BREAKDOWN</span>
            </div>
            <div class="confidence-pill {conf_class}">
                {conf_icon} {conf.capitalize()} Confidence
            </div>
        </div>
        
        <div class="calorie-hero">
            <div class="calorie-label">ESTIMATED CALORIES</div>
            <div class="calorie-value">~{cal} <span class="calorie-unit">kcal</span></div>
            <div class="calorie-sub">Visual estimate &bull; Final calories vary by oil, sauces & portions</div>
        </div>
        
        <div class="macro-grid">
            <div class="macro-stat-card macro-protein">
                <div class="macro-icon">💪</div>
                <div class="macro-info">
                    <div class="macro-title">PROTEIN</div>
                    <div class="macro-num">~{prot}g</div>
                </div>
            </div>
            <div class="macro-stat-card macro-carbs">
                <div class="macro-icon">🍚</div>
                <div class="macro-info">
                    <div class="macro-title">CARBS</div>
                    <div class="macro-num">~{carb}g</div>
                </div>
            </div>
            <div class="macro-stat-card macro-fat">
                <div class="macro-icon">🥑</div>
                <div class="macro-info">
                    <div class="macro-title">FAT</div>
                    <div class="macro-num">~{fat}g</div>
                </div>
            </div>
            <div class="macro-stat-card macro-fiber">
                <div class="macro-icon">🌱</div>
                <div class="macro-info">
                    <div class="macro-title">FIBER</div>
                    <div class="macro-num">~{fib}g</div>
                </div>
            </div>
        </div>
        
        {foods_html}
        {uncertainty_html}
        {coach_html}
        {question_html}
    </div>
    '''
    return html


def render_message(message):
    with st.chat_message(message["role"]):
        if message["kind"] == "text":
            content = message["content"]
            meal_data = parse_meal_response(content)
            if meal_data and meal_data.get("calories"):
                st.markdown(render_meal_html(meal_data), unsafe_allow_html=True)
                with st.expander("🔍 View Detailed Coach Analysis", expanded=False):
                    st.markdown(content)
            elif (
                "Daily Summary" in content
                or "FitBuddy Daily Summary" in content
                or "MacroSnap Daily Summary" in content
            ):
                st.markdown(
                    f"""
                    <div class="glass-card" style="border-left: 3px solid #10b981; background: rgba(16, 185, 129, 0.08);">
                        <div style="font-size:0.75rem; font-weight:700; letter-spacing:0.07em; color:#34d399; margin-bottom:0.4rem;">📊 NUTRITION SUMMARY</div>
                        <div style="line-height:1.6; font-size:0.95rem; white-space:pre-wrap;">{content}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            elif content.startswith("Sorry, something went wrong"):
                st.markdown(
                    f"""
                    <div class="glass-card" style="border-left: 3px solid #f43f5e; background: rgba(244, 63, 94, 0.08);">
                        <div style="font-weight:700; color:#fb7185; margin-bottom:0.3rem;">🔴 Analysis Notice</div>
                        <div style="font-size:0.88rem; color:#cbd5e1;">{content}</div>
                        <div style="font-size:0.8rem; color:#94a3b8; margin-top:0.5rem;">Try uploading a well-lit photo with the full meal in frame!</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            else:
                st.write(content)
        elif message["kind"] == "image":
            st.markdown(
                """
                <div class="user-meal-img-container">
                    <div class="user-meal-img-badge">📸 Uploaded Plate</div>
                """,
                unsafe_allow_html=True,
            )
            st.image(message["content"], use_container_width=True)
            st.markdown("</div>", unsafe_allow_html=True)


def add_message(role, kind, content):
    st.session_state.messages.append({"role": role, "kind": kind, "content": content})
    render_message(st.session_state.messages[-1])


# -----------------------------------------------------------------------------
# 6. ONBOARDING FLOW
# -----------------------------------------------------------------------------
inject_custom_css()

if "onboarded" not in st.session_state:
    st.markdown(
        """
        <div class="onboarding-wrapper">
            <div class="onboarding-badge">🥗 MACROSNAP AI &bull; ONBOARDING</div>
            <div class="onboarding-title">Snap your meal.<br><span style="background: linear-gradient(135deg, #10b981 0%, #06b6d4 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">We'll handle the numbers.</span></div>
            <div class="onboarding-sub">Meet your smart AI gym buddy. Computer-vision food tracking built around uncertainty, practical portion estimates, and daily WhatsApp updates.</div>
        """,
        unsafe_allow_html=True,
    )

    with st.form("onboarding_form"):
        name = st.text_input("Your Name", placeholder="e.g. Aditya")
        whatsapp_number = st.text_input(
            "WhatsApp Number (with country code)",
            placeholder="+91XXXXXXXXXX",
            help="Your daily nutrition summaries will be sent directly to this number.",
        )
        goal = st.selectbox(
            "Your Primary Fitness Goal",
            [
                "💪 Muscle Gain",
                "🔥 Fat Loss",
                "⚖️ Body Recomposition",
                "🌿 Maintenance & Health",
            ],
            index=0,
        )

        submitted = st.form_submit_button(
            "Start Tracking with MacroSnap 🚀", use_container_width=True
        )

    st.markdown("</div>", unsafe_allow_html=True)

    if submitted:
        if not name.strip() or not whatsapp_number.strip():
            st.error("Please enter both your name and WhatsApp number to begin.")
        else:
            # Set personalized macro defaults based on goal
            goal_targets = {
                "💪 Muscle Gain": {
                    "calories": 2400,
                    "protein": 140,
                    "carbs": 260,
                    "fat": 70,
                    "fiber": 30,
                },
                "🔥 Fat Loss": {
                    "calories": 1800,
                    "protein": 130,
                    "carbs": 170,
                    "fat": 55,
                    "fiber": 30,
                },
                "⚖️ Body Recomposition": {
                    "calories": 2100,
                    "protein": 140,
                    "carbs": 220,
                    "fat": 65,
                    "fiber": 30,
                },
                "🌿 Maintenance & Health": {
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
                config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
            )
            st.session_state.messages = []
            st.session_state.processed_hashes = set()
            st.session_state.onboarded = True
            st.rerun()

    st.stop()


# -----------------------------------------------------------------------------
# 7. MAIN APPLICATION INTERFACE
# -----------------------------------------------------------------------------

# Fallback defaults for existing sessions
if "messages" not in st.session_state:
    st.session_state.messages = []
if "processed_hashes" not in st.session_state:
    st.session_state.processed_hashes = set()
if "goal" not in st.session_state:
    st.session_state.goal = "💪 Muscle Gain"
if "targets" not in st.session_state:
    st.session_state.targets = {
        "calories": 2200,
        "protein": 130,
        "carbs": 240,
        "fat": 65,
        "fiber": 28,
    }

# Calculate live daily totals
daily_totals = calculate_daily_totals(st.session_state.messages)
targets = st.session_state.targets

# -----------------------------------------------------------------------------
# 8. SIDEBAR: PROFILE & DAILY PROGRESS
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown(
        """
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:1rem;">
            <div style="font-size:1.35rem; font-weight:800; letter-spacing:-0.02em; color:#f8fafc;">
                MacroSnap <span style="font-size:1.15rem;">🥗</span>
            </div>
            <div class="status-indicator">
                <span class="pulse-dot"></span>
                <span>AI Online</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # User Profile Chip
    st.markdown(
        f"""
        <div class="user-profile-badge">
            <div class="user-name-row">
                <span>👤</span>
                <span>{st.session_state.name}</span>
            </div>
            <div class="user-phone-row">📲 {st.session_state.whatsapp_number}</div>
            <div class="user-goal-pill">{st.session_state.goal}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Today's Progress Card
    cal_pct = min(
        100, int((daily_totals['calories'] / max(1, targets['calories'])) * 100)
    )
    pro_pct = min(
        100, int((daily_totals['protein'] / max(1, targets['protein'])) * 100)
    )

    st.markdown(
        f"""
        <div class="glass-card" style="padding:1.1rem; margin-bottom:1rem;">
            <div style="font-size:0.75rem; font-weight:700; letter-spacing:0.06em; color:#94a3b8; text-transform:uppercase; margin-bottom:0.75rem;">
                📊 TODAY'S NUTRITION
            </div>
            
            <div style="margin-bottom:0.75rem;">
                <div style="display:flex; justify-content:space-between; font-size:0.82rem; margin-bottom:0.3rem;">
                    <span style="color:#fb923c; font-weight:600;">🔥 Calories</span>
                    <span style="color:#f8fafc; font-weight:700;">{int(daily_totals['calories'])} / {targets['calories']} kcal</span>
                </div>
                <div class="progress-track">
                    <div class="progress-fill-cal" style="width: {cal_pct}%;"></div>
                </div>
            </div>

            <div style="margin-bottom:0.75rem;">
                <div style="display:flex; justify-content:space-between; font-size:0.82rem; margin-bottom:0.3rem;">
                    <span style="color:#38bdf8; font-weight:600;">💪 Protein</span>
                    <span style="color:#f8fafc; font-weight:700;">{int(daily_totals['protein'])} / {targets['protein']}g</span>
                </div>
                <div class="progress-track">
                    <div class="progress-fill-pro" style="width: {pro_pct}%;"></div>
                </div>
            </div>

            <div style="display:flex; justify-content:space-between; font-size:0.76rem; color:#94a3b8; border-top:1px solid rgba(255,255,255,0.06); padding-top:0.6rem;">
                <span>🍚 Carbs: <b>{int(daily_totals['carbs'])}g</b></span>
                <span>🥑 Fat: <b>{int(daily_totals['fat'])}g</b></span>
                <span>🌱 Fiber: <b>{int(daily_totals['fiber'])}g</b></span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # WhatsApp CTA Card
    st.markdown(
        """
        <div class="whatsapp-cta-card">
            <div class="whatsapp-badge">📲 WHATSAPP SYNC</div>
            <div class="whatsapp-title">Take Your Summary With You</div>
            <div class="whatsapp-desc">Get today's logged meals, calorie count, macros, and Coach's take sent straight to your phone.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    send_disabled = len(st.session_state.messages) <= 2
    if st.button(
        "📤 Send Details to WhatsApp",
        disabled=send_disabled,
        use_container_width=True,
        key="btn_sidebar_whatsapp",
    ):
        with st.spinner("Summarizing your day..."):
            summary = ask_gemini([SUMMARY_REQUEST_PROMPT])
        success, info = send_whatsapp(
            st.session_state.whatsapp_number, st.session_state.name, summary
        )
        if success:
            st.success("Sent! Check your WhatsApp 📲")
            st.session_state.last_summary = summary
        else:
            st.error(f"Couldn't send that: {info}")
            st.session_state.last_summary = summary

    # Goal Adjuster Expander
    with st.expander("🎯 Adjust Daily Targets", expanded=False):
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

    # Reset Chat Option
    if st.button(
        "🧹 Reset Session / New Day",
        use_container_width=True,
        key="btn_reset_chat",
    ):
        st.session_state.messages = []
        st.session_state.processed_hashes = set()
        st.session_state.chat = gemini_client.chats.create(
            model=MODEL_NAME,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )
        st.rerun()

    # Brand Philosophy Banner
    st.markdown(
        """
        <div class="philosophy-banner">
            TRACK &bull; UNDERSTAND &bull; IMPROVE &bull; REPEAT
        </div>
        """,
        unsafe_allow_html=True,
    )


# -----------------------------------------------------------------------------
# 9. MAIN CONTENT: HERO & CENTERPIECE SCANNER
# -----------------------------------------------------------------------------

# Top Hero Header
st.markdown(
    """
    <div class="hero-header">
        <div class="hero-top-row">
            <div class="brand-badge-group">
                <div class="brand-title">MacroSnap 🥗</div>
                <div class="brand-tag">AI Food & Fitness Coach</div>
            </div>
            <div class="status-indicator">
                <span class="pulse-dot"></span>
                <span>AI Nutrition Coach Online</span>
            </div>
        </div>
        <div class="hero-headline">Snap your meal. We'll handle the numbers.</div>
        <div class="hero-subtext">AI-powered calorie and macro estimation with smart portion guidance, built for real life and honest uncertainty.</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Centerpiece Meal Scanner Card
st.markdown(
    """
    <div class="scanner-card">
        <div class="scanner-icon">📸</div>
        <div class="scanner-title">Drop your meal photo here</div>
        <div class="scanner-subtitle">Snap a photo and let MacroSnap decode your plate &bull; Estimating calories, portions & macro breakdown</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# File uploader and live camera tabs inside the centerpiece
scan_col1, scan_col2 = st.columns([1, 1])

with scan_col1:
    uploaded_file = st.file_uploader(
        "Upload meal image",
        type=["jpg", "jpeg", "png"],
        key="scanner_upload",
        label_visibility="collapsed",
        help="Upload a photo of your plate, bowl, or snack",
    )

with scan_col2:
    with st.expander("📷 Or Snap with Live Camera", expanded=False):
        camera_file = st.camera_input(
            "Take photo", key="scanner_camera", label_visibility="collapsed"
        )

# Process photo uploaded through centerpiece
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
        with st.spinner(
            "🔍 Scanning your plate... Identifying food • Estimating portions • Calculating macros"
        ):
            answer = ask_gemini(parts)
        add_message("assistant", "text", answer)
        st.rerun()


# -----------------------------------------------------------------------------
# 10. DAILY NUTRITION METRICS OVERVIEW
# -----------------------------------------------------------------------------
if daily_totals['meal_count'] > 0:
    st.markdown(
        f"""
        <div class="daily-dashboard-grid">
            <div class="metric-glass-card metric-card-cal">
                <div class="metric-header">
                    <span class="metric-title">Calories</span>
                    <span>🔥</span>
                </div>
                <div class="metric-value-row">
                    <span class="metric-value">{int(daily_totals['calories'])}</span>
                    <span class="metric-target">/ {targets['calories']} kcal</span>
                </div>
                <div class="progress-track">
                    <div class="progress-fill-cal" style="width: {cal_pct}%;"></div>
                </div>
            </div>

            <div class="metric-glass-card metric-card-pro">
                <div class="metric-header">
                    <span class="metric-title">Protein</span>
                    <span>💪</span>
                </div>
                <div class="metric-value-row">
                    <span class="metric-value">{int(daily_totals['protein'])}g</span>
                    <span class="metric-target">/ {targets['protein']}g</span>
                </div>
                <div class="progress-track">
                    <div class="progress-fill-pro" style="width: {pro_pct}%;"></div>
                </div>
            </div>

            <div class="metric-glass-card metric-card-carb">
                <div class="metric-header">
                    <span class="metric-title">Carbs</span>
                    <span>🍚</span>
                </div>
                <div class="metric-value-row">
                    <span class="metric-value">{int(daily_totals['carbs'])}g</span>
                    <span class="metric-target">logged</span>
                </div>
                <div class="progress-track">
                    <div class="progress-fill-carb" style="width: 50%;"></div>
                </div>
            </div>

            <div class="metric-glass-card metric-card-fat">
                <div class="metric-header">
                    <span class="metric-title">Fat</span>
                    <span>🥑</span>
                </div>
                <div class="metric-value-row">
                    <span class="metric-value">{int(daily_totals['fat'])}g</span>
                    <span class="metric-target">logged</span>
                </div>
                <div class="progress-track">
                    <div class="progress-fill-fat" style="width: 50%;"></div>
                </div>
            </div>

            <div class="metric-glass-card metric-card-fib">
                <div class="metric-header">
                    <span class="metric-title">Fiber</span>
                    <span>🌱</span>
                </div>
                <div class="metric-value-row">
                    <span class="metric-value">{int(daily_totals['fiber'])}g</span>
                    <span class="metric-target">logged</span>
                </div>
                <div class="progress-track">
                    <div class="progress-fill-fib" style="width: 50%;"></div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


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

# Empty State Card if no meals analyzed yet
if daily_totals['meal_count'] == 0 and len(st.session_state.messages) <= 1:
    st.markdown(
        """
        <div class="empty-state-card">
            <div class="empty-icon">🥗</div>
            <div class="empty-title">Your plate is waiting</div>
            <div class="empty-subtitle">Upload your first meal photo or tell Coach Macroly what you're eating to start tracking your calories and macros!</div>
            <div class="empty-features">
                <span class="feat-pill">📸 Visual Portion Estimation</span>
                <span class="feat-pill">💪 Macro & Calorie Breakdown</span>
                <span class="feat-pill">⚠️ Uncertainty & Oil Detection</span>
                <span class="feat-pill">🏋️ Mastikhor Gym-Buddy Advice</span>
                <span class="feat-pill">📲 WhatsApp Summary Sync</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# -----------------------------------------------------------------------------
# 12. QUICK ACTIONS & CHAT INPUT
# -----------------------------------------------------------------------------
def handle_quick_query(prompt_text):
    add_message("user", "text", prompt_text)
    with st.spinner("Coach is thinking..."):
        answer = ask_gemini([prompt_text])
    add_message("assistant", "text", answer)
    st.rerun()


chip_cols = st.columns(4)
with chip_cols[0]:
    if st.button("🥗 High-Protein Snack", use_container_width=True, key="chip_snack"):
        handle_quick_query("Suggest 3 quick, high-protein Indian evening snacks.")
with chip_cols[1]:
    if st.button("🍳 Boiled Eggs Macros", use_container_width=True, key="chip_eggs"):
        handle_quick_query(
            "What are the calories and protein in 3 boiled whole eggs vs 3 egg whites?"
        )
with chip_cols[2]:
    if st.button("🍚 Rice Portion Tip", use_container_width=True, key="chip_rice"):
        handle_quick_query(
            "How do I visually estimate 1 cup of cooked rice without a weighing scale?"
        )
with chip_cols[3]:
    if st.button("📊 Review Today's Goals", use_container_width=True, key="chip_goals"):
        handle_quick_query(
            "How am I doing on my calories and protein today based on what I've logged?"
        )

# Fixed bottom chat input with photo support
user_input = st.chat_input(
    "Ask Coach Macroly, or clarify meal details / attach photo...",
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

    with st.spinner("Crunching the numbers..."):
        answer = ask_gemini(parts)
    add_message("assistant", "text", answer)
    st.rerun()
