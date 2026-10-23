#  IPOTrack — IPO & GMP Tracker

IPOTrack is a dashboard for tracking mainboard IPOs, monitoring Grey Market Premium (GMP) trends, and estimating potential listing gains. It brings IPO details, historical GMP data, allotment links, and profit calculations together in one place.

## ✨ Features

- **IPO Dashboard:** View NSE/BSE mainboard IPO details and their latest available status.
- **GMP Charts:** Explore historical GMP trends, track changes over time, and view percentage-based listing gain estimates.
- **Allotment Checker:** Find the relevant official registrar portal, including MUFG Intime, KFintech, and Bigshare.
- **Listing Profit Calculator:** Estimate listing profits after applicable charges and taxes, with lot-based calculations and break-even pricing.
- **Allotment Estimates:** Explore estimated allotment odds for retail and sNII application categories.
- **Automated Data Collection:** Collect IPO data in the background, handle failed requests with retries, and maintain historical snapshots without unnecessary duplicates.
- **Dark Interface:** A clean, dark-themed dashboard designed to make financial data easy to scan.

## 🛠️ Tech Stack

- **Backend:** FastAPI, Python 3.11+, SQLAlchemy
- **Frontend:** Next.js App Router, React, TypeScript, Tailwind CSS
- **Database:** SQLite with WAL mode
- **Data Collection:** HTTPX, BeautifulSoup, APScheduler
- **Data Processing:** Asynchronous scraping, historical snapshots, and automated backfill

## 📂 Project Structure

```text
ipotrack/
├── backend/
│   ├── database.py       # Database connection and models
│   ├── scraper.py        # Data collection
│   ├── sync_service.py   # Data syncing and deduplication
│   └── main.py           # FastAPI application and endpoints
├── frontend/
│   ├── app/              # Next.js application
│   ├── package.json
│   └── tailwind.config.ts
├── .gitignore
└── README.md
```

## 🚀 Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/mrvjc/ipotrack.git
cd ipotrack
```

### 2. Set up the backend

```bash
cd backend
python -m venv .venv
```

Activate the virtual environment.

**Windows (PowerShell):**
```powershell
.venv\Scripts\Activate.ps1
```

**Linux / macOS:**
```bash
source .venv/bin/activate
```

Install the dependencies and start the server:

```bash
pip install fastapi uvicorn httpx sqlalchemy apscheduler beautifulsoup4
uvicorn main:app --reload --port 8000
```

### 3. Set up the frontend

Open a new terminal in the project directory and run:

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) to view the dashboard.

*Make sure the setup commands
