# GBP Review Manager — Deployment Guide
# gbp.kizieetechworld.in

## Project Structure
```
gbp_system/
├── app.py                  # Flask backend (all routes + DB logic)
├── requirements.txt
├── gbp.db                  # SQLite DB (auto-created on first run)
├── .env                    # Your secrets (never commit this)
└── templates/
    ├── review.html         # Customer-facing review page
    ├── dashboard.html      # Business owner dashboard
    ├── login.html          # Login page
    └── register.html       # New business registration
```

## URLs
| URL | Who sees it |
|-----|------------|
| `gbp.kizieetechworld.in/review/{slug}` | Customers — swipeable review page |
| `gbp.kizieetechworld.in/login` | Business owners — login |
| `gbp.kizieetechworld.in/register` | New business signup |
| `gbp.kizieetechworld.in/dashboard` | Business dashboard (after login) |

---

## Step 1 — Server Setup (Ubuntu/Debian VPS)

```bash
# Install Python & pip
sudo apt update && sudo apt install python3 python3-pip python3-venv nginx -y

# Create project folder
mkdir -p /var/www/gbp
cd /var/www/gbp

# Upload all files here, then:
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Step 2 — Environment Variables

Create `/var/www/gbp/.env`:
```
SECRET_KEY=your-random-secret-key-here
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx
```

Get free Groq API key at: https://console.groq.com

## Step 3 — Run with Gunicorn

```bash
# Test run
source venv/bin/activate
gunicorn -w 4 -b 127.0.0.1:5000 app:app

# Production: create systemd service
sudo nano /etc/systemd/system/gbp.service
```

Paste this into the service file:
```ini
[Unit]
Description=GBP Review Manager
After=network.target

[Service]
User=www-data
WorkingDirectory=/var/www/gbp
Environment="PATH=/var/www/gbp/venv/bin"
EnvironmentFile=/var/www/gbp/.env
ExecStart=/var/www/gbp/venv/bin/gunicorn -w 4 -b 127.0.0.1:5000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable gbp
sudo systemctl start gbp
```

## Step 4 — Nginx Config

```bash
sudo nano /etc/nginx/sites-available/gbp
```

```nginx
server {
    listen 80;
    server_name gbp.kizieetechworld.in;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/gbp /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## Step 5 — SSL (Free via Certbot)

```bash
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d gbp.kizieetechworld.in
```

---

## How to Register Woho Jobs

1. Go to `gbp.kizieetechworld.in/register`
2. Name: `Woho Jobs`
3. Slug: `woho-jobs`
4. Paste your Google Maps review URL
5. Set a passcode
6. Done! Share: `gbp.kizieetechworld.in/review/woho-jobs`

## Getting Google Maps Review URL

1. Search your business on Google Maps
2. Click Share → Copy link
   OR
3. In Google Business Profile dashboard → Get more reviews → Copy link

---

## Groq Free Tier
- Model used: `llama3-8b-8192`
- Free tier: 14,400 requests/day — enough for ~500 reviews/day
- No credit card needed
- Get key: https://console.groq.com/keys

## Per-Business Groq Key
Each business can add their own Groq key in Settings for unlimited usage.
