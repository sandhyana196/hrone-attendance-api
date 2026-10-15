# Employee Attendance & Analytics API

## Run locally

1. Install Python 3.11+ and MongoDB 6.0+ (local MongoDB or Atlas).
2. From this folder, create and activate a virtual environment, then run `pip install -r requirements.txt`.
3. Set `MONGO_URI` and `MONGO_DB` in your environment. A local `.env` can be used for development; do not commit it.
4. Start the API with `uvicorn app.main:app --port 8000`.
5. Check readiness at `GET /health`. The app creates its indexes at startup.

The assignment data can be loaded with `python sample_seed.py` after configuring MongoDB. Analytics and explain behavior should be checked against MongoDB 6.0+ and a larger test dataset; the bundled sample is intentionally too small to validate performance.
