# Bloom Anyway — a map of the codebase

This document is for an AI assistant picking the project up cold. It is about
**where things are**, not what the features do. Features get a sentence;
locations, relationships and traps get paragraphs.

Written against `main` at commit `bc1cdb7`. Numbers (line counts, line
references) drift as the code changes — treat them as "look near here", not as
addresses. Everything structural below is true unless the file has been
restructured.

---

## Contents

1. [The thirty-second version](#1-the-thirty-second-version)
2. [Running it](#2-running-it)
3. [Repository shape](#3-repository-shape)
4. [How a request flows](#4-how-a-request-flows)
5. [The data model](#5-the-data-model)
6. [The routing layer](#6-the-routing-layer)
7. [The service layer](#7-the-service-layer)
8. [Money: the whole payment path](#8-money-the-whole-payment-path)
9. [What a buyer gets and when](#9-what-a-buyer-gets-and-when)
10. [Dates, clocks and timezones](#10-dates-clocks-and-timezones)
11. [Templates and the frontend](#11-templates-and-the-frontend)
12. [Email](#12-email)
13. [Migrations](#13-migrations)
14. [Testing](#14-testing)
15. [Config, secrets and deployment](#15-config-secrets-and-deployment)
16. [House style](#16-house-style)
17. [Traps — the list to read before editing](#17-traps--the-list-to-read-before-editing)

---

## 1. The thirty-second version

A Flask monolith that sells digital courses and guides, runs a membership, hosts
two forums, holds live peer-support video sessions, and lets the owner build
marketing pages block by block. One owner ("the owner", "she" throughout the
comments), a few hundred members. Stripe is the merchant of record. Postgres in
production, SQLite in development. Deployed on Render as a single web service.

No React, no API layer, no microservices. Server-rendered Jinja, one CSS file,
a handful of vanilla JS files wired to the templates through `data-` attributes.

Rough size: ~53,500 lines of Python, of which ~13,200 is the test script and
~40,000 is the app; ~11,800 lines of CSS; ~7,700 lines of JS; 98 templates;
92 migrations.

---

## 2. Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FLASK_APP=app:create_app
flask db upgrade          # builds the SQLite dev DB
python seed.py            # content only — quotes, FAQ, legal stubs, forums
flask run                 # http://localhost:5000
```

Then open `/setup` and claim the owner account in the browser. That page locks
itself permanently once the owner has signed in once; after that everything is
managed from `/admin`.

No secrets are needed for local development. Email with no transport configured
prints to the terminal, which is how you read signup verification codes in dev.

`.cursor/install.sh` does all of the above and additionally `pip install ruff`
— **ruff is the linter this codebase is checked with, but it is not in
`requirements.txt` and there is no ruff config file**, so it runs on defaults.
There is no CI, no pre-commit, no pytest. The smoke script is the only gate.

Run the test suite with `python scripts/smoke_test.py`. It takes about 30
seconds and currently ends with `All 1986 checks passed.`

---

## 3. Repository shape

```
/workspace
├── app/
│   ├── __init__.py          app factory — 432 lines, read this first
│   ├── config.py            config classes read from env — 360 lines
│   ├── extensions.py        the five Flask extension singletons — 13 lines
│   ├── models.py            EVERY model, all 50 of them — 3,107 lines
│   ├── main/routes.py       public site — 3,338 lines
│   ├── admin/routes.py      Studio — 4,739 lines
│   ├── auth/routes.py       login/register/reset — 458 lines
│   ├── forums/routes.py     community — 561 lines
│   ├── webhooks/routes.py   Stripe receiver — 231 lines
│   ├── services/            60 modules, ~22,600 lines — the business logic
│   ├── templates/           98 Jinja templates
│   └── static/
│       ├── css/main.css     11,831 lines, the only stylesheet
│       └── js/              15 files
├── migrations/versions/     92 Alembic revisions, hand-written ids
├── scripts/smoke_test.py    13,170 lines, the entire test suite
├── seed.py                  idempotent content seed, never credentials
├── data/quotes_seed.json    150 daily quotes
├── render.yaml              the whole production deployment
├── requirements.txt
├── .env.example             annotated list of every env var
└── README.md                partly stale — see the warning below
```

### A warning about `README.md`

The README is long, well written, and **substantially out of date in places**.
It predates the move from Lemon Squeezy to Stripe and predates a reader-route
rename. Specific falsehoods currently in it:

- It describes `LEMONSQUEEZY_WEBHOOK_SECRET` / `LEMONSQUEEZY_API_KEY` as live
  env vars. `app/config.py` does not read them at all. Stripe replaced them.
- Section 4g describes memberships as sold through Lemon Squeezy checkout URLs
  and variant ids. They are Stripe prices now.
- Section 4c says buyers read courses at `/library/<slug>` gated by
  `_owns_product`. **There is no `/library` route.** The reader is
  `/account/courses/<purchase_id>` and the gate is a `ShopPurchase` lookup.
- The CSP described in section 5 lists Lemon Squeezy; the real CSP in
  `app/__init__.py` lines 19–38 lists Stripe and Daily.co.

Treat the README as background on *intent* and this document plus the code as
truth on *fact*.

---

## 4. How a request flows

`app/__init__.py` is the whole wiring diagram and it is worth reading top to
bottom once. In order:

1. **`create_app()` (line 88)** loads a config class chosen by
   `config.get_config()`, then re-reads a few secrets straight from `os.environ`
   (lines 92–106) because Render dashboard edits need to beat stale class
   attributes.
2. **Upload directories are resolved** (lines 108–160). Four of them:
   `VIDEO_STORAGE_DIR`, `REEL_RAW_DIR`, `SHOP_FILES_DIR`, `COURSE_FILES_DIR`.
   Each falls back to a path under `instance/` when unset. The fallback chain
   for videos is non-obvious: if `VIDEO_STORAGE_DIR` is empty but
   `COURSE_FILES_DIR` is set, videos go to a `videos` folder beside the course
   files, on the assumption that one attached disk is how this is run.
3. **Extensions init** (lines 162–172), then `_ensure_secret_key`,
   `_ensure_brand` and `_heal_stale_tiers` — three boot-time database chores
   that each swallow their own exceptions so an unmigrated database still lets
   `flask db upgrade` run.
4. **Blueprints register** (lines 183–195). The webhook blueprint is
   CSRF-exempt at line 195; nothing else is.
5. **`@app.before_request _timed_reminders` (line 200)** fires three
   opportunistic housekeeping sweeps on most requests — support-group reminders,
   spotlight expiry, the weekly reel clear-out. Each is wrapped in a bare
   `except: pass`. They self-throttle internally.
6. **The view runs.**
7. **`@app.after_request track_and_harden` (line 367)** sets four security
   headers including the CSP, then records a page view with a dialect-specific
   upsert (Postgres vs SQLite `on_conflict_do_update`), then records traffic
   attribution. All of it inside a try/except that rolls back.

Also at app level, outside any blueprint:

- **`/healthz`** (line 330) — Render's health check, does `SELECT 1`.
- **`/cron/support-groups`** and `/cron/support-groups/remind` (line 336) —
  gated by a `CRON_SECRET` shared secret in either an `Authorization: Bearer`
  header or a `?key=` parameter. **Returns 404, not 403, when the secret is
  missing or wrong.** Runs reminders, spotlight notices, Stripe cancel-flag
  sync and the reel sweep.
- **Error handlers** for 404, 500, 429 and 413 (lines 407–430). The 413 handler
  is unusual: it redirects back to the referring form with a flash rather than
  showing an error page, as long as the referrer is same-origin.

### Jinja globals and filters

Registered in `create_app` at lines 222–280. An agent will reach for these
constantly, and templates break silently if you forget they exist:

| Name | Kind | Source | Purpose |
|---|---|---|---|
| `markdown` | filter | `services/markdown.py` | bleach-sanitised Markdown |
| `nl2br` | filter | inline, line 230 | escape then newline→`<br>` |
| `localtime` | filter | `timefmt.format_local` | format a datetime in the viewer's zone |
| `when` | filter | `timefmt.local_tag` | same, but wrapped so JS can correct it |
| `mentions` | filter | `social_graph.linkify_mentions` | @handles → links |
| `lpline` / `lprich` / `lpplain` | filters | `landing_pages` | landing-page text sanitisers |
| `countdown` | global | `timefmt.countdown_tag` | live countdown chip |
| `lp_video_embed`, `lp_block_style`, `lp_block_ink`, `lp_grid_style` | globals | `landing_pages` | landing block rendering |
| `primary_badge`, `profile_badges` | globals | `services/badges.py` | badge art |
| `session_title` | global | `support_groups.meeting_display_title` | what a session is called |
| `week_span` | global | `reel_reviews.week_range_label` | a week written as its days |

The **context processor at line 282** injects `site` (all settings), plus
`announcements`, `viewer_tz`, `viewer_tz_settled`, `server_now_ms`,
`current_year`, `unread_notes`, `nav_notifications`, `owner_preview` and
`turnstile_site_key` into every template. If a template references `site.foo`
and you cannot find where it came from, it is `services/settings.all_settings()`.

---

## 5. The data model

Everything lives in **one file**, `app/models.py`, 3,107 lines, 50 model
classes. There is no `models/` package and no attempt to split it. Business
logic lives on the models to an unusual degree — `Product` alone has 87
methods.

### Module-level helpers worth knowing (top of the file)

- `utcnow()` (line 44) — **naive UTC datetime**. Every `DateTime` column in this
  app stores naive UTC. Nothing is timezone-aware in the database. This is the
  single most important convention in the codebase; see section 10.
- `blurred_shape()` (line 19) — generates filler text for paywalled previews.
- `PRODUCT_KINDS`, `PRODUCT_STATUSES`, `BILLING_PERIODS`, `STRIPE_INTERVALS`,
  `MEMBERSHIPS`, `MEMBERSHIP_RANK`, `higher_membership()` (lines 49–124).
- `DRIP_MODES`, `CONTENT_GROUPS` (lines 414–427).
- `JOURNAL_PROMPTS`, `MOODS` (lines 1908–2022).
- `LOOKING_FOR`, `MARKETPLACE_*`, `SUPPORT_*`, `COACH_SLUGS` further down,
  each sitting immediately above the models that use them.

### The models, in file order

| Line | Class | Table | A row is |
|---:|---|---|---|
| 127 | `User` | `users` | one account |
| 385 | `VerificationCode` | `verification_codes` | one hashed one-time email code |
| 447 | `Product` | `products` | one sellable catalogue item |
| 1478 | `ProductRound` | `product_rounds` | one dated run/cohort of a product |
| 1610 | `ProductAsset` | `product_assets` | one file or written extract in a course |
| 1709 | `CourseProgress` | `course_progress` | one reader's place in one purchase |
| 1759 | `MembershipPlan` | `membership_plans` | one sellable membership SKU |
| 1861 | `Quote` | `quotes` | one daily quote |
| 1877 | `QuotePin` | `quote_pins` | which quote is pinned to a date |
| 1887 | `QuoteFavorite` | `quote_favorites` | a user saved a quote |
| 1897 | `CheckIn` | `check_ins` | one "I showed up" day |
| 2025 | `JournalEntry` | `journal_entries` | one private journal page |
| 2050 | `Follow` | `follows` | one follower→following edge |
| 2061 | `Notification` | `notifications` | one bell item |
| 2093 | `Video` | `videos` | one Content Hub tip |
| 2188 | `Order` | `orders` | one payment |
| 2252 | `ShopPurchase` | `shop_purchases` | one entitlement |
| 2286 | `Subscriber` | `subscribers` | one newsletter email |
| 2294 | `ChallengeWaitlist` | `challenge_waitlist` | legacy, nothing writes it |
| 2307 | `Testimonial` | `testimonials` | one quote on a product page |
| 2318 | `FaqItem` | `faq_items` | one FAQ pair |
| 2327 | `Page` | `pages` | one Markdown CMS page |
| 2337 | `LandingPage` | `landing_pages` | one block-built page |
| 2380 | `Setting` | `settings` | one key→text site setting |
| 2387 | `PageView` | `page_views` | view count for a path on a date |
| 2397 | `VisitEvent` | `visit_events` | one attributed visit |
| 2415 | `ContactMessage` | `contact_messages` | one contact-form message |
| 2430 | `ForumCategory` | `forum_categories` | one forum |
| 2447 | `ForumTag` | `forum_tags` | one topic tag in a forum |
| 2480 | `ForumPost` | `forum_posts` | one thread |
| 2514 | `ForumComment` | `forum_comments` | one comment or reply |
| 2545 | `ForumPostLike` | `forum_post_likes` | one like |
| 2554 | `ForumCommentLike` | `forum_comment_likes` | one like |
| 2563 | `ForumImage` | `forum_images` | one image on a post or comment |
| 2589 | `Announcement` | `announcements` | one home banner |
| 2650 | `MarketplaceListing` | `marketplace_listings` | one member advert |
| 2689 | `ListingImage` | `listing_images` | one advert photo |
| 2701 | `ProductGalleryImage` | `product_gallery_images` | one product gallery JPEG |
| 2721 | `ReelReviewApplication` | `reel_review_applications` | one review request |
| 2755 | `ReelReview` | `reel_reviews` | one published review |
| 2786 | `ReelSubmission` | `reel_submissions` | one Reel-of-the-Week entry |
| 2823 | `SiteImage` | `site_images` | one named site image |
| 2851 | `SiteFeedback` | `site_feedback` | one feedback report |
| 2868 | `ContentReport` | `content_reports` | one moderation report |
| 2941 | `SupportGroupCircle` | `support_group_circles` | one peer-circle topic |
| 2967 | `SupportGroupMeeting` | `support_group_meetings` | one scheduled session |
| 3011 | `SupportGroupApplication` | `support_group_applications` | one seat |
| 3036 | `SupportGroupTopicAlert` | `support_group_topic_alerts` | one alert opt-in |
| 3062 | `CoachAvailability` | `coach_availability` | one weekly coach window |
| 3076 | `CoachingIntake` | `coaching_intakes` | one 1:1 booking |

### Pairs that look alike and are not

This is the section to read twice. Getting these wrong is the most likely way
to break something quietly.

**`Order` vs `ShopPurchase`.** The single most important distinction in the app.

- `Order` is the **money ledger**. One row per payment. Carries `total_cents`,
  a status of `paid`/`ended`/etc., the Stripe id, the buyer's email, an optional
  `membership_tier`, gift addresses. It has an FK to `products`.
- `ShopPurchase` is the **entitlement**. One row per thing somebody owns. It is
  what My Space lists, what the reader checks, what a membership perk hangs off,
  what a refund revokes. Statuses are `pending_link`/`linked`/`refunded`.
- **`ShopPurchase.product_id` is a `String(80)`, not a foreign key to
  `products.id`.** It holds a Stripe/legacy price identifier. To get from a
  purchase to a product you must call
  `course_reader.catalog_product_for_purchase(purchase)`, which also knows how
  to match *retired* price ids. Never `db.session.get(Product, purchase.product_id)`.
- A membership purchase creates an `Order` and **no** `ShopPurchase`.
- `CourseProgress` hangs off `ShopPurchase`, not `Order`.

**`User.membership` vs `MembershipPlan`.** `users.membership` is the tier a
person currently has. `MembershipPlan` is the catalogue of what can be sold.
Plans do not record who owns them.

**`Product` dates vs `ProductRound` dates.** Both expose the same schedule
methods (`drip_starts_at`, `drip_mode_key()`, `drip_days()`, `module_timing()`,
`off_shelf_at`, `perk_starts_at`, `perk_ends_at`, `perk_months()`). This duck
type is deliberate. `Product.schedule()` answers "whose calendar does a shopper
see" (the newest live round, else the product's own dates);
`Product.schedule_for(purchase)` answers the same for a buyer, using the round
stamped on their purchase. Content, price and copy always live on the Product;
only dates live on a round.

**`Page` vs `LandingPage`.** `Page` is Markdown at `/about`-style paths.
`LandingPage` is the block builder at `/p/<slug>`, with separate `draft_json`
and `published_json`.

**`Product.gallery_json` vs `ProductGalleryImage`.** The first is a JSON list of
URL strings. The second is actual image bytes in Postgres. Both are "the
gallery".

**`ReelReviewApplication` vs `ReelSubmission`.** An application asks the owner
to review your reel and carries a raw video file. A submission enters the
home-page Reel of the Week contest and is a URL and a share count, with **no**
video. Different tables, different weekly cycles.

**`ContactMessage` vs `SiteFeedback` vs `ContentReport`.** Public contact form,
feedback/complaint widget, forum moderation report. All three land in the Studio
inbox and all three are separate tables.

**`SupportGroupMeeting.zoom_url` / `zoom_meeting_id`** are legacy column names.
They hold a **Daily.co** room URL and room name. There is no Zoom anywhere.

**`Order.ls_order_id` and `ShopPurchase.lemon_squeezy_order_id`** are legacy
column names from the Lemon Squeezy era. They hold **Stripe** payment intent
ids. Both are the idempotency keys for fulfilment. Do not rename them casually;
do not assume they are dead because of the name.

### Column conventions

- **Money is always integer cents.** `price_cents`, `total_cents`,
  `promo_off_cents`, `compare_at_cents`. The one exception is
  `MarketplaceListing.price`, which is deliberately a free-text string because
  members write things like "from $40".
- **JSON lives in `db.Text` columns** with a getter/setter pair on the model.
  Never write the column directly. Examples: `User.links_json` with
  `links()`/`set_links()`, `Product.curriculum_json` with
  `curriculum()`/`set_curriculum()`, `Product.types_json` with
  `types()`/`set_types()`, `LandingPage.draft_json`.
- **Binary image/video data in `LargeBinary`, usually `db.deferred`.** Avatars,
  product covers, forum images, listing images, site images, gallery images and
  some legacy videos live in Postgres so they survive Render's disk. The
  deferral matters: a members list reads dozens of `User` rows and must not load
  the avatar bytes.
- **Big uploaded files live on disk**, referenced by a `disk_name` column, not
  in the database. Course assets, Content Hub videos, raw reels, review videos.
- Units that are not obvious: `drip_interval_days` (days),
  `perk_membership_months` (months), `trial_days` (days), `size` (bytes),
  `CoachAvailability.start_minute` (minutes from local midnight),
  `CoachAvailability.weekday` (0 = Monday).

### Relationship notes

Cascading deletes are declared where they matter: `Product` → assets, gallery
images and rounds; `ForumPost` → comments, likes and images; `ForumCategory` →
posts and tags; `ProductAsset` → child notes (also `ondelete="CASCADE"` at the
database level); `MarketplaceListing` → images; `ReelReviewApplication` → review.

**Many foreign keys have no `db.relationship` at all** — `CheckIn.user_id`,
`Follow.*`, `Notification.user_id`, `CourseProgress.user_id`,
`ShopPurchase.round_id`, the forum like tables, `ForumImage.user_id`. If you
need the related object you join or `db.session.get()` manually. Adding a
relationship is fine but check you are not creating a lazy-load storm on a list
page.

`User.deleted_at` is a soft delete, and the Flask-Login `user_loader` in
`app/__init__.py` line 177 returns `None` for soft-deleted users, so they cannot
log in but their forum posts survive.

---

## 6. The routing layer

Five blueprints. Registration is in `app/__init__.py` lines 183–195.

| Blueprint | Prefix | File | Routes |
|---|---|---|---|
| `main` | *(none — site root)* | `app/main/routes.py` | ~83 |
| `auth` | *(none — site root)* | `app/auth/routes.py` | 7 |
| `admin` | `/admin` | `app/admin/routes.py` | ~126 |
| `forums` | `/forums` | `app/forums/routes.py` | 12 |
| `webhooks` | `/webhooks` | `app/webhooks/routes.py` | 4 |

**`auth` has no prefix.** The URLs are `/login`, `/register`, `/setup`, not
`/auth/login`. Meanwhile the page-view tracker excludes paths starting with
`/auth`, which therefore excludes nothing. Minor, but it explains why auth pages
appear in the view counts.

### Jump map for `app/main/routes.py` (3,338 lines)

| Lines | Area |
|---|---|
| 61–141 | shared helpers (`_collect_profile_links`, `_spotlight_context`) |
| 143–155 | home |
| 172–333 | catalogue (`/courses`, `/courses/<slug>`) |
| 335–760 | checkout, gifts, membership purchase |
| 778–1055 | Marketplace / Showcase |
| 1058–1094 | about, quotes |
| 1097–1339 | account hub, library, sessions, downloads |
| 1377–1687 | **course reader** and all media-serving routes |
| 1690–2065 | settings, membership cancel/resume, profile |
| 2071–2463 | Content Hub, watch, reel uploads, video streaming |
| 2466–2625 | password, account deletion, feedback, subscribe, FAQ, contact |
| 2628–3263 | support groups, coaching, live session rooms |
| 3266–3338 | public landing pages (`/p/<slug>`) and legal pages |

### Jump map for `app/admin/routes.py` (4,739 lines)

| Lines | Area |
|---|---|
| 55–119 | the guards and shared helpers — **read these before adding a route** |
| 122–368 | preview-as-member, dashboard, payment recovery actions |
| **370–1022** | **product form helpers** — no routes, all parsing |
| 1024–1290 | products list and CRUD, Stripe sync |
| 1291–1421 | product rounds |
| 1424–1713 | assets, chunked uploads, covers, gallery, deletion |
| 1715–2089 | quotes, testimonials, FAQ, CMS pages, legacy redirects |
| 2092–2488 | spotlight, settings, announcements |
| 2490–2709 | marketplace moderation, Content Hub videos |
| 2711–3444 | members, co-owners, membership plans |
| 3446–3786 | badges, inbox |
| 3788–4117 | community moderation |
| 4119–4342 | reel reviews |
| 4344–4521 | landing page builder |
| 4524–4739 | support groups |

### The two admin guards

Both at the top of `app/admin/routes.py`. Together they are the entire Studio
access model.

**`admin_required` (lines 80–103)** is a decorator and **must be applied by hand
to every new admin view**. There is no blueprint-wide login gate. It does two
things: anyone who is not an authenticated admin gets `abort(404)` — not 403,
deliberately, so Studio's existence is hidden — and a login older than
`ADMIN_IDLE_DAYS` is bounced back to the login page.

**`_studio_readonly_guard` (lines 55–77)** is a `@bp.before_request` and
therefore applies to **every** admin route automatically, including any you add.
It allows all GET/HEAD/OPTIONS, and for an owner with `admin_readonly` set it
blocks every POST/PUT/PATCH/DELETE with a flash and a redirect. The one
allowlisted exception is `admin.preview`.

The practical consequences:

- A new admin route needs `@admin_required` or it is wide open.
- A new *mutating* admin route is automatically correct for read-only owners.
  You do not need to do anything.
- **A GET route that mutates state would bypass the read-only guard entirely.**
  The house rule is that mutations are POSTs. Keep it.

### Shared form helpers in `app/admin/routes.py`

Almost every admin route parses `request.form` inline. The product form is the
exception, and it is big:

- **`_apply_product_fields` (lines 492–798)** maps the entire product form onto
  a `Product`. Called only by `product_new` and `product_edit`. **If you add a
  field to `admin/product_form.html`, this is where it gets read.**
- `_parse_price_cents` (384–391) — dollars string → integer cents, or `None`.
  Rejects currency signs on purpose, so `$49` is a validation failure.
- `_parse_accent` (372–381), `_row_has_work` (403–411), `_lesson_numbers`
  (414–433), `_move_module_content` (436–478) — curriculum editing.
- `_save_module_files` (892–960), `_save_asset_notes` (962–999),
  `_save_asset_lessons` (1001–1022) — the upload side of the same form.
- `_sync_stripe_catalog` (811–832), `_settle_publish` (834–845),
  `_announce_product` (862–874) — what happens after a save.
- `_form_ids` (106–119) — parses bulk-select checkboxes, used by every
  `*_bulk_*` route.
- `_csv_response` (2713–2728) — the members CSV export.

### The forum gate

`app/forums/routes.py` has a `@bp.before_request` called
`_require_community_member` (lines 30–44). Non-members get `forums/gate.html`
rendered in place of **any** GET, and a redirect on POST. Inside, further gates
layer on: `_can_access_category` (healing vs building tiers),
`_require_participant` (not banned), `_guard_content` (profanity).

### Rate limiting

Flask-Limiter with **in-memory storage**, so every counter resets on deploy.
Decorators are per-route. Concentrations: all of `auth` (login 20/hr plus 5/min
per email, register 10/hr, reset 3/hr per email), forum writes (post 15/hr,
comment 30/hr), checkout endpoints (20/min), contact (3/hr), feedback (8/hr).
**No admin route is rate-limited** and no webhook route is.

### Non-obvious routing

- `/showcase` and `/marketplace` are two endpoints on one handler.
- Path catch-alls exist for H5P content
  (`/account/courses/<id>/h5p/<asset_id>/<path:filename>`) and product gallery
  images.
- File-serving routes use `send_file(conditional=True)` for HTTP Range, so
  lesson videos can be scrubbed. There is a hand-written `_range_response`
  helper in `main/routes.py` around line 2347 for the video stream.
- `/admin/subscribers` and `/admin/orders` are legacy stubs that flash and
  redirect to the dashboard.
- `/p/<slug>` 404s for an unpublished landing page; the owner sees the draft at
  `/p/<slug>?preview=1`.

---

## 7. The service layer

`app/services/` holds 60 modules. The rule, loosely followed, is that routes
parse the request and render, and services do the work. The big exception is
`app/models.py`, which holds a great deal of logic on `Product` and `User`.

Imports are relative: `from .mailer import ...` inside a service,
`from ..services import X` from a route.

### Full inventory

Grouped by what they concern, with line counts.

**Payments and commerce**
| Module | Lines | Concern |
|---|---:|---|
| `stripe_pay.py` | 3,231 | checkout sessions, webhook verification, fulfilment, subscription lifecycle |
| `stripe_catalog.py` | 505 | pushing Studio products/prices/images *to* Stripe, and putting launch prices back up when their day comes |
| `shop_purchases.py` | 507 | the entitlement shelf |
| `memberships.py` | 480 | deciding a user's tier |
| `membership_audit.py` | 186 | explaining a tier against Stripe |
| `plan_features.py` | 326 | the per-tier feature matrix |
| `perks.py` | 267 | free membership months earned by buying a product |
| `gifts.py` | 359 | buying for somebody else |
| `bundles.py` | 167 | a product that contains other products |
| `founder_pricing.py` | 150 | launch-window discount maths |
| `catalog.py` | 157 | slugs, the challenge product, demo cleanup |
| `rounds.py` | 197 | Studio CRUD for `ProductRound` |

**Course content and delivery**
| Module | Lines | Concern |
|---|---:|---|
| `assets.py` | 808 | file storage and chunked upload for course files |
| `course_reader.py` | 273 | purchase→product matching, progress, H5P extraction |
| `drip.py` | 204 | which modules are unlocked for this buyer today |
| `docs.py` | 652 | DOCX → PDF |
| `slides.py` | 590 | PPTX → PDF |
| `videos.py` | 131 | video validation and 16:9 thumbnails |
| `product_covers.py` | 254 | product cover and gallery bytes in Postgres |

**Community**
| Module | Lines | Concern |
|---|---:|---|
| `social_graph.py` | 400 | usernames, follows, notifications, the `@all` room mention |
| `forum_access.py` | 97 | who can read which room — asked about people other than the viewer |
| `social.py` | 181 | Instagram handles, embeds, previews |
| `moderation.py` | 69 | profanity blocklist and warning tally |
| `forum_moderation.py` | 72 | hard-deleting posts from Studio |
| `forum_quotas.py` | 95 | free-tier weekly post/reply caps |
| `content_reports.py` | 272 | report → auto-hide pipeline |
| `community_images.py` | 80 | forum image attachments |
| `listings.py` | 79 | marketplace images and tier caps |
| `participation.py` | 26 | post+comment counts for My Space |
| `badges.py` | 439 | the achievement ladders |
| `spotlight.py` | 532 | Creator of the Month and featured reel |

**Reels** — four modules, easily confused:
| Module | Lines | Concern |
|---|---:|---|
| `reel_reviews.py` | 182 | the weekly queue of review *requests* |
| `reel_of_week.py` | 208 | the home-page contest |
| `reel_uploads.py` | 287 | members' raw video files (swept weekly) |
| `review_uploads.py` | 218 | the owner's review video files (kept) |

**Live sessions**
| Module | Lines | Concern |
|---|---:|---|
| `support_groups.py` | 1,986 | circles, sessions, seats, reminders |
| `coaching_intake.py` | 878 | 1:1 questionnaire and slot booking |
| `daily.py` | 306 | thin Daily.co API client |

**Pages and content**
| Module | Lines | Concern |
|---|---:|---|
| `landing_pages.py` | 1,190 | the block builder: schema, sanitising, CRUD |
| `landing_templates.py` | 354 | starter block lists for a new page |
| `markdown.py` | 71 | Markdown → sanitised HTML |
| `site_images.py` | 176 | named site images |
| `homepage.py` | 149 | Content Hub drops, product of the day |
| `quotes.py` | 85 | daily quote rotation |
| `legal_copy.py` | 163 | canonical Terms/Privacy/Refunds text |
| `journey.py` | 227 | the My Journey keepsake PDF |

**Infrastructure and cross-cutting**
| Module | Lines | Concern |
|---|---:|---|
| `mailer.py` | 1,425 | every email the app sends |
| `settings.py` | 391 | the `settings` key/value table |
| `timefmt.py` | 389 | timezone normalisation and formatting |
| `stats.py` | 674 | Studio dashboard metrics |
| `privacy.py` | 340 | account closure and scrubbing |
| `owners.py` | 336 | co-owner invite/promote/remove |
| `preview.py` | 163 | Studio "view as a member" |
| `demo_accounts.py` | 89 | stand-in accounts that cannot receive email |
| `avatars.py` | 131 | avatar re-encoding |
| `captcha.py` | 110 | Cloudflare Turnstile |
| `attribution.py` | 159 | UTM/referrer tracking |
| `background.py` | 57 | fire-and-forget daemon threads |
| `storage_health.py` | 152 | detecting a non-persistent disk |
| `recommend.py` | 39 | signup intent keys |

### Dependency shape

**Imported by almost everything:** `settings`, `social_graph`, `mailer`,
`timefmt`, `memberships`, `stripe_pay`, `shop_purchases`.

**Import almost everything:** `stripe_pay`, `support_groups`, `shop_purchases`,
`gifts`, `privacy`.

**Leaves, safe to change in isolation:** `markdown`, `captcha`, `avatars`,
`participation`, `forum_quotas`, `legal_copy`, `landing_templates`,
`recommend`, `background`.

### Name pairs that catch people out

| These two | Are actually |
|---|---|
| `stripe_pay` / `stripe_catalog` | money in / products out |
| `memberships` / `membership_audit` / `plan_features` / `perks` | the tier / a report about the tier / the feature matrix / free months from products |
| `social` / `social_graph` | Instagram / usernames and follows |
| `moderation` / `forum_moderation` / `content_reports` | profanity / hard delete / the report pipeline |
| `landing_pages` / `landing_templates` | the builder / the starter JSON |
| `drip` / `rounds` | unlock maths for a buyer / Studio CRUD for date sets |
| `site_images` / `product_covers` / `community_images` / `avatars` | four different tables, all image bytes in Postgres |
| `daily` / `support_groups` | the API client / the business logic |

---

## 8. Money: the whole payment path

`app/services/stripe_pay.py`, 3,231 lines. The densest file in the app. Its
internal layout:

| Lines | Concern |
|---|---|
| 20–33 | `StripeError`, `configured()` |
| 57–153 | `_with_session_id`, `create_checkout_session` |
| 194–248 | signature verification, `construct_event` |
| 251–430 | price/product resolution, `_session_should_fulfill` |
| 433–775 | event enrichment, `stripe_event_to_internal` |
| 778–973 | fulfil-by-session-id, Studio sync, `maybe_sweep_cancel_flags` |
| 975–1101 | welcome claiming, `upsert_order_from_payment` |
| **1385–1750** | **`handle_payment_event` — the heart of fulfilment** |
| 1753–2390 | membership cancel / replace / resume |
| 2390–2937 | addon prices, live tier from Stripe, cancel-at state |
| 2939–3005 | `sweep_cancel_flags` |
| 3008–3231 | subscription deleted, disputes, disputes, customer updated |

### Buying something, start to finish

1. A member hits `/checkout/product/<slug>` in `main/routes.py` (~line 335).
2. That calls `stripe_pay.create_checkout_session` with a Stripe **price id**
   and metadata. Mode is `subscription` when the metadata says `kind=membership`
   or the price recurs, otherwise `payment`.
3. The success URL carries `session_id={CHECKOUT_SESSION_ID}`, so the browser
   coming back can trigger fulfilment if the webhook is slow or lost.
4. Stripe charges the card and POSTs to `/webhooks/stripe`.
5. `webhooks/routes.py` verifies the HMAC signature (constant-time), then
   `stripe_event_to_internal` normalises the Stripe event into one of
   `payment.succeeded` / `payment.failed` / `payment.refunded`.
6. `handle_payment_event` upserts an `Order`, then branches three ways.

### The three branches

| Branch | Detected by | What happens |
|---|---|---|
| **Membership** | the price matches a `MembershipPlan`, or metadata names a tier | stamps `order.membership_tier`, calls `replace_other_memberships`, sends a welcome. **No `ShopPurchase`, no receipt.** |
| **Product** | the price matches a catalogue `Product` and is not an addon | `upsert_shop_purchase` creates the shelf row, sends a receipt, maybe a challenge welcome, maybe gift notices |
| **Addon** | `is_addon_checkout` — facilitator seat or 1:1 metadata | `Order` only, then `coaching_intake.fulfill_from_payment_metadata`. **Skips the shelf.** |

### Idempotency — four separate mechanisms

Stripe announces one charge up to three times (`checkout.session.completed`,
`payment_intent.succeeded`, `charge.succeeded`), and the browser-return path can
fire a fourth. Everything downstream must be safe to run repeatedly.

1. **Orders** key on `ls_order_id` (the payment intent id) in
   `upsert_order_from_payment`, around line 1044.
2. **Shelf rows** key on `lemon_squeezy_order_id` in `upsert_shop_purchase`.
3. **Receipts** are only sent when the prior order status was not already
   `paid`, around line 1443.
4. **Membership welcomes** use `_claim_membership_welcome` (lines 975–1028),
   which stamps `welcome_sent_at` once per email+tier — necessary because the
   checkout event and the invoice event carry *different* payment ids for the
   same subscription.

**If you add anything to fulfilment, give it a claim of its own.** Gifts do
this with `orders.gift_told_at`. This is the single easiest way to send a
customer three copies of an email.

### Other things in here

- **`configured()`** is `True` if and only if `STRIPE_SECRET_KEY` is non-empty.
  It does **not** check the webhook secret, which fails separately at signature
  verification.
- **`fulfill_checkout_session_id`** (line 778) is the browser-return backstop.
- **`sync_recent_payments`** (line 819) is the Studio dashboard's "find payments
  we missed" button.
- **`sweep_cancel_flags`** (lines 2939–3005) lists live Stripe subscriptions and
  reconciles `User.membership_cancel_at` with Stripe's cancel-at-period-end,
  clearing stale local flags. Throttled hourly, run from the dashboard and cron.

### A launch price puts itself back up

`Product.price_reverts_at` plus `Product.reverts_to_cents` is a launch price
with a day it ends. Both are needed: a date on its own is only a countdown on
the product page, because guessing what she meant the price to become is not
something to do with somebody's money.

`stripe_catalog.apply_due_reversions()` is what makes it happen, and the order
inside `apply_reversion` is the whole of the care needed:

1. set `price_cents` to the figure she named,
2. `sync_product`, which makes the **new Stripe price** and archives the old,
3. only then clear `price_reverts_at` / `reverts_to_cents`.

If Stripe refuses, the price is put back and the date is left alone, so the
launch runs a little long and the next sweep tries again — the harmless
direction. `stripe_sync_error` carries the reason to Studio.

It runs from two places: `maybe_apply_reversions()` in the `before_request`
hook, and `apply_due_reversions()` from `/cron/support-groups` for a site
quiet enough to get no requests. The throttle is not a fixed gap — it wakes
from `earliest_pending()`, the soonest date actually waiting, capped at 60
seconds. A plain every-sixty-seconds throttle gets spent by a request a
moment *before* the date, and then the page advertises a price for the rest
of the minute that the row has not moved to.

Each product is re-read `with_for_update()` and re-checked before its price is
touched, so two workers hitting the same due product don't both make a price.
The strikethrough is never touched by any of this — see the next section but
one; it is hers to set and hers to clear.

### Promo codes are Stripe coupons

Worth stating plainly because it is not obvious from the model: **nothing in
this codebase computes a discount.** `allow_promotion_codes: True` is set on the
Stripe session and the buyer types the code at Stripe's checkout. The
`promo_code` and `promo_off_cents` fields on `Product` are **advertising copy
only** — they render "Save $10 with LAUNCH10" on the product page and nothing
more. This is why the field stores the amount *off* rather than the final price:
a coupon takes a fixed amount off whatever the price is that day, so storing the
finished price would silently drift if the price changed.

---

## 9. What a buyer gets and when

Six services form a chain. This is the part of the app with the most
cross-module coupling, so here it is explicitly.

```
stripe_pay.handle_payment_event
   │
   ├── membership ──► memberships.apply_from_order
   │                      └─► memberships.reconcile_user
   │
   └── product ──► shop_purchases.upsert_shop_purchase
                        ├─► stamp_round()          (which cohort's dates)
                        ├─► sync_membership_perk() ─► perks ─► memberships
                        └─► bundles.grant_contents()  (if it is a bundle)

reading it later (main/routes.py course reader)
   shop_purchases.library_purchases_for
      └─► course_reader.catalog_product_for_purchase   purchase → Product
      └─► course_reader.dates_for                      which calendar applies
             └─► drip.module_rows / drip.asset_unlocked
                    └─► assets.read_bytes  (disk or DB)
```

### `memberships.reconcile_user` — the tier precedence

`app/services/memberships.py` around line 287. When several things could grant a
tier, this is the order:

1. a **manual** tier set by the owner in Studio — always wins
2. a **live Stripe subscription**
3. **purchased membership orders** still in force
4. a **product perk** (free months earned by buying a course)

Anything that changes a user's tier should go through this function rather than
writing `user.membership` directly.

### `drip` — unlock maths

`app/services/drip.py`, 204 lines, no database writes. Given a purchase time and
a schedule object, it computes which modules are open. The schedule object is
either a `Product` or a `ProductRound` — they duck-type each other deliberately
(see section 5). `reads_it_whole()` is the owner/admin bypass.

### `course_reader.catalog_product_for_purchase`

The only correct way to get from a `ShopPurchase` to a `Product`. It handles the
retired-price-id case: when the owner changes a price in Studio, the old Stripe
price id moves into `Product.retired_price_ids_json`, and purchases made at the
old price must still resolve.

---

## 10. Dates, clocks and timezones

Pervasive, easy to get wrong, and the source of a disproportionate number of
past bugs.

### The three rules

1. **The database stores naive UTC.** `models.utcnow()` is the default for every
   timestamp column. Never store an aware datetime, never store local time.
2. **The owner types local time.** Studio forms take a date input and a time
   input and convert with `timefmt.parse_owner_parts(date_str, time_str, tz)`.
3. **The reader sees their own clock.** Display goes through `timefmt`.

### `app/services/timefmt.py`

| Function | Line | Purpose |
|---|---:|---|
| `normalize_timezone` | 69 | validate an IANA name |
| `account_timezone` / `has_account_timezone` | 84–95 | the signed-in user's zone |
| `viewer_timezone` | 102 | cookie → account → UTC |
| `to_local` / `format_local` | 115–124 | convert and format |
| `local_tag` | 132 | the same, wrapped in markup JS can correct |
| `time_left_words` / `countdown_tag` | 159–192 | the live countdown chip |
| `timezone_groups` / `timezone_label` | 305–339 | the settings picker |
| `parse_owner_parts` | 347–364 | owner-entered local → naive UTC |
| `local_day_bounds` | 374 | a day's window in a zone |

### Which to use in a template

- `{{ dt|localtime('%b %d, %Y') }}` — plain formatted text. **Safe inside an
  HTML attribute.** Needs a real `datetime`.
- `{{ dt|when('%b %d · %I:%M %p') }}` — emits a `<time data-when=...>` element
  that `localtime.js` rewrites client-side if the browser's zone differs from
  the one the server rendered in. **Not safe inside an attribute** — it is
  markup.
- `{{ countdown(dt, zero='Closed', refresh=true) }}` — a live ticking chip.

The server sets `<html data-tz>`, `data-tz-settled` and `data-now-ms`.
`data-tz-settled` means the user's account names a zone, in which case
`localtime.js` leaves the page alone. Only an unknown visitor gets corrected
client-side.

---

## 11. Templates and the frontend

### Template inheritance

Two base templates, nothing else extends anything else:

- **`templates/base.html`** — the public site. Blocks: `title`,
  `meta_description`, `og`, `head`, `content`, `scripts`.
- **`templates/admin/base_admin.html`** — Studio. Blocks: `title`, `content`,
  `scripts_pre`, `scripts`.

Everything in `auth/`, `errors/`, `forums/`, `main/` and `marketplace/` extends
`base.html`. Everything in `admin/` except the base and the `_`-prefixed
partials extends `base_admin.html`.

`templates/partials/` holds 17 files of two kinds: **includes** (`page_loader`,
`badge_defs`, `preview_bar`, `logo`, `note_bell`, `feedback_widget`, `captcha`,
`plan_preview`, `tip_card`, `review_card`) and **macro libraries** (`avatar`,
`badges`, `bloom_cover`, `gift_link`, `report_toggle`, `landing_page`,
`landing_blocks`). The macro ones are imported, not included.

`partials/landing_blocks.html` deserves special mention: it is the **only** code
that turns a landing-page block into markup, and both the public page and the
Studio builder call it. The builder passes `editing=True`. This is why the
preview matches the published page exactly — there is no second renderer.

### CSS — `app/static/css/main.css`, 11,831 lines

One file. No second stylesheet, no build step, no preprocessor. Adding one would
mean editing both base templates and the CSP.

**Theme variables are in `:root`, lines 6–30** — the palette (`--plum`,
`--rose`, `--gold`, `--ink`, `--dawn-ivory`), two font stacks, radii, shadow,
and `--container: 1140px`.

**Class naming is BEM-ish with a two-or-three-letter domain prefix.** `pd-` is
product detail, `sg-` support groups, `mem-` membership, `hub-` Content Hub,
`lp-` landing pages, `lp-ed-` the landing builder, `cg-` the catalogue, `ms-` My
Space, `comm-`/`fc-` community, `admin-`/`studio-` Studio, `ch2-` the challenge
page. State classes are `is-active`, `is-open`. So `.pd-hero__price` is "the
price element of the hero block on the product detail page".

**Where to add styles.** The file has grown by accretion and several areas have
an early section *and* a later "remaster" section. Prefer the later one.

| Lines | Section |
|---:|---|
| 1–31 | banner and `:root` variables |
| 32–185 | reset, typography, layout, buttons |
| 186–454 | forms, flashes, preview bar |
| 455–693 | hero announcement, nav, quote accent, filter tabs |
| 694–722 | product detail — **early/legacy, do not add here** |
| 723–804 | FAQ, testimonials, quotes, footer, utilities |
| **805–994** | **Studio shell** |
| 1032–1412 | avatars, account header, toggles |
| **1413–1716** | **forums** |
| **1717–2483** | **library and course reader** |
| 2484–2654 | badges |
| 2655–3146 | memberships, videos, spotlight, plans, wordmark |
| **3147–3768** | **Content Hub** |
| **3786–4172** | **Marketplace / Showcase** |
| 4173–4288 | feedback and reports |
| **4289–4732** | **home page** |
| **4733–5242** | **community and forum category** |
| 5243–6226 | remaster wrappers and My Space polish |
| **6227–6343** | **Studio remaster** |
| 6344–7154 | Showcase and Membership layout remaster |
| **7155–8047** | **support groups** |
| 8048–8828 | exact-mock layouts |
| **8829–9333** | **Courses & Guides catalogue** |
| 9334–9648 | `@media (max-width: 899px)` — handheld and tablet |
| 9649–9680 | the live countdown chip |
| **9681–10005** | **product detail page — current, add `pd-` styles here** |
| **10006–10812** | **Studio product editor** |
| **10813–11279** | **landing pages, public** |
| **11280–11831** | **landing builder chrome** |

### JavaScript

15 files, all vanilla, no bundler, no framework.

| File | Lines | Loaded by | Does |
|---|---:|---|---|
| `page-loader.js` | 235 | both bases | the sunflower loading overlay, scroll restore |
| `localtime.js` | 153 | both bases | timezone sync and `<time>` rewriting |
| `countdown.js` | 94 | both bases | ticks `[data-countdown]` |
| `file-pick.js` | 80 | both bases | brands native file inputs |
| `responsive-table.js` | 61 | both bases | stacks tables on narrow screens |
| `main.js` | 1,693 | `base.html` | public site: nav, confirms, mentions, hub, booking |
| `community-images.js` | 218 | `base.html` | forum photo picker and lightbox |
| `admin.js` | 1,896 | `base_admin.html` | Studio: module editor, chunked upload, bulk select, charts |
| `image-crop.js` | 314 | `base_admin.html` | the site-image crop dialog |
| `landing-editor.js` | 1,624 | `admin/landing_editor.html` | the block builder |
| `course-reader.js` | 717 | `main/course_reader.html` | PDF.js, H5P, progress |
| `gift.js` | 209 | `main/gift.html` | recipient autocomplete |
| `membership-billing.js` | 47 | `main/membership.html` | monthly/annual toggle |
| `support-room.js` | 129 | `main/support_session_room.html` | Daily.co join |
| `support-waiting.js` | 166 | `main/support_session_waiting.html` | waiting-room countdown |

### The Jinja ↔ JS contracts

These are the interfaces most likely to be broken by a careless template edit,
because nothing enforces them and nothing fails loudly.

**`countdown.js`** reads:
- `data-countdown` — an ISO UTC instant, `YYYY-MM-DDTHH:MM:SSZ`
- `data-countdown-zero` — text to show when it expires; without it the node is
  removed
- `data-countdown-refresh="1"` — reload the page ~4s after expiry
- `<html data-now-ms>` — the server's clock, used to correct device skew beyond
  ±10 minutes

Never hand-write these. Emit them with the `countdown()` Jinja global, which is
`timefmt.countdown_tag`.

**`localtime.js`** reads `<html data-tz>`, `<html data-tz-settled>`,
`<body data-tz-sync>` plus `data-csrf`, the `tz=` cookie,
`[data-tz-detected]`, `[data-tz-device-only]`, and rewrites
`time[datetime][data-when]` using a small strftime subset (`%b %d %Y %I %M %p`
and friends). Emit these with the `when` filter.

**`course-reader.js`** expects `.course-reader-page` carrying `data-asset-kind`,
`data-file-url`, `data-progress-url`, `data-bookmark-url`, `data-start-page`,
`data-start-percent`.

**`landing-editor.js`** expects a root `[data-lp-editor]` with `data-save-url`,
`data-render-url`, `data-preview-url`, `data-upload-url`; JSON seeds in
`[data-lp-blocks]`, `[data-lp-defs]`, `[data-lp-page]`; and per-block
`[data-slot]`/`data-slot-id`, `[data-f]`, `[data-item]`, `[data-img]`,
`[data-rich]`.

**`admin.js`** keys off `[data-clamp]`, `textarea[data-format]`,
`[data-fold-toggle]`, `[data-module-row]`, `[data-lesson-row]`,
`form[data-confirm]`, `[data-studio-modules]`, `[data-bulk]`, `[data-bulk-all]`,
and a `#dashboard-data` JSON script tag.

**`page-loader.js`** honours `[data-no-loader]` on a link or form, and skips the
spinner for `form[data-confirm]` until the confirm is accepted.

### The CSP constrains frontend work

`app/__init__.py` lines 19–38, applied at line 372.

- **`script-src` does not include `'unsafe-inline'`.** No inline `<script>`
  blocks, no `onclick=`/`onsubmit=`/`onchange=` attributes. The house pattern is
  external JS keyed off `data-` attributes. (A few legacy inline handlers
  survive in `preview_bar.html` and elsewhere; they are CSP-hostile leftovers,
  not a precedent.)
- `<script type="application/json">` is fine — it is data, not script.
- **`style-src` *does* include `'unsafe-inline'`**, so inline `style=` is
  allowed and is used freely.
- Allowed script origins: jsDelivr, Cloudflare Turnstile, Stripe.js, unpkg.
- Allowed frame origins: Instagram, YouTube-nocookie, Vimeo, Stripe, Daily.co,
  Turnstile. `frame-ancestors 'none'`.

### Images and fonts

There is **no image directory in `static/`**. Every image is either a pasted
external URL or served from a database-backed route:

| Route | Serves |
|---|---|
| `/media/site/<key>` | named site images, including landing uploads under `lp_` keys |
| `/media/product-cover/<id>` | product covers |
| `/media/product-gallery/<id>/<filename>` | product gallery |
| `/avatar/<user_id>` and `/avatar/<user_id>/anim` | avatars |
| `/marketplace/image/<id>` | listing photos |
| `/forums/img/<id>` | forum images, membership-gated |

Fonts are Google-hosted: Fraunces for display, Nunito Sans for body, Caveat on
the public base. Icons are inline SVG — no icon font, no sprite sheet. The only
static image is `favicon.svg`.

---

## 12. Email

`app/services/mailer.py`, 1,425 lines.

**Transport, in order (lines 309–375):** Brevo HTTP API if `BREVO_API_KEY` is
set; otherwise SMTP if `SMTP_HOST` is set; otherwise **print to the console and
return `True`**. That last fallback is why local development works with no
configuration — verification codes appear in the terminal.

Demo-account addresses are silently skipped (lines 324–331). Failures are
retained in `last_send_error()` rather than raised.

Most mail goes through a **Brevo transactional template id**, each configurable
by an env var and each with a documented default. `.env.example` lists every
template with its id and its parameters — that file is the authoritative
reference, better than anything here.

Roughly: #2 welcome, #3 signup code, #4 order receipt, #5/#6/#19 membership
welcomes, #7 card declined, #8 cancellation, #9 newsletter, #10 the general
fallback layout, #11–#18 support-group and 1:1 lifecycle, #20 customer support
reply, #30 challenge welcome. Gift templates default to `0`, which falls back to
#10.

Automated mail is from `MAIL_FROM`. Studio-composed mail uses the `SENDERS` map
(lines 74–84) and `send_customer_support_email`. Internal alerts
(`send_billing_alert`) go to `TEAM_EMAIL`, default `team@bloomanyway.online`.

---

## 13. Migrations

`migrations/versions/`, 92 files, Alembic via Flask-Migrate.

### The naming scheme

Early revisions used real Alembic hashes. **Everything recent is hand-written**
to an 11-character pattern:

- character 0 is always `z`
- character 1 steps through the alphabet: `za…`, `zb…`, … `zq…`, `zr…`
- characters 2–10 are a 9-character sliding window that advances by two each
  time — drop the first two characters, append a letter and a digit

The chain currently ends:

```
zp3w4x5y6z7 → zq4x5y6z7a8 → zr5y6z7a8b9   (head)
```

**Current head: `zr5y6z7a8b9`** (`zr5y6z7a8b9_promo_money_off.py`). Single head,
linear chain. One historical merge revision exists (`749cea858616`) from a
branch long ago — do not create another.

Following the scheme, the next id would be `zs6z7a8b9c0`. In practice the exact
suffix matters less than the `z` + next-letter prefix and the uniqueness; a
revision named `zs...` that chains correctly is fine.

Filenames are `{revision}_{snake_case_slug}.py`.

### Writing one

**Do not run `flask db migrate` and keep its output as-is.** Autogenerate works
and `env.py` even drops empty revisions (lines 86–91), but it emits a hash id
that breaks the scheme. If you use it to generate the operations, rewrite the
revision id and filename afterwards.

The house template:

```python
"""A sentence saying what changed and why.

Revision ID: zs6z7a8b9c0
Revises: zr5y6z7a8b9
Create Date: 2026-10-03

A paragraph or two of prose explaining the reasoning, what happens to
existing rows, and anything a reader six months from now would wonder.
"""
import sqlalchemy as sa
from alembic import op

revision = "zs6z7a8b9c0"
down_revision = "zr5y6z7a8b9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("new_thing", sa.Integer(), nullable=True))
    op.execute("UPDATE products SET new_thing = old_thing")
    with op.batch_alter_table("products") as batch:
        batch.drop_column("old_thing")


def downgrade():
    ...
```

### Hard rules

- **`op.batch_alter_table` for every column add/drop/alter.** 85 of the 92 files
  use it. SQLite cannot `ALTER TABLE ... DROP COLUMN` the normal way, and
  development plus the smoke suite both run on SQLite. Postgres handles batch
  mode fine.
- **Keep `op.execute` SQL portable** between SQLite and Postgres.
- **Use `server_default=` for new NOT NULL columns** so existing rows backfill
  on both engines.
- **Write a real `downgrade`.** The existing files do, including data migrations
  that have to make a judgement call when collapsing two columns into one.
- **Test it both ways before committing.** The procedure that works:

```bash
rm -f /tmp/mig_check.db
export SECRET_KEY=$(python -c "print('x'*32)")
export DATABASE_URL="sqlite:////tmp/mig_check.db"
export FLASK_APP="app:create_app"
python -m flask db upgrade zr5y6z7a8b9     # the revision before yours
# seed rows by hand covering every case your data migration branches on
python -m flask db upgrade                  # yours
# inspect
python -m flask db downgrade zr5y6z7a8b9
# inspect again
```

  When seeding `products` by hand, you must fill every NOT NULL column without a
  default: `title, slug, type, status, featured, sort_order, currency,
  created_at, updated_at`. `PRAGMA table_info(products)` will tell you if that
  list has changed.

### The models/migrations divergence risk

**The smoke suite builds its schema with `db.create_all()`, not with
migrations.** So a migration that does not match the model declarations will
pass every test and then fail in production. Changing `models.py` and the
migration must be done together and the migration must be exercised separately,
as above.

---

## 14. Testing

`scripts/smoke_test.py`, 13,170 lines. It is the only test suite. There is no
pytest, no `tests/` directory, no CI.

Run it with `python scripts/smoke_test.py`. It takes ~30 seconds and prints
`All 1986 checks passed.`

### How it works

It is **one sequential script**, read top to bottom, not a collection of
independent tests. Sections are marked with `# --- N. Something ---` comments
and share a single database that accumulates state as the file runs.

Setup, lines 24–162:
- Stripe env vars are set **before** the app is imported (lines 24–26).
- `TestConfig(DevConfig)` points at a temp SQLite file and sets
  `WTF_CSRF_ENABLED=False`, `RATELIMIT_ENABLED=False`, `TESTING=True`.
- `create_app(TestConfig)` then `db.create_all()`.
- Captcha is stubbed out entirely (lines 104–109).
- `auth_routes.send_verification_code` is replaced with a list appender so codes
  can be read back.

The assertion helper, lines 133–140:

```python
def ok(name, condition, detail=""):
    global PASS
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not condition else ""))
    if condition:
        PASS += 1
    else:
        raise SystemExit(f"FAILED: {name} {detail}")
```

**The first failure aborts the whole run.** Everything after it is unverified.

Clients: `client` is an ordinary visitor/member, `admin` is logged in as the
owner (created via `/setup` around line 295), and ad-hoc `app.test_client()`
instances are made wherever a fresh session is needed.

External services are all faked in-process:
- **Stripe webhooks** — payloads are built by `_payment_payload` and signed with
  the app's own `pay.sign_webhook`, so the real signature verification runs.
- **Stripe API** — fake `Product`/`Price`/`Session` classes are monkey-patched
  onto `pay.stripe.*` for the catalogue-sync tests.
- **Email** — `mailer.send_email` is swapped for a list appender and restored.
- **Daily.co** — `TESTING=True` with no API key auto-stubs.
- **Turnstile** — always passes.

### Rules you must follow when editing it

1. **It is sequential.** Inserting a test changes the state every later test
   sees. Put new checks in the section they belong to and re-run the whole file.
2. **Every identifier must be globally unique across the file.** Payment ids,
   emails, product slugs, Stripe price ids. A reused payment id makes
   `upsert_shop_purchase` take its idempotent "already exists" branch and your
   assertion gets `None` for no visible reason.
3. **The file starts with a UTF-8 BOM.** Preserve it.
4. **Do not blanket `str.replace` across the file.** Renumbering an id with a
   global replace will hit unrelated lines elsewhere.
5. **Assert against rendered strings carefully.** Jinja inserts newlines at
   source line breaks, so `"Reverting to $49 on" in body` can fail even when the
   page is correct. Either make the template emit the phrase contiguously or use
   `re.search(r"Reverting to\s+\$49\s+on", body)`.
6. **Never call a real external service.** Extend the existing stub patterns.
7. CSRF and rate limits are off, so form posts need no token.

### The other scripts

| Script | Lines | Purpose |
|---|---:|---|
| `validate_turnstile.py` | 74 | static checks on the Turnstile wiring, optional live verify |
| `badge_preview.py` | 69 | renders every badge SVG to `instance/badge_preview.html` |
| `journey_preview.py` | 67 | renders a sample My Journey PDF to `instance/` |

---

## 15. Config, secrets and deployment

### Choosing a config

`app/config.py` `get_config()` at line 347: `APP_ENV=production` → `ProdConfig`;
otherwise `RENDER` being set → production; otherwise `DevConfig`.
`ProdConfig.validate()` refuses to boot without `DATABASE_URL`, `MAIL_FROM` and
one working email transport.

### The database URL mangle

`_database_url()`, lines 10–35, and the comment above it explains why in detail.
In short:

1. empty → `sqlite:///firstlight-dev.db`
2. `postgres://` → `postgresql://` (Render and Heroku hand out the old prefix)
3. bare `postgresql://` → `postgresql+psycopg2://`
4. a URL that already names a driver is left alone

Step 3 is not cosmetic. SQLAlchemy 2.1 changed its default Postgres driver from
psycopg2 to psycopg 3, and this project installs `psycopg2-binary`. Without the
explicit `+psycopg2`, a build that resolved SQLAlchemy 2.1 died on
`flask db upgrade` with `No module named 'psycopg'`.

### Environment variables

The complete annotated list is `.env.example`. The short version:

**Required in production:** `DATABASE_URL`, `MAIL_FROM`, either `BREVO_API_KEY`
or all four `SMTP_*`, and `TURNSTILE_SECRET`.

**Notable optional ones:** `SECRET_KEY` (auto-generated into the database if
unset — see `_ensure_secret_key`), `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
`DAILY_API_KEY`, `CRON_SECRET`, `TEAM_EMAIL` (defaults to
`team@bloomanyway.online` and is **not** in `.env.example`), `PUBLIC_BASE_URL`,
`MEMBERSHIP_GRACE_DAYS`, the four upload directories, and the
`REEL_*`/`REVIEW_*`/`COURSE_*` size and chunk caps.

**Read by the README but not by the code:** `LEMONSQUEEZY_WEBHOOK_SECRET`,
`LEMONSQUEEZY_API_KEY`. Dead.

### Where files live

| Kind | Production | Development |
|---|---|---|
| Content Hub + review videos | `/var/media/videos` | `instance/videos` |
| Raw member reels | `/var/media/reel_raw` | sibling of videos |
| Course files | `/var/media/course_files` | `instance/course_files` |
| Shop download files | `/var/media/shop_files` | `instance/shop_files` |
| Avatars, covers, landing images, forum images | **Postgres** | **SQLite** |

The split exists because Render's container disk is wiped on every deploy.
Anything small enough goes in the database; anything large goes on the mounted
disk declared in `render.yaml`:

```yaml
disk:
  name: media
  mountPath: /var/media
  sizeGB: 25
```

If that disk is ever detached, the four directory env vars should be cleared so
the affected paths fall back toward database storage.

### The 100 MB wall

Cloudflare's free plan rejects any request body over roughly 100 MB. Every large
upload in this app therefore slices the file **in the browser** and posts the
pieces to a `begin` / `chunk` / `finish` endpoint trio. There are four such
flows: course files (`/admin/products/<id>/uploads/*`), raw reels
(`/watch/reel-upload/*`), review videos (`/admin/reel-reviews/uploads/*`), and
the same pattern inside `assets.py`. The per-file ceiling (`*_MAX_MB`, default
2048) is *not* the request limit; the chunk size (`*_CHUNK_MB`, default 8) is the
number that must stay under Cloudflare's limit. A file arriving whole in one
request — the no-JS fallback — is capped at 90 MB.

### Production start

```
buildCommand: pip install -r requirements.txt
startCommand: flask db upgrade && python seed.py && gunicorn "app:create_app()" --workers 2 --threads 4 --timeout 300
```

Migrations and seeding run at **start**, not at build, because Render's internal
database hostname only resolves once the service is live.

`seed.py` is idempotent and writes content only — quotes, FAQ stubs, legal
pages, the two forums with their tags, and two inactive membership plans. **It
never touches accounts or passwords**, so redeploys cannot reset the owner's
login.

Rate-limit counters are in memory and reset on every deploy. The `SECRET_KEY`
stored in the database means sessions survive one.

---

## 16. House style

The codebase has a strong and consistent voice. Matching it matters more here
than in most projects, because the comments carry real information.

**Comments explain *why*, never *what*.** There are no `# increment the counter`
comments anywhere. What you will find instead are short prose paragraphs above a
function or a block explaining the decision behind it, often naming the failure
mode it prevents. For example, from `config.py`:

> A bare `postgresql://` means "whatever this version of SQLAlchemy considers
> the default driver", and in 2.1 that default moved from psycopg2 to psycopg 3.
> This app installs `psycopg2-binary`, so the day a build resolved SQLAlchemy
> 2.1 the engine went looking for a psycopg that was never there […]

**Do not write a comment that explains your change.** Comments address the next
reader of the code, not the reviewer of the diff. "Changed this to fix the bug
where…" is wrong; "A code doesn't move this number, because…" is right.

**Prose, not bullet soup.** Docstrings and comments are written in full
sentences. Section markers look like `# --- a launch price with a day it goes
back up ---`, lower-case and descriptive.

**The owner is "she" and "the owner".** Members are "members". Studio is
"Studio", not "the admin panel".

**Commit messages** are a short imperative sentence, then a blank line, then
prose paragraphs explaining the reasoning. They read like the comments. Look at
`git log` for twenty examples.

**User-facing copy is warm and plain.** Flash messages say things like "That
date for the price going up didn't look right, so the page won't mention one."
rather than "Invalid date format". Studio help text explains consequences, not
field types.

**Line length is ~79 characters** in Python. Imports are grouped stdlib /
third-party / local and alphabetised within each group.

---

## 17. Traps — the list to read before editing

Ordered roughly by how likely they are to bite.

1. **`ShopPurchase.product_id` is not a foreign key.** It is a Stripe price id
   string. Use `course_reader.catalog_product_for_purchase(purchase)` to get the
   `Product`, which also handles retired price ids.

2. **`Order` and `ShopPurchase` are different things.** Money versus
   entitlement. A membership purchase makes an `Order` and no `ShopPurchase`. A
   refund has to touch both.

3. **Fulfilment runs more than once per charge.** Stripe sends up to three
   events for one payment and the browser-return path can add a fourth. Anything
   you add to `handle_payment_event` needs its own idempotency claim, like
   `orders.gift_told_at` or `welcome_sent_at`.

4. **The smoke suite is sequential and ids must be unique across all 13,000
   lines.** Reusing a payment id silently takes an idempotent branch and your
   assertion gets `None`.

5. **The smoke suite builds its schema with `db.create_all()`, not migrations.**
   A migration that disagrees with the models passes every test and breaks
   production. Exercise migrations separately, up and down.

6. **Preserve the UTF-8 BOM on `scripts/smoke_test.py`.**

7. **`op.batch_alter_table` or the migration will not run on SQLite**, which is
   what development and the entire test suite use.

8. **Hand-write the migration revision id.** `flask db migrate` emits a hash
   that breaks the chain's naming scheme.

9. **Every new admin route needs `@admin_required` by hand.** The blueprint has
   no login gate. Forgetting it leaves the route public.

10. **Never mutate state in an admin GET.** The read-only-owner guard only
    inspects non-GET methods, so a mutating GET bypasses it entirely.

11. **No inline `<script>` and no `onclick=` attributes.** The CSP forbids them.
    Use external JS and `data-` attributes.

11b. **A link that opens something in the page needs `data-no-loader`.** The
    page loader watches clicks in the *capture* phase, so it has already put
    the spinner up before anything else gets to call `preventDefault` — and
    then nothing takes it down. Failing that, dispatch `page-loader-hide`.

12. **Jinja line breaks land in the rendered HTML.** A phrase split across two
    template lines will not match a literal substring test. Keep user-visible
    phrases on one source line or assert with a regex.

13. **Datetimes in the database are naive UTC.** Never store aware datetimes.
    Parse owner input with `timefmt.parse_owner_parts`, display with `localtime`
    or `when`.

14. **`|localtime` is text and safe in an attribute; `|when` is markup and is
    not.** Putting `|when` inside `title="..."` produces broken HTML.

15. **Never hand-write `data-countdown` attributes.** Use the `countdown()`
    Jinja global.

16. **Promo codes do not discount anything in this codebase.** They are display
    copy; Stripe's own coupon does the arithmetic at checkout.

16b. **A price change means a new Stripe price, never an edited one.** Go
    through `stripe_catalog.sync_product`, which makes the new one, archives
    the old and calls `retire_price_id` so orders placed at it still match.
    Writing `product.price_cents` on its own leaves the page quoting a figure
    Stripe will not charge.

17. **`ls_order_id`, `lemon_squeezy_order_id`, `ls_variant_id`, `zoom_url`,
    `zoom_meeting_id` are live columns with misleading legacy names.** They hold
    Stripe and Daily.co data respectively.

18. **Change a user's tier through `memberships.reconcile_user`,** not by
    assigning `user.membership` directly, or the precedence order (manual >
    Stripe > orders > perk) is lost.

18b. **`User.effective_membership` reads the session.** It layers the owner
    preview on top, which is right for the person looking at the page and
    wrong for anybody else — asking it about a list of other accounts hands
    them all whatever tier the viewer is pretending to be. Use
    `forum_access.tier_of` when the question is about somebody else.

19. **`Product` and `ProductRound` duck-type each other on purpose.** If you add
    a date field to one, add it to the other and to `schedule()` /
    `schedule_for()`, or buyers on an old cohort silently fall back to the
    product's dates.

20. **Binary columns are `db.deferred` for a reason.** Loading `avatar_data` on
    a members list is a real performance cliff.

21. **JSON-in-Text columns have getter/setter pairs on the model.** Write
    `product.set_tags([...])`, never `product.tags_json = ...`.

22. **Don't add a second stylesheet** without also editing both base templates;
    and put new rules in the correct section of `main.css`, preferring the later
    "remaster" block where an area has two.

23. **The README lies in places.** See section 3.

24. **There is no CI.** Nothing will catch a mistake except running
    `python scripts/smoke_test.py` yourself.

25. **`ruff` is the linter but is neither pinned nor configured.** Install it
    manually; it runs on defaults.

---

## Appendix: where to start for a given task

| If the task is about… | Start here |
|---|---|
| a product's fields, price, dates | `models.py` `Product` (447), `admin/routes.py` `_apply_product_fields` (492), `templates/admin/product_form.html` |
| how a product page looks | `templates/main/course_detail.html`, CSS 9681–10005 |
| the catalogue | `main/routes.py` 172–333, `templates/main/courses.html`, CSS 8829–9333 |
| checkout or a payment bug | `services/stripe_pay.py` `handle_payment_event` (1385), `webhooks/routes.py` |
| what someone owns | `services/shop_purchases.py`, `services/course_reader.py` |
| when content unlocks | `services/drip.py`, `Product.schedule_for` |
| memberships and tiers | `services/memberships.py` `reconcile_user` (287), `models.py` `User` (127) |
| cohorts / multiple runs | `models.py` `ProductRound` (1478), `services/rounds.py` |
| an email | `services/mailer.py`, `.env.example` for the template ids |
| the Studio UI | `admin/routes.py` plus `templates/admin/`, CSS 805–994 and 6227–6343 |
| the forums | `forums/routes.py` (whole file), CSS 1413–1716 and 4733–5242 |
| the course reader | `main/routes.py` 1377–1687, `static/js/course-reader.js`, CSS 1717–2483 |
| landing pages | `services/landing_pages.py`, `partials/landing_blocks.html`, `static/js/landing-editor.js` |
| live video sessions | `services/support_groups.py`, `services/daily.py`, `main/routes.py` 2628–3263 |
| dates showing wrong | `services/timefmt.py`, `static/js/localtime.js` |
| adding a column | `models.py`, then a new file in `migrations/versions/`, then `scripts/smoke_test.py` |
