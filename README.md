# Mule Account Catcher at Mandate Registration

This project is a merchant-layer risk-control system for UPI Autopay mandate registration. It detects merchant-visible risk at registration, challenges before redirect when possible, and requests immediate post-confirmation revocation. It does not guarantee prevention of every debit or identify a person as a mule account.

> **Note on Test Mode:** This demonstration uses Razorpay Test Mode with test API keys. It does not process real money or integrate with live banking systems.

## Prerequisites

- **Docker Desktop**
- **Python 3.12**
- **uv** package manager
- **Node 22** and **npm**

## Setup and Commands

1. **Environment Config:** Create your local `.env` file (use PowerShell):
   ```powershell
   Copy-Item .env.example .env
   ```
2. **Bootstrap (Install Dependencies):**
   ```powershell
   uv --directory apps/api sync --all-groups
   npm --prefix apps/web install
   ```
3. **Run Backing Services:**
   ```powershell
   docker compose up -d
   ```
4. **Start API:**
   ```powershell
   uv --directory apps/api run uvicorn app.main:app --reload --port 8000
   ```
5. **Start Web App:**
   ```powershell
   npm --prefix apps/web run dev
   ```

### Other Make Targets (PowerShell equivalents)

- **Contracts Generation:**
  ```powershell
  uv --directory apps/api run python ../../scripts/export_openapi.py
  npm --prefix apps/web run generate:api
  ```
- **Tests:**
  ```powershell
  uv --directory apps/api run pytest -q
  ```
- **Linting:**
  ```powershell
  uv --directory apps/api run ruff check app tests
  uv --directory apps/api run ruff format --check app tests
  uv --directory apps/api run mypy app
  npm --prefix apps/web run lint
  ```

## Local URLs

- **API Documentation:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **API Health Check:** [http://localhost:8000/healthz](http://localhost:8000/healthz)
- **Web App:** [http://localhost:3000](http://localhost:3000)

## Repository Structure & Ownership

Please see [`PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md`](PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md) and [`docs/work-packages/A0_FOUNDATION_AND_CONTRACTS.md`](docs/work-packages/A0_FOUNDATION_AND_CONTRACTS.md) for detailed architecture and parallel delivery plans.

- `apps/api/app/contracts/` - A0: Canonical Pydantic contracts
- `apps/api/app/api/routes/` - A2/A3/A8/A9: API Routes
- `apps/api/app/domain/rules/` - A5: Deterministic rule engine
- `apps/api/app/repositories/` - A1: Database persistence
- `apps/api/app/services/` - A2/A4/A5: Core services
- `apps/api/app/integrations/` - A3/A6/A12: External integrations
- `apps/api/app/workers/` - A6/A12: Async workers
- `apps/web/src/` - A7/A8: Frontend application
- `data/fixtures/` - A0: Contract JSON fixtures
- `infra/` - A6/A10/A12: Infrastructure configurations
