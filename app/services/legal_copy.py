"""Canonical legal page bodies (seeded / refreshed when version changes)."""

#: Bump to push Terms / Refunds / Privacy to live Page rows on next seed.
LEGAL_COPY_VERSION = "2026-09-08"

#: The one-line version, for the places a buyer is looking at a price rather
#: than a policy — the product page says it in its own words, and this goes on
#: the Stripe page itself, where the money actually leaves.
GUIDE_NO_REFUND = ("Guides are non-refundable — a guide opens the moment you "
                   "pay and is yours to keep.")

PRIVACY = """# Privacy Policy

**Last updated:** August 21, 2026

Bloom Anyway ("we", "us") respects your privacy. This page explains what we collect, why, and how you can ask us to change or remove it.

## Who we are
Bloom Anyway is a community and content platform. Digital product and membership checkout is handled by **Stripe** as merchant of record — they process payments and deliver purchase receipts. We never see or store your card number.

## What we collect
- **Account data** — email address, password hash, optional display name / username, bio, profile links, timezone, and avatar if you upload one.
- **Community content** — posts, comments, likes, Showcase listings, journal entries, and check-ins you choose to create.
- **Membership & orders** — membership tier and purchase records synced from Stripe (product name, amount, status, billing interval). Card details stay with Stripe.
- **Support sessions** — peer circle bookings you schedule or join (topic, time, seat), and related notifications.
- **Operational data** — anonymised page-view counts (path + date only; no IP stored in that counter), first-touch traffic source (UTM / referrer labels such as Instagram or organic search — no advertising pixels), feedback you send us, and content reports.
- **Email** — messages we send for verification, password reset, membership notices, and optional contact replies. If you join the Sunday letter, we store that email for the list.

## Cookies & similar tech
We use a session cookie (and an optional "remember me" cookie) so you stay signed in. Your browser may also store a timezone preference cookie so dates show in your local time. We do **not** use advertising pixels or third-party tracking cookies on this site. Cloudflare Turnstile may run on signup to reduce bots — that verification is handled by Cloudflare.

## How we use your data
To run your account, show community features, honour memberships, run peer support sessions, prevent abuse, improve the product from feedback you send, and meet legal obligations. We do not sell your personal information.

## Sharing
We share data only with processors needed to run the service (hosting, email delivery, payment via Stripe, video rooms for support sessions, and Turnstile on signup). We may disclose information if required by law or to protect people from serious harm.

## How long we keep it
Account and community data stay while your account is open. Closing your account scrubs personal profile fields, removes your login, hides your posts/comments from public view, and replaces your email with a non-contactable placeholder. Purchase history may remain with Stripe under their policies. Aggregated analytics without personal identifiers may be kept.

## Your choices
- Update profile details in Settings.
- Cancel membership renewal through Stripe's customer portal or your receipt links when available.
- Close your account in Settings (confirmation required).
- Contact us via the Contact page to ask questions about your data.
- Unsubscribe guidance for the Sunday letter is included in those emails when we send them.

## Children
This service is intended for adults (18+). If you believe a minor has created an account, contact us and we will take it down.

## Changes
We may update this policy. The "Last updated" date above will change when we do. Continued use after an update means you accept the revised policy.

## Contact
Use the [Contact](/contact) page for privacy questions.
"""

TERMS = """# Terms of Service

**Last updated:** August 26, 2026

Welcome to Bloom Anyway. By creating an account, buying a membership or digital product, joining a support session, or using the community, you agree to these terms.

## The service
Bloom Anyway offers [daily quotes](/), [community forums](/forums/) (Healing and Building), member [Showcase](/showcase) listings, a [Content Hub](/watch), [peer support groups](/support-groups), optional paid [memberships](/membership), and [courses/guides](/courses) sold on this site through [Stripe](https://stripe.com). Content is for personal growth, peer support, and education only.

## Not a healthcare provider
Bloom Anyway is **not** a healthcare provider. Neither Bloom Anyway nor its owners, staff, moderators, or peer session hosts are licensed therapists, counselors, psychologists, psychiatrists, physicians, attorneys, or financial advisors, and nothing on this site — including quotes, forum posts, courses, guides, Content Hub material, or peer support sessions — constitutes therapy, medical advice, mental health treatment, legal advice, or financial advice. It is not a substitute for care from a qualified, licensed professional. Do not delay or forgo seeking professional help because of something you read or heard here.

If you are in crisis, having thoughts of harming yourself or others, or need emergency care, contact a local emergency service or [crisis helpline](https://www.iasp.info/suicidalthoughts/) immediately — **not** this site.

## No guarantees on outcomes
Personal growth, healing, business, and income results depend on your own circumstances, effort, and factors outside our control. We do not promise or guarantee any specific result — emotional, financial, business, or otherwise — from using the community, courses, guides, Content Hub, or peer support sessions. Testimonials, examples, and case studies (including our own) reflect individual experiences and are not a guarantee that you will achieve similar results.

## Accounts
- You must provide a real email you control and keep your password private.
- One person per account. Do not share login credentials.
- You are responsible for activity under your account.
- We may suspend or close accounts that break these terms, abuse others, or create security risk.
- You may close your account in [Settings](/account/settings). Closing does not automatically refund purchases (see [Refunds](/refunds)).

## Memberships
- **Free** accounts can browse courses and quotes, use limited community participation, and access free Content Hub picks where marked.
- Paid plans — **Healing**, **Creator**, and **Full Bloom** — unlock the perks described on the [Membership](/membership) page at the time of purchase. Full Bloom combines Healing and Creator access.
- There is **no free trial** on memberships unless we clearly advertise one later. Checkout charges according to the plan and billing interval you choose (monthly or annual).
- **Founder / launch pricing:** When a founder window is live on the Membership page, a discounted membership rate may be available if you subscribe before the stated end date and enter the published Stripe promotion code at checkout (`MEMBERFOUNDER` for Healing and Creator; `FULLBLOOMFOUNDER` for Full Bloom). That founder rate is **locked in for as long as your subscription continues** — it does not step up to the regular list price after the first charge. If you cancel and later resubscribe after the founder window has closed, the then-current list price applies. Promo availability, codes, and amounts can change for new sign-ups; the Membership page and Stripe checkout are the source of truth at purchase time.
- Memberships are billed through Stripe. Prices, currency, billing interval, taxes, and any promo you apply are confirmed at checkout. See also Stripe's [consumer terms](https://stripe.com/legal/consumer).
- Benefits apply while a membership is active. Cancelling stops future renewals per Stripe's terms. Refunds or chargebacks may remove the related tier.
- Site owners retain Full Bloom–level access for operating the studio.
- Membership is a licence to use features — not ownership of the platform, brand, or other members' content.

## Support groups & peer sessions
- Eligible members may schedule or join [peer support sessions](/support-groups) for topics included in their plan.
- Sessions are peer-led community spaces, **not** clinical or therapeutic groups, and are not facilitated by licensed mental health professionals unless a specific session is explicitly advertised as such.
- Be respectful; follow community rules. Seats are limited. Cancelling or missing a session does not create a cash refund unless we say otherwise for a specific paid add-on (see [Refunds](/refunds)).
- Facilitator-led or 1:1 add-ons (when offered) are booked and paid separately under the terms shown at booking, and any professional credentials of a facilitator will be stated on that offer — absent that statement, assume the facilitator is a peer, not a licensed professional.

## Community rules
Be kind. No harassment, hate, threats, sexual exploitation, illegal content, spam, or impersonation. We may hide or remove content, issue warnings, or pause posting access. Reporting tools exist on posts, comments, and after peer sessions; automated checks may remove clearly violating content, and the studio reviews other reports.

Content posted by members — including in the [Healing and Building forums](/forums/) and peer sessions — reflects the views and experiences of that member only, not medical, legal, or professional guidance, and not the views of Bloom Anyway.

## Your content
You keep ownership of what you post. You grant Bloom Anyway a non-exclusive licence to host, display, and moderate that content so the community can function. Do not post material you do not have rights to share. See also our [Privacy Policy](/privacy).

## Digital products & shop
Courses and guides sold on the [Courses & Guides](/courses) page are licensed for **personal use** unless a product page says otherwise. Redistribution or resale is not allowed. Delivery, taxes, and payment disputes for those purchases are handled with [Stripe](https://stripe.com) as merchant of record.

A guide is delivered as soon as your payment clears — it opens in My space straight away — and guides are **non-refundable**. Refunds are only applicable for duplicate purchases; see [Refunds](/refunds).

## Acceptable use
Do not attempt to break, scrape abusively, overload, or reverse-engineer the service; do not bypass membership gates, rate limits, or security checks (including signup verification); do not disrupt support sessions or share private session links publicly.

## Disclaimers and limitation of liability
The service is provided "as is" and "as available," without warranties of any kind, express or implied, including implied warranties of merchantability, fitness for a particular purpose, and non-infringement. We work hard to keep it reliable, but we do not guarantee uninterrupted access, error-free content, or specific outcomes from community support, peer sessions, or courses.

To the fullest extent permitted by law, Bloom Anyway and its owners, employees, contractors, and moderators are not liable for any indirect, incidental, special, consequential, or punitive damages, or any loss of profits, revenue, data, or goodwill, arising from or related to your use of the service, community content, peer sessions, or any decision you make based on material found here — even if we have been advised of the possibility of such damages. To the extent any liability cannot be excluded under applicable law, our total liability to you for any claim arising from the service is limited to the amount you paid us in the twelve (12) months before the claim arose.

You agree to use your own judgment and, where appropriate, consult a qualified licensed professional before making decisions about your health, finances, or legal matters based on anything encountered through Bloom Anyway. Nothing in this section limits rights you cannot waive under applicable consumer law.

## Indemnity
You agree to indemnify and hold harmless Bloom Anyway and its owners, employees, and contractors from claims, losses, and expenses (including reasonable legal fees) arising from your content, your breach of these terms, or your misuse of the service.

## Changes
We may update these terms. Material changes will update the date above. Continued use after changes means you accept them.

## Contact
Questions? Use the [Contact](/contact) page.
"""

REFUNDS = """# Refund Policy

**Last updated:** September 8, 2026

Refunds are only applicable for **duplicate purchases**. If you were charged twice for the same thing, write to us and we will put the extra charge back. A change of mind, a product you already opened, or a membership you used is not a reason we refund. Stripe is the merchant of record for Bloom Anyway checkouts.

Nothing on this page takes away a refund right the law gives you and we can't sign away.

## Duplicate purchases
A duplicate purchase is a second charge for the same product, membership, or session — two receipts for one thing. Reply to your Stripe receipt or use [Contact](/contact) with both order emails and we will refund the extra charge.

## Guides
**Guides are non-refundable.** A guide is delivered the moment your payment clears — it opens in My space straight away and stays yours to keep — so there is nothing to give back, and we don't refund a change of mind once it has been opened. The product page, the price, and what's inside are all there before you buy; please read them, and ask us first if anything is unclear. The only refund we issue on a guide is a **duplicate purchase**.

## Courses
Courses follow the same rule: we refund a **duplicate purchase**, not a change of mind after checkout. Access to a product in My space may be revoked when a refund is issued.

## Memberships
- Memberships renew through Stripe on the interval you chose (monthly or annual) until you cancel.
- **Cancel anytime** to stop future renewals — use the links on your Stripe receipt or Stripe customer portal when available, or contact us if you can't find them. Cancelling does **not** by itself refund time already billed.
- **Founder / launch rates** (when offered): if you correctly enter the promo code at checkout during the published founder window, the discounted rate is **locked in for the life of that subscription** (it does not revert to the regular list price after the first month or year). Cancelling ends the lock-in; rejoining later uses the then-current price. Founder pricing is not a free trial.
- **Refunds for memberships** apply only to a **duplicate purchase** (charged twice for the same plan). We do not refund time already billed or a change of mind after you have used paid community, support sessions, or Hub access.
- Chargebacks filed without contacting us first may result in account review or suspension while we sort out payment status.

## Support sessions & add-ons
Peer support seats included with membership are not separately refundable. Paid facilitator or 1:1 bookings are refunded only for a **duplicate purchase**, unless the cancellation note shown at booking says otherwise for that session.

## How to ask
1. Find your Stripe receipt email, or  
2. Message us through [Contact](/contact) with the order email, approximate date, and what you bought.

We aim to respond within a few business days.
"""
