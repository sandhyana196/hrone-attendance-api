# DECISIONS.md

1. **Indexes.** I create a unique index on `employees.emp_code` and a unique compound index on `attendance_logs.emp_code, date`; they enforce the two natural-key guarantees during concurrent requests. Additional department/join-date, date/employee, employee/date/status, and status/date indexes support the list and analytics filters. I considered a separate index on `late_minutes`, but the month/date range is the primary filter for the leaderboard, so I prioritize date-led access.

2. **Punch-in race.** Both requests can read the employee and prepare a record, but the unique `(emp_code, date)` index allows only one insert. The winner receives 201; MongoDB raises `DuplicateKeyError` for the other request, which the API maps to 409.

3. **Ties.** The leaderboard uses MongoDB `$rank`, not row positions. If two employees tie at rank `limit`, both are returned, and the next rank is skipped (1, 2, 2, 4).

4. **Headcount.** The department summary starts from employees who joined by month-end and looks up attendance. Employees with no matching logs remain in the pipeline, so they still count toward headcount.

5. **100x data.** I would review query plans with production-like data, measure index size/write cost, and consider pre-aggregated daily summaries for expensive trends.
