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
import threading
from datetime import datetime, date
from typing import Optional

# ── Third-Party ───────────────────────────────────────────────────────────────
import streamlit as st
import pandas as pd
import paramiko
import requests
from bs4 import BeautifulSoup

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
SFTP_HOST        = "sftp.floridados.gov"
SFTP_USER        = "Public"
SFTP_PASS        = "PubAccess1845!"
SFTP_PORT        = 22

# Real paths discovered by probing the SFTP server
QUARTERLY_COR    = "/Public/doc/Quarterly/Cor/cordata.zip"   # 1.7 GB master (all corps + status + rpt year)
DAILY_COR_DIR    = "/Public/doc/cor/Events/"                  # Daily change events (YYYYMMDDce.txt)
DAILY_COR_2021   = "/Public/doc/cor/2021/"                    # New-registration files (archive, 2021 only)

# Streaming chunk size for the large quarterly zip (bytes)
ZIP_CHUNK_SIZE   = 256 * 1024   # 256 KB per chunk

DB_PATH      = "sunbiz_leads.db"
PENALTY_FEE  = 400
DEADLINE     = "May 1, 2026"
CURRENT_YEAR = 2026   # Reporting year — entities registered BEFORE this year owe the annual report.
                      # Florida law: register in 2026 → first report due May 1, 2027 (not 2026).
                      # So valid leads = registered 2025 or earlier, still Active status.

# ─── Confirmed field offsets (verified against real 20240919c.txt sample) ────
# Source: /Public/doc/cor/2021/ daily registration files (YYYYMMDDc.txt)
# These are NEW ENTITY registrations. Each record is ~1436 chars.
FW = {
    "entity_number" : (0,   12),
    "entity_name"   : (12, 192),   # padded to 192 chars
    "status"        : (204,  1),   # A=Active
    "state_of_org"  : (205,  2),   # FL
    "entity_type"   : (207,  2),   # AL=LLC CP=Corp MN=NonProfit etc
    "reg_addr1"     : (216, 80),
    "reg_city"      : (296, 30),
    "reg_zip"       : (326, 10),
    "mail_addr1"    : (336, 80),
    "mail_city"     : (416, 30),
    "mail_state"    : (446,  2),
    "mail_zip"      : (448, 10),
    "filed_date"    : (472,  8),   # MMDDYYYY — registration date
    "state_of_inc"  : (489,  2),
    "owner_last"    : (544, 20),   # CONFIRMED offset
    "owner_first"   : (564, 15),   # CONFIRMED offset
    "owner_mid"     : (579,  7),   # CONFIRMED offset
    "owner_title"   : (586,  1),   # P=President/Manager R=Reg.Agent
    "owner_addr1"   : (587, 40),
    "owner_city"    : (627, 28),
    "owner_state"   : (655,  2),
    "owner_zip"     : (657,  9),
}
FW_MIN_LEN = 480

# ── Entity types that owe Florida annual reports ──────────────────────────────
# These are Florida-domestic entities required to file annually.
# Foreign entities (RL, RP, ML, MP, MN) file differently and are excluded.
VALID_ENTITY_TYPES = {
    "AL",   # Florida LLC
    "CP",   # Florida Corporation
    "PA",   # Professional Association
    "NP",   # Non-Profit Corporation
    "LP",   # Limited Partnership
    "PL",   # Professional LLC
}

# Status codes that mean "active and operating"
VALID_STATUS_CODES = {"A"}   # A = Active. I = Inactive, D = Dissolved, V = Vol. Dissolved


def extract_reg_year(record: bytes) -> int:
    """Extract registration year from filed_date (MMDDYYYY at offset 472)."""
    try:
        raw = record[472:480].decode("latin-1").strip()
        if len(raw) == 8 and raw.isdigit():
            return int(raw[4:8])
    except Exception:
        pass
    return 0


# ═════════════════════════════════════════════════════════════════════════════
#  MODULE 3 — SQLite Lead Management
# ═════════════════════════════════════════════════════════════════════════════

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_migrate():
    """Safely add new columns and clean up bad records from previous imports."""
    migrations = [
        "ALTER TABLE leads ADD COLUMN principal_phone TEXT",
        "ALTER TABLE leads ADD COLUMN enriched_at TEXT",
        "ALTER TABLE leads ADD COLUMN website TEXT",
        "ALTER TABLE leads ADD COLUMN linkedin_url TEXT",
        "ALTER TABLE leads ADD COLUMN instagram_url TEXT",
        "ALTER TABLE leads ADD COLUMN facebook_url TEXT",
        "ALTER TABLE leads ADD COLUMN google_search_done INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN is_active_business INTEGER DEFAULT 0",
    ]
    with db_connect() as conn:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
        for sql in migrations:
            col = sql.split("ADD COLUMN")[1].strip().split()[0]
            if col not in existing:
                try:
                    conn.execute(sql)
                except Exception:
                    pass

        # ── Purge bad records from previous imports ──────────────────────
        # Remove non-Active status records
        conn.execute("DELETE FROM leads WHERE status != 'A' AND status IS NOT NULL AND status != ''")
        # Remove foreign entity types that don't owe FL annual reports
        foreign_types = ("'RL'","'RP'","'ML'","'MP'","'MN'")
        conn.execute(f"DELETE FROM leads WHERE record_type IN ({','.join(foreign_types)})")
        # Remove 2026 registrations — they don't owe a report until May 1, 2027
        conn.execute("DELETE FROM leads WHERE last_rpt_year >= 2026")
        conn.commit()


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
                principal_phone  TEXT,
                enriched_at      TEXT,
                website          TEXT,
                linkedin_url     TEXT,
                instagram_url    TEXT,
                facebook_url     TEXT,
                google_search_done INTEGER DEFAULT 0,
                is_active_business INTEGER DEFAULT 0,
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS downloaded_files (
                filename      TEXT PRIMARY KEY,
                downloaded_at TEXT DEFAULT (datetime('now')),
                records_found INTEGER DEFAULT 0
            )
        """)
        conn.commit()
    db_migrate()  # safe every startup — skips already-existing columns


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


def db_enrich_lead(entity_number: str, email: str, phone: str):
    """Update a lead with scraped contact info."""
    with db_connect() as conn:
        conn.execute("""
            UPDATE leads
            SET principal_email = CASE WHEN ? != '' THEN ? ELSE principal_email END,
                principal_phone = CASE WHEN ? != '' THEN ? ELSE principal_phone END,
                enriched_at = datetime('now'),
                last_updated = datetime('now')
            WHERE entity_number = ?
        """, (email, email, phone, phone, entity_number))
        conn.commit()


def db_save_google_results(entity_number: str, results: dict):
    """Save Google search enrichment results to a lead."""
    with db_connect() as conn:
        conn.execute("""
            UPDATE leads SET
                website            = CASE WHEN ? != '' THEN ? ELSE website END,
                linkedin_url       = CASE WHEN ? != '' THEN ? ELSE linkedin_url END,
                instagram_url      = CASE WHEN ? != '' THEN ? ELSE instagram_url END,
                facebook_url       = CASE WHEN ? != '' THEN ? ELSE facebook_url END,
                principal_email    = CASE WHEN ? != '' THEN ? ELSE principal_email END,
                principal_phone    = CASE WHEN ? != '' THEN ? ELSE principal_phone END,
                is_active_business = ?,
                google_search_done = 1,
                enriched_at        = datetime('now'),
                last_updated       = datetime('now')
            WHERE entity_number = ?
        """, (
            results.get("website",""),    results.get("website",""),
            results.get("linkedin",""),   results.get("linkedin",""),
            results.get("instagram",""),  results.get("instagram",""),
            results.get("facebook",""),   results.get("facebook",""),
            results.get("email",""),      results.get("email",""),
            results.get("phone",""),      results.get("phone",""),
            1 if results.get("is_active") else 0,
            entity_number,
        ))
        conn.commit()


def db_get_ungoogled(limit: int = 50) -> list:
    """Return leads that haven't had a Google search run yet."""
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT entity_number, entity_name, owner_name, principal_addr
            FROM leads
            WHERE google_search_done = 0 OR google_search_done IS NULL
            ORDER BY inserted_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def db_delete_lead(entity_number: str, reason: str = ""):
    """Permanently remove a lead from the database."""
    with db_connect() as conn:
        conn.execute("DELETE FROM leads WHERE entity_number = ?", (entity_number,))
        conn.commit()


# ═════════════════════════════════════════════════════════════════════════════
#  BACKGROUND ENRICHMENT ENGINE
#  Runs silently in a daemon thread — no user interaction needed.
#  Writes directly to SQLite. UI polls and refreshes automatically.
# ═════════════════════════════════════════════════════════════════════════════

# Shared state between background thread and Streamlit UI
_BG = {
    "running"      : False,
    "current"      : "",
    "checked"      : 0,
    "removed"      : 0,
    "emails_found" : 0,
    "phones_found" : 0,
    "last_action"  : "",
    "log"          : [],      # rolling last-20 activity lines
    "started_at"   : None,
}

def _bg_log(msg: str):
    """Add a line to the rolling activity log (max 20 entries)."""
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    _BG["log"].append(entry)
    if len(_BG["log"]) > 20:
        _BG["log"].pop(0)
    _BG["last_action"] = msg
_BG_LOCK = threading.Lock()


def _bg_enrich_worker(delay_sec: float = 2.0):
    """
    Background daemon thread.
    Continuously pulls unenriched leads from SQLite, checks Sunbiz live,
    deletes inactive ones, saves contact info for active ones.
    Stops automatically when all leads are enriched.
    """
    while True:
        leads = db_get_unenriched(limit=1)  # one at a time — steady trickle
        if not leads:
            with _BG_LOCK:
                _BG["running"]   = False
                _BG["current"]   = ""
                _BG["last_action"] = "✅ All leads checked — enrichment complete"
            break

        lead       = leads[0]
        entity_num = lead["entity_number"]
        entity_name = lead["entity_name"]

        with _BG_LOCK:
            _BG["current"] = entity_name

        data = scrape_sunbiz_entity(entity_num)

        with _BG_LOCK:
            _BG["checked"] += 1

            if not data["page_found"] or data["is_inactive"]:
                reason = data.get("live_status") or "Not found / Inactive"
                db_delete_lead(entity_num, reason=reason)
                _BG["removed"] += 1
                _bg_log(f"🗑  REMOVED  {entity_name[:45]}  ({reason})")
            else:
                db_enrich_lead(entity_num, data.get("email",""), data.get("phone",""))
                if data.get("email"):  _BG["emails_found"] += 1
                if data.get("phone"):  _BG["phones_found"] += 1
                parts = []
                if data.get("email"):  parts.append(f"✉ {data['email']}")
                if data.get("phone"):  parts.append(f"📞 {data['phone']}")
                suffix = "  ·  " + "  ".join(parts) if parts else "  ·  no contact info"
                _bg_log(f"✅  ACTIVE   {entity_name[:45]}{suffix}")

        time.sleep(delay_sec)

    with _BG_LOCK:
        _BG["running"] = False
        _bg_log(f"🏁 Complete — {_BG['checked']} checked, {_BG['removed']} removed, "
                f"{_BG['emails_found']} emails, {_BG['phones_found']} phones")


def start_bg_enrichment(delay_sec: float = 2.0):
    """Start background enrichment if not already running."""
    with _BG_LOCK:
        if _BG["running"]:
            return False   # already running
        _BG["running"]       = True
        _BG["current"]       = ""
        _BG["last_action"]   = "Starting…"

    t = threading.Thread(
        target=_bg_enrich_worker,
        args=(delay_sec,),
        daemon=True,
        name="sunbiz-bg-enrichment",
    )
    t.start()
    return True


def bg_status() -> dict:
    """Thread-safe snapshot of background enrichment state."""
    with _BG_LOCK:
        return dict(_BG)


def db_get_unenriched(limit: int = 50) -> list:
    """Return entity numbers that haven't been enriched yet."""
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT entity_number, entity_name FROM leads
            WHERE (principal_email IS NULL OR principal_email = '')
            AND (enriched_at IS NULL)
            ORDER BY inserted_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def db_stats() -> dict:
    with db_connect() as conn:
        total     = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        new_ct    = conn.execute("SELECT COUNT(*) FROM leads WHERE contact_status='New'").fetchone()[0]
        contacted = conn.execute("SELECT COUNT(*) FROM leads WHERE contact_status='Contacted'").fetchone()[0]
        paid      = conn.execute("SELECT COUNT(*) FROM leads WHERE contact_status='Paid'").fetchone()[0]
        emailed   = conn.execute("SELECT COUNT(*) FROM email_log").fetchone()[0]
    enriched  = conn.execute("SELECT COUNT(*) FROM leads WHERE enriched_at IS NOT NULL").fetchone()[0]
    with_email = conn.execute("SELECT COUNT(*) FROM leads WHERE principal_email != '' AND principal_email IS NOT NULL").fetchone()[0]
    with_phone = conn.execute("SELECT COUNT(*) FROM leads WHERE principal_phone != '' AND principal_phone IS NOT NULL").fetchone()[0]
    return {"total":total,"new":new_ct,"contacted":contacted,"paid":paid,
            "emailed":emailed,"enriched":enriched,"with_email":with_email,"with_phone":with_phone}


def db_get_downloaded_files() -> set:
    """Return set of filenames already downloaded."""
    with db_connect() as conn:
        rows = conn.execute("SELECT filename FROM downloaded_files").fetchall()
    return {r[0] for r in rows}


def db_mark_file_downloaded(filename: str, records_found: int):
    """Record that a file has been downloaded and parsed."""
    with db_connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO downloaded_files (filename, records_found)
            VALUES (?, ?)
        """, (filename, records_found))
        conn.commit()


def db_get_cursor_stats() -> dict:
    """Return stats about pipeline progress."""
    with db_connect() as conn:
        total_files = conn.execute("SELECT COUNT(*) FROM downloaded_files").fetchone()[0]
        oldest      = conn.execute("SELECT MIN(filename) FROM downloaded_files").fetchone()[0]
        newest      = conn.execute("SELECT MAX(filename) FROM downloaded_files").fetchone()[0]
        total_recs  = conn.execute("SELECT SUM(records_found) FROM downloaded_files").fetchone()[0]
    return {
        "files_downloaded": total_files,
        "oldest_file": oldest or "—",
        "newest_file": newest or "—",
        "total_parsed": total_recs or 0,
    }


def db_reset_cursor():
    """Clear the downloaded files cursor — forces full re-download on next run."""
    with db_connect() as conn:
        conn.execute("DELETE FROM downloaded_files")
        conn.commit()


# ═════════════════════════════════════════════════════════════════════════════
#  MODULE 2 — Fixed-Width Parser & Filter (confirmed offsets from real data)
# ═════════════════════════════════════════════════════════════════════════════

def _s(record: bytes, start: int, length: int) -> str:
    return record[start: start + length].decode("latin-1", errors="replace").strip()


def parse_record(line: bytes, source_file: str = "") -> Optional[dict]:
    """
    Parse one record from a Sunbiz daily registration file (YYYYMMDDc.txt).
    Field offsets confirmed against real file 20240919c.txt.
    Returns a lead dict only for Active entities registered before CURRENT_YEAR.
    """
    rec = line.rstrip(b"\r\x00")
    if len(rec) < FW_MIN_LEN:
        return None

    entity_name  = _s(rec, 12,  192)
    status       = _s(rec, 204,   1)
    entity_type  = _s(rec, 207,   2)

    # Must be Active
    if status not in VALID_STATUS_CODES:
        return None

    # Must be a Florida-domestic entity type that owes annual reports
    if entity_type not in VALID_ENTITY_TYPES:
        return None

    if not entity_name:
        return None

    reg_year = extract_reg_year(rec)

    # Florida law: entities registered in CURRENT_YEAR don't owe their first
    # annual report until Jan 1 – May 1 of the FOLLOWING year.
    # So for 2026: only entities registered in 2025 or earlier owe a 2026 report.
    # Entities registered in 2026 are EXCLUDED — they owe nothing until 2027.
    if reg_year >= CURRENT_YEAR:
        return None

    # Also exclude entities with no registration year — data quality issue
    if reg_year == 0:
        return None

    owner_first = _s(rec, 564, 15)
    owner_mid   = _s(rec, 579,  7)
    owner_last  = _s(rec, 544, 20)
    owner_name  = " ".join(p for p in [owner_first, owner_mid, owner_last] if p)

    reg_addr  = _s(rec, 216, 80)
    reg_city  = _s(rec, 296, 30)
    mail_state= _s(rec, 446,  2)
    reg_zip   = _s(rec, 326, 10)
    addr      = ", ".join(p for p in [reg_addr, reg_city, mail_state, reg_zip] if p)

    return {
        "record_type"    : _s(rec, 207, 2),   # AL=LLC CP=Corp etc
        "entity_number"  : _s(rec, 0,   12),
        "status"         : status,
        "filing_date"    : _s(rec, 472,  8),  # MMDDYYYY
        "entity_name"    : entity_name,
        "last_rpt_year"  : reg_year,
        "principal_addr" : addr,
        "principal_email": "",                 # not in registration file; enriched separately
        "owner_name"     : owner_name,
        "owner_title"    : _s(rec, 586, 1),
        "source_file"    : source_file,
    }


def parse_file_buffer(data: bytes, source_file: str = "") -> list:
    """Parse an entire daily registration file buffer."""
    results = []
    for line in data.split(b"\n"):
        r = parse_record(line, source_file)
        if r:
            results.append(r)
    return results


# ═════════════════════════════════════════════════════════════════════════════
#  MODULE 1.5 — Sunbiz Web Scraper & Contact Enrichment
# ═════════════════════════════════════════════════════════════════════════════

SUNBIZ_DETAIL_URL = "https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResultDetail"
SUNBIZ_SEARCH_URL = "https://search.sunbiz.org/Inquiry/CorporationSearch/ByName"

_SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://search.sunbiz.org/",
}


# Status words on Sunbiz that mean the business is NOT active
_INACTIVE_STATUSES = {
    "inactive", "dissolved", "revoked", "cancelled", "withdrawn",
    "administratively dissolved", "voluntarily dissolved",
    "merged", "converted", "expired",
}

def scrape_sunbiz_entity(entity_number: str) -> dict:
    """
    Scrape the Sunbiz detail page for a single entity number.
    Returns dict with keys:
      email, phone, registered_agent, last_report_year,
      live_status, is_inactive, page_found
    live_status — the exact status string from the Sunbiz page
    is_inactive — True if the business is not currently active
    page_found  — False if entity doesn't exist on Sunbiz at all
    """
    import re
    result = {
        "email": "", "phone": "", "registered_agent": "",
        "last_report_year": "", "live_status": "",
        "is_inactive": False, "page_found": True,
    }
    try:
        params = {
            "inquirytype": "DocumentNumber",
            "inquiryDirective": "StartsWith",
            "inquiryValue": entity_number.strip(),
            "redirected": "true",
        }
        resp = requests.get(
            SUNBIZ_DETAIL_URL, params=params,
            headers=_SCRAPE_HEADERS, timeout=12
        )
        if resp.status_code != 200:
            result["page_found"] = False
            return result

        soup = BeautifulSoup(resp.text, "html.parser")
        page_text = soup.get_text(" ")

        # ── Check if entity exists at all ─────────────────────────────────
        if "no records found" in page_text.lower() or len(page_text.strip()) < 200:
            result["page_found"] = False
            result["is_inactive"] = True
            return result

        # ── Live status — most important field ────────────────────────────
        # Sunbiz shows "Status: Active" or "Status: Inactive" etc.
        status_match = re.search(
            r'Status[:\s]+([A-Za-z ]+?)(?:\n|<|\|)',
            page_text, re.IGNORECASE
        )
        if status_match:
            raw_status = status_match.group(1).strip().lower()
            result["live_status"] = raw_status.title()
            # Mark inactive if any inactive keyword found
            result["is_inactive"] = any(s in raw_status for s in _INACTIVE_STATUSES)
        
        # Also check page for inactive keywords anywhere prominent
        if not result["is_inactive"]:
            for kw in _INACTIVE_STATUSES:
                if kw in page_text.lower():
                    result["is_inactive"] = True
                    result["live_status"] = result["live_status"] or kw.title()
                    break

        # ── Annual report year ────────────────────────────────────────────
        yr_matches = re.findall(r'Annual Report.*?20(\d{2})', page_text, re.IGNORECASE)
        if yr_matches:
            result["last_report_year"] = "20" + yr_matches[-1]

        # ── Email ─────────────────────────────────────────────────────────
        skip_domains = {"sunbiz.org", "dos.myflorida.com", "floridados.gov",
                        "myfloridacfo.com", "dor.myflorida.com"}
        email_matches = re.findall(
            r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
            page_text
        )
        for em in email_matches:
            if em.split("@")[-1].lower() not in skip_domains:
                result["email"] = em.lower()
                break

        # ── Phone ─────────────────────────────────────────────────────────
        phone_matches = re.findall(
            r'\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}',
            page_text
        )
        if phone_matches:
            result["phone"] = phone_matches[0].strip()

        # ── Registered agent ──────────────────────────────────────────────
        for label in soup.find_all(string=re.compile("Registered Agent", re.I)):
            parent = label.parent
            if parent:
                nxt = parent.find_next_sibling()
                if nxt:
                    result["registered_agent"] = nxt.get_text(strip=True)[:80]
            break

    except requests.exceptions.Timeout:
        result["page_found"] = False
    except Exception:
        pass

    return result


def run_enrichment_sync(batch_size: int = 50, delay_sec: float = 1.5):
    """
    Scrape Sunbiz detail pages for leads missing contact info.
    Runs synchronously inside st.status() — call directly from button.
    Rate-limited to be polite to the state server.
    """
    leads = db_get_unenriched(limit=batch_size)

    with st.status(f"Enriching {len(leads)} leads from Sunbiz…", expanded=True) as status:
        def w(msg):
            ts = datetime.now().strftime("%H:%M:%S")
            status.write(f"[{ts}] {msg}")

        if not leads:
            w("✅ All leads already enriched — nothing to do.")
            status.update(label="Already up to date", state="complete", expanded=False)
            return

        found_email = found_phone = 0
        removed     = 0
        not_found   = 0

        for i, lead in enumerate(leads):
            entity_num  = lead["entity_number"]
            entity_name = lead["entity_name"]

            w(f"🔍 [{i+1}/{len(leads)}] {entity_name} ({entity_num})")
            data = scrape_sunbiz_entity(entity_num)

            # ── Status check — clean DB in real time ──────────────────────
            if not data["page_found"]:
                db_delete_lead(entity_num, reason="Not found on Sunbiz")
                not_found += 1
                removed   += 1
                w(f"  🗑  Not found on Sunbiz — removed from database")
                if i < len(leads) - 1:
                    time.sleep(delay_sec)
                continue

            if data["is_inactive"]:
                status_label = data.get("live_status") or "Inactive/Dissolved"
                db_delete_lead(entity_num, reason=status_label)
                removed += 1
                w(f"  🗑  Status: {status_label} — removed from database")
                if i < len(leads) - 1:
                    time.sleep(delay_sec)
                continue

            # ── Active — save contact info ────────────────────────────────
            live_status = data.get("live_status", "")
            w(f"  ✅ Status: {live_status or 'Active'}")

            if data["email"]:
                found_email += 1
                w(f"  ✉  {data['email']}")
            if data["phone"]:
                found_phone += 1
                w(f"  📞 {data['phone']}")
            if not data["email"] and not data["phone"]:
                w(f"  ─  No contact info on file")

            db_enrich_lead(entity_num, data["email"], data["phone"])

            if i < len(leads) - 1:
                time.sleep(delay_sec)

        w(f"")
        kept = len(leads) - removed
        w(f"✅ Enrichment complete")
        w(f"   ✉  Emails found:    {found_email}")
        w(f"   📞 Phones found:    {found_phone}")
        w(f"   🗑  Removed (inactive/not found): {removed}")
        w(f"   ✅ Active leads kept: {kept}")
        if kept > 0:
            w(f"   Email hit rate: {(found_email/kept*100):.0f}%")
        status.update(
            label=f"✅ {kept} active leads kept · {removed} inactive removed · {found_email} emails found",
            state="complete", expanded=False
        )


# ═════════════════════════════════════════════════════════════════════════════
#  MODULE 1.6 — Google Search Enrichment (Social + Website + Contact)
# ═════════════════════════════════════════════════════════════════════════════

def google_search_lead(entity_name: str, owner_name: str, city: str,
                       serp_api_key: str = "") -> dict:
    """
    Search Google for a business/owner and extract:
      - Website URL
      - LinkedIn profile
      - Instagram profile
      - Facebook page
      - Email found on website
      - Phone found on website
      - Whether the business appears actively operating

    Uses SerpAPI if an API key is provided (most reliable).
    Falls back to scraping DuckDuckGo HTML (free, no key needed, rate-limited).
    """
    import re

    result = {
        "website": "", "linkedin": "", "instagram": "",
        "facebook": "", "email": "", "phone": "",
        "is_active": False, "search_snippet": "",
    }

    # Build search query — specific enough to find the right business
    city_clean = city.split(",")[0].strip() if city else "Florida"
    query = f'"{entity_name}" {owner_name} Florida'

    # ── Option A: SerpAPI (paid, reliable, $50/mo for 5k searches) ────────
    if serp_api_key:
        try:
            resp = requests.get("https://serpapi.com/search", params={
                "q": query, "api_key": serp_api_key,
                "num": 10, "gl": "us", "hl": "en",
            }, timeout=10)
            data = resp.json()
            organic = data.get("organic_results", [])

            for r in organic:
                url   = r.get("link", "").lower()
                title = r.get("title", "").lower()
                snip  = r.get("snippet", "")

                # Tag social profiles
                if "linkedin.com/in/" in url or "linkedin.com/company/" in url:
                    if not result["linkedin"]:
                        result["linkedin"] = r["link"]
                elif "instagram.com/" in url and not url.endswith("instagram.com/"):
                    if not result["instagram"]:
                        result["instagram"] = r["link"]
                elif "facebook.com/" in url and not url.endswith("facebook.com/"):
                    if not result["facebook"]:
                        result["facebook"] = r["link"]
                elif not result["website"] and not any(s in url for s in [
                    "sunbiz", "floridados", "linkedin", "instagram",
                    "facebook", "twitter", "yelp", "bbb.org", "yellowpages"
                ]):
                    result["website"] = r["link"]

                result["search_snippet"] += snip + " "

            result["is_active"] = bool(result["website"] or result["linkedin"]
                                        or result["instagram"] or result["facebook"])
            return result

        except Exception:
            pass  # fall through to DuckDuckGo

    # ── Option B: DuckDuckGo HTML scrape (free, ~1 req/2s to avoid blocks) ─
    try:
        ddg_url = "https://html.duckduckgo.com/html/"
        resp = requests.post(
            ddg_url,
            data={"q": query, "b": "", "kl": "us-en"},
            headers={**_SCRAPE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=12,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.select("a.result__url, a.result__a")

        for tag in links:
            href = tag.get("href", "")
            # DuckDuckGo wraps URLs — extract the real URL
            if "uddg=" in href:
                from urllib.parse import unquote, urlparse, parse_qs
                qs = parse_qs(urlparse(href).query)
                href = unquote(qs.get("uddg", [""])[0])

            url = href.lower()
            if not url.startswith("http"):
                continue

            if "linkedin.com/in/" in url or "linkedin.com/company/" in url:
                if not result["linkedin"]: result["linkedin"] = href
            elif "instagram.com/" in url and len(url.split("instagram.com/")[-1]) > 1:
                if not result["instagram"]: result["instagram"] = href
            elif "facebook.com/" in url and len(url.split("facebook.com/")[-1]) > 1:
                if not result["facebook"]: result["facebook"] = href
            elif not result["website"] and not any(s in url for s in [
                "sunbiz", "floridados", "linkedin", "instagram", "facebook",
                "twitter", "x.com", "yelp", "bbb.org", "yellowpages", "mapquest",
                "whitepages", "spokeo", "radaris", "bizapedia", "opencorporates"
            ]):
                result["website"] = href

        # ── Scrape the website for email + phone ──────────────────────────
        if result["website"]:
            try:
                wr = requests.get(result["website"], headers=_SCRAPE_HEADERS,
                                  timeout=8, allow_redirects=True)
                page_text = BeautifulSoup(wr.text, "html.parser").get_text(" ")

                # Email
                emails = re.findall(
                    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', page_text
                )
                skip_domains = {"example.com", "yourdomain.com", "gmail.com" , "wixpress.com",
                                 "squarespace.com", "wordpress.com", "sentry.io"}
                for em in emails:
                    if em.split("@")[-1].lower() not in skip_domains:
                        result["email"] = em.lower()
                        break

                # Phone
                phones = re.findall(
                    r'\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}', page_text
                )
                if phones:
                    result["phone"] = phones[0].strip()

            except Exception:
                pass

        result["is_active"] = bool(
            result["website"] or result["linkedin"]
            or result["instagram"] or result["facebook"]
        )

    except Exception:
        pass

    return result


def run_google_enrichment_sync(batch_size: int = 25, delay_sec: float = 2.0,
                                serp_api_key: str = ""):
    """
    Run Google search enrichment for leads that haven't been searched yet.
    Synchronous — call directly from button handler.
    """
    leads = db_get_ungoogled(limit=batch_size)

    with st.status(f"Google enrichment: {len(leads)} leads…", expanded=True) as status:
        def w(msg):
            ts = datetime.now().strftime("%H:%M:%S")
            status.write(f"[{ts}] {msg}")

        if not leads:
            w("✅ All leads already searched.")
            status.update(label="Nothing to search", state="complete", expanded=False)
            return

        found_web = found_li = found_ig = found_fb = found_email = found_phone = 0

        for i, lead in enumerate(leads):
            name    = lead["entity_name"]
            owner   = lead.get("owner_name", "")
            city    = lead.get("principal_addr", "")
            num     = lead["entity_number"]

            w(f"🔍 [{i+1}/{len(leads)}] {name}")

            data = google_search_lead(name, owner, city, serp_api_key)
            db_save_google_results(num, data)

            parts = []
            if data["website"]:   found_web   += 1; parts.append(f"🌐 {data['website'][:50]}")
            if data["linkedin"]:  found_li    += 1; parts.append(f"💼 LinkedIn")
            if data["instagram"]: found_ig    += 1; parts.append(f"📸 Instagram")
            if data["facebook"]:  found_fb    += 1; parts.append(f"👍 Facebook")
            if data["email"]:     found_email += 1; parts.append(f"✉ {data['email']}")
            if data["phone"]:     found_phone += 1; parts.append(f"📞 {data['phone']}")

            if parts:
                w(f"  → {' | '.join(parts)}")
            else:
                w(f"  → No web presence found (likely dormant)")

            if i < len(leads) - 1:
                time.sleep(delay_sec)

        w("")
        w(f"✅ Done — {len(leads)} searched")
        w(f"   🌐 Websites:  {found_web}  |  💼 LinkedIn: {found_li}")
        w(f"   📸 Instagram: {found_ig}  |  👍 Facebook: {found_fb}")
        w(f"   ✉  Emails:   {found_email}  |  📞 Phones: {found_phone}")
        w(f"   🏢 Active businesses confirmed: {found_web+found_li+found_ig+found_fb}")
        status.update(
            label=f"✅ {len(leads)} searched — {found_web} websites, {found_li} LinkedIn, {found_ig} Instagram",
            state="complete", expanded=False,
        )


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
    # Only use valid FL domestic entity types — same filter as the real parser
    valid_types = list(VALID_ENTITY_TYPES)
    first_names = ["James","Maria","Robert","Linda","Michael","Patricia",
                   "Carlos","Ana","David","Jennifer","Luis","Sofia"]
    last_names  = ["Smith","Johnson","Williams","Garcia","Martinez","Rodriguez",
                   "Brown","Jones","Davis","Miller","Wilson","Taylor"]
    fl_cities   = ["Miami","Orlando","Tampa","Jacksonville","Fort Lauderdale",
                   "Boca Raton","Naples","Sarasota","Gainesville","Tallahassee"]
    records = []
    for i in range(n):
        fn, ln  = random.choice(first_names), random.choice(last_names)
        etype   = random.choice(valid_types)
        yr      = random.choice([2023, 2024, 2025, 2025])   # 2025 or earlier — these owe 2026 annual report
        city    = random.choice(fl_cities)
        num     = f"L{random.randint(10000000,99999999)}"
        suffix  = {"AL":"LLC","CP":"CORP","PA":"P.A.","NP":"INC","LP":"LP","PL":"PLLC"}.get(etype,"LLC")
        records.append({
            "record_type"    : etype,
            "entity_number"  : num,
            "status"         : "A",          # always Active in mock data
            "filing_date"    : f"0{random.randint(1,9)}{random.randint(10,28)}{yr}",
            "entity_name"    : f"{ln} {suffix} {i+1}",
            "last_rpt_year"  : yr,
            "principal_addr" : f"{random.randint(100,9999)} {ln} BLVD, {city} FL",
            "principal_email": "",           # blank — like real data, enriched separately
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
            w(f"🔐 Connecting to {SFTP_HOST}:{SFTP_PORT} as \'{SFTP_USER}\' …")
            transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
            transport.connect(username=SFTP_USER, password=SFTP_PASS)
            sftp = paramiko.SFTPClient.from_transport(transport)
            w("✅ SFTP connection established.")

            # ── All daily files live flat in /Public/doc/cor/ ─────────────
            remote_dir = "/Public/doc/cor/"
            attrs = sftp.listdir_attr(remote_dir)

            # Keep only YYYYMMDDc.txt files
            all_daily = sorted(
                [a for a in attrs if a.filename.endswith("c.txt") and a.filename[:4].isdigit()],
                key=lambda a: a.filename,
                reverse=True,   # NEWEST first — recent registrations are far more likely active
            )

            # ── Date window filter ────────────────────────────────────────
            # START: set in sidebar (default Jan 1 2025)
            # END:   always capped at Dec 31 2025 — files from 2026 onward contain
            #        brand-new 2026 registrations which don't owe a report until 2027.
            #        Downloading those files wastes runs and produces zero usable leads.
            start_cutoff = st.session_state.get("pipeline_start_date", "20250101")
            end_cutoff   = "20251231"   # hard cap — never pull 2026 registration files

            daily = [
                a for a in all_daily
                if start_cutoff <= a.filename[:8] <= end_cutoff
            ]

            w(f"📂 Found {len(all_daily):,} total files on server")
            w(f"   Window: {start_cutoff[:4]}-{start_cutoff[4:6]}-{start_cutoff[6:]} → 2025-12-31")
            w(f"   Files in window: {len(daily):,}")
            if daily:
                w(f"   Newest in range: {daily[0].filename}  |  Oldest: {daily[-1].filename}")

            # ── Cursor: skip files already downloaded ─────────────────────
            already_done = db_get_downloaded_files()
            new_files    = [a for a in daily if a.filename not in already_done]

            w(f"   Already downloaded: {len(already_done):,} files")
            w(f"   New files to fetch: {len(new_files):,} files")

            if not new_files:
                w("✅ All files in date range already downloaded.")
                sftp.close()
                transport.close()
                w("🔌 SFTP connection closed.")
                all_records = []
            else:
                # Batch size from sidebar
                num_files = st.session_state.get("pipeline_num_files", 5)
                target    = new_files[:num_files]   # newest-first batch of N new files

                if len(new_files) > num_files:
                    w(f"⏭  Processing {num_files} of {len(new_files)} new files this run")
                    w(f"   ({len(new_files) - num_files} more on next run)")
                else:
                    w(f"⬇  Downloading all {len(target)} new files …")

                all_records_raw = []
                for attr in target:
                    fname = attr.filename
                    fpath = remote_dir + fname
                    w(f"   📄 {fname}  ({attr.st_size/1024:.0f} KB)")
                    buf = io.BytesIO()
                    sftp.getfo(fpath, buf)
                    buf.seek(0)
                    recs = parse_file_buffer(buf.read(), source_file=fname)
                    db_mark_file_downloaded(fname, len(recs))
                    w(f"      → {len(recs):,} records  ✓ cursor saved")
                    all_records_raw.extend(recs)

                sftp.close()
                transport.close()
                w("🔌 SFTP connection closed.")

                cursor = db_get_cursor_stats()
                w(f"")
                w(f"📊 Cursor progress: {cursor['files_downloaded']:,} / {len(daily):,} total files")
                w(f"   Total records ever parsed: {cursor['total_parsed']:,}")
                w(f"   Files remaining on server: {len(daily) - cursor['files_downloaded']:,}")
                w(f"✅ Total new records this run: {len(all_records_raw):,}")
                all_records = all_records_raw

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

        w("🏁 Pipeline complete — starting background enrichment…")
        status.update(label="✅ Pipeline complete", state="complete", expanded=False)

    st.session_state.pipeline_logs     = logs
    st.session_state.pipeline_complete = True
    st.session_state.last_pipeline_run = datetime.now().strftime("%b %d, %Y at %I:%M %p")

    # Auto-start silent background enrichment immediately after pipeline
    start_bg_enrichment(delay_sec=2.0)


# ═════════════════════════════════════════════════════════════════════════════
#  EMAIL LOGIC
# ═════════════════════════════════════════════════════════════════════════════

EMAIL_SUBJECT = "Action Required: Your Florida Annual Report Is Past Due"

def build_email(entity_name, owner_name, last_rpt_year, entity_number) -> str:
    owner = (owner_name or "").strip() or "Business Owner"
    yr    = str(last_rpt_year) if last_rpt_year else "a prior year"
    return textwrap.dedent(f"""\
        Dear {owner},

        I'm reaching out because {entity_name} (Entity # {entity_number}) is
        listed as Active in Florida's public Sunbiz records, and based on your
        registration date your 2026 Annual Report appears to still be pending.

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

    st.markdown('<div class="sh" style="margin-top:1rem;"><span class="dot"></span>PIPELINE SETTINGS</div>', unsafe_allow_html=True)

    from datetime import date as _date
    # Hard boundaries:
    #   Start — configurable (default Jan 1 2025 for freshest leads)
    #   End   — always Dec 31 2025 (2026 files = new registrations, no report due until 2027)
    default_start = _date(2025, 1, 1)
    end_cap       = _date(2025, 12, 31)

    start_date = st.date_input(
        "Pull files starting from",
        value=default_start,
        min_value=_date(2022, 11, 1),
        max_value=end_cap,
        key="pipeline_start_date_picker",
        help="Files are always capped at Dec 31 2025. "
             "2026 registration files are excluded — those businesses don't owe "
             "an annual report until May 1, 2027."
    )
    start_date_str = start_date.strftime("%Y%m%d")
    st.session_state["pipeline_start_date"] = start_date_str

    days_in_range = (end_cap - start_date).days
    est_files     = max(1, int(days_in_range * 0.71))
    est_leads     = est_files * 2500
    st.caption(f"Window: {start_date.strftime('%b %d %Y')} → Dec 31 2025")
    st.caption(f"≈ {est_files:,} files · {est_leads:,} potential leads")

    pipeline_num_files = st.slider("Files per run", 1, 30, 5, key="pipeline_num_files",
                                    help="How many daily files to download each time you click Run Pipeline")
    st.caption(f"≈ {pipeline_num_files * 2500:,} new leads per run")

    st.markdown('<div class="sh" style="margin-top:0.75rem;"><span class="dot"></span>EMAIL LIMITS</div>', unsafe_allow_html=True)
    daily_limit = st.slider("Daily Send Limit", 10, 500, 100)
    delay_sec   = st.slider("Delay Between Sends (s)", 0, 10, 2)

    st.markdown('<div class="sh" style="margin-top:1rem;"><span class="dot"></span>ENRICHMENT</div>', unsafe_allow_html=True)
    bg_now = bg_status()
    enrich_delay_s = st.slider("Delay between checks (s)", 1.0, 5.0, 2.0, step=0.5,
                                key="enrich_delay_s",
                                help="Lower = faster but more load on Sunbiz servers")
    if bg_now["running"]:
        st.markdown('<span style="color:#4caf7d;font-size:.8rem;font-family:monospace;">● Running in background…</span>', unsafe_allow_html=True)
        st.caption(f"Checked: {bg_now['checked']} · Removed: {bg_now['removed']}")
    else:
        if st.button("▶ Start Background Enrichment", use_container_width=True,
                     help="Silently checks every lead against Sunbiz live status"):
            start_bg_enrichment(delay_sec=enrich_delay_s)
            st.rerun()
    st.caption("Runs silently in background. Inactive leads deleted automatically.")

    st.markdown('<div class="sh" style="margin-top:0.75rem;"><span class="dot"></span>GOOGLE ENRICHMENT</div>', unsafe_allow_html=True)
    serp_api_key  = st.text_input("SerpAPI Key (optional)", type="password",
                                   placeholder="leave blank = free DuckDuckGo",
                                   key="serp_api_key",
                                   help="Get a key at serpapi.com — $50/mo for 5k searches. Leave blank to use free DuckDuckGo scraping.")
    google_batch  = st.slider("Google batch size", 5, 100, 25)
    google_delay  = st.slider("Google delay (s)", 1.0, 6.0, 2.0, step=0.5)
    st.caption("Finds website, LinkedIn, Instagram, Facebook, email & phone.")

    st.markdown("---")
    if st.session_state.last_pipeline_run:
        st.caption(f"Last run: {st.session_state.last_pipeline_run}")

    # Cursor progress
    try:
        cur = db_get_cursor_stats()
        if cur["files_downloaded"] > 0:
            st.markdown('<div class="sh"><span class="dot"></span>PIPELINE CURSOR</div>', unsafe_allow_html=True)
            st.caption(f"📁 {cur['files_downloaded']:,} files downloaded")
            st.caption(f"📄 {cur['total_parsed']:,} total records parsed")
            st.caption(f"📅 Last file: {cur['newest_file']}")
            if st.button("🔄 Reset Cursor", use_container_width=True,
                         help="Forces a full re-download of all files on next run"):
                db_reset_cursor()
                st.success("Cursor reset — next run will start from the beginning.")
                st.rerun()
    except Exception:
        pass


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
    google_btn = st.button(
        "🌐  Google Search",
        type="secondary",
        use_container_width=True,
        help="Find websites, LinkedIn, Instagram, Facebook for each lead",
    )

if run_btn:
    run_pipeline_sync()
    st.rerun()

if google_btn:
    run_google_enrichment_sync(
        batch_size=google_batch,
        delay_sec=google_delay,
        serp_api_key=serp_api_key,
    )
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
    <div class="kpi-sub">{stats['contacted']:,} contacted · {stats['paid']:,} paid · {stats.get('enriched',0):,} enriched</div>
  </div>
  <div class="kpi-cell">
    <div class="kpi-lbl">Hard Deadline</div>
    <div class="kpi-val" style="font-size:1.35rem;">{DEADLINE}</div>
    <div class="kpi-sub">Florida DOS cutoff</div>
  </div>
</div>
""", unsafe_allow_html=True)


# ── Background enrichment live panel ─────────────────────────────────────────
bg = bg_status()
if bg["running"] or bg["checked"] > 0:
    total    = bg["checked"]
    removed  = bg["removed"]
    kept     = total - removed
    emails   = bg["emails_found"]
    phones   = bg["phones_found"]
    running  = bg["running"]
    log_lines = bg.get("log", [])
    started   = bg.get("started_at", "")

    # ── Stat pills ──
    status_color = "#4caf7d" if running else "#5a6472"
    status_label = "● LIVE — CHECKING SUNBIZ" if running else "■ ENRICHMENT COMPLETE"
    pct_removed  = int(removed / total * 100) if total > 0 else 0

    st.markdown(f"""
    <div style="background:#0a0f0d;border:1px solid #1e3a2a;border-radius:6px;
                overflow:hidden;margin-bottom:1.25rem;">

        <!-- Header bar -->
        <div style="background:#0d1f17;padding:.6rem 1.1rem;
                    display:flex;justify-content:space-between;align-items:center;
                    border-bottom:1px solid #1e3a2a;">
            <div style="display:flex;align-items:center;gap:.6rem;">
                <span style="font-size:.68rem;font-weight:700;color:{status_color};
                             text-transform:uppercase;letter-spacing:.12em;
                             font-family:'IBM Plex Mono',monospace;">
                    {status_label}
                </span>
                {f'<span style="font-size:.68rem;color:#3d5a47;font-family:monospace;">started {started}</span>' if started else ''}
            </div>
            <div style="display:flex;gap:1.25rem;">
                <span style="font-family:'IBM Plex Mono',monospace;font-size:.72rem;color:#5a6472;">
                    <span style="color:#f0f4f8;font-weight:600;">{total}</span> checked
                </span>
                <span style="font-family:'IBM Plex Mono',monospace;font-size:.72rem;color:#5a6472;">
                    <span style="color:#e05252;font-weight:600;">{removed}</span> removed ({pct_removed}%)
                </span>
                <span style="font-family:'IBM Plex Mono',monospace;font-size:.72rem;color:#5a6472;">
                    <span style="color:#4caf7d;font-weight:600;">{kept}</span> active
                </span>
                <span style="font-family:'IBM Plex Mono',monospace;font-size:.72rem;color:#5a6472;">
                    <span style="color:#5b9cf6;font-weight:600;">{emails}</span> emails ·
                    <span style="color:#e8a020;font-weight:600;">{phones}</span> phones
                </span>
            </div>
        </div>

        <!-- Progress bar -->
        <div style="height:2px;background:#1e3a2a;">
            <div style="height:2px;background:{'#4caf7d' if running else '#2d5a3d'};
                        width:{min(pct_removed+5,100) if running else 100}%;
                        transition:width .5s;"></div>
        </div>

        <!-- Activity log -->
        <div style="padding:.65rem 1.1rem;font-family:'IBM Plex Mono',monospace;
                    font-size:.75rem;line-height:1.9;max-height:180px;overflow-y:auto;">
            {"".join([
                f'<div style="color:{"#e05252" if "REMOVED" in l else "#4caf7d" if "ACTIVE" in l else "#e8a020" if "Complete" in l or "started" in l else "#a8b4c0"};'
                f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{l}</div>'
                for l in reversed(log_lines)
            ]) if log_lines else
            '<div style="color:#3d4450;">Waiting for first result…</div>'}
        </div>
    </div>
    """, unsafe_allow_html=True)

    if running:
        time.sleep(2)
        st.rerun()


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

        COLS = ["entity_number","entity_name","owner_name","principal_email",
                "principal_phone","website","linkedin_url","instagram_url",
                "facebook_url","last_rpt_year","contact_status","inserted_at"]
        cfg  = {
            "entity_number"  : st.column_config.TextColumn("Entity #",    width="small"),
            "entity_name"    : st.column_config.TextColumn("Entity",      width="large"),
            "owner_name"     : st.column_config.TextColumn("Owner",       width="medium"),
            "principal_email": st.column_config.TextColumn("Email",       width="medium"),
            "principal_phone": st.column_config.TextColumn("Phone",       width="small"),
            "website"        : st.column_config.LinkColumn("Website",     width="medium", display_text="🌐 Open"),
            "linkedin_url"   : st.column_config.LinkColumn("LinkedIn",    width="small",  display_text="💼"),
            "instagram_url"  : st.column_config.LinkColumn("Instagram",   width="small",  display_text="📸"),
            "facebook_url"   : st.column_config.LinkColumn("Facebook",    width="small",  display_text="👍"),
            "last_rpt_year"  : st.column_config.NumberColumn("Reg Year",  width="small",  format="%d"),
            "contact_status" : st.column_config.SelectboxColumn("Status",
                                   options=["New","Contacted","Paid"], width="small"),
            "inserted_at"    : st.column_config.TextColumn("Added",       width="medium"),
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

            enriched = "✅ enriched" if sel.get("enriched_at") else "⏳ not yet enriched"
            st.markdown(f"""
            <div class="lcard">
              <div class="lcard-name">{sel.get('entity_name','—')}</div>
              <div class="lcard-det">
Owner     {sel.get('owner_name','—') or '—'}
Email     {sel.get('principal_email','—') or '─ run Enrich'}
Phone     {sel.get('principal_phone','—') or '─ run Enrich'}
Website   {sel.get('website','—') or '─ run Google Search'}
LinkedIn  {sel.get('linkedin_url','—') or '─'}
Instagram {sel.get('instagram_url','—') or '─'}
Facebook  {sel.get('facebook_url','—') or '─'}
Entity    {sel.get('entity_number','—')}
Addr      {sel.get('principal_addr','—') or '—'}
Enrich    {enriched}</div>
            </div>
            <span class="badge {sc}">{sel.get('contact_status','New')}</span>
            <div style="margin-top:0.75rem; display:flex; gap:0.5rem; flex-wrap:wrap;">
                {f'<a href="{sel["website"]}" target="_blank" style="text-decoration:none;"><span class="badge bn">🌐 Website</span></a>' if sel.get("website") else ""}
                {f'<a href="{sel["linkedin_url"]}" target="_blank" style="text-decoration:none;"><span class="badge bc">💼 LinkedIn</span></a>' if sel.get("linkedin_url") else ""}
                {f'<a href="{sel["instagram_url"]}" target="_blank" style="text-decoration:none;"><span class="badge bn">📸 Instagram</span></a>' if sel.get("instagram_url") else ""}
                {f'<a href="{sel["facebook_url"]}" target="_blank" style="text-decoration:none;"><span class="badge bp">👍 Facebook</span></a>' if sel.get("facebook_url") else ""}
            </div>
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
