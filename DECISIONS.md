# Implementation Decisions

1. **Indexes.** `emp_code` is unique on employees. Department plus `emp_code` supports filtered employee listing; department, `joined_on`, and `emp_code`, plus the standalone `joined_on` index, support department and headcount lookups. Attendance has a unique (`emp_code`, `date`) index to prevent duplicate days, a (`date` descending, `emp_code`) index for the global list order, and (`emp_code`, `date` descending) for employee history. I did not add a low-cardinality `status` index: it would not provide the attendance list's required sort, and the date indexes already support that query shape.

2. **Punch-in race.** Both requests can read the employee, then attempt to insert the same natural key. MongoDB's unique attendance index accepts one insert; the other raises `DuplicateKeyError`, which the API returns as 409. The winner returns 201.

3. **Ties.** `$rank` gives tied employees the same competition rank. Filtering on `rank <= limit` happens after ranking, so every employee tied at the cutoff is returned. Final order is late minutes descending, then employee code ascending.

4. **Headcount.** The department summary starts from employees joined by month end, looks up logs, and preserves employees with no matching logs during unwind. They are counted once.

5. **At 100x.** I would redesign department trends to avoid collecting every department employee into one aggregation document; that array can hit MongoDB's 16 MB document limit. A bounded daily rollup would scale better.
