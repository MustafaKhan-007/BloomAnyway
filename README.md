# First Light

A warm, mobile-first storefront for digital courses and notebook guides, built to
replace Gumroad. Flask + PostgreSQL, with **Lemon Squeezy hosted checkout** as the
merchant of record (payments, tax, and file delivery all happen on their side —
this site never touches card data and stores no files).

What's inside:

- Full catalog with filterable shop, rich product pages, and overlay checkout
- Daily motivational quote with deterministic rotation, pinning, and a
  kind-by-design check-in streak system
- Passwordless magic-link login (no passwords anywhere)
- Admin studio: dashboard with revenue charts, product/quote/testimonial/FAQ/page
  management, subscriber & order CSV exports, site settings
- Lemon Squeezy webhook receiver (signed, idempotent) + manual API reconciliation

---

## 1. Local setup

Requires Python 3.12+.

```bash
git clone <this repo> && cd <this repo>
python -m venv .venv
# Windows:  .venv\Scripts\activate     macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

copy .env.example .env        # cp on macOS/Linux — then edit ADMIN_EMAIL at least

# create the SQLite dev database (no DATABASE_URL needed locally)
set FLASK_APP=app:create_app  # PowerShell: $env:FLASK_APP = "app:create_app"
flask db upgrade

# create the admin user + load 150 quotes + starter FAQ/legal stubs
python seed.py

flask run
```

Open http://localhost:5000. **Email in dev:** when `SMTP_HOST` is empty, every
email (including your magic login link) is printed to the terminal running
`flask run` — copy the link from there to sign in as admin, then visit
http://localhost:5000/admin.

### Environment variables

See `.env.example` for the full annotated list. In production ALL of these are
required (the app refuses to boot otherwise): `SECRET_KEY`, `DATABASE_URL`,
`LEMONSQUEEZY_WEBHOOK_SECRET`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
`SMTP_PASSWORD`, `MAIL_FROM`, `ADMIN_EMAIL`, `SITE_URL`.

---

## 2. Lemon Squeezy setup

1. **API key** — LS dashboard → *Settings → API* → create a key →
   `LEMONSQUEEZY_API_KEY`. Used only by the dashboard "Sync with Lemon Squeezy"
   button (drift repair); day-to-day order data arrives via webhooks.
2. **Store ID** — *Settings → Stores*; the number next to your store →
   `LEMONSQUEEZY_STORE_ID`.
3. **Webhook** — *Settings → Webhooks → "+"*:
   - Callback URL: `https://<your-app>.onrender.com/webhooks/lemonsqueezy`
   - Signing secret: any long random string → also set it as
     `LEMONSQUEEZY_WEBHOOK_SECRET` on the server
   - Subscribe to: `order_created` and `order_refunded`
4. **Per product** (in this site's admin → Products):
   - **Buy link**: LS product → *Share* → copy the checkout/buy link → paste
     into "Lemon Squeezy buy link". Buttons use `lemon.js`, so checkout opens
     as an overlay on your page (plain link if JS is off).
   - **Variant ID**: LS product → *Variants* tab → the variant's ID → paste
     into "Lemon Squeezy variant ID". This is how webhook orders are matched
     to the product for your dashboard stats.
5. **PayPal** — enable it once in LS *Settings → Payment methods*; it appears
   at checkout automatically, no code change.

To test the webhook locally, send a signed request:

```bash
python - <<'PY'
import hmac, hashlib, json, urllib.request
secret = b"change-me-too"   # your LEMONSQUEEZY_WEBHOOK_SECRET
body = json.dumps({"meta": {"event_name": "order_created"},
  "data": {"id": "1001", "attributes": {"user_email": "buyer@example.com",
  "total": 2900, "currency": "USD", "status": "paid",
  "first_order_item": {"variant_id": 123456}}}}).encode()
sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
req = urllib.request.Request("http://localhost:5000/webhooks/lemonsqueezy",
  data=body, headers={"Content-Type": "application/json", "X-Signature": sig})
print(urllib.request.urlopen(req).read())
PY
```

---

## 3. Deploying to Render

`render.yaml` defines everything (web service + managed Postgres):

1. Push the repo to GitHub.
2. In Render: *New → Blueprint*, pick the repo. Render creates the web service
   and the database, wiring `DATABASE_URL` automatically.
3. Fill in the `sync: false` env vars (secrets) in the Render dashboard.
4. Deploy. The build runs `pip install -r requirements.txt && flask db upgrade`;
   the server is `gunicorn "app:create_app()" --workers 2 --threads 4 --timeout 60`
   with health checks on `/healthz`.
5. Open a shell on the service (or a one-off job) and run `python seed.py` once.
6. Point the Lemon Squeezy webhook (section 2) at your Render URL.

### Things to know about Render

- **The disk is ephemeral.** It's wiped on every deploy/restart. That's why
  product images are pasted URLs (Instagram CDN, Imgur, Cloudinary, ...) and all
  content lives in Postgres; nothing is ever written to local disk.
- **Rate limits reset on restart.** Flask-Limiter uses in-memory storage, which
  is fine at this scale — but note that a deploy clears the counters.
- Render terminates TLS; the app sets secure cookies and
  `PREFERRED_URL_SCHEME=https` in production.
- Logs (auth events, webhook failures) go to stdout — visible in Render's log tab.

---

## 4. How the daily quote works

- `quote_for(date)` picks deterministically: pinned quote if one exists for the
  date, otherwise `sha256(date) % len(active quotes)` after a weekday tone
  filter (Mon/Tue lean *determination*, Sat/Sun lean *comfort*). Same quote for
  everyone all day; changes at midnight; survives restarts.
- Admin → Quotes lets her add, edit, deactivate, bulk-import
  (`text | author | category` per line, deduped with preview), pin a quote to a
  launch date, and preview tomorrow's pick.
- Streaks are gentle by design: one missed day per rolling 7 is a "rest day"
  and doesn't reset the streak.

## 5. Security notes

- Magic links: 32-byte random tokens, only the SHA-256 hash stored, single-use,
  15-minute expiry, uniform responses (no account enumeration), 3/email/hour +
  10/IP/hour rate limits, `next` restricted to relative paths.
- Sessions: `Secure`/`HttpOnly`/`SameSite=Lax`, 30-day remember cookie. Admin
  routes require `is_admin` **and** a login fresher than 24h, and return 404 to
  everyone else.
- CSRF on every form (webhook route exempt — it's authenticated by HMAC
  signature instead, verified with constant-time compare).
- All admin-entered Markdown is sanitized (bleach allow-list) before rendering.
- Security headers + a conservative CSP (self + Google Fonts + Lemon Squeezy +
  jsDelivr; `img-src https:` since product images are admin-pasted URLs).
