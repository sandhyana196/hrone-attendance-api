# Employee Attendance & Analytics API

A REST API for employee attendance management and analytics, built with FastAPI and MongoDB.

## Run Locally

1. Install Python 3.11+ and MongoDB 6.0+ (local MongoDB or Atlas).
2. From the project folder, create and activate a virtual environment, then install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Configure `MONGO_URI` and `MONGO_DB` as environment variables. A local `.env` file may be used for development; **never commit credentials or `.env` to GitHub**.
4. Start the API:

   ```bash
   uvicorn app.main:app --port 8000
   ```

5. Verify readiness at `http://localhost:8000/health`.
6. Explore and test the endpoints using Swagger UI at `http://localhost:8000/docs`.

## Sample Data

After configuring MongoDB, sample data can be loaded with:

```bash
python sample_seed.py
```

**Warning:** Check `sample_seed.py` before running it against a database containing data you want to preserve. The seed script may delete existing employee and attendance records.

## Verification Notes

The bundled sample dataset is intentionally small and does not establish performance at the assignment's target scale. Analytics results, index usage, and explain execution statistics should be validated against MongoDB 6.0+ and a larger test dataset.
