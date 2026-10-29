# GrantFlow — Full Stack (Phases 1–5)

Django + Django REST Framework backend, plus a complete React/TypeScript
frontend: multi-tenant grant management with org-scoped access control,
versioned proposal sections with real-time collaborative editing (Django
Channels + Redis + ProseMirror), an indirect-cost-aware budget builder, an
AI drafting engine, Stripe billing with plan-based feature gating, a funder
intelligence browser, and auth/dashboard/billing screens tying it together.

This covers **Phase 1** (backend/auth/data models), **Phase 2** (AI drafting
engine), **Phase 3** (Stripe billing/tiers), **Phase 4** (real-time
collaborative editing, backend + client), and **Phase 5** (the full
application frontend — dashboard, grant detail with Sections/Budget/Outcomes
tabs, funder database browser, billing/upgrade screens) of the full
GrantFlow build. It is real, tested, working code — not a mockup.

**The full stack was verified with an actual headless-browser end-to-end
test** (Playwright + Chromium) driven against the real production build and
the real backend (daphne + Redis, not mocks): register → create an
organization → create a grant → add a proposal section → open the live
collaborative ProseMirror editor → type real text → save a version (with
the save confirmation rendering) → switch to the Budget tab → add a line
item → see the computed indirect-cost summary update — all passed, zero
console errors. Two real, independent WebSocket connections were also
exercised directly over TCP against daphne to confirm live multi-user
collaboration broadcasts correctly (see Phase 4 notes below).

## What's actually implemented

- **Full frontend app** (`frontend/`): login/register, a dashboard listing
  an org's grants (with plan-limit errors surfaced as real UI, not just
  console noise), a grant detail page with Sections / Budget / Outcomes
  tabs, a funder-intelligence browser with search, and a billing page
  showing plan limits with working Stripe Checkout/portal redirects.
  React Router-based, JWT persisted in `localStorage`, typechecked clean
  and building to a 61-module production bundle with zero errors.

- **Auth**: email/password registration (creates a User + Organization +
  owner Membership atomically), JWT login (`djangorestframework-simplejwt`),
  refresh tokens, rate-limited login/register endpoints.
- **Multi-tenancy**: every Grant, Section, Budget, and Outcome is scoped to
  an Organization via Membership. Role-based permissions (owner/admin/editor/
  viewer) enforced at the object level — verified by tests that a user in
  Org B gets a 404 (not a 403) trying to view Org A's data, and that a
  viewer gets a 403 trying to edit.
- **Funder intelligence**: `Funder` / `FunderProgram` models for past awards,
  program officer notes, deadlines.
- **Grants**: status pipeline (drafting → internal review → submitted →
  awarded/rejected), linked to a FunderProgram.
- **Version-controlled sections**: `GrantSection.versions` action
  (`GET`/`POST /api/sections/{id}/versions/`) appends immutable
  `SectionVersion` snapshots instead of overwriting content — full edit
  history, tagged human vs. AI-generated.
- **Budget builder**: line items by category, three selectable indirect-cost
  bases (Total Direct, MTDC, Salaries & Wages only), correct exclusion of
  equipment/exempt items from MTDC, `GET /api/budgets/{id}/summary/` for
  computed totals.
- **Outcome tracking**: target vs. actual metrics per grant, ready to feed
  auto-populated impact reports later.
- **Security**: env-driven secrets (nothing hardcoded), HSTS/SSL-redirect/
  secure-cookie settings auto-enabled when `DEBUG=False`, scoped rate
  throttling on auth endpoints, strong password validation.

- **AI drafting engine**: `POST /api/sections/{id}/generate/` builds a
  context-rich prompt from the organization's mission statement, the
  target FunderProgram's focus areas / program officer notes / past-awards
  pattern, and any other sections already drafted in the same proposal
  (for cross-section consistency) — then calls the Anthropic API and saves
  the result as a new `ai_generated` SectionVersion, leaving prior versions
  untouched. The system prompt explicitly forbids fabricating statistics,
  dates, or dollar figures, instructing the model to use bracketed
  placeholders instead. Requires `ANTHROPIC_API_KEY` in `.env`; without it
  the endpoint returns `503` with a clear message rather than failing
  silently or faking a response. Rate-limited (30/hour/user) and
  permission-checked (viewers can't trigger generation).
- **Stripe billing**: `POST /api/billing/checkout-session/` creates a real
  Stripe Checkout session for upgrading to Starter or Pro (creates/reuses a
  Stripe Customer, stores the ID on the Organization); `POST
  /api/billing/portal-session/` returns a hosted billing-portal link for
  managing or cancelling a subscription; `POST /api/billing/webhook/`
  verifies Stripe's signature and applies `checkout.session.completed` /
  `customer.subscription.updated` / `customer.subscription.deleted` events
  to the Organization's plan — idempotently, via a `WebhookEvent` log, so a
  retried delivery (which Stripe does routinely) never double-applies.
  `GET /api/billing/plans/` is public and returns the tier limits below.
- **Plan-based feature gating**: enforced server-side, not just in a
  pricing page — Free plan orgs get a `402 Payment Required` (not a vague
  403) if they try to create a 2nd grant or exceed 5 AI generations in a
  calendar month. Limits live in one place (`billing/plans.py`):

  | Plan | Grants | AI generations/mo | Seats |
  |---|---|---|---|
  | Free | 1 | 5 | 2 |
  | Starter | 5 | 50 | 5 |
  | Pro | unlimited | 500 | 20 |

- **50 automated tests, all passing** — auth, cross-org isolation, role
  permissions, version history, every budget-math edge case (MTDC
  exclusions, exempt line items, zero-line-item budgets), AI-drafting
  context assembly and error handling, and billing (checkout/portal
  permission checks, webhook signature verification + idempotency, plan
  gating on both grants and AI generations). (63 total with real-time —
  see below.)
- **Real-time collaborative editing**: `ws://.../ws/sections/{id}/collab/`
  (Django Channels, ASGI) implements the same optimistic-concurrency
  protocol as ProseMirror's reference collab server — a Redis list of
  (step, clientID) entries, atomically compare-and-appended via a Lua
  script so concurrent submissions from different server processes never
  race. JWT-authenticated over the WebSocket query string; viewers can
  connect and watch but not submit edits. When a section's baseline
  content changes via the REST version endpoints (a human save or an AI
  generation), the pending collab steps for that section are automatically
  cleared, since they were relative to the old baseline. `frontend/`
  contains a real ProseMirror + `prosemirror-collab` client
  (`src/collab/`) implementing the exact wire protocol — verified against
  the actual library's TypeScript types, and against the actual running
  server: two independent WebSocket connections were opened over real TCP
  against a live `daphne` process backed by real Redis, and a step
  submitted by one was confirmed to broadcast correctly to the other with
  matching version and client ID. 13 additional tests cover the consumer
  and the Redis store directly (conflict/rebase, permissions, catch-up),
  run against a real local Redis instance, not a mock.

## Not yet built

The fine-tuning pipeline (would need a training corpus you'd supply —
prompting + retrieval, already built in Phase 2, is the better starting
point anyway). Cosmetic/production-readiness gaps worth knowing about:
no automated frontend test suite (verification so far is the manual E2E
script + TypeScript strictness), no code-splitting on the frontend bundle
(single ~514KB JS chunk — fine for internal tooling, worth splitting before
a public launch), and the account/org settings screens (editing org name,
mission statement, EIN, team member management) aren't built — those fields
exist on the backend (`PATCH /api/organizations/{id}/`) but have no UI yet.

## Running the real-time layer locally

You need a Redis server running (`redis-server`), and you must serve the
app via an ASGI server, not `manage.py runserver` (which only speaks HTTP):

```bash
pip install daphne
daphne -b 0.0.0.0 -p 8000 grantflow_backend.asgi:application
```

## Frontend

```bash
cd frontend
npm install
cp .env.example .env   # point at your running backend
npm run dev
```

Needs the backend running via an ASGI server (see above — `manage.py
runserver` won't serve the WebSocket collab endpoint).

Structure: `src/api/client.ts` (every backend endpoint), `src/auth/`
(login/register/session context), `src/pages/` (Dashboard, GrantDetail with
its Sections/Budget/Outcomes tab panels, Funders, Billing), `src/collab/`
(the ProseMirror real-time editor — `CollabEditor.tsx`,
`useCollabEditor.ts` for the collab-plugin wiring, `socket.ts` for the raw
WebSocket protocol, `serialization.ts` for HTML ⇄ ProseMirror doc
conversion). `App.tsx` wires it all together with React Router; unauthenticated
users are redirected to `/login` via `ProtectedLayout`.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # edit values as needed
python manage.py migrate
python manage.py createsuperuser  # optional, for /admin/
python manage.py runserver
```

Defaults to SQLite with `DEBUG=True` for zero-config local dev. For
production, set `DATABASE_ENGINE=postgres` plus the `DB_*` vars in
`.env`, and set `DJANGO_DEBUG=False` (this automatically turns on
HSTS, secure cookies, and SSL redirect).

## Running tests

```bash
python manage.py test accounts grants billing realtime -v 2
```

Realtime tests need a real Redis instance reachable at `REDIS_URL`
(default `redis://localhost:6379/0`) — start one with `redis-server`
before running them.

## API overview

All endpoints are under `/api/`. Auth uses `Authorization: Bearer <access_token>`.

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/register/` | Create user + org + owner membership, returns JWT pair |
| `POST /api/auth/login/` | Get JWT pair |
| `POST /api/auth/refresh/` | Refresh access token |
| `GET /api/auth/me/` | Current user + memberships |
| `/api/organizations/` | CRUD, scoped to your orgs |
| `/api/funders/`, `/api/funder-programs/` | Funder intelligence DB |
| `/api/grants/` | CRUD, scoped to your orgs |
| `/api/sections/` | Grant sections |
| `GET/POST /api/sections/{id}/versions/` | Version history / save new version |
| `/api/budgets/` | One per grant |
| `GET /api/budgets/{id}/summary/` | Computed totals |
| `/api/budget-line-items/` | Line items |
| `/api/outcomes/` | Outcome tracking |
| `POST /api/sections/{id}/generate/` | AI-draft this section (requires `ANTHROPIC_API_KEY`) |
| `GET /api/billing/plans/` | Plan tiers + limits (public) |
| `POST /api/billing/checkout-session/` | Stripe Checkout URL for upgrading (owner/admin only) |
| `POST /api/billing/portal-session/` | Stripe billing-portal URL (owner/admin only) |
| `POST /api/billing/webhook/` | Stripe webhook receiver (signature-verified, no user auth) |
| `ws://.../ws/sections/{id}/collab/?token=<jwt>` | Real-time collaborative editing (WebSocket, ASGI only) |

## Design notes

- UUID primary keys throughout (safe to expose in URLs, no enumeration).
- Every write to a `GrantSection` creates a new `SectionVersion` rather than
  mutating one row — that's the "version control" from the spec, no extra
  library needed.
- Budget math lives on the `Budget` model itself (`indirect_costs()`,
  `total_budget()`, etc.), not duplicated in the view layer, so it's testable
  in isolation from the API.
- `IsGrantOrgMember` walks `.organization` / `.grant.organization` /
  `.budget.grant.organization` so the same permission class covers Grant,
  GrantSection, Budget, and BudgetLineItem without repeating logic.
