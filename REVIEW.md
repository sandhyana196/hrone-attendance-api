# REVIEW.md

## Overview

Reviewed the starter implementation against `openapi.yaml`, `DATA_MODEL.md`, and the R1–R10 requirements. The replacement implementation is in `app/main.py`.

The findings below describe defects identified in the starter implementation and the corresponding changes made in the replacement. Verification notes are listed separately so that implementation changes are not confused with test results.

## Findings and Fixes

| # | Area | Finding in the starter implementation | Detection approach | Change made |
|---|---|---|---|---|
| 1 | Health check | Returned success without checking MongoDB connectivity. | Stop MongoDB and call `/health`. | Added a database ping and a 503 response when MongoDB is unavailable. |
| 2 | Employee creation | Duplicate employee codes could race during concurrent requests. | Submit simultaneous requests with the same employee code. | Added a unique index and mapped duplicate-key errors to 409. |
| 3 | Employee validation | Employee fields and shift values were insufficiently validated. | Submit malformed codes, emails, dates, or shift times. | Added Pydantic validation for documented field constraints. |
| 4 | Employee timestamps | Creation timestamps did not consistently follow the required UTC and epoch-millisecond API conventions. | Compare stored timestamps with API output. | Added UTC storage and epoch-millisecond serialization. |
| 5 | Employee pagination | Pagination offsets and filtered totals could be incorrect. | Test multiple pages and department filters. | Applied the correct page offset and filtered count. |
| 6 | Employee ordering | Employee results did not consistently follow the required code ordering. | Insert employees in a different order and list them. | Sorted results by employee code. |
| 7 | Punch-in validation | Unknown employees were not handled consistently. | Punch in with an unknown employee code. | Added a 404 response for missing employees. |
| 8 | Punch-in timestamps | Local-time conversion and fractional seconds could affect attendance dates and stored instants. | Test timestamps near midnight and with fractional seconds. | Added explicit UTC conversion, IST attendance-date derivation, and second-level truncation. |
| 9 | Duplicate attendance | Concurrent same-day punch-ins needed a database-enforced uniqueness guarantee. | Submit simultaneous punch-ins for the same employee and date. | Added a unique `(emp_code, date)` index and a 409 conflict response. |
| 10 | Late calculations | Grace-period handling needed an exact seconds-based boundary. | Compare punches at 10 minutes and 10 minutes 1 second after shift start. | Applied the strict 600-second boundary and whole-minute flooring. |
| 11 | Work-hour rounding | Standard Python rounding does not guarantee the required half-up behavior. | Test values at rounding boundaries. | Used `Decimal` with `ROUND_HALF_UP`. |
| 12 | Overtime | Overnight shifts and the minimum overtime threshold needed explicit handling. | Test short overruns and overnight shifts. | Added shift-end calculation and the 30-minute minimum threshold. |
| 13 | Attendance listing | Sorting and pagination should be performed by MongoDB rather than loading all matching records into Python. | Inspect query behavior and test pagination. | Added database-side sorting, skipping, limiting, and filtered counts. |
| 14 | Filter validation | Invalid date ranges and status values needed consistent rejection. | Submit reversed dates and invalid statuses. | Added validation and 422 responses. |
| 15 | Analytics and audit behavior | Several contract endpoints and business rules were missing from the starter implementation. | Compare implemented routes with `openapi.yaml`. | Added punch-out, regularization, monthly analytics, department summary, late leaderboard, department trend, and explain endpoints. |
| 16 | Concurrent updates | Attendance updates needed protection against competing requests. | Submit simultaneous updates to the same attendance record. | Added conditional punch-out updates and optimistic concurrency checks for regularization. |

## Development and Verification Notes

The following behaviors were exercised during development:

- Late-arrival grace-period boundaries: exactly 10 minutes and 10 minutes 1 second.
- Work-hour rounding at representative boundary values.
- Overtime thresholds, including an overrun below 30 minutes, exactly 30 minutes, and an overnight shift.
- Half-day classification using the rounded work-hour value.
- Attendance listing, monthly analytics, department summary, leaderboard, and department trend responses.
- Department trend output for a date range with no attendance records, including weekdays, weekends, headcount, and moving averages.
- Explain execution statistics for the five supported analytics/listing query types.

These checks are development verification, not a guarantee that every hidden evaluation test passes. The final implementation should be evaluated against the complete assignment contract.

## Design Constraints Preserved

- Employee business identifiers use `emp_code`; MongoDB `_id` is not exposed in normal API responses.
- Calendar dates use `YYYY-MM-DD`; shift times use `HH:MM`.
- Attendance instants use epoch milliseconds in API requests and responses and UTC BSON datetimes in MongoDB.
- Derived work hours, late minutes, overtime minutes, and half-day values are stored and used by analytics.
- Analytics calculations are intended to run in MongoDB aggregation pipelines, with API-side formatting limited to response serialization and rounding.

## Remaining Review Considerations

Before final submission, verify the final code against every requirement, especially empty-range trend aggregation, matching explain pipelines, missing-history regularization behavior, concurrency, and index usage. Any unresolved limitation should be documented accurately rather than described as a completed fix.
