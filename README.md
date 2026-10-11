# HROne Attendance Management API

A REST API built with **FastAPI** and **MongoDB Atlas** for employee management, attendance tracking, punch-in/punch-out operations, attendance regularization, and analytics.

## Features

- Employee creation and listing
- Attendance punch-in and punch-out
- Late-arrival calculation with a 10-minute grace period
- Overtime calculation with a 30-minute minimum threshold
- Work-hours calculation and half-day detection
- Attendance regularization with history
- Monthly employee attendance analytics
- Department summaries and trends
- Late-arrival leaderboard
- MongoDB aggregation pipelines and query explain plans

## Tech Stack

- Python
- FastAPI
- MongoDB Atlas
- PyMongo
- Pydantic
- Uvicorn

## Project Structure

```text
candidate_kit/
├── app/
│   └── main.py
├── tests/
├── .env.example
├── .gitignore
├── openapi.yaml
├── requirements.txt
├── README.md
├── DECISIONS.md
└── REVIEW.md
```

Adjust the structure above if your actual folders or filenames differ.

## Setup

### 1. Clone the repository

```bash
git clone <YOUR_GITHUB_REPOSITORY_URL>
cd candidate_kit
```

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file in the project root:

```env
MONGO_URI=your_mongodb_atlas_connection_string
MONGO_DB=attendance_db
```

Use your own MongoDB Atlas connection string. Never commit `.env` or expose database credentials.

### 5. Start the API

```bash
uvicorn app.main:app --reload --port 8000
```

### 6. Open API documentation

Visit:

- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc

## Attendance Rules

- Attendance dates are based on Indian Standard Time (IST).
- API timestamps use epoch milliseconds.
- Late arrival is counted only when the employee is more than 10 minutes late.
- Overtime is recorded only when it reaches 30 minutes.
- Work hours are rounded to two decimal places using half-up rounding.
- A half-day is recorded when rounded work hours are below 4.50 hours.
- Overnight shifts are associated with their shift-start attendance date.

## Testing

Use Swagger UI to test the API endpoints. Automated test coverage should be documented as tests are added and executed.

## Security

- Store credentials in environment variables.
- Exclude `.env` and virtual-environment directories from Git.
- Restrict MongoDB Atlas network access and database permissions appropriately.

## License

Add a license if you intend to distribute this project under a specific open-source license.
