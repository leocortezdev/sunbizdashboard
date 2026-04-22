# Sunbiz Compliance Pro — Deployment & Wiring Guide

## Quick Start (Local)

```bash
pip install -r requirements.txt
streamlit run app.py
```

Runs at http://localhost:8501. On first "Run Pipeline" click with no SFTP
reachability, a mock dataset of ~350 delinquent records is auto-generated
so you can explore the full UI immediately.

---

## Deploy to Streamlit Community Cloud (Free)

1. Push to GitHub
```bash
git add app.py requirements.txt
git commit -m "Sunbiz Compliance Pro v2"
git push
```

2. Go to https://share.streamlit.io → "Create app"
   - Repository: `your-username/your-repo`
   - Branch: `main`
   - Main file: `app.py`
   - Click **Deploy**

3. Add Secrets (Settings → Secrets):
```toml
RESEND_API_KEY   = "re_xxxxxxxxxxxxxxxxxxxx"
FROM_EMAIL       = "compliance@yourdomain.com"
```
Or for SendGrid:
```toml
SENDGRID_API_KEY = "SG.xxxxxxxxxxxxxxxxxxxx"
FROM_EMAIL       = "compliance@yourdomain.com"
```

> ⚠️ Never commit API keys to git. Use Streamlit Secrets only.

---

## Wiring Real Email (Resend — Recommended)

```python
# Already implemented in app.py send_email()
# Just set environment variable:
import os
os.environ["RESEND_API_KEY"] = "re_..."
```

Install: `pip install resend`
Docs: https://resend.com/docs/send-with-python

---

## SQLite Database

The app creates `sunbiz_leads.db` in the working directory automatically.
Schema:
- `leads` table — entity records with contact_status (New/Contacted/Paid)
- `email_log` table — every send is logged with timestamp + API response

To inspect locally:
```bash
sqlite3 sunbiz_leads.db
.tables
SELECT COUNT(*) FROM leads;
SELECT * FROM email_log LIMIT 10;
```

On Streamlit Cloud, the DB resets on each deploy (ephemeral filesystem).
For persistence on Cloud, swap SQLite for:
- Supabase (free Postgres): https://supabase.com
- PlanetScale (free MySQL): https://planetscale.com

---

## File Structure

```
your-project/
├── app.py              ← Everything (all 4 modules)
├── requirements.txt    ← Python dependencies
├── DEPLOY.md           ← This file
├── sunbiz_leads.db     ← Auto-created SQLite (gitignore this)
└── .streamlit/
    └── secrets.toml    ← Local API keys (gitignore this)
```

Add to `.gitignore`:
```
sunbiz_leads.db
.streamlit/secrets.toml
__pycache__/
*.pyc
```
