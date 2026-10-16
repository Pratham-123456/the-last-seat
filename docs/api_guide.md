# API Guide — try every flow yourself

Base URL: `http://127.0.0.1:8000` (Workflow A or B). Interactive docs: **http://127.0.0.1:8000/docs** — every endpoint has a "Try it out" button; the `Idempotency-Key` header field appears on every POST.

All responses below were captured from the running implementation (IDs and timestamps will differ for you).

## Endpoints

| Method & path | Purpose | Success | Errors | Idempotency-Key |
|---|---|---|---|---|
| `GET /health/live` | process up | 200 | — | — |
| `GET /health/ready` | `SELECT 1` on PostgreSQL | 200 | 503 | — |
| `GET /api/v1/events` | events + live counts | 200 | — | — |
| `GET /api/v1/events/{event_id}` | one event + live counts | 200 | 404 | — |
| `GET /api/v1/events/{event_id}/seats[?available_only=true]` | derived availability | 200 | 404 | — |
| `GET /api/v1/events/{event_id}/seats/{seat_number}` | one seat | 200 | 404 | — |
| `POST /api/v1/reservations` | hold a seat | 201 | 404, 409, 422 | yes |
| `GET /api/v1/reservations/{id}` | status, expiry, payments | 200 | 404 | — |
| `POST /api/v1/reservations/{id}/confirm` | simulated payment | 200, 202 | 402, 404, 409, 422 | yes |
| `POST /api/v1/reservations/{id}/cancel` | cancel + release | 200 | 404, 409 | yes |
| `GET /api/v1/payments/{id}` | one payment attempt | 200 | 404 | — |
| `POST /api/v1/payments/{id}/simulate-callback` | complete a DELAYED payment | 200 | 404, 409, 422 | yes |
| `GET /api/v1/customers/{customer_ref}/bookings` | booking history | 200 | 422 | — |

## Error codes

| HTTP | `error.code` | Meaning |
|---|---|---|
| 402 | `PAYMENT_FAILED` | simulated FAILURE; hold kept, retry allowed (attempt recorded) |
| 404 | `EVENT_NOT_FOUND`, `SEAT_NOT_FOUND`, `RESERVATION_NOT_FOUND`, `PAYMENT_NOT_FOUND`, `NOT_FOUND` | unknown id / route |
| 409 | `SEAT_UNAVAILABLE` | seat is held or confirmed by someone |
| 409 | `RESERVATION_EXPIRED` | tried to pay after `expires_at` (seat released) |
| 409 | `RESERVATION_NOT_ACTIVE` | tried to pay for a cancelled reservation |
| 409 | `RESERVATION_ALREADY_EXPIRED` | tried to cancel an expired hold |
| 409 | `PAYMENT_ALREADY_PENDING` | a DELAYED payment is still pending |
| 409 | `LATE_PAYMENT_VOIDED` | callback arrived after the hold ended; payment voided, no booking |
| 409 | `IDEMPOTENCY_KEY_PAYLOAD_MISMATCH` | key reused with a different request |
| 422 | `VALIDATION_ERROR`, `INVALID_IDEMPOTENCY_KEY` | bad input |
| 500 | `INTERNAL_ERROR` | unexpected bug (no stack trace leaked; transaction rolled back) |
| 503 | `DATABASE_BUSY`, `DATABASE_UNAVAILABLE` | lock/statement timeout or DB down; retry with the same key |

## Walkthrough in PowerShell

PowerShell's `Invoke-RestMethod` (alias `irm`) parses JSON for you. To see the status code of an error, use `-SkipHttpErrorCheck` in PowerShell 7, or `curl.exe` (shown at the end) in Windows PowerShell 5.1.

```powershell
$B = "http://127.0.0.1:8000"

# 1. Browse
irm "$B/api/v1/events"
irm "$B/api/v1/events/1/seats/42"

# 2. Hold seat 42 (with an idempotency key)
$body = @{ event_id = 1; seat_number = 42; customer_ref = "22BCE1234" } | ConvertTo-Json
$key  = [guid]::NewGuid().ToString()
$hold = irm "$B/api/v1/reservations" -Method Post -ContentType "application/json" -Body $body -Headers @{ "Idempotency-Key" = $key }
$rid  = $hold.reservation.id
$hold.reservation | Select-Object id, status, expires_at, seconds_remaining

# 3. Same request again = replay (same reservation id, no new row)
(irm "$B/api/v1/reservations" -Method Post -ContentType "application/json" -Body $body -Headers @{ "Idempotency-Key" = $key }).reservation.id

# 4. Pay: try FAILURE, then DELAYED + callback
curl.exe -s -X POST "$B/api/v1/reservations/$rid/confirm" -H "Content-Type: application/json" -d '{\"scenario\":\"FAILURE\"}'
$pending = irm "$B/api/v1/reservations/$rid/confirm" -Method Post -ContentType "application/json" -Body '{"scenario":"DELAYED"}'
$pid_ = $pending.payment.id
irm "$B/api/v1/payments/$pid_/simulate-callback" -Method Post -ContentType "application/json" -Body '{"outcome":"SUCCEEDED"}'

# 5. Inspect, then cancel (records a refund) and view history
irm "$B/api/v1/reservations/$rid"
irm "$B/api/v1/reservations/$rid/cancel" -Method Post
irm "$B/api/v1/customers/22BCE1234/bookings"
```

> Avoid naming a variable `$pid` in PowerShell — it is a read-only automatic variable (the shell's process id).

## Captured responses

### Hold — `POST /api/v1/reservations` → 201
```json
{
  "result": "HELD",
  "message": "Seat 42 is held for you for 300 seconds.",
  "reservation": {
    "id": "a9f065b5-9eb2-4572-b24b-b376903dee77",
    "event_id": 3, "seat_id": 1542, "seat_number": 42,
    "customer_ref": "22BCEC92E",
    "status": "HELD", "effective_status": "HELD",
    "created_at": "2026-10-16T18:43:38.142641Z",
    "expires_at": "2026-10-16T18:48:38.142641Z",
    "seconds_remaining": 300.0,
    "confirmed_at": null, "cancelled_at": null,
    "updated_at": "2026-10-16T18:43:38.142641Z",
    "payments": []
  },
  "payment": null,
  "next_step": "POST /api/v1/reservations/a9f065b5-9eb2-4572-b24b-b376903dee77/confirm before expires_at"
}
```
Repeating it with the same `Idempotency-Key` returned **201** with header `Idempotent-Replayed: true` and the same JSON (keys may come back in a different order because PostgreSQL `JSONB` normalises key order; the values are identical).

### Someone else tries the same seat → 409
```json
{
  "error": {
    "code": "SEAT_UNAVAILABLE",
    "message": "Seat 42 for this event is currently held or confirmed by another user.",
    "details": {"event_id": 3, "seat_number": 42, "seat_status": "HELD",
                "held_until": "2026-10-16T18:48:38.142641+00:00"}
  }
}
```

### Same key, different seat → 409
```json
{"error": {"code": "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH",
           "message": "This Idempotency-Key was already used with a different request payload.",
           "details": {"scope": "POST:/api/v1/reservations", "idempotency_key": "demo-C92E-hold"}}}
```

### Confirm with `FAILURE` → 402 (hold kept)
```json
{"error": {"code": "PAYMENT_FAILED",
  "message": "Payment failed. Your hold is still active; you may retry before it expires.",
  "details": {
    "payment": {"attempt_number": 1, "payment_type": "CHARGE", "scenario": "FAILURE", "status": "FAILED", "...": "..."},
    "reservation": {"status": "HELD", "seconds_remaining": 299.97, "...": "..."},
    "retry_allowed": true}}}
```

### Confirm with `DELAYED` → 202
```json
{
  "result": "PAYMENT_PENDING",
  "message": "Payment is processing. The hold stays active until expires_at.",
  "payment": {"id": "aaea13b8-5765-4819-bb09-fb923ff95796", "attempt_number": 2,
              "scenario": "DELAYED", "status": "PENDING", "completed_at": null, "...": "..."},
  "reservation": {"status": "HELD", "...": "..."},
  "next_step": "POST /api/v1/payments/aaea13b8-5765-4819-bb09-fb923ff95796/simulate-callback {\"outcome\": \"SUCCEEDED\" | \"FAILED\"}"
}
```

### Callback `SUCCEEDED` → 200
```json
{"result": "CONFIRMED", "message": "Delayed payment succeeded; your seat is booked.",
 "reservation": {"status": "CONFIRMED", "confirmed_at": "2026-10-16T18:43:38.182863Z", "...": "..."},
 "payment": {"status": "SUCCEEDED", "completed_at": "2026-10-16T18:43:38.182863Z", "...": "..."}}
```

### Cancel a confirmed booking → 200 with refund
```json
{"result": "CANCELLED",
 "message": "Booking cancelled and refunded; the seat is free for other students.",
 "reservation": {"status": "CANCELLED", "cancelled_at": "2026-10-16T18:43:38.187742Z",
   "payments": [
     {"attempt_number": 1, "payment_type": "CHARGE", "scenario": "FAILURE", "status": "FAILED"},
     {"attempt_number": 2, "payment_type": "CHARGE", "scenario": "DELAYED", "status": "SUCCEEDED"},
     {"attempt_number": 3, "payment_type": "REFUND", "scenario": "REFUND", "status": "SUCCEEDED",
      "related_payment_id": "aaea13b8-5765-4819-bb09-fb923ff95796"}]},
 "payment": {"payment_type": "REFUND", "status": "SUCCEEDED", "...": "..."}}
```

### Validation error → 422
```json
{"error": {"code": "VALIDATION_ERROR", "message": "Request validation failed.",
  "details": [
    {"loc": ["body", "seat_number"], "msg": "Input should be greater than or equal to 1", "type": "greater_than_equal"},
    {"loc": ["body", "customer_ref"], "msg": "Field required", "type": "missing"}]}}
```

## curl.exe (Windows) / curl (Linux, macOS)

```powershell
curl.exe -s -i -X POST http://127.0.0.1:8000/api/v1/reservations -H "Content-Type: application/json" -H "Idempotency-Key: my-key-1" -d '{\"event_id\":1,\"seat_number\":7,\"customer_ref\":\"22BCE1234\"}'
```
In Windows PowerShell 5.1 the inner double quotes must be escaped as `\"`. In bash use `-d '{"event_id":1,"seat_number":7,"customer_ref":"22BCE1234"}'`.
