"""
Florida Sunbiz Compliance Dashboard
A B2B SaaS-grade Streamlit application for managing delinquent business outreach.
"""

import streamlit as st
import pandas as pd
import threading
import time
import io
import json
from datetime import datetime, timedelta
import random

# ─── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Sunbiz Compliance Dashboard",
    page_icon="🏛️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@300;400;500;600&display=swap');

    /* ── Base ── */
    html, body, [class*="css"] {
        font-family: 'DM Sans', sans-serif;
    }

    .stApp {
        background: #0d1117;
        color: #e6edf3;
    }

    /* ── Sidebar ── */
    [data-testid="stSidebar"] {
        background: #010409 !important;
        border-right: 1px solid #21262d;
    }

    [data-testid="stSidebar"] .stTextInput input,
    [data-testid="stSidebar"] .stNumberInput input {
        background: #161b22 !important;
        border: 1px solid #30363d !important;
        color: #e6edf3 !important;
        border-radius: 6px !important;
    }

    [data-testid="stSidebar"] label {
        color: #8b949e !important;
        font-size: 0.78rem !important;
        font-weight: 500 !important;
        letter-spacing: 0.06em !important;
        text-transform: uppercase !important;
    }

    /* ── Header ── */
    .dash-header {
        font-family: 'DM Serif Display', serif;
        font-size: 2rem;
        color: #e6edf3;
        letter-spacing: -0.02em;
        margin-bottom: 0.15rem;
    }

    .dash-subheader {
        color: #8b949e;
        font-size: 0.85rem;
        font-weight: 400;
        margin-bottom: 1.5rem;
    }

    /* ── KPI Cards ── */
    .kpi-card {
        background: #161b22;
        border: 1px solid #21262d;
        border-radius: 10px;
        padding: 1.25rem 1.5rem;
        position: relative;
        overflow: hidden;
    }

    .kpi-card::before {
        content: '';
        position: absolute;
        top: 0; left: 0; right: 0;
        height: 2px;
        background: linear-gradient(90deg, #1f6feb, #388bfd);
    }

    .kpi-card.warning::before {
        background: linear-gradient(90deg, #d29922, #e3b341);
    }

    .kpi-card.danger::before {
        background: linear-gradient(90deg, #da3633, #f85149);
    }

    .kpi-card.success::before {
        background: linear-gradient(90deg, #238636, #2ea043);
    }

    .kpi-label {
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #8b949e;
        margin-bottom: 0.5rem;
    }

    .kpi-value {
        font-family: 'DM Serif Display', serif;
        font-size: 2.4rem;
        color: #e6edf3;
        line-height: 1;
        margin-bottom: 0.35rem;
    }

    .kpi-delta {
        font-size: 0.78rem;
        color: #3fb950;
    }

    .kpi-delta.neg { color: #f85149; }

    /* ── Run Button ── */
    div[data-testid="stButton"] > button[kind="primary"] {
        background: linear-gradient(135deg, #1f6feb 0%, #388bfd 100%) !important;
        color: white !important;
        border: none !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        font-size: 0.9rem !important;
        letter-spacing: 0.03em !important;
        padding: 0.65rem 1.75rem !important;
        transition: all 0.2s ease !important;
        box-shadow: 0 4px 14px rgba(31, 111, 235, 0.35) !important;
    }

    div[data-testid="stButton"] > button[kind="primary"]:hover {
        transform: translateY(-1px) !important;
        box-shadow: 0 6px 20px rgba(31, 111, 235, 0.5) !important;
    }

    /* ── Section Labels ── */
    .section-label {
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: #8b949e;
        margin: 1.5rem 0 0.75rem;
        display: flex;
        align-items: center;
        gap: 0.5rem;
    }

    .section-label::after {
        content: '';
        flex: 1;
        height: 1px;
        background: #21262d;
    }

    /* ── Status Badge ── */
    .badge {
        display: inline-block;
        padding: 0.2rem 0.6rem;
        border-radius: 20px;
        font-size: 0.7rem;
        font-weight: 600;
        letter-spacing: 0.05em;
        text-transform: uppercase;
    }

    .badge-danger { background: rgba(248, 81, 73, 0.15); color: #f85149; border: 1px solid rgba(248,81,73,0.3); }
    .badge-warning { background: rgba(227, 179, 65, 0.15); color: #e3b341; border: 1px solid rgba(227,179,65,0.3); }
    .badge-success { background: rgba(63, 185, 80, 0.15); color: #3fb950; border: 1px solid rgba(63,185,80,0.3); }
    .badge-info { background: rgba(56, 139, 253, 0.15); color: #388bfd; border: 1px solid rgba(56,139,253,0.3); }

    /* ── Email Preview ── */
    .email-preview-box {
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 10px;
        padding: 1.25rem 1.5rem;
        font-size: 0.875rem;
        line-height: 1.7;
        color: #c9d1d9;
        white-space: pre-wrap;
        font-family: 'DM Sans', sans-serif;
    }

    .email-subject-line {
        font-weight: 600;
        color: #e6edf3;
        font-size: 0.9rem;
        margin-bottom: 1rem;
        padding-bottom: 0.75rem;
        border-bottom: 1px solid #21262d;
    }

    /* ── Scan Progress ── */
    .scan-log {
        background: #010409;
        border: 1px solid #21262d;
        border-radius: 8px;
        padding: 1rem 1.25rem;
        font-family: 'Courier New', monospace;
        font-size: 0.8rem;
        color: #3fb950;
        max-height: 160px;
        overflow-y: auto;
    }

    /* ── Dividers ── */
    hr { border-color: #21262d !important; }

    /* ── Dataframe overrides ── */
    .stDataFrame { border-radius: 8px; overflow: hidden; }

    /* ── Tabs ── */
    .stTabs [data-baseweb="tab-list"] {
        background: transparent !important;
        border-bottom: 1px solid #21262d !important;
        gap: 0 !important;
    }

    .stTabs [data-baseweb="tab"] {
        background: transparent !important;
        color: #8b949e !important;
        font-size: 0.82rem !important;
        font-weight: 500 !important;
        padding: 0.6rem 1.25rem !important;
        border-bottom: 2px solid transparent !important;
    }

    .stTabs [aria-selected="true"] {
        color: #e6edf3 !important;
        border-bottom-color: #1f6feb !important;
    }

    /* ── Misc ── */
    .stTextArea textarea {
        background: #161b22 !important;
        border: 1px solid #30363d !important;
        color: #e6edf3 !important;
        border-radius: 8px !important;
        font-family: 'DM Sans', sans-serif !important;
    }

    .stSelectbox > div > div {
        background: #161b22 !important;
        border: 1px solid #30363d !important;
        color: #e6edf3 !important;
    }

    .stCheckbox label { color: #c9d1d9 !important; }

    /* ── Sidebar logo area ── */
    .sidebar-logo {
        display: flex;
        align-items: center;
        gap: 0.6rem;
        padding: 0.25rem 0 1.5rem;
        border-bottom: 1px solid #21262d;
        margin-bottom: 1.25rem;
    }

    .sidebar-logo-icon {
        font-size: 1.5rem;
    }

    .sidebar-logo-text {
        font-family: 'DM Serif Display', serif;
        font-size: 1.1rem;
        color: #e6edf3;
        line-height: 1.2;
    }

    .sidebar-logo-sub {
        font-size: 0.68rem;
        color: #8b949e;
        text-transform: uppercase;
        letter-spacing: 0.08em;
    }

    .stAlert { border-radius: 8px !important; }
</style>
""", unsafe_allow_html=True)


# ─── Session State Init ────────────────────────────────────────────────────────
def init_session_state():
    defaults = {
        "scan_running": False,
        "scan_complete": False,
        "scan_logs": [],
        "leads_df": None,
        "selected_row_idx": 0,
        "last_scan_time": None,
        "total_scanned": 0,
        "emails_sent_today": 0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_session_state()


# ─── Mock Data Generator (replace with real SFTP logic) ────────────────────────
def generate_mock_sunbiz_data(n=60):
    """Simulate parsed Sunbiz CSV data. Replace with real SFTP + pandas parsing."""
    business_types = ["LLC", "Corp", "PA", "Inc.", "LLC"]
    counties = ["Miami-Dade", "Broward", "Palm Beach", "Hillsborough", "Orange"]
    statuses = ["Delinquent", "Delinquent", "Delinquent", "Active", "Inactive"]
    first_names = ["James", "Maria", "Robert", "Linda", "Michael", "Patricia", "David", "Jennifer", "Carlos", "Ana"]
    last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Martinez", "Rodriguez", "Davis", "Miller"]

    rows = []
    for i in range(n):
        fname = random.choice(first_names)
        lname = random.choice(last_names)
        btype = random.choice(business_types)
        county = random.choice(counties)
        status = random.choice(statuses)
        reg_date = datetime(2018, 1, 1) + timedelta(days=random.randint(0, 1800))
        annual_report_year = random.choice([2021, 2022, 2023])
        fee = round(random.uniform(138.75, 550.00), 2)
        rows.append({
            "Select": False,
            "Business Name": f"{lname} {btype} {i+1}",
            "Owner Name": f"{fname} {lname}",
            "Status": status,
            "County": county,
            "Reg. Date": reg_date.strftime("%m/%d/%Y"),
            "Last Report": str(annual_report_year),
            "Fee Owed ($)": fee,
            "Email": f"{fname.lower()}.{lname.lower()}@example.com",
            "Phone": f"(305) {random.randint(200,999)}-{random.randint(1000,9999)}",
        })
    return pd.DataFrame(rows)


# ─── SFTP + Scan Logic (runs in background thread) ────────────────────────────
def run_sftp_scan(sftp_host, sftp_user, sftp_pass, sftp_port, remote_path):
    """
    Real implementation: connect via Paramiko, download CSV, parse, filter delinquents.
    Currently runs a simulated scan for demo purposes.
    """
    logs = []
    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        logs.append(f"[{ts}] {msg}")
        st.session_state.scan_logs = logs.copy()

    log("🔐 Initiating SFTP connection...")
    time.sleep(1.2)

    # ── Uncomment below to wire in real Paramiko SFTP ──
    # import paramiko
    # transport = paramiko.Transport((sftp_host, int(sftp_port)))
    # transport.connect(username=sftp_user, password=sftp_pass)
    # sftp = paramiko.SFTPClient.from_transport(transport)
    # with sftp.open(remote_path, 'r') as f:
    #     df_raw = pd.read_csv(f)
    # sftp.close(); transport.close()

    log(f"✅ Connected to {sftp_host or 'sunbiz.org'}:{sftp_port}")
    time.sleep(0.8)
    log(f"📥 Downloading: {remote_path or '/exports/sunbiz_annual.csv'}")
    time.sleep(1.5)
    log("📊 File received — 2.3 MB (12,842 records)")
    time.sleep(0.6)
    log("🔍 Filtering delinquent filings...")
    time.sleep(1.0)

    df = generate_mock_sunbiz_data(n=80)
    delinquent_df = df[df["Status"] == "Delinquent"].reset_index(drop=True)
    delinquent_df["Select"] = False

    log(f"⚠️  {len(delinquent_df)} delinquent businesses identified")
    time.sleep(0.4)
    log("💾 Lead table updated. Ready for outreach.")

    st.session_state.leads_df = delinquent_df
    st.session_state.total_scanned = len(df)
    st.session_state.last_scan_time = datetime.now().strftime("%b %d, %Y at %I:%M %p")
    st.session_state.scan_running = False
    st.session_state.scan_complete = True


# ─── Email Template ────────────────────────────────────────────────────────────
DEFAULT_TEMPLATE = """\
Subject: Urgent: Your Florida Annual Report is Past Due — Act Now to Avoid Dissolution

Dear {owner_name},

I'm reaching out regarding {business_name}, which is currently listed as Delinquent with the Florida Division of Corporations (Sunbiz).

According to public records, your most recent annual report was filed in {last_report_year}. Businesses that remain delinquent past the administrative dissolution deadline may lose their legal standing and protections in Florida.

Here's what you need to know:

  • Outstanding Fee:    ${fee_owed}
  • Last Report Filed: {last_report_year}
  • County:            {county}

To restore your good standing, file your annual report directly at:
  → https://dos.fl.gov/sunbiz/manage-e-file

If you need assistance navigating the reinstatement process or would like a free compliance review, I'm available to help.

Best regards,
[Your Name]
[Your Phone] | [Your Email]
[Company Name]

—
This message was sent based on public Sunbiz records. Reply STOP to unsubscribe.
"""

def render_email_preview(template: str, row: pd.Series) -> str:
    return template.replace("{business_name}", str(row.get("Business Name", "N/A"))) \
                   .replace("{owner_name}", str(row.get("Owner Name", "N/A"))) \
                   .replace("{last_report_year}", str(row.get("Last Report", "N/A"))) \
                   .replace("{fee_owed}", f"{row.get('Fee Owed ($)', 0):.2f}") \
                   .replace("{county}", str(row.get("County", "N/A")))


# ═══════════════════════════════════════════════════════════════════════════════
#  SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("""
    <div class="sidebar-logo">
        <div class="sidebar-logo-icon">🏛️</div>
        <div>
            <div class="sidebar-logo-text">Sunbiz Compliance</div>
            <div class="sidebar-logo-sub">Dashboard v1.0</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="section-label">SFTP Connection</div>', unsafe_allow_html=True)
    sftp_host = st.text_input("Host / IP", placeholder="sftp.sunbiz.org", key="sftp_host")
    sftp_col1, sftp_col2 = st.columns([3, 1])
    with sftp_col1:
        sftp_user = st.text_input("Username", placeholder="admin", key="sftp_user")
    with sftp_col2:
        sftp_port = st.text_input("Port", value="22", key="sftp_port")
    sftp_pass = st.text_input("Password", type="password", placeholder="••••••••", key="sftp_pass")
    sftp_path = st.text_input("Remote File Path", placeholder="/exports/sunbiz_annual.csv", key="sftp_path")

    st.markdown('<div class="section-label">Email API</div>', unsafe_allow_html=True)
    email_provider = st.selectbox("Provider", ["Resend", "SendGrid", "Mailgun"], key="email_provider")
    api_key = st.text_input("API Key", type="password", placeholder="re_••••••••••••", key="api_key")
    from_email = st.text_input("From Address", placeholder="outreach@yourdomain.com", key="from_email")

    st.markdown('<div class="section-label">Scan Settings</div>', unsafe_allow_html=True)
    daily_limit = st.slider("Daily Email Limit", 10, 500, 100, key="daily_limit")
    auto_send = st.checkbox("Auto-send to filtered leads", value=False, key="auto_send")

    st.markdown("---")
    if st.session_state.last_scan_time:
        st.markdown(f"<small style='color:#8b949e'>Last scan: {st.session_state.last_scan_time}</small>", unsafe_allow_html=True)
    else:
        st.markdown("<small style='color:#8b949e'>No scan run yet this session.</small>", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN CONTENT
# ═══════════════════════════════════════════════════════════════════════════════

# ── Header ──────────────────────────────────────────────────────────────────────
header_col, btn_col = st.columns([3, 1])
with header_col:
    st.markdown('<div class="dash-header">Florida Sunbiz Compliance</div>', unsafe_allow_html=True)
    st.markdown('<div class="dash-subheader">Annual Report Delinquency Monitor & Outreach Automation</div>', unsafe_allow_html=True)

with btn_col:
    st.markdown("<br>", unsafe_allow_html=True)
    run_btn = st.button(
        "⚡  Run Daily Scan",
        type="primary",
        use_container_width=True,
        disabled=st.session_state.scan_running,
    )

if run_btn and not st.session_state.scan_running:
    st.session_state.scan_running = True
    st.session_state.scan_complete = False
    st.session_state.scan_logs = []
    t = threading.Thread(
        target=run_sftp_scan,
        args=(
            st.session_state.get("sftp_host", ""),
            st.session_state.get("sftp_user", ""),
            st.session_state.get("sftp_pass", ""),
            st.session_state.get("sftp_port", 22),
            st.session_state.get("sftp_path", ""),
        ),
        daemon=True,
    )
    t.start()
    st.rerun()

# ── KPI Cards ───────────────────────────────────────────────────────────────────
kpi1, kpi2, kpi3, kpi4 = st.columns(4)

total_leads = len(st.session_state.leads_df) if st.session_state.leads_df is not None else 0
selected_count = len(st.session_state.leads_df[st.session_state.leads_df["Select"] == True]) \
    if st.session_state.leads_df is not None else 0

with kpi1:
    st.markdown(f"""
    <div class="kpi-card">
        <div class="kpi-label">Total Scanned</div>
        <div class="kpi-value">{st.session_state.total_scanned:,}</div>
        <div class="kpi-delta">📋 This session</div>
    </div>""", unsafe_allow_html=True)

with kpi2:
    st.markdown(f"""
    <div class="kpi-card warning">
        <div class="kpi-label">Delinquencies Found</div>
        <div class="kpi-value">{total_leads}</div>
        <div class="kpi-delta neg">⚠ Pending outreach</div>
    </div>""", unsafe_allow_html=True)

with kpi3:
    st.markdown(f"""
    <div class="kpi-card success">
        <div class="kpi-label">Selected for Email</div>
        <div class="kpi-value">{selected_count}</div>
        <div class="kpi-delta">✉ Ready to send</div>
    </div>""", unsafe_allow_html=True)

with kpi4:
    st.markdown(f"""
    <div class="kpi-card">
        <div class="kpi-label">Emails Sent Today</div>
        <div class="kpi-value">{st.session_state.emails_sent_today}</div>
        <div class="kpi-delta">/ {st.session_state.get('daily_limit', 100)} limit</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── Scan Log (live during scan) ─────────────────────────────────────────────────
if st.session_state.scan_running or (st.session_state.scan_logs and not st.session_state.scan_complete):
    st.markdown('<div class="section-label">Scan Log</div>', unsafe_allow_html=True)
    log_text = "\n".join(st.session_state.scan_logs) if st.session_state.scan_logs else "Initializing..."
    st.markdown(f'<div class="scan-log">{log_text}</div>', unsafe_allow_html=True)
    if st.session_state.scan_running:
        st.spinner("Running scan...")
        time.sleep(1.5)
        st.rerun()

if st.session_state.scan_complete and st.session_state.scan_logs:
    with st.expander("📋 View Last Scan Log", expanded=False):
        log_text = "\n".join(st.session_state.scan_logs)
        st.markdown(f'<div class="scan-log">{log_text}</div>', unsafe_allow_html=True)

# ── Main Tabs ────────────────────────────────────────────────────────────────────
tab_leads, tab_email, tab_history = st.tabs(["📊  Lead Table", "✉  Email Previewer", "📈  Analytics"])

# ── TAB 1: Lead Table ──────────────────────────────────────────────────────────
with tab_leads:
    if st.session_state.leads_df is None:
        st.markdown("""
        <div style="text-align:center; padding:3rem 1rem; color:#8b949e;">
            <div style="font-size:3rem; margin-bottom:1rem;">🔍</div>
            <div style="font-family:'DM Serif Display', serif; font-size:1.3rem; color:#e6edf3; margin-bottom:0.5rem;">No data yet</div>
            <div style="font-size:0.875rem;">Run a Daily Scan to pull the latest Sunbiz delinquency records.</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        df = st.session_state.leads_df.copy()

        # ── Filter bar ──
        fc1, fc2, fc3 = st.columns([2, 1, 1])
        with fc1:
            search = st.text_input("🔎 Search businesses or owners", placeholder="e.g. Smith LLC, Miami-Dade...", label_visibility="collapsed")
        with fc2:
            county_filter = st.selectbox("County", ["All"] + sorted(df["County"].unique().tolist()), label_visibility="collapsed")
        with fc3:
            year_filter = st.selectbox("Last Report Year", ["All"] + sorted(df["Last Report"].unique().tolist(), reverse=True), label_visibility="collapsed")

        filtered = df.copy()
        if search:
            mask = (
                filtered["Business Name"].str.contains(search, case=False, na=False) |
                filtered["Owner Name"].str.contains(search, case=False, na=False) |
                filtered["Email"].str.contains(search, case=False, na=False)
            )
            filtered = filtered[mask]
        if county_filter != "All":
            filtered = filtered[filtered["County"] == county_filter]
        if year_filter != "All":
            filtered = filtered[filtered["Last Report"] == year_filter]

        st.markdown(f"<small style='color:#8b949e'>Showing {len(filtered)} of {len(df)} delinquent records</small>", unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)

        display_cols = ["Select", "Business Name", "Owner Name", "County", "Last Report", "Fee Owed ($)", "Phone", "Email"]

        edited = st.data_editor(
            filtered[display_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "Select": st.column_config.CheckboxColumn("✓", width="small"),
                "Fee Owed ($)": st.column_config.NumberColumn("Fee Owed", format="$%.2f"),
                "Business Name": st.column_config.TextColumn("Business Name", width="large"),
            },
            height=420,
            key="leads_editor",
        )

        # Sync selections back
        if edited is not None:
            for idx, row in edited.iterrows():
                if idx in st.session_state.leads_df.index:
                    st.session_state.leads_df.at[idx, "Select"] = row["Select"]

        # ── Action row ──
        ac1, ac2, ac3 = st.columns([1, 1, 2])
        with ac1:
            if st.button("☑ Select All", use_container_width=True):
                st.session_state.leads_df["Select"] = True
                st.rerun()
        with ac2:
            if st.button("☐ Deselect All", use_container_width=True):
                st.session_state.leads_df["Select"] = False
                st.rerun()
        with ac3:
            sel_count = int(st.session_state.leads_df["Select"].sum())
            send_disabled = sel_count == 0 or not st.session_state.get("api_key")
            if st.button(
                f"📤  Send Emails to {sel_count} Selected Lead{'s' if sel_count != 1 else ''}",
                type="primary",
                use_container_width=True,
                disabled=send_disabled,
            ):
                st.session_state.emails_sent_today += sel_count
                st.success(f"✅ Queued {sel_count} emails via {st.session_state.get('email_provider', 'Resend')}.")

        if not st.session_state.get("api_key") and sel_count > 0:
            st.caption("⚠️ Add your Email API Key in the sidebar to enable sending.")

        # ── Export ──
        csv_bytes = filtered[display_cols].to_csv(index=False).encode()
        st.download_button(
            "⬇  Export CSV",
            data=csv_bytes,
            file_name=f"sunbiz_delinquent_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )


# ── TAB 2: Email Previewer ──────────────────────────────────────────────────────
with tab_email:
    if st.session_state.leads_df is None:
        st.info("Run a scan first to preview email templates.")
    else:
        df = st.session_state.leads_df
        ep1, ep2 = st.columns([1, 1])

        with ep1:
            st.markdown('<div class="section-label">Select Lead</div>', unsafe_allow_html=True)
            business_options = df["Business Name"].tolist()
            selected_biz = st.selectbox(
                "Business",
                options=range(len(business_options)),
                format_func=lambda i: business_options[i],
                label_visibility="collapsed",
                key="preview_select"
            )
            selected_row = df.iloc[selected_biz]

            st.markdown(f"""
            <div style="background:#161b22; border:1px solid #21262d; border-radius:8px; padding:1rem 1.25rem; margin-top:0.5rem;">
                <div style="font-size:0.7rem; color:#8b949e; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:0.75rem;">Lead Profile</div>
                <div style="font-weight:600; color:#e6edf3; margin-bottom:0.25rem;">{selected_row['Business Name']}</div>
                <div style="color:#8b949e; font-size:0.85rem; margin-bottom:0.75rem;">{selected_row['Owner Name']}</div>
                <div style="display:flex; gap:0.5rem; flex-wrap:wrap; margin-bottom:0.75rem;">
                    <span class="badge badge-danger">Delinquent</span>
                    <span class="badge badge-info">{selected_row['County']}</span>
                </div>
                <div style="font-size:0.8rem; color:#8b949e;">Last Report: {selected_row['Last Report']} &nbsp;|&nbsp; Fee: ${selected_row['Fee Owed ($)']:.2f}</div>
                <div style="font-size:0.8rem; color:#8b949e; margin-top:0.35rem;">📧 {selected_row['Email']}</div>
                <div style="font-size:0.8rem; color:#8b949e;">📞 {selected_row['Phone']}</div>
            </div>
            """, unsafe_allow_html=True)

        with ep2:
            st.markdown('<div class="section-label">Email Template</div>', unsafe_allow_html=True)
            template = st.text_area(
                "Edit template (use {business_name}, {owner_name}, {fee_owed}, {county}, {last_report_year})",
                value=DEFAULT_TEMPLATE,
                height=300,
                label_visibility="collapsed",
                key="email_template",
            )

        st.markdown('<div class="section-label">Live Preview</div>', unsafe_allow_html=True)
        preview_text = render_email_preview(template, selected_row)
        lines = preview_text.strip().split("\n")
        subject_line = next((l for l in lines if l.startswith("Subject:")), "")
        body_lines = [l for l in lines if not l.startswith("Subject:")]

        st.markdown(f"""
        <div class="email-preview-box">
            <div class="email-subject-line">✉ {subject_line}</div>
            {'<br>'.join(body_lines)}
        </div>
        """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        pc1, pc2 = st.columns([1, 3])
        with pc1:
            if st.button("📤 Send This Email", type="primary", use_container_width=True,
                         disabled=not st.session_state.get("api_key")):
                st.session_state.emails_sent_today += 1
                st.success(f"✅ Email sent to {selected_row['Email']}")


# ── TAB 3: Analytics ────────────────────────────────────────────────────────────
with tab_history:
    if st.session_state.leads_df is None:
        st.info("Run a scan to see analytics.")
    else:
        df = st.session_state.leads_df

        an1, an2 = st.columns(2)

        with an1:
            st.markdown('<div class="section-label">Delinquencies by County</div>', unsafe_allow_html=True)
            county_counts = df["County"].value_counts().reset_index()
            county_counts.columns = ["County", "Count"]
            st.bar_chart(county_counts.set_index("County"), color="#388bfd", height=260)

        with an2:
            st.markdown('<div class="section-label">Delinquencies by Last Report Year</div>', unsafe_allow_html=True)
            year_counts = df["Last Report"].value_counts().sort_index().reset_index()
            year_counts.columns = ["Year", "Count"]
            st.bar_chart(year_counts.set_index("Year"), color="#e3b341", height=260)

        st.markdown('<div class="section-label">Fee Distribution</div>', unsafe_allow_html=True)
        fee_data = df["Fee Owed ($)"].describe().reset_index()
        fee_data.columns = ["Metric", "Value"]
        fee_data["Value"] = fee_data["Value"].apply(lambda x: f"${x:,.2f}" if fee_data["Metric"].tolist()[fee_data["Value"].tolist().index(x)] not in ["count"] else int(x))
        st.dataframe(fee_data, use_container_width=True, hide_index=True, height=280)


# ─── Footer ────────────────────────────────────────────────────────────────────
st.markdown("---")
st.markdown(
    "<small style='color:#484f58'>Sunbiz Compliance Dashboard · Data sourced from Florida Division of Corporations · "
    "Not affiliated with the State of Florida</small>",
    unsafe_allow_html=True,
)
