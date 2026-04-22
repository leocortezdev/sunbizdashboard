# Florida Sunbiz Compliance Dashboard
## Deployment Guide

---

## 1. Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run the app
streamlit run app.py
```

Access at: http://localhost:8501

---

## 2. Deploy to Streamlit Community Cloud (Free, Recommended)

### Prerequisites
- Free account at https://streamlit.io/cloud
- Your code in a **GitHub repository** (public or private)

### Steps

1. **Push your files to GitHub:**
   ```bash
   git init
   git add app.py requirements.txt
   git commit -m "Initial Sunbiz dashboard"
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
   git push -u origin main
   ```

2. **Deploy from Streamlit Cloud:**
   - Go to https://share.streamlit.io
   - Click **"New app"**
   - Select your repository, branch (`main`), and file (`app.py`)
   - Click **"Deploy"**

3. **Add Secrets (API keys — never hardcode them):**
   In Streamlit Cloud, go to **App Settings → Secrets** and add:
   ```toml
   [sftp]
   host = "sftp.example.com"
   user = "your_user"
   password = "your_password"
   port = 22

   [email]
   resend_api_key = "re_xxxxxxxxxxxx"
   from_address = "you@yourdomain.com"
   ```
   Then access in `app.py` with `st.secrets["sftp"]["host"]`

4. Your app is now **live at a public URL** and accessible from your phone.

---

## 3. Deploy via Cloudflare Tunnel (Self-Hosted, Phone Access)

Use this if you want to run on your own machine and expose it securely without port-forwarding.

### Install cloudflared
```bash
# macOS
brew install cloudflare/cloudflare/cloudflared

# Linux / Ubuntu
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb

# Windows
winget install --id Cloudflare.cloudflared
```

### One-command tunnel (no account needed for testing)
```bash
# Start Streamlit in background
streamlit run app.py &

# Expose via Cloudflare Tunnel
cloudflared tunnel --url http://localhost:8501
```
You'll get a temporary HTTPS URL like:
`https://random-name.trycloudflare.com`

Access it from your phone instantly.

### Persistent Tunnel (requires free Cloudflare account)
```bash
# Authenticate
cloudflared tunnel login

# Create a named tunnel
cloudflared tunnel create sunbiz-dashboard

# Configure (create ~/.cloudflared/config.yml)
# tunnel: <TUNNEL_ID>
# credentials-file: /root/.cloudflared/<TUNNEL_ID>.json
# ingress:
#   - hostname: dashboard.yourdomain.com
#     service: http://localhost:8501
#   - service: http_status:404

# Start the tunnel
cloudflared tunnel run sunbiz-dashboard
```

---

## 4. Wiring in Real SFTP (Paramiko)

In `app.py`, inside `run_sftp_scan()`, uncomment and use:

```python
import paramiko
import io

transport = paramiko.Transport((sftp_host, int(sftp_port)))
transport.connect(username=sftp_user, password=sftp_pass)
sftp = paramiko.SFTPClient.from_transport(transport)

# Download file to memory buffer
buf = io.BytesIO()
sftp.getfo(remote_path, buf)
buf.seek(0)

df_raw = pd.read_csv(buf)
sftp.close()
transport.close()

# Filter delinquent
delinquent_df = df_raw[df_raw["Status"].str.lower() == "delinquent"].copy()
```

---

## 5. Wiring in Resend Email API

```python
import resend

resend.api_key = st.secrets["email"]["resend_api_key"]

params = {
    "from": st.secrets["email"]["from_address"],
    "to": [row["Email"]],
    "subject": "Urgent: Your Florida Annual Report is Past Due",
    "html": email_body_html,
}
resend.Emails.send(params)
```

---

## File Structure

```
your-project/
├── app.py              ← Main Streamlit dashboard
├── requirements.txt    ← Python dependencies
├── README.md           ← This file
└── .streamlit/
    └── secrets.toml    ← Local secrets (never commit this!)
```

Add `.streamlit/secrets.toml` to your `.gitignore`.
