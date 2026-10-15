# Starter Implementation Review

The findings below refer to the original starter implementation supplied with the assignment and the requirements in `openapi.yaml`. The current `app/main.py` contains the fixes and completed routes.

| # | Area | Defect and detection | Fix in current implementation |
|---|---|---|---|
| 1 | Employee creation | A read-before-insert duplicate check is vulnerable to concurrent requests creating duplicate employee codes. | A unique MongoDB index enforces employee-code uniqueness. Duplicate-key errors return HTTP 409. |
| 2 | Employee listing | Pagination skips `page * page_size`, omitting the first page's records. The total count does not consistently respect department filtering. | Use `(page - 1) * page_size` and the same filter for counting and retrieval, with stable sorting and bounded pagination. |
| 3 | Punch-in validation | Unknown employee codes are not handled safely, and attendance status is insufficiently restricted. | Return HTTP 404 for unknown employees and validate status against the allowed presence values. |
| 4 | Time calculations | Naive local datetime handling makes results dependent on the host timezone. Timestamp types and ranges are insufficiently validated. | Validate integer epoch milliseconds, truncate to whole seconds, and consistently convert UTC instants to IST for attendance calculations. |
| 5 | Work hours and overtime | Built-in rounding uses ties-to-even, overtime lacks the required minimum threshold, and overnight shifts are not handled correctly. | Use decimal half-up rounding, apply the 30-minute overtime threshold, and calculate overnight shift boundaries on the correct date. |
| 6 | Attendance listing | Loading and sorting all matching records in Python does not scale to large datasets. MongoDB's internal identifier is also exposed. | Perform filtering, counting, sorting, and pagination in MongoDB. Keep MongoDB `_id` internal. |
| 7 | Database integrity | Startup indexes are missing. Concurrent attendance writes can create duplicate employee/day records. | Create idempotent indexes, including unique employee-code and employee/date indexes. Translate duplicate-key errors into HTTP 409 responses. |
| 8 | Routes and health checks | Punch-out, regularization, analytics, and explain routes are missing. Health checks do not verify database readiness. | Implement the required routes and check MongoDB availability in `/health`, returning HTTP 503 when unavailable. |
| 9 | Request validation and errors | Invalid dates, pagination parameters, filters, and punch-out sequences can produce incorrect results or runtime errors. | Add request validation and appropriate HTTP 404, 409, and 422 responses. Sanitize database failure responses with HTTP 503. |

## Checked, Not Defects

- `emp_code` is the public employee identifier; MongoDB `_id` remains internal.
- No holiday calendar is intentional because the assignment defines working days as Monday through Friday.
- Analytics use stored derived attendance fields where required by the data model.
- Missing `history` and `half_day` fields in seeded records are intentional and are serialized with empty or false defaults.