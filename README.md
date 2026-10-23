# 📈 IPOTrack — Institutional Mainboard IPO & GMP Intelligence Terminal

A modern, high-performance IPO intelligence terminal inspired by Groww and institutional trading desks. IPOTrack aggregates real-time NSE/BSE mainboard issue data, tracks Grey Market Premium (GMP) time-series velocity, models SEBI allotment odds, and provides deterministic routing to official registrar allotment portals.

---

## ⚡ Key Highlights

- **Pitch-Black Carbon UI:** High-contrast dark theme optimized for dense financial metrics, fast scanning, and zero eye fatigue.
- **Time-Series GMP Canvas:** In-memory HTML5 canvas rendering price trends, historic points, and live percentage listing gains on crosshair hover.
- **Smart Allotment Router:** Automatic registrar resolution (MUFG Intime, KFintech, Bigshare) with one-click direct portal navigation for closed and listed issues.
- **Listing Day P&L Simulator:** Dynamic lot-based calculator computing gross listing gain, transaction charges, STCG tax liability, and break-even share price.
- **Resilient Data Pipeline:** Async background scraping engine with snapshot deduplication and SQLite WAL (Write-Ahead Logging) concurrency.

---

## 🛠️ Tech Stack

- **Backend:** FastAPI, Python 3.11+, SQLAlchemy, SQLite (WAL mode), HTTPX, APScheduler
- **Frontend:** Next.js (App Router), React, TypeScript, Tailwind CSS
- **Data Ingestion:** Asynchronous web scraping with automated backfill

---

## 📂 Project Structure

```text
ipotrack/
├── backend/
│   ├── database.py       # SQLite connection & SQLAlchemy schemas
│   ├── scraper.py        # Async data ingestion engine
│   ├── sync_service.py   # Deduplication & time-series syncing
│   └── main.py           # FastAPI endpoints & canvas chart renderer
├── frontend/
│   ├── app/              # Next.js App Router (page.tsx, layout.tsx)
│   ├── package.json
│   └── tailwind.config.ts
├── .gitignore
└── README.md
