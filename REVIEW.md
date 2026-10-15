# Starter Implementation Review

The starter is preserved as `app/main_backup.py`; findings below refer to that implementation and the contract in `openapi.yaml`. The current `app/main.py` contains the fixes and completed routes.

| # | Where | Defect and detection | Fix in current implementation |
|---|---|---|---|
| 1 | `create_employee` | A read-before-insert duplicate check can race; two requests can both pass it. Parallel identical creates can produce duplicate employee codes. | A unique MongoDB index enforces `emp_code`; duplicate-key errors become HTTP 409. |
| 2 | `list_employees` | Pagination skips `page * page_size`, so page 1 omits its first page; `total` ignores the department filter. | Skip is `(page - 1) * page_size`; count and page use the same filter, with `emp_code` ordering and bounded parameters. |
| 3 | `punch_in` | A missing employee is not rejected before accessing `emp`; an unknown code can become a server error. The status is also an unrestricted string. | Return 404 for unknown employees and validate punch status against the three allowed presence values. |
| 4 | `punch_in` and time helpers | Epoch values are interpreted with naive local `datetime` objects; seconds, floats, and invalid ranges are not rejected. Results can vary by host timezone. | Validate integer epoch milliseconds, truncate to seconds, and consistently convert UTC instants to IST for calendar and shift calculations. |
| 5 | `compute_work_hours` / `compute_overtime` | Python's built-in rounding is ties-to-even; overtime has no 30-minute minimum, and the calculation assumes a same-day shift end. | Use decimal half-up rounding, apply the 30-minute threshold, and calculate end times on the following date for overnight shifts. |
| 6 | `list_attendance` | The entire matching collection is materialized and sorted in Python, which does not scale to the contract's 100,000 records. It also returns an `id` derived from MongoDB `_id`. | Count and page in MongoDB with deterministic sort and indexes; serialize without either MongoDB identifier. |
| 7 | Employee and attendance writes | There are no startup indexes. Attendance check-then-insert can race, and attendance uniqueness is not enforced in MongoDB. | Create idempotent indexes, including unique indexes on `emp_code` and (`emp_code`, `date`); translate duplicate-key writes to 409. |
| 8 | Route coverage and health | Punch-out, regularization, analytics, and explain routes are absent. Health always reports ready without checking MongoDB. | Implement the contract routes and make `/health` ping MongoDB, returning 503 when unavailable. |
| 9 | Request validation | Pagination bounds, dates, filters, and punch-out sequencing are not validated; invalid values can lead to incorrect queries or runtime errors. | Add Pydantic/request validation and explicit 404, 409, and 422 responses. MongoDB operation failures use a sanitized 503 response. |

## Checked, Not Defects

- `emp_code` is the correct public identity; MongoDB `_id` remains internal.
- The absence of a holiday calendar is intentional: the contract defines working days as Monday-Friday.
- Analytics read stored derived values rather than recomputing them; this is explicitly required by the data model.
- Missing `history` and `half_day` fields in seeded documents are intentional and are serialized with empty/false defaults.
