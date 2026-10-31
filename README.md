# Aurelia Wealth — Portfolio Intelligence Workspace

A full-stack starting implementation for wealth managers to manage equity baskets, register explicit instruments, persist provider-sourced market history, view valuation snapshots, record transactions, export CSV reports, and share read-only snapshots.

> **Verification status:** this repository is an implemented development baseline, not yet certified production-ready. Follow the verification checklist below. It does not promise live streaming prices or licensed commercial redistribution rights.

## Stack

- Frontend: React, TypeScript, Vite, Tailwind CSS, TanStack Query, Recharts dependency, Lucide icons.
- API: FastAPI, Pydantic, SQLAlchemy 2, Alembic, JWT, Argon2 password hashing.
- Storage: PostgreSQL in Docker; SQLite is the local default for backend-only development.
- Market data: `yfinance` historical polling, with validation, retry attempts, idempotent daily price persistence and job history.
- Packaging: Docker Compose.

## Features in this baseline

- JWT login and account registration; owner-scoped portfolio access.
- Bootstrap administrator account for first launch (role exists; granular admin/client role management is not yet implemented).
- Portfolio creation, editing, duplication, archive/restore, delete, holdings CRUD, and CSV export.
- Instrument registry with explicit exchange/provider symbol mapping and provider history verification.
- Manual yfinance history ingestion with bounded retries, normalization, data validation, upsert-by-instrument/date behavior, and job status history.
- Valuation snapshot and dashboard totals are withheld when any active holding lacks a price.
- Current allocation chart and side-by-side current-snapshot comparison for up to four baskets.
- Buy/sell transaction recording with weighted-average cost and over-sell guard.
- Read-only share links that can be revoked.
- Health endpoints, data freshness labels, provider limitation disclosures, and calculation methodology page.
- Docker Compose, Alembic scaffolding, backend tests, and setup guidance.

## Deploy with GitHub, Vercel, and Render

See [`docs/DEPLOY_GITHUB_VERCEL.md`](docs/DEPLOY_GITHUB_VERCEL.md) for the step-by-step deployment path. Vercel hosts the React frontend; Render hosts the FastAPI backend and PostgreSQL database.

## Quick start — Docker

1. Install Docker Desktop / Docker Engine with Compose.
2. Copy `.env.example` to `.env`.
3. Set strong, unique `POSTGRES_PASSWORD`, `SECRET_KEY`, and `BOOTSTRAP_ADMIN_PASSWORD`. Do not use the example values.
4. Set `CORS_ORIGINS` to the browser origin you will actually use.
5. Start the stack:

   ```bash
   docker compose up --build -d
   ```

6. Open `http://localhost:8080`. The bootstrap account is the `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` from `.env`.
7. API docs: `http://localhost:8000/docs`; readiness: `http://localhost:8000/health/ready`.

The backend container runs `alembic upgrade head` using the included initial schema migration. The app's lifespan also creates tables for convenient local startup. See `docs/IMPLEMENTATION_STATUS.md` for outstanding production gates.

## Local development

### Backend

```bash
cd backend
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Set `DATABASE_URL`, `SECRET_KEY`, `BOOTSTRAP_ADMIN_EMAIL`, and `BOOTSTRAP_ADMIN_PASSWORD` in `backend/.env` or in the shell environment. SQLite is used by default.

Run backend tests from `backend/`:

```bash
pytest -q
```

### Frontend

```bash
cd frontend
npm install
# optional: set VITE_API_URL=http://localhost:8000/api in frontend/.env.local
npm run dev
```

Build/type-check:

```bash
npm run typecheck
npm run build
```

## First workflow

1. Sign in using the configured bootstrap credentials.
2. Open **Instrument registry** and add explicit mappings, for example `RELIANCE` / `RELIANCE.NS` (NSE) or `RELIANCE` / `RELIANCE.BO` (BSE). Confirm the exact instrument yourself.
3. Click **Verify**. This confirms yfinance returned recent history; it does not independently prove the legal identity of the instrument.
4. Click **Fetch history** to persist provider data.
5. Create a portfolio and add holdings using registered instrument IDs.
6. Open the portfolio snapshot and export CSV when required.

## API overview

- `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me`
- `GET /api/dashboard`
- `GET/POST /api/portfolios`, `GET/PATCH/DELETE /api/portfolios/{id}`
- `POST /api/portfolios/{id}/duplicate`, `GET /api/portfolios/{id}/snapshot`
- `GET/POST /api/portfolios/{id}/holdings`, `PATCH/DELETE /api/portfolios/{id}/holdings/{holding_id}`
- `GET/POST /api/instruments`, `POST /api/instruments/{id}/verify`
- `POST/GET /api/portfolios/{id}/transactions`
- `POST /api/market-data/ingest`, `GET /api/market-data/status`
- `GET /api/portfolios/{id}/export.csv`, `GET /api/portfolios/{id}/export.xlsx`, `GET /api/portfolios/{id}/export.pdf`
- `POST/DELETE /api/portfolios/{id}/share`, `GET /api/shared/{token}`
- `GET /health/live`, `GET /health/ready`

All `/api` endpoints except registration, login, and shared snapshot retrieval require a bearer token.

## Market data policy

The app uses real yfinance responses and does not substitute fabricated prices. The ingestion endpoint currently supports daily, weekly, and monthly history intervals with periods allowlisted in code. It uses retries and persists records by instrument/date so repeated downloads update rather than duplicate rows. The current job runner is synchronous per API request; for high volume, move ingestion to a durable background worker and add a scheduler/queue.

`yfinance` does not guarantee availability, timeliness, uptime, streaming updates, or permission for commercial redistribution. Inspect current provider terms and obtain a licensed provider if the proposed client-facing use requires it. Do not label stored prices “live”. The app labels the data source and retrieval timestamp and discloses that freshness is only a retrieval indicator.

## Calculation definitions and limitations

- **Market value:** sum of `quantity × latest stored close` for all holdings; withheld if any holding has no price.
- **Invested value:** sum of `quantity × stored average cost`; withheld if any holding has no price for consistency with the current snapshot.
- **Unrealized P/L:** market value minus invested value; excludes realized gains/losses, dividends, taxes, cash balances, and complete corporate-action reconciliation.
- **Absolute return %:** unrealized P/L divided by invested value, only when invested value is nonzero and all prices are available.
- Transaction buy cost uses weighted-average cost including buy fees per unit; sells reduce quantity and retain average cost. A full realized-P&L ledger and corporate-action reconciliation are not yet implemented.
- Time-weighted return, money-weighted return, historical portfolio/benchmark return series, risk metrics, and tax lots are not shown as if they were available. They require a complete, validated cash-flow and valuation series.

## Security notes before production

- Use HTTPS and a reverse proxy; do not expose the database port publicly.
- Replace secrets, configure exact CORS origins, use a secret manager, and rotate credentials.
- Add rate limiting / brute-force protection, password reset and MFA if required by your risk policy.
- Decide whether token storage in localStorage meets your threat model; consider secure HttpOnly cookies with CSRF controls.
- Enforce role-based access and tenant/client isolation before multiple client users are introduced.
- Add audit events for portfolio changes, transaction amendments, exports, and share-link creation/revocation.
- Review personal-data retention, encryption, backup/restore, disaster recovery, incident response, dependency scanning, and penetration testing.
- Review Indian financial-services regulations and data licensing obligations with qualified counsel/compliance advisers before commercial use.

## Docs

- `docs/IMPLEMENTATION_STATUS.md` — verified vs outstanding requirements.
- `docs/ARCHITECTURE.md` — components and data flow.
- `docs/FINANCIAL_METHODOLOGY.md` — calculation definitions and constraints.
