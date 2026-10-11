# Technical Decisions

## 1. FastAPI for the REST API

**Decision:** Use FastAPI to implement the attendance management API.

**Reason:** FastAPI supports request validation through Pydantic, automatic OpenAPI documentation, and a straightforward structure for building REST endpoints.

## 2. MongoDB Atlas as the Database

**Decision:** Store employee and attendance records in MongoDB Atlas.

**Reason:** MongoDB provides document-based storage and aggregation pipelines for attendance analytics. Atlas provides a managed database service.

## 3. Separate Employee and Attendance Collections

**Decision:** Store employee details and attendance records separately.

**Reason:** Employee information and daily attendance have different structures and access patterns. Separating them avoids repeatedly storing employee details in every attendance record.

## 4. UTC Storage and IST Attendance Dates

**Decision:** Store attendance instants in UTC and calculate attendance dates using IST.

**Reason:** UTC provides a consistent representation of timestamps, while IST is used to determine the relevant attendance date and scheduled shift times.

## 5. Epoch Milliseconds in API Responses

**Decision:** Represent punch-in and punch-out instants as epoch milliseconds in API requests and responses.

**Reason:** This provides a consistent timestamp format for clients and avoids returning database-specific date objects.

## 6. Late-Arrival Grace Period

**Decision:** Apply a 10-minute grace period.

**Rule:** An employee is late only when the calculated lateness is strictly greater than 10 minutes. Late minutes are measured from the scheduled shift start.

## 7. Overtime Threshold

**Decision:** Record overtime only when an employee works at least 30 minutes beyond the scheduled shift end.

**Reason:** This prevents small overruns from being counted as overtime.

## 8. Work-Hours Rounding

**Decision:** Calculate elapsed working time in seconds and round the resulting hours to two decimal places using half-up rounding.

**Reason:** Explicit rounding avoids relying on Python's default banker's-rounding behavior.

## 9. Half-Day Classification

**Decision:** Set `half_day` to true when rounded work hours are below 4.50.

**Rule:** Exactly 4.50 hours does not qualify as a half-day.

## 10. Overnight Shifts

**Decision:** Associate an overnight attendance record with the date on which its shift started.

**Reason:** A shift beginning on one calendar day and ending the next should remain a single attendance record.

## 11. MongoDB Indexes and Atomic Updates

**Decision:** Use indexes for frequently queried employee and attendance fields and atomic database updates for punch-out operations.

**Reason:** Indexes can reduce query work, while atomic updates help prevent concurrent requests from successfully punching out the same attendance record more than once.

## 12. Analytics and Query Explain Plans

**Decision:** Use MongoDB aggregation pipelines for analytics and inspect execution plans.

**Reason:** Database-side aggregation reduces the need to retrieve raw attendance documents into Python and helps identify inefficient query plans.

## 13. Configuration and Credentials

**Decision:** Keep database connection settings in environment variables.

**Reason:** This separates configuration from source code and reduces the risk of committing secrets.

## Review Note

These decisions describe the intended design. Correctness and performance should be verified against the implementation, API contract, and test results.
