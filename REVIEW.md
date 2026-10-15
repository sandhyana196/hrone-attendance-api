# REVIEW.md

The following defects were verified by tracing the starter implementation against `openapi.yaml` and R1-R10. The replacement implementation is in `app/main.py`.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Always returned `{"status":"ok"}` without contacting MongoDB. | Stop MongoDB and call `/health`; it still returned 200. | Ping MongoDB and return 503 on a PyMongo failure. |
| 2 | `create_employee` | Check-then-insert did not prevent concurrent duplicate employee codes. | Send simultaneous POSTs with the same `emp_code`. | Create a unique `emp_code` index and map `DuplicateKeyError` to 409. |
| 3 | `create_employee` / `EmployeeIn` | Input fields and shift equality were not validated to the contract. | Send malformed employee code/email/date, invalid shift time, or equal shift start/end. | Added Pydantic validation matching the documented patterns and constraints. |
| 4 | `create_employee` | Stored `created_at` as a naive local `datetime`, and returned it without epoch milliseconds. | Inspect the stored instant or compare API output with the required epoch-ms format. | Store UTC BSON datetimes and serialize instants to integer milliseconds. |
| 5 | `list_employees` | Used `page * page_size` as the offset, skipping the first page. | With 25 employees, page 1 of size 20 returned the last 5. | Use `(page - 1) * page_size`; validate page and page size. |
| 6 | `list_employees` | `total` counted the whole collection rather than the department-filtered set. | Filter by a department and compare `total` with that department's count. | Count using the same query as the result list. |
| 7 | `list_employees` | Did not sort by `emp_code`, as required. | Insert codes in reverse order and list them. | Sort ascending by `emp_code`. |
| 8 | `punch_in` | Did not return 404 for an unknown employee before dereferencing `emp`. | Punch in with an unknown `emp_code`; starter code could fail with 500. | Check the employee and return 404. |
| 9 | `punch_in` | Used local-time `fromtimestamp` / `now`, so date and stored instant depended on the machine timezone; it did not truncate instants to whole seconds. | Use an epoch timestamp near midnight or with fractional seconds. | Convert epoch milliseconds explicitly to UTC, derive the IST attendance date, and truncate to seconds. |
| 10 | `punch_in` | Checked for an existing record and then inserted without an atomic uniqueness guarantee. | Concurrent same-day punch-ins could both pass the check. | Enforce unique `(emp_code, date)` in MongoDB and map duplicate-key errors to 409. |
| 11 | `compute_late_minutes` | Compared whole minutes to the grace period, losing the strict second boundary. | For a 09:30 shift, 09:40:01 must return 10, not 0. | Compare elapsed seconds strictly against 600, then floor elapsed minutes. Overnight starts are handled against the prior date when appropriate. |
| 12 | `compute_work_hours` | Used Python's banker's `round`, not half-up rounding. | A value exactly at a half-cent boundary can round differently. | Use `Decimal` with `ROUND_HALF_UP`. |
| 13 | `compute_overtime` | Did not handle overnight shifts and did not apply the minimum 30-minute threshold. | A 5-minute overrun should be 0; a 40-minute overnight overrun should be 40. | Compute shift end from the attendance date and overnight rule; return minutes only at 30 or more. |
| 14 | `list_attendance` | Loaded all matching attendance documents into Python to sort, count and paginate. | Performance and memory usage degrade on 100,000 records. | Apply MongoDB sort/skip/limit and a filtered `count_documents` query. |
| 15 | `list_attendance` | Did not validate date ranges/status and omitted consistent response serialization/defaults. | Try `date_from > date_to` or inspect timestamps/history. | Validate filters and normalize dates, derived defaults and audit timestamp values at the API boundary. |
| 16 | Missing endpoints | Punch-out, regularization, monthly analytics, department summary, late leaderboard, department trend and explain endpoint were not implemented. | Calls to contract paths returned 404. | Implemented the missing paths and their business rules in the single required `app/main.py`. |

## Inspected and retained

- `emp_code` remains the employee's business identifier; MongoDB `_id` is not exposed.
- Calendar dates remain `YYYY-MM-DD` strings and shift times remain `HH:MM` strings, as required by `DATA_MODEL.md`.
- Analytics use stored `late_minutes`, `overtime_minutes`, `work_hours` and `half_day` values instead of recomputing them from seeded punch times.
