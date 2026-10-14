# DECISIONS.md

1. **Indexes.**
On `employees`, I created a unique index on `emp_code` (guaranteeing uniqueness) and a compound index on `{department: 1, joined_on: 1, emp_code: 1}` to serve department filtering, summary, and trend queries via `IXSCAN`. On `attendance_logs`, I created a unique compound index on `{emp_code: 1, date: 1}` to enforce the natural key and prevent race conditions; `{date: -1, emp_code: 1}` to support fast pagination; `{emp_code: 1, punch_in: -1}` for punch-out lookups; and `{date: 1, late_minutes: 1, emp_code: 1}` for the leaderboard. I considered and rejected an index on `work_hours` alone because no query filters on duration independently of date or employee.

2. **Punch-in race.**
Both requests calculate the attendance date and attempt to insert into `attendance_logs`. Because `{emp_code: 1, date: 1}` is unique at the database engine level, MongoDB accepts exactly one write (returning 201) and throws a `DuplicateKeyError` (E11000) on the other. My exception handler catches this and returns 409 Conflict.

3. **Ties.**
The pipeline uses MongoDB's `$setWindowFields` with `$rank: {}`, implementing standard competition ranking (1, 2, 2, 4). The `limit` is applied as `$match: { rank: { $lte: limit } }`. Both tied employees share the same rank and are returned (yielding more than `limit` items if needed), ordered by `total_late_minutes` descending then `emp_code` ascending.

4. **Headcount.**
The department summary aggregation starts from `employees` filtered by `joined_on <= month_end`. It computes `headcount` directly from the employee group before using `$lookup` to join `attendance_logs`. Employees with zero logs are retained with empty log arrays.

5. **One thing you would change for 100x the data.**
I would maintain pre-aggregated daily department summaries via change streams or background workers, eliminating cross-collection lookups and dynamic `$densify` on millions of raw records.
