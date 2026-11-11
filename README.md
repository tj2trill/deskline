# Deskline — self-hosted AI receptionist agency platform

Run your own white-label AI phone receptionist business: paste a client's website, get a
trained receptionist on a real phone number in about a minute, bill the client through Stripe, and
prospect new clients from Google Maps — all from one dashboard you own.

It covers everything Ulio's partner plans sell (agent builder, numbers, call logs, booking,
SMS, white-label, lead finder, chat widget, website builder, proposals, email writer, workflows,
custom functions, client billing). It also fixes the things that make their model fragile:

| | Ulio partner plan | Deskline |
|---|---|---|
| Minute costs | Subsidized "250,000 min" plans; their admin panel tracks partners who cost more than they pay | Real per-call provider cost, per-client margin, metered Stripe billing, minute caps |
| Platform take | 3% of everything you collect + monthly plan | 0% — you pay providers directly |
| Call safety net | — | Every number falls back to the owner's phone if anything fails; paused/unpaid clients forward instead of ringing out |
| Gets better over time | — | Unanswered questions become **knowledge gaps**; answer once → live agent updated. Bad calls are flagged for review |
| Retention | — | Automatic weekly value report to each owner (calls answered, after-hours catches, bookings, est. value) |
| Models | Locked in | Any OpenAI-compatible LLM for building, chat and writing (incl. local Ollama/vLLM); Vapi provider and model per agent |
| Selling | Fake countdowns, income testimonials | Honest proposals (ROI computed from the prospect's own numbers), CASL/CAN-SPAM-compliant outreach |

---

## Quick start (local, 5 minutes)

```bash
cp .env.example .env          # set SECRET_KEY, ADMIN_EMAIL, ADMIN_PASSWORD at minimum
docker compose up -d --build  # Postgres + app on http://localhost:8000
```

Sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`. With no API keys every feature still runs in a local
fallback mode: heuristic website parsing, console email/SMS shown under **Settings → Test delivery**,
and the internal calendar. Add keys as you go; **Settings → Integrations** shows what's connected.

Without Docker: `pip install -r requirements.txt`, set `DATABASE_URL` (SQLite works:
`sqlite:///./deskline.db`), then `uvicorn app.main:app --reload`.

Calls need a **public HTTPS URL** so Vapi and Stripe can reach your webhooks. Locally use
`cloudflared tunnel --url http://localhost:8000` (or ngrok) and put that URL in `BASE_URL`.

## Production

1. A small VPS (2 GB RAM is plenty) with Docker. Point a domain (e.g. `desk.youragency.com`) at it.
2. In `.env`: `BASE_URL=https://desk.youragency.com`, `DOMAIN=desk.youragency.com`, `SECURE_COOKIES=true`, a strong `SECRET_KEY` and `POSTGRES_PASSWORD`.
3. `docker compose --profile https up -d --build` — Caddy fetches the TLS certificate automatically.
4. Back up the `pgdata` volume (e.g. nightly `pg_dump`).

Upgrades: `git pull && docker compose up -d --build`. New columns are added automatically on boot
(additive only — nothing is ever dropped).

## Accounts to connect

| Service | What for | Setting(s) | Notes |
|---|---|---|---|
| **Vapi** | Live calls (telephony bridge, Deepgram STT, LLM, TTS) | `VAPI_API_KEY` | Roughly $0.05/min platform fee plus providers; ~$0.10–0.20/min all-in is typical. Real per-call cost is recorded. |
| **OpenAI-compatible LLM** | Website → agent, call analysis, chat widget, writing tools | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | Use a local server (Ollama `http://host.docker.internal:11434/v1`) to keep these costs at zero. |
| **Twilio** | SMS (confirmations, owner alerts); numbers outside the US | `TWILIO_*` | Vapi's free numbers are US-only. For Canada buy a Twilio number and use **Import Twilio number**. Register A2P 10DLC / toll-free verification before texting at volume. |
| **Stripe** | Billing your clients | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Add webhook `BASE_URL/webhooks/stripe` with the events listed in Settings. |
| **Google Places** | Lead finder | `GOOGLE_PLACES_API_KEY` | Enable "Places API (New)". Or import any CSV of leads. |
| **Email** | Owner alerts, reports, outreach | `EMAIL_PROVIDER=resend` or `smtp` | Use a separate sending domain for cold outreach. |
| Cal.com (per client) | Book into the client's real calendar | Client → Receptionist → Booking | API key + event type ID. Otherwise the built-in calendar uses business hours. |
| Firecrawl (optional) | Better extraction on JS-heavy sites | `FIRECRAWL_API_KEY` | Built-in crawler is the default. |

## Selling and onboarding a client

1. **Find:** Lead finder → niche + city. Results are scored by how much missed calls likely cost them, with the reasons shown. **Check top 10 websites** finds emails, chat widgets and online booking.
2. **Demo:** On a prospect, **Build demo from their website**. Add a number on the Phone tab and call it in front of them.
3. **Pitch:** generate a 3-email sequence (sent one at a time, with your mailing address and unsubscribe link) and a proposal whose ROI is computed from *their* missed-call estimate.
4. **Close:** client → Billing & access → set pricing → **Create & email checkout link**. Stripe handles cards, trials, setup fees, invoices and dunning. The client flips to *active* when they pay.
5. **Go live:** set the owner's cell as the **transfer number** (it's also the safety net), then have them forward calls to the AI number (*busy / no answer* for overflow, or *always*).
6. **Retain:** owners get a summary after every call, a weekly report, and a portal to see calls, recordings, transcripts, bookings, and to answer knowledge gaps themselves.

## Pricing guidance (do your own math)

The defaults ($297/mo, 500 included minutes, $0.25/min overage) leave room above provider cost: at
~$0.15/min, a client using all 500 minutes costs ~$75. Most small businesses use far less. **Billing &
margin** shows real numbers per client, and you get an email when any client's cost passes your alert
threshold (default 70% of revenue). Use the per-client **minute cap** for heavy users.

## How calls flow

```
Caller → number (Vapi or imported Twilio)
       → Vapi asks  POST /webhooks/vapi  {assistant-request}
            ├─ active            → {"assistantId": …}           (the client's receptionist)
            ├─ paused / over cap → {"destination": owner's cell} (never a dead line)
            └─ server down       → Vapi uses the number's fallbackDestination (owner's cell)
       → during call: tool-calls → check_availability / book_appointment / take_message /
                                   capture_lead / send_text / your custom functions / transferCall
       → end-of-call-report → call log + recording + transcript + summary, outcome classified,
                              QA flags, knowledge gaps, owner alert, workflows,
                              Stripe meter event (idempotent: identifier = call id), margin/cap checks
```

Every Vapi request carries an `X-Deskline-Token` header (derived from `SECRET_KEY`); unauthenticated
webhook calls are rejected. Stripe webhooks are verified by signature.

## Code map

```
app/
  main.py            app factory, sessions, custom-domain routing, scheduler start
  models.py          all tables (SQLAlchemy 2)
  services/
    agent_builder.py website → profile → prompt + knowledge (LLM or heuristic)
    crawler.py       same-site crawler, JSON-LD, tel/mailto, chat/booking-tool detection, SSRF guard
    vapi.py          assistants, numbers, tools, server-message parsing
    calls.py         tool execution, routing, end-of-call processing, QA, guardrails, alerts
    scheduling.py    availability + booking (built-in or Cal.com v2)
    billing.py       Stripe products/prices/meters/checkout/portal/webhooks (plain REST)
    pricing.py       flat / per-minute / tiered → graduated tiers (one source of truth)
    leads.py         Google Places search, scoring with reasons, enrichment, CSV
    writer.py        proposals, outreach sequences, review replies, SEO audit, scripts…
    sitegen.py       one-page websites   chat.py  website widget brain
    workflows.py     trigger → condition → SMS / email / webhook
    reports.py       weekly owner report      messaging.py  SMS + email
    outreach.py      guarded sending: mailing address, suppression, daily cap, unsubscribe, follow-up timing
    provisioning.py  create client, buy number, checkout, owner login (shared by UI and agents)
    learning.py      the learning loop: facts → posteriors → verdicts, target/angle choice, proposals
    replies.py       inbound replies → typed intent + confidence → applied or handed to you
  agents/            base (modes, act/dedupe), runner (locks, isolation), team (10 agents), actions
                     (executors), registry (knobs), office (live snapshot for the Office view)
  routers/           clients (agency), growth, portal (owners), public, webhooks, settings
  templates/         server-rendered pages; _ui.html holds the shared components (badges, fields,
                     switches, call table, hours editor), base.html the app frame
  static/app.css     the design system: tokens, components, dark mode, mobile layouts
  static/app.js      soft navigation and small enhancements (see below); no build step
  static/office.js   the Office: canvas pixel-art building, sprites, bubbles, panels (no dependencies)
  static/fonts/      IBM Plex Sans (OFL), bundled so nothing loads from a CDN
tests/               44 tests with a fake internet (crawled site, OpenAI, Vapi, Stripe, Twilio, Places)
```

## Interface

- **One vocabulary.** Every status and call outcome has one colour and one label everywhere (`tone` and `label` filters in `templating.py`). Booked is green, lead violet, message amber, transferred teal, missed red.
- **Soft navigation.** Links, tabs and forms swap the page in place with a short crossfade instead of a full reload. Saving keeps your scroll position, back/forward restores it, and tabs prefetch on hover. Confirmations appear as toasts, destructive actions ask in a dialog, and buttons show a busy state. Without JavaScript everything still works as plain links and forms; add `data-hard` to any form or link that must do a full navigation.
- **Phones.** Below 860px the menu becomes a slide-out drawer and tables restack into labelled rows. Pages are checked for horizontal overflow at 360px.
- **Brand.** The agency colour from Settings drives buttons, links and highlights, including for clients in their portal. Dark mode follows the device setting.

Run tests: `pip install pytest && pytest -q` (SQLite) or
`TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/db_test pytest -q` (Postgres).

## Agents (the business runs itself, within your limits)

Ten agents run on a schedule (`/agents`). Each has a mode: **Off**, **Ask me first** (anything that sends or spends waits for your approval) or **Autopilot** (acts right away inside its settings). A master switch pauses the whole team.

| Agent | Job |
|---|---|
| Prospector | Searches Google Maps for your targets, scores leads, checks websites. Searches the targets that bring paying clients first. |
| Demo builder | Builds a receptionist trained on the prospect's site, plus a demo site and (with approval) a local number. |
| Outreach | Writes and sends a 3-email sequence inside your send window and daily cap. Picks the email angle that gets replies in that niche. |
| Closer | Sends checkout links for deals you mark won, follows up, onboards paid owners. |
| Quality coach | Answers repeated caller questions from the client's own information (quote-checked), asks the owner otherwise. |
| Account manager | Flags silent lines and falling call volume before a client churns. |
| Billing guardian | Reports usage to Stripe and pauses clients who stay unpaid. |
| Ops monitor | Watches errors, failing agents, Vapi and Stripe health. |
| Daily brief | Emails your morning summary, including what was learned. |
| Strategist | Measures what works and what makes money, and proposes setting changes. |

Every action has a dedupe key, so re-runs never repeat work and a rejected proposal is never made again. A crash in one agent never stops the others.

## Learning loop ("What's working")

`/agents/learning` and the Strategist work from raw facts only: emails actually sent, replies, calls to demo lines, wins, Stripe payments and estimated provider costs. Nothing relies on an agent grading its own work.

- **What's measured:** positive-reply rate by search target, email angle, demo line or not, lead score, send time. Also margin per client and per industry, and how often you approve each agent.
- **How it decides:** each rate gets a Beta posterior that starts from your overall reply rate. A result is called *working* or *not working* only with at least 20 emails behind it and 90% confidence against the rest, with the bar raised when many things are compared. Everything else is shown as "too early".
- **How it steers:** the Prospector ranks targets by sampled expected value (reply rate × leads per search × share with email × niche revenue), and the Outreach agent picks email angles the same way. Both still explore unproven options. The Strategist proposes retiring dead targets, raising the score cutoff, waiting for demo lines, and Autopilot for agents you always approve. Each proposal comes with its evidence.
- **Replies are captured automatically:** set `INBOUND_REPLY_TO` and forward that mailbox (Postmark, SendGrid Inbound Parse, Mailgun routes, a Cloudflare Email Worker) to the inbound webhook shown in Settings. Each reply is classified as a typed decision (intent + confidence). Confident unsubscribes, declines and interested replies are applied, anything unsure is sent to you, and any human reply stops the follow-ups.

## Office view

`/agents/office` shows the team as a pixel-art building: one room per agent, plus the lobby (live scoreboard) and your office. Bubbles show only real data: current run steps, last results, blockers with the fix, approvals waiting at your desk, and hand-offs built from actual events. Click an agent or a room to approve or reject, change its mode, run it, or mark replied leads won. The banner on top always shows the single most valuable next step, closest to cash first.

## Compliance checklist (your responsibility)

- **Call recording:** the greeting says "this call may be recorded" by default. Keep it on where two-party consent applies (e.g. California, and recommended in Canada).
- **AI disclosure:** some places require telling callers they're talking to an AI. Add it to the greeting if needed.
- **SMS:** register A2P 10DLC (US) / toll-free verification. Only text callers about their own request.
- **Cold email:** CASL (Canada) and CAN-SPAM (US) need sender ID, a mailing address and a working unsubscribe. All three are built in; set your mailing address in Settings. CASL also restricts who you may email without consent. Implied consent for conspicuously published business addresses is narrow, so check it applies.
- **Data:** recordings and transcripts may contain personal information. Disclose this in your client agreement.

## Security defaults

- **Secrets:** on any non-localhost `BASE_URL` the app refuses to start while `SECRET_KEY` or the first `ADMIN_PASSWORD` is still a placeholder from `.env.example`. `SECRET_KEY` signs sessions and derives the webhook token.
- **Sessions:** changing a password signs out every other session of that user. Sign-in is throttled (8 failures per address and email, 30 per address, per 10 minutes).
- **Browser guards:** state-changing requests from another origin are refused (Origin / Sec-Fetch-Site check on top of SameSite=Lax cookies). Pages ship a Content-Security-Policy with no inline scripts, `frame-ancestors 'none'`, `nosniff`, and `no-store` on signed-in HTML. HSTS is sent when `SECURE_COOKIES=true`.
- **Untrusted text:** anything a caller, a website or an imported file supplied is escaped in pages and in outbound email; links only render with `http(s)`; CSV exports neutralise spreadsheet formulas.
- **Outbound requests** to user-supplied URLs (crawler, webhooks, custom functions) are refused unless every address the host resolves to is publicly routable, and redirects are re-checked hop by hop.
- **Reverse proxy:** the container trusts `X-Forwarded-*` from any peer so it works behind Caddy. Do not expose port 8000 to the internet directly; publish only the proxy.

## Known limits

- One business-hours span per day in the UI. For split shifts, describe them in *Extra instructions*.
- The in-process scheduler (weekly reports) assumes one app container. With several, set `SCHEDULER_ENABLED=false` on all but one.
- The chat widget rate limiter is in-memory, per container.
- This is a single-agency platform. A partner/reseller tier (sub-accounts reselling your platform) is not included.
