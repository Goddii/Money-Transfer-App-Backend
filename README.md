# Vyloc — Backend

Vyloc is a secure, mobile-friendly digital wallet and money transfer platform developed as a Moringa School capstone project.

This repository contains the **Flask REST API backend** for Vyloc. It provides authentication, wallet management, peer-to-peer transfers, beneficiary management, transaction history, administrative analytics, and M-Pesa integration through Safaricom Daraja.

---

## Table of Contents

- [Features](#features)
- [Technology Stack](#technology-stack)
- [Backend Architecture](#backend-architecture)
- [Database](#database)
- [API Structure](#api-structure)
- [M-Pesa Integration](#m-pesa-integration)
- [Internal Money Transfer](#internal-money-transfer)
- [Security](#security)
- [Environment Variables](#environment-variables)
- [Installation](#installation)
- [Database Setup](#database-setup)
- [Running the Backend](#running-the-backend)
- [Testing](#testing)
- [API Response Convention](#api-response-convention)
- [Project Scope](#project-scope)
- [Future Features](#future-features)
- [Development Guidelines](#development-guidelines)
- [Development Workflow](#development-workflow)
- [Team](#team)
- [Project Status](#project-status)
- [License](#license)

---

## Features

### User Features

- User registration and authentication
- JWT-based authorization
- User profile management
- Automatic wallet creation
- Wallet balance management
- Add funds through M-Pesa Daraja
- Beneficiary management
- Peer-to-peer money transfers
- Transaction history and details
- Wallet and transaction analytics

### Admin Features

- Admin authentication and authorization
- View and manage users
- View user wallets
- View platform transactions
- Monitor transaction volumes
- View transaction statistics
- View transaction fees and profit trends

### Payment Integration

- M-Pesa STK Push through Safaricom Daraja
- M-Pesa payment status tracking
- Daraja callback processing
- M-Pesa transaction references and receipts
- Automatic wallet crediting after successful deposits

---

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python |
| Framework | Flask |
| ORM | Flask-SQLAlchemy |
| Database | PostgreSQL |
| Migrations | Flask-Migrate / Alembic |
| Auth | Flask-JWT-Extended |
| CORS | Flask-CORS |
| HTTP Client | Requests |
| Testing | Pytest |
| Payments | Safaricom Daraja API |

---

## Backend Architecture

The backend follows a layered architecture that separates HTTP routes, business logic, database models, and external services.

```
vyloc-backend/
│
├── app/
│   ├── models/
│   ├── routes/
│   ├── services/
│   ├── schemas/
│   ├── utils/
│   ├── api/
│   ├── config.py
│   ├── extensions.py
│   └── __init__.py
│
├── migrations/
├── tests/
├── .env.example
├── .gitignore
├── requirements.txt
├── run.py
└── README.md
```

### Architecture Flow

```
React Frontend
      │
      │ HTTP / JSON
      ▼
 Flask Routes
      │
      ▼
 Business Services
      │
 ┌────┴───────────────┐
 ▼                    ▼
PostgreSQL          Daraja
Database            M-Pesa API
```

---

## Database

Vyloc uses **PostgreSQL** as its primary relational database.

The current database consists of six core tables:

| Table | Purpose |
|---|---|
| `users` | Stores registered users and roles |
| `wallets` | Stores user wallet balances |
| `beneficiaries` | Stores saved transfer contacts |
| `transactions` | Stores internal financial transactions |
| `wallet_ledger` | Provides an audit trail of wallet changes |
| `mpesa_transactions` | Stores M-Pesa/Daraja payment records |

### Core Relationships

```
User
 │
 ├── 1:1 ── Wallet
 │
 ├── 1:M ── Beneficiaries
 │
 └── 1:M ── M-Pesa Transactions
                    │
                    └── Internal Transaction

Wallet
 │
 ├── 1:M ── Transactions
 │
 ├── 1:M ── Wallet Ledger
 │
 └── 1:M ── M-Pesa Transactions
```

The wallet balance represents the current state, while the wallet ledger provides an auditable history of balance changes.

---

## API Structure

All API endpoints are versioned under `/api`.

### Authentication
```
POST /api/auth/register
POST /api/auth/login
```

### User
```
GET  /api/users/me
PUT  /api/users/me
```

### Wallet
```
GET  /api/wallet
POST /api/wallet/deposit
```

### Beneficiaries
```
GET    /api/beneficiaries
POST   /api/beneficiaries
DELETE /api/beneficiaries/<id>
```

### Transactions
```
POST /api/transactions/transfer
GET  /api/transactions
GET  /api/transactions/<id>
```

### M-Pesa
```
POST /api/mpesa/stk-push
POST /api/mpesa/callback
```

### Admin
```
GET /api/admin/users
GET /api/admin/transactions
GET /api/admin/analytics
```

---

## M-Pesa Integration

Vyloc uses **Safaricom Daraja** as its external payment integration. The primary MVP use case is allowing users to add funds to their Vyloc wallet using M-Pesa.

### Deposit Flow

```
User
 │
 ▼
Vyloc Frontend
 │
 ▼
Flask API
 │
 ▼
Create Pending M-Pesa Transaction
 │
 ▼
Daraja STK Push
 │
 ▼
User Completes M-Pesa Payment
 │
 ▼
Daraja Callback
 │
 ▼
Verify Payment Result
 │
 ▼
Create Internal Transaction
 │
 ▼
Update Wallet
 │
 ▼
Create Wallet Ledger Entry
```

> M-Pesa credentials and other sensitive configuration are stored as environment variables and are never stored in the database or committed to GitHub.

---

## Internal Money Transfer

Transfers between Vyloc users are handled internally by the application.

```
Sender Wallet
      │
      │ Debit
      ▼
 Transaction
      │
      │ Credit
      ▼
Receiver Wallet
```

For every successful transfer:

1. Sender balance is validated.
2. Beneficiary is validated.
3. Available balance is checked.
4. Transaction fee is calculated.
5. Sender wallet is debited.
6. Receiver wallet is credited.
7. Transaction record is created.
8. Ledger entries are created.
9. Transaction receives a unique reference.
10. The operation is committed atomically.

---

## Security

The backend implements security measures including:

- Password hashing
- JWT authentication
- Role-based authorization
- Protected API endpoints
- Environment-based secrets
- Input validation
- Database constraints
- Transaction validation
- CORS configuration
- Secure handling of Daraja credentials

> Financial operations should be performed atomically to prevent situations where a sender is debited without the receiver being credited.

---

## Environment Variables

Create a `.env` file locally. Example:

```env
FLASK_ENV=development

SECRET_KEY=your-secret-key
JWT_SECRET_KEY=your-jwt-secret-key

DATABASE_URL=postgresql://username:password@localhost:5432/vyloc

DARAJA_CONSUMER_KEY=
DARAJA_CONSUMER_SECRET=
DARAJA_SHORTCODE=
DARAJA_PASSKEY=
DARAJA_CALLBACK_URL=
```

**Important:** Never commit `.env` to GitHub. Use `.env.example` to document the required variables without exposing real credentials.

---

## Installation

### 1. Clone the repository

```bash
git clone <BACKEND_REPOSITORY_URL>
cd vyloc-backend
```

### 2. Create a virtual environment

```bash
python -m venv venv
```

Activate it on Linux/macOS:

```bash
source venv/bin/activate
```

Activate it on Windows:

```bash
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Update `.env` with your local PostgreSQL and Daraja configuration.

---

## Database Setup

Initialize migrations if required:

```bash
flask db init
```

Create a migration:

```bash
flask db migrate -m "Initial database schema"
```

Apply migrations:

```bash
flask db upgrade
```

---

## Running the Backend

Start the Flask development server:

```bash
flask run
```

Or:

```bash
python run.py
```

The API will normally be available at:

```
http://localhost:5000
```

---

## Testing

Run the test suite with:

```bash
pytest
```

Tests cover areas including:

- Authentication
- Users
- Wallets
- Beneficiaries
- Transactions
- M-Pesa integration
- Admin functionality

---

## API Response Convention

The API returns consistent JSON responses.

**Successful response**

```json
{
  "success": true,
  "message": "Transfer completed successfully",
  "data": {}
}
```

**Error response**

```json
{
  "success": false,
  "message": "Insufficient wallet balance",
  "error": "INSUFFICIENT_BALANCE"
}
```

---

## Project Scope

The Vyloc MVP focuses on:

- Digital wallet management
- M-Pesa wallet funding
- Peer-to-peer transfers
- Beneficiary management
- Transaction tracking
- User analytics
- Administrative management
- Administrative financial analytics

---

## Future Features

The following are outside the core MVP but may be considered for future versions:

- M-Pesa withdrawals
- Airtel Money integration
- KCB BUNI integration
- International transfers
- Multiple currencies
- Advanced fraud detection
- Two-factor authentication
- Advanced KYC
- Automated financial reporting

---

## Development Guidelines

### Routes

Routes should primarily handle:

- HTTP requests
- Authentication
- Request validation
- Calling services
- Returning responses

### Services

Business logic should live in the service layer, for example:

```
transaction_routes.py
        ↓
transaction_service.py
        ↓
database models
```

Avoid putting complex financial logic directly inside route functions.

### Models

Database models should represent the PostgreSQL schema and relationships.

### M-Pesa

All Daraja-specific logic should remain inside `services/mpesa_service.py` and be exposed through `routes/mpesa_routes.py`.

---

## Development Workflow

The recommended workflow for the team is:

```
Create Issue
    ↓
Create Feature Branch
    ↓
Implement
    ↓
Write Tests
    ↓
Run Tests
    ↓
Create Pull Request
    ↓
Code Review
    ↓
Merge
```

Recommended branch naming:

```
feature/authentication
feature/wallet
feature/beneficiaries
feature/transactions
feature/mpesa
feature/admin
test/transactions
fix/wallet-balance
```

---

## Team

Vyloc is a collaborative Moringa School capstone project.

| Name | Role |
|---|---|
| Godwin | Scrum Master / Project Coordination / Architecture / M-Pesa Integration | Backend
| Robbin | Frontend |
| Lenny | Frontend |
| Faith | Testing / Backend & Admin Support |

---

## Project Status

**Status:** 🚧 In Development

Vyloc is currently being developed as a Moringa School capstone project. The initial goal is to deliver a functional MVP demonstrating a complete financial workflow:

```
Register
   ↓
Login
   ↓
Wallet
   ↓
M-Pesa Deposit
   ↓
Wallet Balance
   ↓
Beneficiary
   ↓
Send Money
   ↓
Transaction History
   ↓
Analytics
   ↓
Admin Dashboard
```

---

## License

This project was developed for educational purposes as part of the Moringa School Software Engineering Capstone Project.