"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         FLORIDA SUNBIZ COMPLIANCE DASHBOARD  —  All-in-One v2.0            ║
║  Modules: SFTP Pipeline · Fixed-Width Parser · SQLite CRM · Streamlit GUI  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

# ── Standard Library ──────────────────────────────────────────────────────────
import os
import io
import time
import sqlite3
import textwrap
import random
from datetime import datetime, date
from typing import Optional

# ── Third-Party ───────────────────────────────────────────────────────────────
import streamlit as st
import pandas as pd
import paramiko

# ═════════════════════════════════════════════════════════════════════════════
#  PAGE CONFIG  (must be first Streamlit call)
# ═════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="Sunbiz Compliance Pro",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ═════════════════════════════════════════════════════════════════════════════
#  GLOBAL CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════
SFTP_HOST    = "sftp.floridados.gov"
SFTP_USER    = "Public"
SFTP_PASS    = "PubAccess1845!"
SFTP_PORT    = 22
COR_PATH     = "/public/cor/daily/"
LLC_PATH     = "/public/llc/daily/"

DB_PATH      = "sunbiz_leads.db"
PENALTY_FEE  = 400
DEADLINE     = "May 1, 2026"
CURRENT_YEAR = 2026

# ─── Fixed-Width Field Specs (Sunbiz CORLIST/LLCLIST layout) ─────────────────
# Each tuple is (start_offset, length). Adjust if floridados.gov updates format.
FW_FIELDS = {
    "record_type"    : (0,   2),
    "entity_number"  : (2,   12),
    "status"         : (14,  1),    # 'A'=Active 'I'=Inactive 'D'=Dissolved
    "filing_date"    : (15,  8),    # YYYYMMDD
    "entity_name"    : (23,  120),
    "last_rpt_year"  : (143, 4),
    "principal_addr" : (147, 60),
    "principal_email": (207, 60),
    "owner_name"     : (267, 60),
    "state_of_inc"   : (327, 2),
    "fei_number"     : (329, 10),
}
FW_RECORD_LEN = 339


# ═════════════════════════════════════════════════════════════════════════════
#  MODULE 3 — SQLite Lead Management
# ═════════════════════════════════════════════════════════════════════════════

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_number    TEXT    UNIQUE NOT NULL,
                entity_name      TEXT    NOT NULL,
                owner_name       TEXT,
                principal_email  TEXT,
                principal_addr   TEXT,
                status           TEXT    DEFAULT 'A',
                last_rpt_year    INTEGER,
                filing_date      TEXT,
                record_type      TEXT,
                contact_status   TEXT    DEFAULT 'New',
                source_file      TEXT,
                inserted_at      TEXT    DEFAULT (datetime('now')),
                last_updated     TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_number TEXT NOT NULL,
                sent_at       TEXT DEFAULT (datetime('now')),
                subject       TEXT,
                body_snippet  TEXT,
                api_response  TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_cs ON leads(contact_status)")
        conn.commit()


def db_upsert_leads(records: list) -> tuple:
    inserted = skipped = 0
    with db_connect() as conn:
        for r in records:
            try:
                conn.execute("""
                    INSERT INTO leads
                        (entity_number,entity_name,owner_name,principal_email,
                         principal_addr,status,last_rpt_year,filing_date,
                         record_type,source_file)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    r["entity_number"], r["entity_name"],
                    r.get("owner_name",""), r.get("principal_email",""),
                    r.get("principal_addr",""), r.get("status","A"),
                    r.get("last_rpt_year"), r.get("filing_date",""),
                    r.get("record_type",""), r.get("source_file",""),
                ))
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
        conn.commit()
    return inserted, skipped


def db_get_leads(search: str = "", status_filter: str = "All") -> pd.DataFrame:
    q = "SELECT * FROM leads WHERE 1=1"
    p = []
    if search:
        q += " AND (entity_name LIKE ? OR owner_name LIKE ? OR principal_email LIKE ?)"
        p.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    if status_filter != "All":
        q += " AND contact_status = ?"
        p.append(status_filter)
    q += " ORDER BY inserted_at DESC"
    with db_connect() as conn:
        return pd.read_sql_query(q, conn, params=p)


def db_update_status(entity_number: str, new_status: str):
    with db_connect() as conn:
        conn.execute(
            "UPDATE leads SET contact_status=?,last_updated=datetime('now') WHERE entity_number=?",
            (new_status, entity_number)
        )
        conn.commit()


def db_log_email(entity_number: str, subject: str, body: str, api_resp: str):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO email_log (entity_number,subject,body_snippet,api_response) VALUES (?,?,?,?)",
            (entity_number, subject, body[:300], api_resp)
        )
        conn.commit()


def db_stats() -> dict:
    with db_connect() as conn:
        total     = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        new_ct    = conn.execute("SELECT COUNT(*) FROM leads WHERE contact_status='New'").fetchone()[0]
        contacted = conn.execute("SELECT COUNT(*) FROM leads WHERE contact_status='Contacted'").fetchone()[0]
        paid      = conn.execute("SELECT COUNT(*) FROM leads WHERE contact_status='Paid'").fetchone()[0]
        emailed   = conn.execute("SELECT COUNT(*) FROM email_log").fetchone()[0]
    return {"total":total,"new":new_ct,"contacted":contacted,"paid":paid,"emailed":emailed}


# ═════════════════════════════════════════════════════════════════════════════
#  MODULE 2 — Fixed-Width Parser & Filter
# ═════════════════════════════════════════════════════════════════════════════

def _clean(raw: bytes) -> str:
    return raw.decode("latin-1", errors="replace").strip()


def parse_fw_line(line: bytes, source_file: str = "") -> Optional[dict]:
    if len(line) < FW_RECORD_LEN:
        return None

    def f(name):
        s, l = FW_FIELDS[name]
        return _clean(line[s: s + l])

    rec_type = f("record_type")
    if rec_type in ("HD", "TR", "  ", ""):
        return None

    status       = f("status")
    last_rpt_raw = f("last_rpt_year")
    try:
        last_rpt_year = int(last_rpt_raw) if last_rpt_raw.isdigit() else 0
    except ValueError:
        last_rpt_year = 0

    # ── CORE FILTER: Active AND not yet filed 2026 ────────────────────────
    if status != "A":
        return None
    if last_rpt_year >= CURRENT_YEAR:
        return None

    entity_name = f("entity_name")
    if not entity_name:
        return None

    return {
        "record_type"    : rec_type,
        "entity_number"  : f("entity_number"),
        "status"         : status,
        "filing_date"    : f("filing_date"),
        "entity_name"    : entity_name,
        "last_rpt_year"  : last_rpt_year,
        "principal_addr" : f("principal_addr"),
        "principal_email": f("principal_email"),
        "owner_name"     : f("owner_name"),
        "source_file"    : source_file,
    }


def parse_fw_buffer(data: bytes, source_file: str = "") -> list:
    records = []
    for raw_line in data.split(b"\n"):
        line = raw_line.rstrip(b"\r")
        result = parse_fw_line(line, source_file)
        if result:
            records.append(result)
    return records


# ═════════════════════════════════════════════════════════════════════════════
#  MODULE 1 — SFTP Pipeline (runs synchronously in main thread via st.status)
# ═════════════════════════════════════════════════════════════════════════════

def _latest_file(sftp: paramiko.SFTPClient, remote_dir: str, status_write) -> Optional[str]:
    try:
        attrs = sftp.listdir_attr(remote_dir)
        if not attrs:
            return None
        attrs.sort(key=lambda a: a.st_mtime or 0, reverse=True)
        return remote_dir + attrs[0].filename
    except Exception as e:
        status_write(f"⚠  Cannot list {remote_dir}: {e}")
        return None


def _generate_mock(n: int = 350) -> list:
    biz_types  = ["LLC","CORP","INC","PA","PL","LLP"]
    first_names= ["James","Maria","Robert","Linda","Michael","Patricia",
                  "Carlos","Ana","David","Jennifer","Luis","Sofia"]
    last_names = ["Smith","Johnson","Williams","Garcia","Martinez","Rodriguez",
                  "Brown","Jones","Davis","Miller","Wilson","Taylor"]
    records = []
    for i in range(n):
        fn, ln = random.choice(first_names), random.choice(last_names)
        yr     = random.choice([2022,2023,2024,2025])
        num    = f"L{random.randint(10000000,99999999)}"
        records.append({
            "record_type"    : random.choice(["LC","CP"]),
            "entity_number"  : num,
            "status"         : "A",
            "filing_date"    : f"20{random.randint(10,22):02d}0{random.randint(1,9)}01",
            "entity_name"    : f"{ln} {random.choice(biz_types)} {i+1}",
            "last_rpt_year"  : yr,
            "principal_addr" : f"{random.randint(100,9999)} {ln} BLVD, MIAMI FL",
            "principal_email": f"{fn.lower()}.{ln.lower()}{random.randint(1,99)}@gmail.com",
            "owner_name"     : f"{fn} {ln}",
            "source_file"    : "MOCK_DATA",
        })
    return records


def run_pipeline_sync():
    """
    Runs the full pipeline synchronously inside an st.status() context so
    Streamlit can stream live updates without threading issues.
    Call this directly from the button handler — no threads needed.
    """
    all_records = []
    logs = []

    with st.status("Running pipeline…", expanded=True) as status:
        def w(msg):
            ts = datetime.now().strftime("%H:%M:%S")
            line = f"[{ts}] {msg}"
            logs.append(line)
            status.write(line)

        try:
            w(f"🔐 Connecting to {SFTP_HOST}:{SFTP_PORT} as '{SFTP_USER}' …")
            transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
            transport.connect(username=SFTP_USER, password=SFTP_PASS)
            sftp = paramiko.SFTPClient.from_transport(transport)
            w("✅ SFTP connection established.")

            for label, remote_dir in [("Corporations (COR)", COR_PATH),
                                       ("LLCs (LLC)",         LLC_PATH)]:
                w(f"📂 Scanning {label} → {remote_dir}")
                path = _latest_file(sftp, remote_dir, w)
                if not path:
                    w(f"⚠  No files found in {remote_dir}, skipping.")
                    continue
                filename = path.split("/")[-1]
                w(f"⬇  Downloading: {filename}")
                buf = io.BytesIO()
                sftp.getfo(path, buf)
                buf.seek(0)
                raw = buf.read()
                w(f"📦 {len(raw)/1024:,.1f} KB received — parsing …")
                recs = parse_fw_buffer(raw, source_file=filename)
                w(f"🎯 {len(recs):,} delinquent Active records found in {filename}")
                all_records.extend(recs)

            sftp.close()
            transport.close()
            w("🔌 SFTP connection closed.")

        except paramiko.AuthenticationException:
            w("❌ Authentication failed — check SFTP credentials in sidebar.")
            status.update(label="Pipeline failed", state="error", expanded=True)
            st.session_state.pipeline_logs = logs
            return

        except Exception as e:
            w(f"⚠  Live SFTP unavailable ({type(e).__name__}: {e})")
            w("🔄 Falling back to mock dataset for demonstration …")
            all_records = _generate_mock(350)
            w(f"✅ Mock dataset ready — {len(all_records):,} records.")

        if all_records:
            w(f"💾 Upserting {len(all_records):,} leads into SQLite …")
            inserted, skipped = db_upsert_leads(all_records)
            w(f"✅ Inserted: {inserted:,}  |  Duplicates skipped: {skipped:,}")
        else:
            w("ℹ  No new delinquent records found.")

        w("🏁 Pipeline complete.")
        status.update(label="✅ Pipeline complete", state="complete", expanded=False)

    st.session_state.pipeline_logs     = logs
    st.session_state.pipeline_complete = True
    st.session_state.last_pipeline_run = datetime.now().strftime("%b %d, %Y at %I:%M %p")


# ═════════════════════════════════════════════════════════════════════════════
#  EMAIL LOGIC
# ═════════════════════════════════════════════════════════════════════════════

EMAIL_SUBJECT = "Action Required: Your Florida Annual Report Is Past Due"

def build_email(entity_name, owner_name, last_rpt_year, entity_number) -> str:
    owner = (owner_name or "").strip() or "Business Owner"
    yr    = str(last_rpt_year) if last_rpt_year else "a prior year"
    return textwrap.dedent(f"""\
        Dear {owner},

        I'm reaching out because {entity_name} (Entity # {entity_number}) appears
        in Florida's public Sunbiz records as Active but without a 2026 Annual
        Report on file with the Division of Corporations.

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
          DEADLINE  :  {DEADLINE}
          LAST FILED:  {yr}
          LATE FEE  :  ${PENALTY_FEE:,} (assessed after {DEADLINE})
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        Businesses that miss the deadline face a ${PENALTY_FEE:,} late penalty and
        risk administrative dissolution — which can void contracts, bank accounts,
        and liability protections.

        To file, visit the official Florida DOS portal (takes ~10 min):
          → https://dos.fl.gov/sunbiz/manage-e-file/

        Filing fee: $138.75 for most LLCs and Corporations.

        If you have questions about the process or would like a complimentary
        compliance review, simply reply to this email.

        Best regards,
        [Your Name]
        [Your Company] · [Phone] · [Website]

        ─────────────────────────────────────────────────────────────────
        LEGAL DISCLAIMER: This message is a courtesy reminder based solely
        on public information from Florida's Sunbiz database. This service
        is NOT affiliated with, endorsed by, or acting on behalf of the
        State of Florida or the Florida Division of Corporations.
        Reply REMOVE to unsubscribe.
        ─────────────────────────────────────────────────────────────────
    """)


def send_email(to_address: str, subject: str, body: str, entity_number: str) -> dict:
    resend_key   = os.environ.get("RESEND_API_KEY")
    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    from_addr    = os.environ.get("FROM_EMAIL", "compliance@yourdomain.com")

    if resend_key:
        try:
            import resend
            resend.api_key = resend_key
            resp = resend.Emails.send({
                "from": from_addr, "to": [to_address],
                "subject": subject, "text": body,
            })
            return {"success": True, "message": f"Resend OK · ID: {resp.get('id','')}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    if sendgrid_key:
        try:
            import sendgrid
            from sendgrid.helpers.mail import Mail
            sg  = sendgrid.SendGridAPIClient(api_key=sendgrid_key)
            msg = Mail(from_email=from_addr, to_emails=to_address,
                       subject=subject, plain_text_content=body)
            r   = sg.send(msg)
            return {"success": True, "message": f"SendGrid {r.status_code}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # Mock
    time.sleep(0.4)
    return {"success": True,
            "message": "MOCK send — set RESEND_API_KEY or SENDGRID_API_KEY to send real emails."}


# ═════════════════════════════════════════════════════════════════════════════
#  CUSTOM CSS — Dark Editorial / Monospace
# ═════════════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

:root {
    --bg:      #08090a; --surface: #111316; --raised: #1a1d22;
    --border:  #2a2f38; --border2: #3d4450;
    --hi:      #f0f4f8; --md:      #a8b4c0; --lo:     #5a6472;
    --gold:    #e8a020; --gold-d:  rgba(232,160,32,.15);
    --red:     #e05252; --red-d:   rgba(224,82,82,.12);
    --green:   #4caf7d; --green-d: rgba(76,175,125,.12);
    --blue:    #5b9cf6; --blue-d:  rgba(91,156,246,.12);
}
html,body,[class*="css"]{ font-family:'IBM Plex Sans',sans-serif; background:var(--bg)!important; color:var(--hi)!important; }
.stApp{ background:var(--bg)!important; }

/* Sidebar */
[data-testid="stSidebar"]{ background:#060708!important; border-right:1px solid var(--border)!important; }
[data-testid="stSidebar"] .stTextInput input{ background:var(--raised)!important; border:1px solid var(--border)!important; color:var(--hi)!important; border-radius:4px!important; font-family:'IBM Plex Mono',monospace!important; font-size:.8rem!important; }
[data-testid="stSidebar"] label{ color:var(--lo)!important; font-size:.72rem!important; font-weight:500!important; letter-spacing:.1em!important; text-transform:uppercase!important; }

/* Headers */
.main-title{ font-family:'Playfair Display',serif; font-size:2.6rem; font-weight:900; color:var(--hi); letter-spacing:-.03em; line-height:1.1; }
.main-title span{ color:var(--gold); }
.main-sub{ font-size:.8rem; color:var(--lo); letter-spacing:.08em; text-transform:uppercase; margin-bottom:.3rem; }
.dbadge{ display:inline-block; background:var(--red-d); border:1px solid var(--red); color:var(--red); padding:.18rem .7rem; border-radius:2px; font-size:.72rem; font-weight:600; letter-spacing:.08em; text-transform:uppercase; font-family:'IBM Plex Mono',monospace; margin-bottom:1.5rem; }

/* KPI Grid */
.kpi-grid{ display:grid; grid-template-columns:repeat(4,1fr); gap:1px; background:var(--border); border:1px solid var(--border); border-radius:6px; overflow:hidden; margin-bottom:1.75rem; }
.kpi-cell{ background:var(--surface); padding:1.1rem 1.4rem; }
.kpi-lbl{ font-size:.67rem; font-weight:600; letter-spacing:.12em; text-transform:uppercase; color:var(--lo); margin-bottom:.4rem; }
.kpi-val{ font-family:'Playfair Display',serif; font-size:2.1rem; font-weight:700; color:var(--hi); line-height:1; margin-bottom:.25rem; }
.kpi-val.g{ color:var(--gold); } .kpi-val.r{ color:var(--red); } .kpi-val.gr{ color:var(--green); }
.kpi-sub{ font-size:.72rem; color:var(--lo); font-family:'IBM Plex Mono',monospace; }

/* Section heads */
.sh{ font-size:.67rem; font-weight:600; letter-spacing:.14em; text-transform:uppercase; color:var(--lo); padding:.4rem 0; border-bottom:1px solid var(--border); margin-bottom:.8rem; display:flex; align-items:center; gap:.45rem; }
.sh .dot{ width:5px; height:5px; border-radius:50%; background:var(--gold); flex-shrink:0; }

/* Buttons */
div[data-testid="stButton"]>button{ border-radius:3px!important; font-weight:600!important; font-size:.8rem!important; letter-spacing:.06em!important; text-transform:uppercase!important; }
div[data-testid="stButton"]>button[kind="primary"]{ background:var(--gold)!important; color:#000!important; border:none!important; box-shadow:0 2px 12px rgba(232,160,32,.3)!important; }
div[data-testid="stButton"]>button[kind="primary"]:hover{ background:#f0b030!important; }
div[data-testid="stButton"]>button[kind="secondary"]{ background:var(--raised)!important; color:var(--md)!important; border:1px solid var(--border2)!important; }

/* Log terminal */
.log-term{ background:#030405; border:1px solid var(--border); border-left:3px solid var(--gold); border-radius:4px; padding:.8rem 1rem; font-family:'IBM Plex Mono',monospace; font-size:.76rem; color:#7ec8a0; max-height:190px; overflow-y:auto; line-height:1.7; white-space:pre-wrap; }

/* Email card */
.email-card{ background:var(--surface); border:1px solid var(--border); border-radius:5px; padding:1.2rem 1.4rem; font-family:'IBM Plex Mono',monospace; font-size:.77rem; line-height:1.8; color:var(--md); white-space:pre-wrap; overflow-x:auto; }
.email-subj{ font-weight:600; font-family:'IBM Plex Sans',sans-serif; color:var(--hi); font-size:.88rem; margin-bottom:.8rem; padding-bottom:.65rem; border-bottom:1px solid var(--border); }

/* Lead card */
.lcard{ background:var(--surface); border:1px solid var(--border); border-left:3px solid var(--gold); border-radius:4px; padding:1rem 1.2rem; margin-bottom:.7rem; }
.lcard-name{ font-weight:600; color:var(--hi); font-size:.9rem; margin-bottom:.2rem; }
.lcard-det{ font-size:.77rem; color:var(--lo); font-family:'IBM Plex Mono',monospace; line-height:1.9; }

/* Penalty box */
.pbox{ background:var(--red-d); border:1px solid var(--red); border-radius:4px; padding:.8rem 1.1rem; margin-bottom:.9rem; }
.pnum{ font-family:'Playfair Display',serif; font-size:1.8rem; color:var(--red); font-weight:700; }
.plbl{ font-size:.7rem; color:var(--lo); text-transform:uppercase; letter-spacing:.1em; }

/* Badges */
.badge{ display:inline-block; padding:.14rem .5rem; border-radius:2px; font-size:.67rem; font-weight:600; letter-spacing:.07em; text-transform:uppercase; font-family:'IBM Plex Mono',monospace; }
.bn{ background:var(--blue-d); color:var(--blue); border:1px solid var(--blue); }
.bc{ background:var(--gold-d); color:var(--gold); border:1px solid var(--gold); }
.bp{ background:var(--green-d);color:var(--green);border:1px solid var(--green);}

/* Tabs */
.stTabs [data-baseweb="tab-list"]{ background:transparent!important; border-bottom:1px solid var(--border)!important; gap:0!important; }
.stTabs [data-baseweb="tab"]{ background:transparent!important; color:var(--lo)!important; font-size:.73rem!important; font-weight:600!important; letter-spacing:.1em!important; text-transform:uppercase!important; padding:.55rem 1.1rem!important; border-bottom:2px solid transparent!important; }
.stTabs [aria-selected="true"]{ color:var(--gold)!important; border-bottom-color:var(--gold)!important; }

/* Misc */
.stTextArea textarea{ background:var(--raised)!important; border:1px solid var(--border)!important; color:var(--hi)!important; font-family:'IBM Plex Mono',monospace!important; font-size:.77rem!important; border-radius:4px!important; }
.stTextInput input,.stSelectbox>div>div{ background:var(--raised)!important; border:1px solid var(--border)!important; color:var(--hi)!important; }
hr{ border-color:var(--border)!important; }
</style>
""", unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
#  SESSION STATE
# ═════════════════════════════════════════════════════════════════════════════
_DEFAULTS = {
    "pipeline_complete": False,
    "pipeline_logs"    : [],
    "last_pipeline_run": None,
    "selected_entity"  : None,
    "email_sent_ids"   : set(),
    "email_template"   : "",
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

db_init()

if not st.session_state.email_template:
    st.session_state.email_template = build_email(
        "{entity_name}", "{owner_name}", None, "{entity_number}"
    )


# ═════════════════════════════════════════════════════════════════════════════
#  SIDEBAR
# ═════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("""
    <div style="padding:.5rem 0 1.4rem;border-bottom:1px solid #2a2f38;margin-bottom:1.2rem;">
        <div style="font-family:'Playfair Display',serif;font-size:1.2rem;color:#f0f4f8;font-weight:700;">⚖ Sunbiz Pro</div>
        <div style="font-size:.67rem;color:#5a6472;text-transform:uppercase;letter-spacing:.1em;margin-top:.2rem;">Compliance Outreach Platform</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="sh"><span class="dot"></span>SFTP CONFIG</div>', unsafe_allow_html=True)
    st.text_input("Host",     value=SFTP_HOST, key="si_host")
    st.text_input("Username", value=SFTP_USER, key="si_user")
    st.text_input("Password", type="password", value=SFTP_PASS, key="si_pass")
    st.text_input("Port",     value=str(SFTP_PORT), key="si_port")

    st.markdown('<div class="sh" style="margin-top:1rem;"><span class="dot"></span>EMAIL API</div>', unsafe_allow_html=True)
    st.selectbox("Provider", ["Resend","SendGrid"], key="email_prov")
    st.caption("Set `RESEND_API_KEY` or `SENDGRID_API_KEY` env var to enable real sends.")
    st.text_input("From Address", placeholder="you@yourdomain.com", key="from_email")

    st.markdown('<div class="sh" style="margin-top:1rem;"><span class="dot"></span>LIMITS</div>', unsafe_allow_html=True)
    daily_limit = st.slider("Daily Send Limit", 10, 500, 100)
    delay_sec   = st.slider("Delay Between Sends (s)", 0, 10, 2)

    st.markdown("---")
    if st.session_state.last_pipeline_run:
        st.caption(f"Last run: {st.session_state.last_pipeline_run}")


# ═════════════════════════════════════════════════════════════════════════════
#  HEADER
# ═════════════════════════════════════════════════════════════════════════════
hc1, hc2 = st.columns([3, 1])
with hc1:
    st.markdown("""
    <div class="main-sub">Florida Division of Corporations · Annual Report Monitor</div>
    <div class="main-title">Sunbiz <span>Compliance</span> Dashboard</div>
    """, unsafe_allow_html=True)
    st.markdown(
        f'<div class="dbadge">⚠ Deadline: {DEADLINE} &nbsp;·&nbsp; Late Penalty: ${PENALTY_FEE:,}</div>',
        unsafe_allow_html=True
    )

with hc2:
    st.markdown("<br><br>", unsafe_allow_html=True)
    run_btn = st.button(
        "▶  Run Pipeline",
        type="primary",
        use_container_width=True,
        help="SFTP download → parse fixed-width → filter delinquents → SQLite upsert",
    )

if run_btn:
    run_pipeline_sync()
    st.rerun()

if st.session_state.pipeline_complete and st.session_state.pipeline_logs:
    with st.expander("📋 Last Pipeline Log", expanded=False):
        log_html = "\n".join(st.session_state.pipeline_logs)
        st.markdown(f'<div class="log-term">{log_html}</div>', unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
#  KPI BANNER
# ═════════════════════════════════════════════════════════════════════════════
stats = db_stats()
st.markdown(f"""
<div class="kpi-grid">
  <div class="kpi-cell">
    <div class="kpi-lbl">Potential Penalties to Prevent</div>
    <div class="kpi-val g">${stats['new']*PENALTY_FEE:,}</div>
    <div class="kpi-sub">${PENALTY_FEE} × {stats['new']:,} new leads</div>
  </div>
  <div class="kpi-cell">
    <div class="kpi-lbl">Delinquent Active Entities</div>
    <div class="kpi-val r">{stats['total']:,}</div>
    <div class="kpi-sub">Total in database</div>
  </div>
  <div class="kpi-cell">
    <div class="kpi-lbl">Outreach Sent</div>
    <div class="kpi-val">{stats['emailed']:,}</div>
    <div class="kpi-sub">{stats['contacted']:,} contacted · {stats['paid']:,} paid</div>
  </div>
  <div class="kpi-cell">
    <div class="kpi-lbl">Hard Deadline</div>
    <div class="kpi-val" style="font-size:1.35rem;">{DEADLINE}</div>
    <div class="kpi-sub">Florida DOS cutoff</div>
  </div>
</div>
""", unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
#  TABS
# ═════════════════════════════════════════════════════════════════════════════
tab_leads, tab_email, tab_outlog, tab_analytics = st.tabs([
    "📋  Lead Table",
    "✉  Email Previewer",
    "📜  Outreach Log",
    "📊  Analytics",
])

# ─────────────────────────────────────────────────────────────────────────────
#  TAB 1 — Lead Table
# ─────────────────────────────────────────────────────────────────────────────
with tab_leads:
    if stats["total"] == 0:
        st.markdown("""
        <div style="text-align:center;padding:3.5rem 1rem;">
            <div style="font-size:3rem;margin-bottom:1rem;">📂</div>
            <div style="font-family:'Playfair Display',serif;font-size:1.3rem;color:#f0f4f8;margin-bottom:.5rem;">Database is empty</div>
            <div style="font-size:.85rem;color:#5a6472;">Click "Run Pipeline" to fetch and parse the latest Sunbiz records.</div>
        </div>""", unsafe_allow_html=True)
    else:
        fc1, fc2, fc3 = st.columns([3,1,1])
        with fc1:
            search = st.text_input("Search", placeholder="entity name, owner, or email…",
                                    label_visibility="collapsed", key="lt_search")
        with fc2:
            sf = st.selectbox("Status", ["All","New","Contacted","Paid"],
                               label_visibility="collapsed", key="lt_status")
        with fc3:
            sb = st.selectbox("Sort", ["Newest","Entity Name","Report Year"],
                               label_visibility="collapsed", key="lt_sort")

        df = db_get_leads(search=search, status_filter=sf)
        if sb == "Entity Name":   df = df.sort_values("entity_name")
        elif sb == "Report Year": df = df.sort_values("last_rpt_year")

        st.caption(f"{len(df):,} records  ·  Potential penalties: **${len(df)*PENALTY_FEE:,}**")

        COLS = ["entity_number","entity_name","owner_name",
                "principal_email","last_rpt_year","contact_status","inserted_at"]
        cfg  = {
            "entity_number"  : st.column_config.TextColumn("Entity #",   width="small"),
            "entity_name"    : st.column_config.TextColumn("Entity",     width="large"),
            "owner_name"     : st.column_config.TextColumn("Owner",      width="medium"),
            "principal_email": st.column_config.TextColumn("Email",      width="medium"),
            "last_rpt_year"  : st.column_config.NumberColumn("Last Rpt", width="small", format="%d"),
            "contact_status" : st.column_config.SelectboxColumn("Status",
                                   options=["New","Contacted","Paid"], width="small"),
            "inserted_at"    : st.column_config.TextColumn("Added",      width="medium"),
        }

        edited = st.data_editor(df[COLS], use_container_width=True, hide_index=True,
                                 column_config=cfg, height=400, key="lt_editor",
                                 num_rows="fixed")
        if edited is not None and not edited.empty:
            for _, row in edited.iterrows():
                orig = df[df["entity_number"] == row["entity_number"]]
                if not orig.empty and row["contact_status"] != orig.iloc[0]["contact_status"]:
                    db_update_status(row["entity_number"], row["contact_status"])

        st.markdown('<div class="sh" style="margin-top:.5rem;"><span class="dot"></span>LOAD INTO EMAIL PREVIEWER</div>', unsafe_allow_html=True)
        opts = df["entity_name"].tolist()
        if opts:
            idx = st.selectbox("Pick entity", range(len(opts)),
                                format_func=lambda i: opts[i],
                                label_visibility="collapsed", key="lt_pick")
            if st.button("✉  Load & Preview →", type="primary"):
                st.session_state.selected_entity = df.iloc[idx].to_dict()
                st.info("Switch to the **Email Previewer** tab.")

        csv_bytes = df[COLS].to_csv(index=False).encode()
        st.download_button("⬇ Export CSV", data=csv_bytes,
                           file_name=f"sunbiz_{date.today().isoformat()}.csv",
                           mime="text/csv")

# ─────────────────────────────────────────────────────────────────────────────
#  TAB 2 — Email Previewer
# ─────────────────────────────────────────────────────────────────────────────
with tab_email:
    sel = st.session_state.get("selected_entity")
    if sel is None and stats["total"] > 0:
        df0 = db_get_leads()
        if not df0.empty:
            sel = df0.iloc[0].to_dict()
            st.session_state.selected_entity = sel

    if sel is None:
        st.info("Run the pipeline then select a lead from the Lead Table.")
    else:
        ep1, ep2 = st.columns([1, 1.45], gap="large")

        with ep1:
            st.markdown('<div class="sh"><span class="dot"></span>LEAD PROFILE</div>',
                        unsafe_allow_html=True)
            sc_map = {"New":"bn","Contacted":"bc","Paid":"bp"}
            sc     = sc_map.get(sel.get("contact_status","New"), "bn")
            yrs_del = max(CURRENT_YEAR - int(sel.get("last_rpt_year") or CURRENT_YEAR-1), 1)

            st.markdown(f"""
            <div class="lcard">
              <div class="lcard-name">{sel.get('entity_name','—')}</div>
              <div class="lcard-det">
Owner   {sel.get('owner_name','—') or '—'}
Email   {sel.get('principal_email','—') or '—'}
Entity  {sel.get('entity_number','—')}
Addr    {sel.get('principal_addr','—') or '—'}
Rpt Yr  {sel.get('last_rpt_year','—')}
Source  {sel.get('source_file','—') or '—'}</div>
            </div>
            <span class="badge {sc}">{sel.get('contact_status','New')}</span>
            <div class="pbox" style="margin-top:.8rem;">
              <div class="plbl">Estimated Penalty Exposure</div>
              <div class="pnum">${PENALTY_FEE * yrs_del:,}</div>
              <div style="font-size:.73rem;color:#a8b4c0;margin-top:.2rem;">${PENALTY_FEE} × {yrs_del} yr{"s" if yrs_del>1 else ""} · deadline {DEADLINE}</div>
            </div>
            """, unsafe_allow_html=True)

            st.markdown('<div class="sh" style="margin-top:.6rem;"><span class="dot"></span>SWITCH LEAD</div>',
                        unsafe_allow_html=True)
            df_all = db_get_leads()
            if not df_all.empty:
                all_opts = df_all["entity_name"].tolist()
                try:    ci = all_opts.index(sel.get("entity_name",""))
                except: ci = 0
                ni = st.selectbox("Lead", range(len(all_opts)), index=ci,
                                   format_func=lambda i: all_opts[i],
                                   label_visibility="collapsed", key="ep_sel")
                if st.button("Load", use_container_width=True):
                    st.session_state.selected_entity = df_all.iloc[ni].to_dict()
                    st.rerun()

        with ep2:
            st.markdown('<div class="sh"><span class="dot"></span>EMAIL TEMPLATE (EDITABLE)</div>',
                        unsafe_allow_html=True)
            tmpl = st.text_area(
                "Template",
                value=st.session_state.email_template,
                height=210,
                label_visibility="collapsed",
                key="ep_tmpl",
                help="Variables: {entity_name} {owner_name} {entity_number} {last_rpt_year}",
            )
            st.session_state.email_template = tmpl

            rendered = (tmpl
                .replace("{entity_name}",    str(sel.get("entity_name","N/A")))
                .replace("{owner_name}",     str(sel.get("owner_name","Business Owner") or "Business Owner"))
                .replace("{entity_number}",  str(sel.get("entity_number","N/A")))
                .replace("{last_rpt_year}",  str(sel.get("last_rpt_year","N/A")))
            )

            st.markdown('<div class="sh" style="margin-top:.4rem;"><span class="dot"></span>LIVE PREVIEW</div>',
                        unsafe_allow_html=True)
            st.markdown(f"""
            <div class="email-card">
              <div class="email-subj">✉ {EMAIL_SUBJECT}</div>{rendered}
            </div>""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            oc1, oc2 = st.columns([1.5, 1])
            with oc1:
                to_override = st.text_input("To (override):",
                                             placeholder="leave blank = use DB email",
                                             label_visibility="collapsed",
                                             key="ep_to_override")
            with oc2:
                eid        = sel.get("entity_number","")
                already    = eid in st.session_state.email_sent_ids
                send_clicked = st.button("📤 Send Email", type="primary",
                                          use_container_width=True,
                                          disabled=already, key="ep_send")

            if already:
                st.caption("✓ Already sent this session.")

            if send_clicked:
                to_addr = to_override.strip() or sel.get("principal_email","")
                if not to_addr or "@" not in to_addr:
                    st.error("No valid email address — enter one in the override field.")
                else:
                    with st.spinner("Sending…"):
                        result = send_email(to_addr, EMAIL_SUBJECT, rendered, eid)
                    if result["success"]:
                        st.success(f"✅ {result['message']}")
                        db_update_status(eid, "Contacted")
                        db_log_email(eid, EMAIL_SUBJECT, rendered, result["message"])
                        st.session_state.email_sent_ids.add(eid)
                        st.session_state.selected_entity["contact_status"] = "Contacted"
                    else:
                        st.error(f"❌ {result['message']}")

# ─────────────────────────────────────────────────────────────────────────────
#  TAB 3 — Outreach Log
# ─────────────────────────────────────────────────────────────────────────────
with tab_outlog:
    st.markdown('<div class="sh"><span class="dot"></span>EMAIL OUTREACH HISTORY</div>',
                unsafe_allow_html=True)
    with db_connect() as conn:
        log_df = pd.read_sql_query("""
            SELECT el.sent_at, el.entity_number, l.entity_name,
                   l.owner_name, el.subject, el.api_response
            FROM email_log el
            LEFT JOIN leads l ON l.entity_number = el.entity_number
            ORDER BY el.sent_at DESC LIMIT 200
        """, conn)

    if log_df.empty:
        st.markdown("""
        <div style="text-align:center;padding:2.5rem;color:#5a6472;">
          No emails sent yet. Use the Email Previewer to start outreach.
        </div>""", unsafe_allow_html=True)
    else:
        st.caption(f"{len(log_df)} records in log")
        st.dataframe(log_df, use_container_width=True, hide_index=True, height=400)
        st.download_button("⬇ Export Log",
                           data=log_df.to_csv(index=False).encode(),
                           file_name=f"outreach_log_{date.today().isoformat()}.csv",
                           mime="text/csv")

# ─────────────────────────────────────────────────────────────────────────────
#  TAB 4 — Analytics
# ─────────────────────────────────────────────────────────────────────────────
with tab_analytics:
    if stats["total"] == 0:
        st.info("Run the pipeline to see analytics.")
    else:
        df_a = db_get_leads()
        a1, a2 = st.columns(2)

        with a1:
            st.markdown('<div class="sh"><span class="dot"></span>DELINQUENCIES BY LAST REPORT YEAR</div>',
                        unsafe_allow_html=True)
            st.bar_chart(df_a["last_rpt_year"].value_counts().sort_index(),
                         color="#e8a020", height=230)

        with a2:
            st.markdown('<div class="sh"><span class="dot"></span>CONTACT STATUS BREAKDOWN</div>',
                        unsafe_allow_html=True)
            st.bar_chart(df_a["contact_status"].value_counts(),
                         color="#5b9cf6", height=230)

        st.markdown('<div class="sh" style="margin-top:.2rem;"><span class="dot"></span>RECORD TYPE SPLIT</div>',
                    unsafe_allow_html=True)
        rt = df_a["record_type"].value_counts().reset_index()
        rt.columns = ["Type","Count"]
        st.dataframe(rt, use_container_width=True, hide_index=True, height=200)

        st.markdown('<div class="sh" style="margin-top:.4rem;"><span class="dot"></span>PIPELINE SUMMARY</div>',
                    unsafe_allow_html=True)
        summary_df = pd.DataFrame([
            ("Total Leads",              stats["total"]),
            ("New / Uncontacted",        stats["new"]),
            ("Contacted",                stats["contacted"]),
            ("Paid / Converted",         stats["paid"]),
            ("Emails Sent",              stats["emailed"]),
            ("Potential Penalties ($)",  f"${stats['new']*PENALTY_FEE:,}"),
            ("Penalty Per Entity ($)",   f"${PENALTY_FEE:,}"),
            ("Filing Deadline",          DEADLINE),
        ], columns=["Metric","Value"])
        st.dataframe(summary_df, use_container_width=True, hide_index=True, height=310)


# ═════════════════════════════════════════════════════════════════════════════
#  FOOTER
# ═════════════════════════════════════════════════════════════════════════════
st.markdown("---")
st.markdown("""
<div style="font-size:.7rem;color:#3d4450;line-height:1.8;padding:.2rem 0 .5rem;">
  <strong style="color:#5a6472;">LEGAL DISCLAIMER</strong> — This application is NOT affiliated with,
  endorsed by, or acting on behalf of the State of Florida or the Florida Division of Corporations.
  Data is sourced exclusively from publicly available Sunbiz records. All outreach is sent as a
  courtesy service and does not constitute legal advice. &nbsp;·&nbsp; Sunbiz Compliance Pro v2.0
</div>
""", unsafe_allow_html=True)
