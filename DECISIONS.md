# Implementation Decisions

## 1. Indexes

Employees use a unique `emp_code` index, a department-plus-code index for filtered listing, and department/join-date indexes for analytics. Attendance uses a unique `(emp_code, date)` index, a date-descending/code index for global listing, and an employee/date index for history. A standalone status index was omitted because it does not support the required list ordering.

## 2. Punch-in Race

Concurrent requests can both pass an initial read. The unique attendance index permits only one insert for an employee/day. The winner receives HTTP 201; the losing request receives HTTP 409.

## 3. Leaderboard Ties

MongoDB `$rank` assigns equal ranks to employees with equal total late minutes. Applying the limit after ranking preserves all employees tied at the cutoff. Results are ordered by late minutes descending, then employee code ascending.

## 4. Headcount

The department summary starts with employees who joined by the end of the selected month, then looks up attendance logs. Preserving employees without matching logs during the unwind ensures they are still counted once.

## 5. Scaling to 100×

The department trend pipeline collects department employees into an array, which can hit MongoDB's 16 MB document limit at larger scales. I would replace this approach with bounded daily rollups or precomputed attendance aggregates to reduce repeated work and memory pressure.