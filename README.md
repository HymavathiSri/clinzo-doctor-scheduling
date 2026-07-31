# Clinzo — Doctor Slot Scheduling

A working implementation of the doctor-slot-scheduling exercise: turn a
doctor's broad availability window into discrete, bookable slots, and
guarantee that two patients can never both book the same slot, even
under real concurrent access.

```
clinzo-scheduler/
├── app/
│   ├── database.py         SQLAlchemy engine/session setup
│   ├── models.py           Doctor, AvailabilityWindow, Slot, Booking, AuditLog
│   ├── schemas.py          Pydantic request/response models
│   ├── exceptions.py       Domain error types
│   ├── main.py             FastAPI routes
│   └── services/
│       ├── slot_service.py     slot generation + retroactive availability changes
│       ├── booking_service.py  hold / confirm / book / cancel / reschedule
│       └── audit.py            append-only audit log writer
├── tests/
│   ├── test_slot_generation.py   boundary correctness, buffers, idempotency
│   ├── test_concurrency.py       ← the double-booking proof (multi-threaded)
│   └── test_booking_flow.py      cancellation, reschedule, retroactive changes
├── requirements.txt
└── README.md   (this file)
```

## Setup

```bash
git clone <this-repo-url>
cd clinzo-scheduler
python3 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

pytest -q                              # 19 tests, includes the concurrency proof
uvicorn app.main:app --reload          # interactive API + Swagger UI at http://127.0.0.1:8000/docs
```

No external services required — it runs against a local SQLite file
(`clinzo_scheduler.db`, created automatically) out of the box. Point
`SCHEDULER_DB_URL` at a Postgres DSN to run it against Postgres instead;
no code changes needed (see §7).

## Assumptions

Since the brief leaves several product decisions open, these are the
choices this implementation makes:

- **A patient can hold/book more than one slot with the same doctor at
  once** — there's no "one active appointment per patient per doctor"
  constraint. Easy to add (a partial unique index on
  `(doctor_id, patient_id) WHERE status='ACTIVE'`) but wasn't specified,
  and real platforms vary on this (some allow follow-ups to be booked
  ahead while a consult is pending).
- **Slots that don't evenly divide a window are dropped, not
  shortened** — a 50-minute window with 15-minute slots yields 3 full
  slots, not 3 full + 1 five-minute slot. Assumed a doctor never wants a
  slot shorter than their configured duration.
- **Hold duration defaults to 5 minutes** and is caller-configurable per
  request; no product spec was given for this, 5 minutes is a common
  e-commerce-checkout-style default.
- **A doctor's `AvailabilityWindow` is single-date, not the recurring
  weekly pattern** mentioned as an alternative in the brief
  ("...on a given date **or recurring weekday**"). Implementing
  recurrence was left out of scope for this exercise; the model doesn't
  block adding it — it would slot in as a `recurrence_rule` field on
  `AvailabilityWindow` plus a scheduled job that materializes upcoming
  weeks' slots ahead of time, reusing `generate_slots_for_window`
  unchanged.
- **Retroactive availability changes never auto-cancel a booked
  appointment** (see §4) — treated as a hard product requirement rather
  than a configurable policy, since silently cancelling a patient's
  confirmed consult seemed like the wrong default to assume.
- **No authentication/authorization layer** — `patient_id` and
  `actor` are passed as plain strings in request bodies. A real system
  would derive these from a session/JWT; omitted here to keep the
  scheduling logic (the point of the exercise) unobscured by auth
  plumbing.
- **No payment integration** — the hold/confirm split exists specifically
  to leave room for a payment step between them, but no payment
  provider is wired in.

---

## 1. Data model

```
Doctor 1───* AvailabilityWindow 1───* Slot 1───* Booking
```

- **AvailabilityWindow** — the doctor's broad statement ("Monday
  10:00–18:00"), plus the configured `slot_duration_minutes`,
  `buffer_minutes`, and `appointment_type` for that window. Kept as a
  durable row (not discarded after slot generation) so edits can be
  diffed against it later.
- **Slot** — a single bookable unit of time. This is the row everything
  else hangs off: it has a `status` (`AVAILABLE / HELD / BOOKED /
  WITHDRAWN`), a `version` column for optimistic concurrency, and hold
  metadata (`hold_expires_at`, `held_by`).
- **Booking** — a patient's claim on a slot. `status` is
  `ACTIVE / CANCELLED / RESCHEDULED`.
- **AuditLog** — append-only record of every state transition
  (held / confirmed / cancelled / rescheduled / window edited), who did
  it, and when.

### Slot representation: materialized, not computed

Slots are **materialized as real rows** the moment an availability
window is created, rather than computed on-the-fly by subtracting
bookings from windows at read time. Tradeoff, made deliberately:

| | Materialized (chosen) | Computed on read |
|---|---|---|
| "What's free right now" | indexed `WHERE status='AVAILABLE'` query | must scan window + all bookings, subtract, every request |
| Concurrency control | a real row to put a version column / unique constraint / row lock on | nothing to lock — race conditions have to be solved elsewhere (e.g. a separate bookings-only unique constraint), which is possible but doesn't naturally give you *holds* |
| Storage | more rows (bounded — a doctor's week of 15-min slots is a few hundred rows) | none |
| Retroactive window edit | need to reconcile existing slot rows (see §4) | just changes the window; but then booked appointments have nothing durable to hang off |

Given the requirement to support reservation holds and prevent double
booking under concurrency, having a real row per slot is what makes the
solution in §2 possible at all — so materialized wins here.

---

## 2. Concurrency-safe booking — the core of the exercise

### The guarantee, and how it's actually enforced

**Two independent layers**, so a bug in one doesn't cause a double-booking:

**Layer 1 — atomic conditional update.** Every state transition
(`AVAILABLE → HELD`, `HELD → BOOKED`, `AVAILABLE → BOOKED`, etc.) is a
single SQL statement of the shape:

```sql
UPDATE slots
SET status = 'BOOKED', version = version + 1
WHERE id = :id
  AND version = :expected_version
  AND status = 'AVAILABLE'
```

`UPDATE ... WHERE` is atomic in every relational database. If two
requests race to run this against the same row, the database itself
guarantees only one can actually match the `WHERE` clause and change the
row — the loser gets `rowcount == 0` back, because there's no gap
between "check" and "set" for two threads to both slip through. There is
no separate `SELECT` to check status followed by a separate `UPDATE` —
that pattern is exactly the classic TOCTOU race this design avoids.

**Layer 2 — a database constraint as a backstop.** `Booking` has a
partial unique index:

```python
Index("uix_one_active_booking_per_slot", "slot_id", unique=True,
      sqlite_where=(status == 'ACTIVE'))
```

At most one `ACTIVE` booking can ever exist for a given slot — enforced
by the database, not application code. Even if Layer 1 had a bug, the
second `INSERT` would be rejected with an `IntegrityError`, which the
service layer catches and turns into a clean `SlotUnavailableError`
rather than a 500 or (worse) a silent double-booking.

### Proof: `tests/test_concurrency.py`

Three tests fire **25 threads** at the same slot simultaneously (each
thread opens its own DB session/connection — this matters, it's what
makes it a genuine test of database-level atomicity rather than
Python's GIL doing the work for us):

1. `test_concurrent_direct_booking_never_double_books` — 25 threads all
   call `book_slot()` on the same slot at once. Asserts exactly 1
   succeeds, 24 get a clean rejection, and the DB independently confirms
   exactly one `ACTIVE` booking exists afterward.
2. `test_concurrent_hold_then_confirm_never_double_books` — same race
   through the two-phase hold/confirm flow.
3. `test_concurrent_cancel_and_rebook_race` — a booked slot is cancelled
   and re-booked by 25 other patients at the same instant; asserts the
   final state is never "0 active bookings but slot still shows BOOKED"
   or "2 active bookings," whichever request wins.

All pass consistently (run `pytest tests/test_concurrency.py -v`).

### Reservation holds

For flows with a gap between "pick a slot" and "confirm" (e.g.
collecting payment), the two-phase flow reserves without committing:

```
hold_slot(slot, patient)      # AVAILABLE -> HELD, expires in N seconds
   ...patient enters payment details...
confirm_booking(slot, patient) # HELD -> BOOKED, creates the Booking row
```

- A hold is opportunistically reclaimed by the *next* caller if it's
  expired (`hold_slot` also matches `status='HELD' AND hold_expires_at <
  now`), so an abandoned checkout doesn't permanently lock a slot.
- `sweep_expired_holds()` is provided for a periodic background job to
  proactively release stale holds even without a new booking attempt
  triggering the lazy path.
- A hold is tied to a `held_by` patient id — only that patient can
  confirm or release it (`HoldOwnershipError` otherwise).

### Correctness under partial failure

`reschedule_booking` is the interesting partial-failure case: moving a
patient from slot A to slot B involves two writes (free A, book B) that
must not leave the patient with neither. The ordering is deliberate:

1. **Claim the new slot first** (via `hold_slot` + `confirm_booking`).
   If slot B is already taken, this fails immediately and slot A / the
   original booking are **never touched** — the patient keeps their
   existing appointment rather than losing it.
2. Only after B is safely claimed do we release A and mark the old
   booking `RESCHEDULED`.
3. If step 3 somehow fails, the hold on B is released in a `finally`-style
   cleanup so it doesn't get stuck.

Tested in `test_reschedule_preserves_appointment_if_new_slot_taken` and
`test_reschedule_success_moves_booking_and_frees_old_slot`.

---

## 3. API design

| Method & path | Purpose |
|---|---|
| `POST /doctors` | create a doctor |
| `POST /availability` | create a window; immediately generates its slots |
| `PATCH /availability/{id}` | edit a window's start/end (see §4) |
| `DELETE /availability/{id}` | deactivate a window (see §4) |
| `GET /doctors/{id}/slots?date=&tz=&appointment_type=` | list **available** slots for a local calendar day, converted from UTC |
| `POST /slots/{id}/hold` | reserve a slot (two-phase flow) |
| `POST /slots/{id}/release-hold` | voluntarily give up a hold |
| `POST /slots/{id}/confirm` | turn a held slot into a booking |
| `POST /slots/{id}/book` | single-step book (no hold phase) |
| `POST /bookings/{id}/cancel` | cancel; slot becomes available immediately |
| `POST /bookings/{id}/reschedule` | move to a new slot, old one preserved on failure |
| `POST /admin/sweep-expired-holds` | background-job endpoint to reclaim stale holds |

The patient-facing `GET .../slots` endpoint is the important
availability-hiding boundary: it filters on `status='AVAILABLE'` at the
query level, so there is no separate "now go filter out the booked ones"
step for a developer to forget — booked/held slots are structurally
invisible to that endpoint.

Errors map to HTTP status via `app/main.py`'s exception handler:
`NotFoundError → 404`, `SlotUnavailableError / HoldExpiredError → 409`,
`HoldOwnershipError → 403`, `InvalidWindowError → 422`.

---

## 4. Retroactive availability changes

What happens when a doctor edits or removes a window that already has
booked slots in it? Policy, implemented in
`slot_service.update_availability_window` /
`deactivate_availability_window`:

- Slots that fall **outside** the new range and are still `AVAILABLE`
  (or `HELD` with an expired hold) → **withdrawn**. Nobody has a claim
  on them, so this is safe.
- Slots that fall outside the new range but are **`BOOKED`** →
  **never auto-cancelled**. A confirmed booking is a commitment to a
  patient; silently cancelling it because the doctor changed their hours
  would be a real harm. Instead these are returned as
  `orphaned_booked_slot_ids` for a human (doctor or admin) to explicitly
  resolve — e.g. contact the patient to reschedule, or just honor the
  appointment as a one-off exception to the new hours.
- The window's new range is then re-run through `generate_slots_for_window`,
  which is idempotent, to backfill any newly available time.

Tested in `test_retroactive_shrink_preserves_booked_slots_but_drops_unbooked`,
`test_retroactive_shrink_withdraws_unbooked_slots_outside_new_range`, and
`test_deactivating_window_preserves_booked_appointments`.

---

## 5. Time zones

All timestamps are stored as naive UTC `DateTime` columns internally —
this is the one source of truth and avoids an entire class of DST bugs
from mixing offsets in comparisons/arithmetic. The only place timezone
conversion happens is at the API boundary:

- `GET /doctors/{id}/slots` takes a `tz` query param (IANA name, e.g.
  `Asia/Kolkata`), interprets `date` in that zone, converts the resulting
  local-day boundaries to UTC for the query, and could equally convert
  the response timestamps back to `tz` for display (left as a one-line
  addition — the conversion utility is already in `main.py`).
- A `Doctor.timezone` field is stored for convenience (e.g. pre-filling
  a scheduling UI in the doctor's own zone) but is never used for
  correctness-critical comparisons.

---

## 6. Auditability

Every state-changing operation writes an `AuditLog` row (entity type/id,
action, actor, a free-text detail string, timestamp) inside the same
service call, via `services/audit.py`. This is deliberately append-only
and separate from the mutable `Slot`/`Booking` rows so "what actually
happened, in order" survives even if the current-state rows get edited
further.

---

## 7. Scalability notes

The exercise is implemented against SQLite (zero setup for review), but
the concurrency approach is standard SQL and **generalizes directly to
Postgres/MySQL** — swap the `DATABASE_URL` in `app/database.py` and
nothing else changes:

- The conditional `UPDATE ... WHERE version=... AND status=...` pattern
  works identically and scales better on Postgres, since MVCC lets
  concurrent transactions on *different* rows proceed fully in parallel
  (SQLite's single-writer model serializes all writes regardless of
  which rows they touch, which is fine for this exercise's scale but
  would become the bottleneck at real production volume).
- The partial unique index (`WHERE status='ACTIVE'`) is standard
  Postgres syntax and needs no translation.
- For horizontal read scaling, `GET .../slots` is a single indexed query
  (`doctor_id, appointment_type, status, start_utc`) and read replicas
  would serve it fine, since patients browsing slots don't need
  strong consistency the instant before they attempt to hold one — the
  hold/book call is where consistency actually matters, and that's a
  single-row conditional update on the primary.
- Materialized slots do mean row count scales with
  `doctors × hours_available × (60/slot_duration)`; for very large
  doctor counts this is still bounded and index-friendly (a doctor's
  week of 15-minute slots is on the order of a few hundred rows), and
  old/past slots can be archived out of the hot table on a schedule.

---

## 8. Bonus: extending to multiple doctors / variable durations

- **Variable-length appointment types** are already supported —
  `AvailabilityWindow.appointment_type` and `slot_duration_minutes` are
  per-window, so a doctor can have one window generating 15-minute
  follow-up slots and another generating 30-minute first-visit slots
  over the same or different hours; `appointment_type` is part of the
  slot's uniqueness key so they never collide.
- **Waitlist** (not implemented, sketch only): a `Waitlist` table
  keyed on `(doctor_id, desired_window, patient_id)`. On
  `cancel_booking`, after freeing the slot, check for a matching
  waitlist entry and either auto-hold it for that patient with a short
  confirmation window, or fire a notification — same
  hold/confirm machinery, just triggered by cancellation instead of a
  patient browsing.
- **Booking across multiple doctors** (e.g. "any available cardiologist
  in the next 2 hours"): the current per-doctor uniqueness constraints
  (`doctor_id, start_utc, appointment_type`) already make this safe to
  extend — the query just becomes `WHERE specialty=... AND status=
  'AVAILABLE' AND start_utc BETWEEN ...` across doctors instead of one,
  and the hold/confirm calls are unchanged since they operate on a
  specific `slot_id` regardless of whose slot it is. The only new piece
  needed is a search/ranking layer on top (e.g. soonest available, or
  patient's preferred doctor first) — the booking-safety guarantees
  underneath don't change at all.
