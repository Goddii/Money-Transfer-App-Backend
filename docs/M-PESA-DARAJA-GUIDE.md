# M-Pesa / Safaricom Daraja Integration Guide (Beginner-Friendly)

> **Audience:** You know basic programming but have **never** integrated M-Pesa/Daraja.
> **Goal:** Go from "I have never done this" to "my app can initiate, receive, verify, record, and handle M-Pesa payments."
> **Scope:** This explains the **existing** integration in this project (the "Vyloc" wallet backend + React frontend) and teaches the concepts behind it.
> **Security:** All credential values below are **placeholders**. Never paste real keys/secrets into code, logs, or this document.

---

## 0. TL;DR — What this project already has

The M-Pesa deposit flow is **already implemented** in this repository and follows good practice:

- **Backend:** Flask (Python) service that talks to Safaricom Daraja.
- **Flow:** STK Push (Lipa na M-Pesa Online) — the user enters amount + phone, gets a prompt on their phone, enters PIN, and the wallet is credited **only after Safaricom confirms**.
- **Safety:** The app **never trusts the callback's "success" flag**. It always re-checks with Safaricom using server-side credentials before crediting any money.
- **Idempotency:** A duplicate callback or a late recovery can **never** credit the wallet twice (database constraint + row lock + terminal-state guards).

You do **not** need to rebuild the architecture. This guide explains how it works and how to configure, test, and ship it.

> **One advanced note (not a bug):** in `process_callback` the row lock is taken *before* the server-side Daraja check. This is safe (it serialises workers and never double-credits) but means a row lock is briefly held during an external HTTP call. It is a hardening opportunity, not a correctness problem. See §24.

---

# 1. SECURITY RULE (read first)

The backend reads Daraja credentials **only from environment variables**. This project already does this correctly.

| Thing | Where it lives | Never… |
|-------|---------------|--------|
| Consumer Key | backend env var | …hard-code it, log it, or send it to the browser |
| Consumer Secret | backend env var | …put it in frontend JS, commit it, or return it in any API response |
| Passkey | backend env var | …expose it anywhere client-side |
| Access token | fetched at runtime, in memory | …log it or store it in the database |

Rules you must follow:

- Never commit `.env`. The repo's `.gitignore` already ignores `.env`.
- Never hard-code secrets in source code.
- Never put Consumer Secret / Passkey in frontend JavaScript.
- Never expose credentials through API responses.
- Use environment variables / secrets manager (Render, GitHub Actions secrets, etc.).

Example of what your `.env` should contain (placeholders only):

```env
DARAJA_CONSUMER_KEY=<your Daraja consumer key>
DARAJA_CONSUMER_SECRET=<your Daraja consumer secret>
DARAJA_SHORTCODE=<your Daraja shortcode>
DARAJA_PASSKEY=<your Daraja passkey>
DARAJA_CALLBACK_URL=<your public https callback url>
```

---

# 2. AUDIT OF THE EXISTING PROJECT

## 2.1 Backend

| Concern | What this project uses |
|---------|------------------------|
| Language | Python 3 |
| Framework | Flask (`app/__init__.py` → `create_app`) |
| Entry point | `run.py` (calls `create_app`) |
| Config | `app/config.py` (`Config` class reads `os.environ`) |
| Env handling | `os.environ.get(...)` inside `Config` |
| DB | Flask-SQLAlchemy (`app/extensions.py`) |
| Migrations | Flask-Migrate / Alembic (`migrations/`) |
| Auth | Flask-JWT-Extended (`app/utils/decorators.py`) |
| Routes | Blueprints in `app/routes/` |
| Services | `app/services/` (business logic) |
| Models | `app/models/` |
| Validation | `app/schemas/` + `app/utils/validators.py` |
| Errors | `app/utils/errors.py` (`ApiError`) |
| Testing | pytest (`tests/`) |

### Where M-Pesa lives (do not invent a new architecture)

| Layer | File | Responsibility |
|-------|------|----------------|
| Routes | `app/routes/mpesa_routes.py` | HTTP endpoints (initiate, callback, status, admin reconcile) |
| Service | `app/services/mpesa_service.py` | All Daraja/OAuth logic + crediting |
| Wallet | `app/services/wallet_service.py` | Balance change + ledger entry |
| Transaction | `app/services/transaction_service.py` | Internal `Transaction` record |
| Model | `app/models/mpesa_transaction.py` | `MpesaTransaction` table |
| Model | `app/models/wallet_ledger.py` | Immutable ledger (the double-credit guard) |
| Schema | `app/schemas/mpesa_schema.py` | Validate input + parse callback |
| Config | `app/config.py` | Reads all `DARAJA_*` env vars |

This is a clean **routes → service → model** layering. Add M-Pesa code here; do not scatter it.

## 2.2 Frontend

| Concern | What this project uses |
|---------|------------------------|
| Framework | React |
| API client | `src/utils/api.js` (axios instance) |
| Base URL | `VITE_API_URL` or default `http://localhost:5000/api` (note: already ends in `/api`) |
| Deposit page | `src/pages/Deposit/Deposit.jsx` |
| Auth | JWT stored in `localStorage`, attached by axios interceptor |
| Status polling | `Deposit.jsx` polls `GET /mpesa/transactions/<id>` every 5s |

### How frontend ↔ backend ↔ Safaricom communicate

```
Browser (React)
   │  POST /api/mpesa/stk-push   (carries user JWT)
   ▼
Your Backend (Flask)
   │  OAuth + STK Push request (uses CONSUMER KEY/SECRET + PASSKEY)
   ▼
Safaricom Daraja
   │  phone prompt → user enters PIN
   ▼
Safaricom
   │  POST /api/mpesa/callback   (server-to-server, NO browser, NO auth)
   ▼
Your Backend
   │  reconciles with Daraja, then credits wallet
   ▼
Database (wallet + ledger + transaction)

Frontend polls GET /api/mpesa/transactions/<id> to learn the result.
```

**The frontend never talks to Safaricom directly and never sees Daraja secrets.** All M-Pesa crypto happens on the backend.

---

# 3. WHAT KIND OF M-PESA INTEGRATION THIS PROJECT NEEDS

This is a **wallet top-up / deposit** feature: the app **collects money** from users into their in-app wallet.

| Daraja API | Needed? | Why |
|-----------|---------|-----|
| OAuth / Authorization | ✅ Yes | Every Daraja call needs a bearer token |
| Lipa na M-Pesa Online (STK Push) | ✅ Yes | The primary way a user pays from their phone |
| STK Push Query (status) | ✅ Yes | Server-side reconciliation (proves payment really happened) |
| C2B / B2C / Transaction Status / Account Balance / Reversal | ❌ No | Not needed for wallet deposits in this MVP |

So: **STK Push is the primary path**, and the **STK Query** is used for reconciliation/verification. The app does **not** send money out (no B2C), so those APIs are out of scope.

---

# 4. M-PESA & DARAJA FROM ZERO

## What is M-Pesa?

M-Pesa is Safaricom's mobile-money service in Kenya. A user holds money in a phone-linked wallet. They can send, receive, and pay with it. When your app wants to accept a payment from a user, you use M-Pesa.

## What is Daraja?

**Daraja** is Safaricom's official developer platform — the API gateway that lets your backend talk to M-Pesa programmatically. Think of it as "the door" between your code and Safaricom's payment system.

Official portal (authoritative reference): **https://developer.safaricom.co.ke/**

From the portal you can:
- create a developer account,
- create "applications" that hold your API credentials,
- read API documentation,
- use the **sandbox** (a safe test environment).

> Daraja's UI changes over time. Button labels/positions may differ slightly from old tutorials. Always trust the current portal over blog posts.

---

# 5. THE COMPLETE PAYMENT FLOW (STK Push)

```
User
 │  enters amount + phone number
 ▼
Frontend (React)
 │  POST /api/mpesa/stk-push  (with JWT)
 ▼
Backend (Flask)
 │  1) validates amount/phone
 │  2) gets Daraja access token (OAuth)
 │  3) sends STK Push request
 ▼
Safaricom Daraja
 │  responds: "request accepted" (NOT "paid")
 │  sends prompt to user's phone
 ▼
User's phone
 │  shows "Pay KES X to Vyloc?" → user enters M-Pesa PIN
 ▼
Safaricom
 │  processes the payment
 │  POSTs a callback to your backend
 ▼
Backend callback (POST /api/mpesa/callback)
 │  re-checks with Safaricom (STK Query) to be sure
 │  if confirmed: credit wallet + write ledger + write Transaction
 │  if failed/inconclusive: mark FAILED or RECONCILIATION_PENDING
 ▼
Database
 │  MpesaTransaction = Completed, wallet balance increased
 ▼
Frontend (polling)
 │  GET /api/mpesa/transactions/<id> → status Completed
 │  shows "KSh X added to your wallet"
 ▼
User sees success
```

**Every arrow explained:**
- The **frontend** only starts the request; it cannot confirm payment.
- The **backend** obtains a token, then asks Safaricom to prompt the user.
- **"Request accepted" ≠ "paid."** The user can still cancel or have insufficient funds.
- Safaricom later calls your **callback** with the real outcome.
- Your backend **reconciles** (asks Safaricom again) before trusting the callback, then updates the DB.
- The frontend learns the result by **polling** its own backend.

---

# 6. SETTING UP DARAJA

### Step 1 — Create a Daraja account
Go to **https://developer.safaricom.co.ke/** and sign up (individual or company as applicable). Use your own account — do not share someone else's.

### Step 2 — Log in
Log in with the credentials you created. You'll land on the developer portal.

### Step 3 — Find the application/API area
Daraja groups things into **Applications** and **APIs**. You are looking for:
- a place to **create an app**, and
- the **Lipa na M-Pesa Online** / STK Push API documentation.

Because the UI changes, look for "My Apps", "Create App", or the API **catalogue**. The exact text may differ.

### Step 4 — Create a sandbox application
- Sandbox = a free, safe test environment with **test money** (no real shillings move).
- Create an app; Daraja gives you a **Consumer Key** and **Consumer Secret**.
- For STK Push you also need a **Shortcode** and a **Passkey** (sandbox values).

You will end up with four important values (see §7). Start in **sandbox**; only move to production after Daraja approves go-live.

---

# 7. EVERY CREDENTIAL EXPLAINED

| Credential | What it means | Where it comes from | Where it belongs |
|-----------|---------------|---------------------|------------------|
| Consumer Key | Public-ish app ID for Daraja OAuth | Daraja app | Backend env `DARAJA_CONSUMER_KEY` |
| Consumer Secret | The secret half of OAuth | Daraja app | Backend env `DARAJA_CONSUMER_SECRET` |
| Shortcode | Your Daraja "till/paybill" number | Daraja/sandbox config | Backend env `DARAJA_SHORTCODE` |
| Passkey | Shared secret used to build the STK password | Daraja app | Backend env `DARAJA_PASSKEY` |
| Callback URL | Public URL Safaricom calls with results | Your deployed backend | Daraja config `DARAJA_CALLBACK_URL` |

- **Consumer Key/Secret + Passkey are secrets.** Only the backend sees them.
- **Shortcode** is not secret but identifies your merchant.
- **Callback URL** is a public HTTPS endpoint you control.

---

# 8. MAPPING DARAJA VALUES INTO THIS PROJECT

This project already reads these env vars in `app/config.py`:

```env
DARAJA_ENV=sandbox
DARAJA_CONSUMER_KEY=<your-key>
DARAJA_CONSUMER_SECRET=<your-secret>
DARAJA_SHORTCODE=<your-shortcode>
DARAJA_PASSKEY=<your-passkey>
DARAJA_CALLBACK_URL=https://your-backend.example.com/api/mpesa/callback
DARAJA_TRANSACTION_TYPE=CustomerPayBillOnline
# DARAJA_BASE_URL is derived automatically from DARAJA_ENV
DARAJA_TIMEOUT=30
# Optional: DARAJA_CALLBACK_ALLOWED_IPS=1.2.3.4,5.6.7.8
```

Flow of a credential:

```
Daraja value
   ▼  (you paste into .env)
Environment variable
   ▼  (Config reads it)
app.config["DARAJA_CONSUMER_KEY"]  (app/config.py)
   ▼  (service reads current_app.config)
MpesaService.get_access_token() / send_stk_push()
   ▼  (HTTP)
Safaricom Daraja
```

`app/config.py` maps each variable:
- `DARAJA_CONSUMER_KEY` → `Config.DARAJA_CONSUMER_KEY`
- `DARAJA_CONSUMER_SECRET` → `Config.DARAJA_CONSUMER_SECRET`
- `DARAJA_SHORTCODE` → `Config.DARAJA_SHORTCODE`
- `DARAJA_PASSKEY` → `Config.DARAJA_PASSKEY`
- `DARAJA_CALLBACK_URL` → `Config.DARAJA_CALLBACK_URL`
- `DARAJA_BASE_URL` → chosen by `DARAJA_ENV` (`sandbox.safaricom.co.ke` vs `api.safaricom.co.ke`)
- `DARAJA_TRANSACTION_TYPE` → `CustomerPayBillOnline` (default)

---

# 9. LOCAL DEV & CALLBACK URLS (critical for beginners)

Safaricom calls your **callback URL over the public internet**. It **cannot** reach:

```
http://localhost:5000/api/mpesa/callback      ❌ not reachable by Safaricom
http://127.0.0.1:5000/api/mpesa/callback      ❌ same machine only
```

Three different URLs — do not confuse them:

| URL | Used by | Example |
|-----|---------|---------|
| Frontend URL | the browser | `http://localhost:5173` |
| Backend API URL | the frontend's axios calls | `http://localhost:5000/api` |
| M-Pesa callback URL | Safaricom (server-to-server) | `https://your-backend.example.com/api/mpesa/callback` |

For local testing you need the callback to be publicly reachable. Options:
- Deploy a dev backend (e.g., Render) and use its HTTPS URL as `DARAJA_CALLBACK_URL`.
- Use a secure public tunnel (e.g., a temporary HTTPS tunnel) that forwards to your localhost backend — only for sandbox testing.
- Use your real production HTTPS endpoint (production only).

**Never** use a real customer's phone in sandbox; use the sandbox test numbers Daraja provides.

---

# 10. OAUTH (ACCESS TOKENS)

## Why the backend needs a token
Daraja requires every API call to carry a **bearer access token** proving your app is authorised. You get it by exchanging your Consumer Key + Secret.

## How the token is requested
`MpesaService.get_access_token()` (in `app/services/mpesa_service.py`):

1. Reads `DARAJA_CONSUMER_KEY` / `DARAJA_CONSUMER_SECRET` from config.
2. `GET {DARAJA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials` with HTTP Basic auth (key:secret).
3. Daraja returns `{ "access_token": "...", "expires_in": 3599 }`.
4. The function returns the token string.

## Expiry & caching
Sandbox tokens expire in ~1 hour. This project **requests a fresh token per Daraja call** (simple and correct; a production optimisation would cache it). Never log the token.

## Error handling
If the request fails or returns no token, it raises `ApiError` (502) — the caller shows "Could not reach M-Pesa."

---

# 11. STK PUSH

## Purpose
Ask Safaricom to send a payment prompt to the user's phone.

`MpesaService.send_stk_push(amount, phone, account_reference)`:

| Field | Meaning |
|-------|---------|
| BusinessShortCode | Your shortcode |
| Password | `Base64(Shortcode + Passkey + Timestamp)` |
| Timestamp | `YYYYMMDDHHMMSS` UTC, must match the password |
| TransactionType | `CustomerPayBillOnline` |
| Amount | whole number, in KES |
| PartyA | the user's phone (payer) |
| PartyB | your shortcode |
| PhoneNumber | the user's phone |
| CallBackURL | where Safaricom posts results |
| AccountReference | your reference (≤12 alphanumeric) |
| TransactionDesc | human description |

### Password formula (conceptual — never paste a real passkey)
```
Password = Base64( Shortcode + Passkey + Timestamp )
```
The timestamp must be the **same** one used in the request, or Daraja rejects it.

### Phone formatting
Kenyan numbers are normalised to `2547XXXXXXXX` (no leading `0`, with `254` country code). The project does this in `normalize_kenyan_phone` (validator).

### Amount rules
STK Push only accepts **whole-number KES** (`validate_money_amount` + integer check).

### What Daraja returns immediately
A JSON like `{ "ResponseCode": "0", "CheckoutRequestID": "ws_CO_...", "MerchantRequestID": "..." }`.
**`ResponseCode: "0"` means "request accepted" — NOT that the user paid.** The actual result comes later via the callback (and your reconciliation query).

---

# 12. THE CALLBACK

## What a callback is
After the user acts on the phone prompt, Safaricom **POSTs** a JSON body to your `DARAJA_CALLBACK_URL`. Your backend must accept it, verify it, and update the DB.

### Example callback structure (sanitised, fictional)
```json
{
  "Body": {
    "stkCallback": {
      "MerchantRequestID": "29115-34620561-1",
      "CheckoutRequestID": "ws_CO_1_20260823",
      "ResultCode": 0,
      "ResultDesc": "The service request is processed successfully.",
      "CallbackMetadata": {
        "Item": [
          { "Name": "Amount", "Value": 500 },
          { "Name": "MpesaReceiptNumber", "Value": "QK12AB34CD" },
          { "Name": "TransactionDate", "Value": 20260823010203 },
          { "Name": "PhoneNumber", "Value": 254712345678 }
        ]
      }
    }
  }
}
```

| Field | Meaning |
|-------|---------|
| ResultCode | `0` = success; non-zero = failure/cancel |
| ResultDesc | human text |
| CheckoutRequestID | links callback to the original request (unique per deposit) |
| MerchantRequestID | Daraja correlation id |
| Amount / Receipt / Phone | details (never trusted for crediting) |

## How the backend processes it
`MpesaService.process_callback(parsed_callback)` in `mpesa_service.py`:
1. Looks up the deposit by `CheckoutRequestID` (the row created at STK push).
2. If already terminal (`Completed`/`Failed`) → ignores (idempotency).
3. **Reconciles with Daraja** via `query_stk_status` (server-side, authenticated) — this is the authoritative check.
4. If Daraja says success (`ResultCode 0`) → credit the wallet.
5. If Daraja says definitive failure (e.g., `1032` user cancelled) → `Failed`.
6. If Daraja is inconclusive/unreachable → `RECONCILIATION_PENDING` (recoverable, never a silent failure).

**Key safety rule:** the callback's own `ResultCode` is **attacker-controlled** and is **never** used to credit money. Only the server-side `query_stk_status` result decides.

---

# 13. DATABASE DESIGN (what already exists)

The project already has these tables (do not duplicate them):

| Table | File | Holds |
|-------|------|-------|
| `mpesa_transactions` | `app/models/mpesa_transaction.py` | one row per deposit attempt |
| `wallets` | `app/models/wallet.py` | user balance |
| `wallet_ledger` | `app/models/wallet_ledger.py` | immutable record of every balance change |
| `transactions` | `app/models/transaction.py` | internal business transaction |

`MpesaTransaction` columns worth knowing:

| Column | Purpose |
|--------|---------|
| `id` | internal primary key (what the frontend polls) |
| `user_id`, `wallet_id` | who/where the money goes |
| `checkout_request_id` | Daraja id, **unique** — links callback to the row |
| `merchant_request_id` | Daraja correlation id |
| `amount` | requested amount (the amount actually credited) |
| `status` | `Pending` / `ReconciliationPending` / `Completed` / `Failed` |
| `mpesa_receipt_number` | set after success (unique) |
| `result_code` / `result_desc` | from callback (not trusted) |
| `query_result_code` / `query_result_desc` | from server-side reconciliation |
| `reconciliation_attempts`, `last_reconciled_at` | observability |
| `failure_reason` | why FAILED / why stuck in RECONCILIATION_PENDING |
| `created_at`, `updated_at` | timestamps |

`wallet_ledger` has the **critical guard**: a unique constraint
`unique_wallet_ledger_reference` on `(wallet_id, entry_type, reference)`.
Since the reference is the `checkout_request_id`, a second credit for the same deposit is **physically impossible** at the database level.

---

# 14. TRANSACTION STATES

| State | Meaning | Credited? |
|-------|---------|-----------|
| `Pending` | STK request accepted, awaiting outcome | No |
| `ReconciliationPending` | Callback/query inconclusive; keep trying later | No |
| `Completed` | Daraja confirmed payment; wallet credited | Yes |
| `Failed` | Definitively cancelled/failed (e.g., user cancelled) | No |

**A deposit is NEVER marked `Completed` just because the STK request was accepted.** It becomes `Completed` only after Daraja confirms via reconciliation.

---

# 15. IDEMPOTENCY (why duplicates must not double-credit)

A callback can arrive **more than once** (Safaricom retries; or your recovery job runs). Without protection, the wallet could be credited twice.

This project prevents it with **three layers**:

1. **Unique `checkout_request_id`** on `mpesa_transactions` → one row per deposit.
2. **Terminal-state guards**: `process_callback` and `_credit_confirmed_deposit` refuse to credit a row already `Completed`/`Failed`.
3. **Row lock** (`SELECT … FOR UPDATE` + `populate_existing`) so two workers can't both pass the guard; and the **`unique_wallet_ledger_reference`** DB constraint blocks a second ledger CREDIT for the same `(wallet_id, checkout_request_id)`.

Both callback and recovery use the **same** reference (`checkout_request_id`), so the constraint catches either path.

---

# 16. WALLET / BUSINESS LOGIC AFTER SUCCESS

When Daraja confirms:

```
_credit_confirmed_deposit()
   │  re-lock row, confirm not terminal
   │  reference = checkout_request_id
   ▼
TransactionService.record_deposit(user, wallet, amount, reference)
   │  creates internal Transaction (DEPOSIT)
   │  WalletService.credit(wallet, amount, reference)
   │     └─ updates wallet.balance
   │     └─ inserts ONE wallet_ledger CREDIT row (reference = checkout_request_id)
   ▼
MpesaTransaction.status = Completed ; links transaction
   ▼
db.session.commit()   (all-or-nothing)
```

**Distinction:**
- `MpesaTransaction` = "what Safaricom said about this deposit."
- `Transaction` = "the internal business record (a wallet deposit)."
- `wallet_ledger` = "the immutable audit trail of the balance change."

All three are written in **one** database transaction, so they can't partially succeed.

The credited amount is **always** the stored `amount`, never the callback's reported amount.

---

# 17. RECONCILIATION / VERIFICATION

Relying only on the initial STK response or the callback is unsafe:
- The callback is unauthenticated and attacker-controlled.
- A callback might never arrive; or arrive late; or be inconclusive.

So the backend **always** calls `MpesaService.query_stk_status(checkout_request_id)` (the STK Query API) using its own credentials, and uses **that** result to decide success/failure. If the query is inconclusive or Daraja is unreachable, the deposit goes to `RECONCILIATION_PENDING` and a later callback or the **recovery sweep** (`recover_deposits`) retries it.

Recovery (`POST /api/mpesa/admin/reconcile` or `recover_deposits()`):
- Finds all `Pending` / `RECONCILIATION_PENDING` rows.
- For each: queries Daraja first (**no lock held during the HTTP call**), then locks the row, re-checks status, and credits only if confirmed.
- Processes each row in its own transaction, so one bad row doesn't abort the sweep.

Reference the current Daraja docs for the STK Query API: **https://developer.safaricom.co.ke/** → API catalogue.

---

# 18. BACKEND ENDPOINTS

| Endpoint | Method | Purpose | Called by |
|----------|--------|---------|-----------|
| `/api/mpesa/stk-push` | POST | Initiate deposit (validates, sends STK) | Frontend (JWT) |
| `/api/mpesa/transactions/<id>` | GET | Poll deposit status (owner-scoped) | Frontend (JWT) |
| `/api/mpesa/callback` | POST | Receive Daraja callback | Safaricom (no auth) |
| `/api/mpesa/admin/reconcile` | POST | Run recovery sweep | Admin (JWT, admin role) |

All under the `mpesa_bp` blueprint registered at `/api/mpesa` in `app/__init__.py`.

---

# 19. FRONTEND IMPLEMENTATION

`src/pages/Deposit/Deposit.jsx` already implements the full flow:

1. User enters amount + phone.
2. `handleDeposit` validates (whole number, valid phone) and `POST /mpesa/stk-push`.
3. On success, stores `depositId` and shows "Waiting for M-Pesa confirmation…".
4. A `useEffect` polls `GET /mpesa/transactions/${depositId}` every 5s.
5. On `Completed` → shows success, refreshes wallet + transactions, stops polling.
6. On `Failed` → shows error, stops polling.
7. On `ReconciliationPending` → keeps polling ("Confirming your payment…").
8. Cleanup clears the interval on unmount.

The axios client (`src/utils/api.js`) already has `/api` in its baseURL, so `api.get('/mpesa/transactions/<id>')` correctly becomes `/api/mpesa/transactions/<id>` (no double `/api`).

**Frontend must never:**
- contain Daraja keys/passkey,
- decide success from a callback it receives (it only reads its own backend's status),
- optimistically mark success before `Completed`.

---

# 20. TESTING IN SANDBOX

1. Start the backend (`flask run` / `gunicorn`) with `.env` set (sandbox values).
2. Start the frontend (`npm run dev`).
3. Confirm `DARAJA_*` env vars are present (backend logs only names, never values).
4. Confirm `DARAJA_CALLBACK_URL` is a publicly reachable HTTPS URL pointing at your backend.
5. Confirm the database is migrated (`flask db upgrade`).
6. Log in / register on the frontend.
7. Open Deposit, enter a whole amount (e.g., `500`) and a sandbox phone.
8. Submit → backend returns the deposit `id`; phone receives the prompt.
9. Enter the sandbox M-Pesa PIN.
10. Wait a few seconds for the callback.
11. Inspect backend logs: you should see token fetch, STK push, callback, reconciliation.
12. Inspect DB: `mpesa_transactions` row = `Completed`; `wallets.balance` increased; `wallet_ledger` has one CREDIT; `transactions` has one DEPOSIT.
13. Frontend shows "KSh 500.00 added to your wallet."

Test the negative paths too: cancel the prompt (expect `Failed`); use a wrong amount in callback simulation if you control the sandbox (expect `RECONCILIATION_PENDING`).

---

# 21. DEBUGGING

### OAuth failure
- Wrong Consumer Key / Secret.
- Wrong `DARAJA_ENV` (sandbox key used against production URL, or vice-versa).
- Typo / trailing spaces in env values.
- Fix: double-check env vars; confirm `DARAJA_BASE_URL` matches the env.

### STK Push not appearing
- Wrong phone format (must normalise to `2547…`).
- Wrong shortcode / passkey.
- Timestamp not matching the password.
- API request error (check backend logs / 502 response).
- Fix: verify phone normalisation and that `ResponseCode` is `"0"`.

### Callback never arrives
- `DARAJA_CALLBACK_URL` is `localhost` / `127.0.0.1` (Safaricom can't reach it).
- Not HTTPS where required.
- Firewall / tunnel down.
- Route mismatch (must be `POST /api/mpesa/callback`).
- Fix: use a public HTTPS URL; verify the route exists; check tunnel logs.

### Callback arrives but wallet not updated
- Callback parsing bug (check `parse_stk_callback`).
- `ResultCode` handling — remember the backend reconciles via `query_stk_status`, not the callback flag.
- DB transaction failure (check logs for `IntegrityError`/rollback).
- Wrong `checkout_request_id` lookup.
- Idempotency already applied (row already `Completed` → that's correct!).
- Fix: trace `process_callback` → `query_stk_status` → `_credit_confirmed_deposit`.

### Payment succeeds but frontend says pending/failed
Trace: Frontend polls `GET /mpesa/transactions/<id>` → backend reads `MpesaTransaction.status` → if stuck in `RECONCILIATION_PENDING`, the recovery sweep (`admin/reconcile`) will finish it. Confirm the callback actually reached the backend and reconciliation succeeded.

---

# 22. SECURITY CHECKLIST

- [x] Secrets are in environment variables (`app/config.py`).
- [x] `.env` is git-ignored.
- [x] No Daraja secrets in frontend (`src/utils/api.js` has no keys).
- [x] No secrets in logs (config logs only variable *names*).
- [ ] Use HTTPS for production callback + API.
- [x] Callback endpoint is unauthenticated by design but **reconciles server-side** (the real protection).
- [x] Callback data is validated (`parse_stk_callback`).
- [x] Duplicate callbacks cannot double-credit (unique constraint + locks + terminal guards).
- [x] DB updates are atomic (one `commit` for balance + ledger + transaction).
- [x] Payment status is verified via `query_stk_status` before crediting.
- [ ] Keep sandbox and production credentials separate.
- [x] Sensitive data is not exposed in API responses (`to_status_dict` masks phone, omits secrets).

---

# 23. SANDBOX vs PRODUCTION

| Area | Sandbox | Production |
|------|---------|-----------|
| Purpose | Testing | Real money |
| Money | Test/sandbox | Real KES |
| Credentials | Sandbox app | Production app (after go-live) |
| Shortcode | Sandcode (e.g., test shortcode) | Your approved shortcode |
| Callback | Test/public dev URL | Production HTTPS URL |
| Risk | Low | Real financial consequences |

Sandbox credentials **cannot** simply be swapped into production — you must complete Daraja's go-live process for a production app.

---

# 24. GO-LIVE GUIDE

1. Complete Daraja **go-live** for a production app (follow the current portal process).
2. Put **production** Consumer Key/Secret, Shortcode, Passkey in production env vars.
3. Set `DARAJA_ENV=production` (this switches `DARAJA_BASE_URL` to `api.safaricom.co.ke`).
4. Set `DARAJA_CALLBACK_URL` to your production HTTPS endpoint.
5. Ensure HTTPS everywhere; restrict `DARAJA_CALLBACK_ALLOWED_IPS` if you can (defence-in-depth only).
6. Deploy backend + frontend; run `flask db upgrade`.
7. Add monitoring/logging; alert on reconciliation failures.
8. Test with a **small real** amount in production before enabling large limits.
9. Keep reconciliation + recovery enabled so `RECONCILIATION_PENDING` rows get resolved.

**Optional hardening (not required):** in `process_callback`, consider releasing the row lock before calling `query_stk_status`, then re-locking just before crediting (mirroring `recover_deposits`). This avoids holding a DB row lock across an external HTTP call. It is a performance/availability improvement, not a correctness fix.

---

# 25. OFFICIAL DOCUMENTATION

Use the current Daraja portal as the source of truth:

- Portal / account: **https://developer.safaricom.co.ke/**
- Authorization / OAuth: see the "Authorization" API in the Daraja API catalogue.
- Lipa na M-Pesa Online (STK Push): API catalogue → "Lipa na M-Pesa Online".
- STK Query / Transaction Status: API catalogue (used by `query_stk_status`).
- Go-live: Daraja portal go-live / docs section.

If a URL changes, open the portal and use the **API catalogue** to locate the same API — do not trust outdated blog URLs.

---

# 26. CODE IMPLEMENTATION (existing, explained)

You normally do **not** need to write these — they exist. Use this section to understand them.

### `app/config.py`
- **Purpose:** read all `DARAJA_*` env vars; choose base URL by env.
- **Why here:** single source of config.

### `app/services/mpesa_service.py` — `MpesaService`
Static methods (no instance needed):

- `get_access_token()` — OAuth (§10).
- `send_stk_push(amount, phone, account_reference)` — STK request (§11).
- `query_stk_status(checkout_request_id)` — server-side reconciliation (§17). Returns Daraja JSON.
- `initiate_deposit(user, amount, phone)` — validates, calls STK, creates a `Pending` `MpesaTransaction` (Daraja called **first**, then DB write).
- `process_callback(parsed_callback)` — callback handler (§12). Locks row, reconciles, credits or marks terminal.
- `_lock_mpesa_transaction(id)` — `SELECT … FOR UPDATE` + `populate_existing()` (fresh, locked row).
- `_record_reconciliation_attempt(...)` — stores observability fields.
- `_credit_confirmed_deposit(mpesa_transaction, callback_amount, receipt_number)` — the **single crediting path**; re-locks, checks terminal, checks amount mismatch, uses `checkout_request_id` as reference, writes ledger+transaction+balance in one commit.
- `_recoverable_candidates()` — snapshot of `Pending`/`ReconciliationPending` rows, releases read txn.
- `_recover_one(id, checkout_request_id)` — Daraja query **first** (no lock), then lock, re-check, credit or skip.
- `recover_deposits()` — sweeps candidates, isolates per-row failures.

### `app/models/wallet_ledger.py`
- The `unique_wallet_ledger_reference` constraint is the DB-level double-credit backstop.

### `app/routes/mpesa_routes.py`
- Wires the four endpoints; callback is unauthenticated but reconciles; status is owner-scoped; reconcile is admin-only.

---

# 27. IMPORTANT FUNCTIONS (beginner view)

### `get_access_token()`
- **Why:** Daraja needs a bearer token.
- **In:** nothing (reads config). **Out:** token string.
- **External:** `GET /oauth/v1/generate`. **Errors:** 502 on failure.
- **Security:** token never logged/stored.

### `send_stk_push(...)`
- **Why:** ask Safaricom to prompt the user.
- **In:** amount, phone, account_reference. **Out:** Daraja response.
- **External:** `POST /mpesa/stkpush/v1/processrequest`.
- **Security:** password = `Base64(shortcode+passkey+timestamp)`.

### `query_stk_status(checkout_request_id)`
- **Why:** authoritative confirmation of payment.
- **Out:** Daraja query JSON. **Security:** uses backend credentials only.

### `process_callback(parsed_callback)`
- **Why:** handle Safaricom's result.
- **In:** parsed callback. **DB:** row update + (if confirmed) wallet/ledger/transaction.
- **Out:** the `MpesaTransaction`. **Errors:** network → `RECONCILIATION_PENDING`.
- **Security:** never trusts callback `ResultCode`.

### `_credit_confirmed_deposit(...)`
- **Why:** the one place money moves.
- **DB:** one commit for balance+ledger+transaction+status.
- **Errors:** terminal/amount-mismatch → no credit; `IntegrityError` → already credited.
- **Security:** credits stored amount, uses canonical reference.

---

# 28. END-TO-END TRACE (fictional example)

```
User wants to deposit KES 500
   ▼ Frontend: POST /api/mpesa/stk-push {amount:500, phone:"0712345678"}
   ▼ Backend: validates; normalises phone to 254712345678
   ▼ Backend: get_access_token() → token
   ▼ Backend: send_stk_push → Daraja accepts (ResponseCode 0)
   ▼ Backend: creates MpesaTransaction(Pending, checkout_request_id=ws_CO_1)
   ▼ Daraja: phone shows "Pay KES 500 to Vyloc"
   ▼ User enters PIN
   ▼ Daraja: processes payment, POSTs callback to /api/mpesa/callback
   ▼ Backend: process_callback locks row, calls query_stk_status
   ▼ Daraja query: ResultCode 0 → confirmed
   ▼ Backend: _credit_confirmed_deposit
       - reference = ws_CO_1
       - record_deposit → Transaction(DEPOSIT)
       - WalletService.credit → balance +500, ledger CREDIT(ref=ws_CO_1)
       - MpesaTransaction.status = Completed
       - commit
   ▼ Frontend (polling): GET /api/mpesa/transactions/<id> → Completed
   ▼ Frontend: "KSh 500.00 added to your wallet"
```

No step credits money without Daraja confirmation, and a duplicate callback is ignored.

---

# 29. "DO THIS IN ORDER" CHECKLIST

- [ ] Understand the API: STK Push + STK Query (this project collects money only).
- [ ] Create a Daraja account at https://developer.safaricom.co.ke/
- [ ] Log in; create a **sandbox** application.
- [ ] Copy Consumer Key, Consumer Secret, Shortcode, Passkey (placeholders).
- [ ] Add `DARAJA_*` vars to backend `.env` (never commit it).
- [ ] Confirm `.env` is git-ignored.
- [ ] Set `DARAJA_CALLBACK_URL` to a **public HTTPS** URL.
- [ ] Understand OAuth (`get_access_token`).
- [ ] Understand STK Push (`send_stk_push`) and that "accepted ≠ paid".
- [ ] Understand the callback (`process_callback`) and reconciliation.
- [ ] Understand idempotency (unique constraint + locks + terminal guards).
- [ ] Run `flask db upgrade`.
- [ ] Start backend + frontend.
- [ ] Initiate a sandbox payment; complete it on the phone.
- [ ] Confirm DB: `Completed`, balance +amount, one ledger CREDIT, one Transaction.
- [ ] Test a cancelled payment (expect `Failed`).
- [ ] Test a duplicate callback (expect single credit).
- [ ] Run the recovery sweep for stuck rows (`admin/reconcile`).
- [ ] Review the security checklist (§22).
- [ ] Prepare production env vars (separate from sandbox).
- [ ] Complete Daraja go-live; deploy; test a small real amount.

---

# 30. IS THE EXISTING PROJECT CORRECT?

A full independent review was performed. Findings:

- **No insecure credential handling:** secrets only in env; not in frontend, logs, or responses. ✅
- **No outdated endpoints:** uses current Daraja paths (`/oauth/v1/generate`, `/mpesa/stkpush/v1/processrequest`, `/mpesa/stkpushquery/v1/query`). ✅
- **Callback handling present and safe:** reconciles server-side; never trusts callback `ResultCode`. ✅
- **Transaction states correct:** `Pending`/`ReconciliationPending`/`Completed`/`Failed`; not marked paid on STK accept. ✅
- **Idempotency present:** unique `checkout_request_id`, terminal guards, row lock, and `unique_wallet_ledger_reference`. ✅
- **Wallet/ledger updates atomic:** one commit for balance + ledger + transaction. ✅
- **Phone formatting & amount rules** enforced in `validate_stk_push`. ✅
- **Frontend/backend match:** `/api` base URL correct; polling/status correct. ✅

**No blocking problems found.** The only non-blocking note is the optional callback-lock-during-HTTP hardening in §24. No code changes are required to follow this guide; the existing implementation is the reference.

---

# 31. FINAL NOTES

You now understand:
- what M-Pesa/Daraja are,
- how to get credentials and configure them,
- why the callback URL must be public,
- how OAuth, STK Push, callbacks, and reconciliation work,
- how this project's backend and frontend implement the flow,
- how idempotency and atomicity prevent money bugs,
- how to test in sandbox and go live safely.

Keep the official Daraja portal as your source of truth, never expose secrets, and always verify payments server-side before crediting.
