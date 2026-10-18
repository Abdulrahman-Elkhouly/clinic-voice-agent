"""The clinic's data layer: the only code that runs SQL.

A mock clinic system on SQLite. Switching to the clinic's real system means rewriting
this one file; the public methods of ClinicDB are the contract that system must match.

Free slots are never stored. They are calculated:
    weekly schedule - exceptions - booked & held - booking window = free slots

All times are timezone-aware (Qatar, UTC+3) and stored as ISO 8601 with "+03:00".
"""
import random
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

QATAR_TZ = timezone(timedelta(hours=3))   # no daylight saving in Qatar
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "clinic.db"

# Booking rules (fictional; see the switch checklist in docs/clinic_plan.md)
MIN_NOTICE = timedelta(minutes=30)     # nothing bookable sooner than this
HORIZON = timedelta(days=60)           # nothing bookable further ahead than this
HOLD_TIME = timedelta(minutes=5)       # an unconfirmed hold expires after this
LATE_CHANGE = timedelta(hours=4)       # cancelling/moving closer than this is a "late change"
LOCKOUT_FAILURES = 5                   # failed phone verifications on one file number...
LOCKOUT_WINDOW = timedelta(hours=24)   # ...within this window lock phone verification for that file
FILE_NUMBER_DIGITS = 5                 # random digits; a Luhn check digit is added (6 in total)

SCHEMA = """
CREATE TABLE IF NOT EXISTS doctors (
    doctor_id   INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    specialty   TEXT NOT NULL,
    gender      TEXT NOT NULL CHECK (gender IN ('female', 'male')),
    languages   TEXT NOT NULL,                     -- comma-separated
    active      INTEGER NOT NULL DEFAULT 1         -- deactivated, never deleted
);

-- The regular weekly pattern. Several rows per weekday are allowed (split shifts, Ramadan).
CREATE TABLE IF NOT EXISTS doctor_schedules (
    schedule_id  INTEGER PRIMARY KEY,
    doctor_id    INTEGER NOT NULL REFERENCES doctors(doctor_id),
    weekday      INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),   -- Monday = 0 ... Sunday = 6
    start_time   TEXT NOT NULL,                    -- 'HH:MM'
    end_time     TEXT NOT NULL,                    -- 'HH:MM', '24:00' = midnight
    slot_minutes INTEGER NOT NULL CHECK (slot_minutes > 0),
    last_start   TEXT,                             -- latest appointment start, e.g. '21:30'
    valid_from   TEXT NOT NULL,                    -- date, inclusive
    valid_to     TEXT                              -- date, inclusive; NULL = no end
);

-- One-off breaks in the pattern. doctor_id NULL = the whole clinic (public holiday).
CREATE TABLE IF NOT EXISTS schedule_exceptions (
    exception_id INTEGER PRIMARY KEY,
    doctor_id    INTEGER REFERENCES doctors(doctor_id),
    date_from    TEXT NOT NULL,
    date_to      TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('closed', 'leave', 'special_hours')),
    start_time   TEXT,                             -- special_hours only
    end_time     TEXT,
    reason       TEXT
);

CREATE TABLE IF NOT EXISTS patients (
    patient_id     INTEGER PRIMARY KEY,            -- internal, never shown to anyone
    file_number    TEXT NOT NULL UNIQUE,           -- what the patient knows
    full_name      TEXT NOT NULL,
    dob            TEXT NOT NULL,
    phone          TEXT NOT NULL,                  -- not unique: families share numbers
    guardian_phone TEXT,
    id_verified    INTEGER NOT NULL DEFAULT 0,     -- reception saw the Qatar ID / passport
    created_at     TEXT NOT NULL
);

-- Bookings and holds in one table, so one unique index guards both.
CREATE TABLE IF NOT EXISTS appointments (
    appointment_id  INTEGER PRIMARY KEY,
    patient_id      INTEGER NOT NULL REFERENCES patients(patient_id),
    doctor_id       INTEGER NOT NULL REFERENCES doctors(doctor_id),
    start_at        TEXT NOT NULL,
    end_at          TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                        ('held', 'booked', 'cancelled', 'completed', 'no_show', 'released', 'expired')),
    hold_expires_at TEXT,
    call_id         TEXT,                          -- which call placed the hold
    channel         TEXT NOT NULL CHECK (channel IN ('phone_agent', 'whatsapp', 'reception')),
    late_change     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
-- The double-booking guard: at most one active (held or booked) row per doctor and start time
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_slot
    ON appointments(doctor_id, start_at) WHERE status IN ('held', 'booked');
CREATE INDEX IF NOT EXISTS appointments_by_patient ON appointments(patient_id, start_at);

-- Phone verification attempts per file number, across calls (cross-call lockout).
-- Recorded for any file number entered, existing or not, so a lockout never reveals that a file exists.
CREATE TABLE IF NOT EXISTS verification_attempts (
    attempt_id  INTEGER PRIMARY KEY,
    file_number TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('failure', 'success', 'unlock')),
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_by_file ON verification_attempts(file_number, at);

CREATE TABLE IF NOT EXISTS waitlist (
    waitlist_id INTEGER PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(patient_id),
    doctor_id   INTEGER REFERENCES doctors(doctor_id),
    specialty   TEXT,
    date_from   TEXT NOT NULL,
    date_to     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'offered', 'booked', 'removed')),
    created_at  TEXT NOT NULL,
    CHECK (doctor_id IS NOT NULL OR specialty IS NOT NULL)
);
"""


# --- Errors: each message is safe to pass to the LLM as a tool result ---

class BookingError(Exception):
    """Base class. str(error) explains the problem in words the agent can relay."""


class SlotTaken(BookingError):
    """Someone else holds or booked this slot (the unique index refused it)."""


class SlotUnavailable(BookingError):
    """Not a real slot: outside the doctor's hours, off the slot grid, a closed day, or outside the booking window."""


class PatientBusy(BookingError):
    """The patient already has an appointment overlapping this time."""


class HoldExpired(BookingError):
    """The hold ran out (or was released) before it was confirmed."""


class NotAllowed(BookingError):
    """The change isn't permitted, e.g. cancelling an appointment that has already started."""


class NotFound(BookingError):
    """No such record for this patient."""


# --- Small helpers ---

def now_qatar() -> datetime:
    return datetime.now(QATAR_TZ)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime: every time must carry its time zone")
    return dt.astimezone(QATAR_TZ).isoformat(timespec="minutes")


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _at(day: date, hhmm: str) -> datetime:
    """'HH:MM' on a given day; '24:00' means midnight at the end of that day."""
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(0), QATAR_TZ) + timedelta(hours=h, minutes=m)


def _row(r: sqlite3.Row) -> dict:
    """Row -> dict, with *_at columns as datetimes."""
    d = dict(r)
    for k, v in d.items():
        if k.endswith("_at") and isinstance(v, str):
            d[k] = _parse_dt(v)
    return d


def luhn_check_digit(digits: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:          # every second digit from the right, starting next to the check digit
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return str((10 - total % 10) % 10)


def is_valid_file_number(file_number: str) -> bool:
    """Catches a single misheard digit (and most swapped pairs) before any database lookup."""
    s = "".join(ch for ch in file_number if ch.isdigit())
    return len(s) == FILE_NUMBER_DIGITS + 1 and luhn_check_digit(s[:-1]) == s[-1]


class ClinicDB:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # A new short-lived connection per call: safe from any thread (asyncio.to_thread) and any process
    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)   # autocommit; BEGIN explicitly
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")      # readers never wait for the writer
        conn.execute("PRAGMA busy_timeout = 10000")    # a second writer waits instead of failing
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self):
        """A write transaction. BEGIN IMMEDIATE takes the write lock up front, so the checks
        and the insert inside it can't be interleaved with another writer's."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # --- Doctors ---

    def add_doctor(self, name: str, specialty: str, gender: str, languages: list[str]) -> int:
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO doctors (name, specialty, gender, languages) VALUES (?, ?, ?, ?)",
                (name, specialty, gender, ", ".join(languages)))
            return cur.lastrowid

    def add_schedule(self, doctor_id: int, weekday: int, start_time: str, end_time: str,
                     slot_minutes: int, valid_from: date, valid_to: date | None = None,
                     last_start: str | None = None) -> int:
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO doctor_schedules (doctor_id, weekday, start_time, end_time, slot_minutes,"
                " last_start, valid_from, valid_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (doctor_id, weekday, start_time, end_time, slot_minutes, last_start,
                 valid_from.isoformat(), valid_to.isoformat() if valid_to else None))
            return cur.lastrowid

    def add_exception(self, date_from: date, date_to: date, kind: str, doctor_id: int | None = None,
                      start_time: str | None = None, end_time: str | None = None,
                      reason: str | None = None) -> int:
        if kind == "special_hours" and not (start_time and end_time):
            raise ValueError("special_hours needs start_time and end_time")
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO schedule_exceptions (doctor_id, date_from, date_to, kind, start_time, end_time, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (doctor_id, date_from.isoformat(), date_to.isoformat(), kind, start_time, end_time, reason))
            return cur.lastrowid

    def list_doctors(self, specialty: str | None = None, gender: str | None = None,
                     language: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM doctors WHERE active = 1", []
        if specialty:
            sql += " AND lower(specialty) = lower(?)"
            args.append(specialty)
        if gender:
            sql += " AND gender = ?"
            args.append(gender)
        with self._connect() as conn:
            rows = [_row(r) for r in conn.execute(sql + " ORDER BY doctor_id", args)]
        if language:
            rows = [d for d in rows if language.lower() in d["languages"].lower().split(", ")]
        return rows

    def get_doctor(self, doctor_id: int) -> dict | None:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM doctors WHERE doctor_id = ?", (doctor_id,)).fetchone()
        return _row(r) if r else None

    def schedules_on(self, day: date) -> list[dict]:
        """Every schedule row in force on a date (used by the consistency check)."""
        with self._connect() as conn:
            return [_row(r) for r in conn.execute(
                "SELECT * FROM doctor_schedules WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)",
                (day.isoformat(), day.isoformat()))]

    # --- Availability ---

    def _day_slots(self, conn, doctor_id: int, day: date) -> list[tuple[datetime, datetime]]:
        """The doctor's slot grid for one day: schedule minus exceptions (ignores bookings and the window)."""
        d = day.isoformat()
        exceptions = conn.execute(
            "SELECT * FROM schedule_exceptions WHERE date_from <= ? AND date_to >= ?"
            " AND (doctor_id IS NULL OR doctor_id = ?)", (d, d, doctor_id)).fetchall()
        if any(e["kind"] in ("closed", "leave") for e in exceptions):
            return []
        special = [(_at(day, e["start_time"]), _at(day, e["end_time"]))
                   for e in exceptions if e["kind"] == "special_hours"]

        schedules = conn.execute(
            "SELECT * FROM doctor_schedules WHERE doctor_id = ? AND weekday = ?"
            " AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?) ORDER BY start_time",
            (doctor_id, day.weekday(), d, d)).fetchall()

        slots = []
        for s in schedules:
            step = timedelta(minutes=s["slot_minutes"])
            start, end = _at(day, s["start_time"]), _at(day, s["end_time"])
            last = _at(day, s["last_start"]) if s["last_start"] else None
            t = start
            while t + step <= end and (last is None or t <= last):
                # Special hours narrow the day: the slot must fit inside every special window
                if all(a <= t and t + step <= b for a, b in special):
                    slots.append((t, t + step))
                t += step
        return slots

    def _window(self, now: datetime) -> tuple[datetime, datetime]:
        return now + MIN_NOTICE, now + HORIZON

    def free_slots(self, doctor_id: int, date_from: date, date_to: date,
                   now: datetime | None = None) -> list[dict]:
        """Bookable slots for a doctor between two dates (inclusive), soonest first."""
        now = now or now_qatar()
        earliest, latest = self._window(now)
        with self._connect() as conn:
            taken = {r["start_at"] for r in conn.execute(
                "SELECT start_at FROM appointments WHERE doctor_id = ? AND start_at >= ? AND start_at < ?"
                " AND (status = 'booked' OR (status = 'held' AND hold_expires_at > ?))",
                (doctor_id, _iso(_at(date_from, "00:00")), _iso(_at(date_to, "24:00")), _iso(now)))}
            result, day = [], date_from
            while day <= date_to:
                for start, end in self._day_slots(conn, doctor_id, day):
                    if earliest <= start <= latest and _iso(start) not in taken:
                        result.append({"doctor_id": doctor_id, "start_at": start, "end_at": end})
                day += timedelta(days=1)
        return result

    # --- Patients ---

    def create_patient(self, full_name: str, dob: date, phone: str, guardian_phone: str | None = None,
                       now: datetime | None = None) -> dict:
        """Registers a patient and returns {patient_id, file_number}. Duplicate checks are the caller's job
        (find_by_dob_phone first), because only the conversation knows whether it's really the same person."""
        now = now or now_qatar()
        with self._write() as conn:
            for _ in range(50):
                base = str(random.randint(10 ** (FILE_NUMBER_DIGITS - 1), 10 ** FILE_NUMBER_DIGITS - 1))
                file_number = base + luhn_check_digit(base)
                try:
                    cur = conn.execute(
                        "INSERT INTO patients (file_number, full_name, dob, phone, guardian_phone, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (file_number, full_name.strip(), dob.isoformat(), phone, guardian_phone, _iso(now)))
                    return {"patient_id": cur.lastrowid, "file_number": file_number}
                except sqlite3.IntegrityError:
                    continue   # file number already used; draw another
        raise RuntimeError("could not generate a free file number")

    def get_patient(self, patient_id: int) -> dict | None:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()
        return _row(r) if r else None

    def find_by_file_number(self, file_number: str) -> dict | None:
        s = "".join(ch for ch in file_number if ch.isdigit())
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM patients WHERE file_number = ?", (s,)).fetchone()
        return _row(r) if r else None

    def verify_patient(self, file_number: str, dob: date) -> dict | None:
        """The patient only if BOTH match. None never says which one was wrong."""
        p = self.find_by_file_number(file_number)
        return p if p and p["dob"] == dob.isoformat() else None

    def find_by_dob_phone(self, dob: date, phone: str) -> list[dict]:
        """Forgot-file-number path: date of birth + phone, both exact (digits are reliable over STT;
        names aren't). Matches the patient's own phone or their guardian's."""
        digits = "".join(ch for ch in phone if ch.isdigit())
        with self._connect() as conn:
            rows = [_row(r) for r in conn.execute("SELECT * FROM patients WHERE dob = ?", (dob.isoformat(),))]
        only_digits = lambda s: "".join(ch for ch in (s or "") if ch.isdigit())   # noqa: E731
        return [p for p in rows if digits in (only_digits(p["phone"]), only_digits(p["guardian_phone"]))]

    # --- Cross-call verification lockout ---

    def record_verification(self, file_number: str, success: bool, now: datetime | None = None):
        now = now or now_qatar()
        with self._write() as conn:
            conn.execute("INSERT INTO verification_attempts (file_number, kind, at) VALUES (?, ?, ?)",
                         (file_number, "success" if success else "failure", _iso(now)))

    def verification_locked(self, file_number: str, now: datetime | None = None) -> bool:
        """Locked when the file number has LOCKOUT_FAILURES failures within LOCKOUT_WINDOW,
        counted since the last success or reception unlock."""
        now = now or now_qatar()
        with self._connect() as conn:
            reset = conn.execute("SELECT max(at) FROM verification_attempts WHERE file_number = ?"
                                 " AND kind IN ('success', 'unlock')", (file_number,)).fetchone()[0]
            since = max(filter(None, [_iso(now - LOCKOUT_WINDOW), reset]))
            failures = conn.execute("SELECT count(*) FROM verification_attempts WHERE file_number = ?"
                                    " AND kind = 'failure' AND at > ?", (file_number, since)).fetchone()[0]
        return failures >= LOCKOUT_FAILURES

    def unlock_verification(self, file_number: str, now: datetime | None = None):
        """Reception unlocks a file after confirming the patient in person (staff view, Stage 4c)."""
        now = now or now_qatar()
        with self._write() as conn:
            conn.execute("INSERT INTO verification_attempts (file_number, kind, at) VALUES (?, 'unlock', ?)",
                         (file_number, _iso(now)))

    # --- Holds and bookings ---

    def _check_new_slot(self, conn, patient_id: int, doctor_id: int, start_at: datetime, now: datetime,
                        ignore_appointment_id: int | None = None) -> datetime:
        """Validates a slot inside a write transaction. Returns its end time or raises."""
        doctor = conn.execute("SELECT active FROM doctors WHERE doctor_id = ?", (doctor_id,)).fetchone()
        if not doctor or not doctor["active"]:
            raise NotFound("That doctor isn't available for booking.")
        if not conn.execute("SELECT 1 FROM patients WHERE patient_id = ?", (patient_id,)).fetchone():
            raise NotFound("Patient not found.")

        earliest, latest = self._window(now)
        if start_at < earliest:
            raise SlotUnavailable("That time is too soon or already past. Offer a later slot.")
        if start_at > latest:
            raise SlotUnavailable(f"Bookings open only {HORIZON.days} days ahead. Offer an earlier date or the waitlist.")
        grid = dict(self._day_slots(conn, doctor_id, start_at.astimezone(QATAR_TZ).date()))
        end_at = grid.get(start_at.astimezone(QATAR_TZ))
        if end_at is None:
            raise SlotUnavailable("The doctor has no appointment at that time. Use check_availability for real slots.")

        overlap = conn.execute(
            "SELECT 1 FROM appointments WHERE patient_id = ? AND status IN ('held', 'booked')"
            " AND start_at < ? AND end_at > ? AND appointment_id IS NOT ?",
            (patient_id, _iso(end_at), _iso(start_at), ignore_appointment_id)).fetchone()
        if overlap:
            raise PatientBusy("The patient already has an appointment at that time.")
        return end_at

    @staticmethod
    def _expire_holds(conn, now: datetime):
        conn.execute("UPDATE appointments SET status = 'expired', updated_at = ?"
                     " WHERE status = 'held' AND hold_expires_at <= ?", (_iso(now), _iso(now)))

    def hold_slot(self, patient_id: int, doctor_id: int, start_at: datetime, call_id: str,
                  channel: str = "phone_agent", replaces: int | None = None,
                  now: datetime | None = None) -> dict:
        """Holds a slot while the caller confirms. Releases this call's previous hold (one per call).
        `replaces`: the appointment being rescheduled, so it doesn't count as an overlap."""
        now = now or now_qatar()
        _iso(start_at)   # rejects naive datetimes early
        with self._write() as conn:
            self._expire_holds(conn, now)   # so an expired hold never blocks the unique index
            conn.execute("UPDATE appointments SET status = 'released', updated_at = ?"
                         " WHERE status = 'held' AND call_id = ?", (_iso(now), call_id))
            end_at = self._check_new_slot(conn, patient_id, doctor_id, start_at, now, ignore_appointment_id=replaces)
            try:
                cur = conn.execute(
                    "INSERT INTO appointments (patient_id, doctor_id, start_at, end_at, status, hold_expires_at,"
                    " call_id, channel, created_at, updated_at) VALUES (?, ?, ?, ?, 'held', ?, ?, ?, ?, ?)",
                    (patient_id, doctor_id, _iso(start_at), _iso(end_at), _iso(now + HOLD_TIME),
                     call_id, channel, _iso(now), _iso(now)))
            except sqlite3.IntegrityError:
                raise SlotTaken("That time was just taken. Offer the next free slot.") from None
            return {"appointment_id": cur.lastrowid, "start_at": start_at, "end_at": end_at,
                    "hold_expires_at": now + HOLD_TIME}

    def _take_hold(self, conn, hold_id: int, now: datetime) -> sqlite3.Row:
        hold = conn.execute("SELECT * FROM appointments WHERE appointment_id = ?", (hold_id,)).fetchone()
        if not hold or hold["status"] not in ("held", "expired", "released"):
            raise NotFound("No such hold.")
        if hold["status"] != "held" or _parse_dt(hold["hold_expires_at"]) <= now:
            raise HoldExpired("The hold on that slot ran out. Check availability again.")
        return hold

    def confirm_hold(self, hold_id: int, now: datetime | None = None) -> dict:
        """Turns a hold into a booking (after the caller said yes)."""
        now = now or now_qatar()
        with self._write() as conn:
            self._take_hold(conn, hold_id, now)
            conn.execute("UPDATE appointments SET status = 'booked', hold_expires_at = NULL, updated_at = ?"
                         " WHERE appointment_id = ?", (_iso(now), hold_id))
            return _row(conn.execute("SELECT * FROM appointments WHERE appointment_id = ?", (hold_id,)).fetchone())

    def release_hold(self, hold_id: int | None = None, call_id: str | None = None,
                     now: datetime | None = None) -> int:
        """Releases one hold, or every hold of a call (on hang-up). Returns how many were released."""
        now = now or now_qatar()
        if hold_id is None and call_id is None:
            raise ValueError("give hold_id or call_id")
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE appointments SET status = 'released', updated_at = ? WHERE status = 'held'"
                " AND (appointment_id = ? OR call_id = ?)", (_iso(now), hold_id, call_id))
            return cur.rowcount

    def book_slot(self, patient_id: int, doctor_id: int, start_at: datetime, channel: str,
                  now: datetime | None = None) -> dict:
        """Books directly, without a hold (reception, WhatsApp staff, the seed script)."""
        now = now or now_qatar()
        _iso(start_at)
        with self._write() as conn:
            self._expire_holds(conn, now)
            end_at = self._check_new_slot(conn, patient_id, doctor_id, start_at, now)
            try:
                cur = conn.execute(
                    "INSERT INTO appointments (patient_id, doctor_id, start_at, end_at, status, channel,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, 'booked', ?, ?, ?)",
                    (patient_id, doctor_id, _iso(start_at), _iso(end_at), channel, _iso(now), _iso(now)))
            except sqlite3.IntegrityError:
                raise SlotTaken("That time was just taken. Offer the next free slot.") from None
            return {"appointment_id": cur.lastrowid, "start_at": start_at, "end_at": end_at}

    def _changeable(self, conn, appointment_id: int, patient_id: int, now: datetime) -> sqlite3.Row:
        """An appointment the patient may cancel or move. The patient_id check means one patient
        can never touch another patient's appointment, whatever the caller says."""
        appt = conn.execute("SELECT * FROM appointments WHERE appointment_id = ? AND patient_id = ?",
                            (appointment_id, patient_id)).fetchone()
        if not appt or appt["status"] in ("held", "released", "expired"):
            raise NotFound("No such appointment for this patient.")
        if appt["status"] in ("completed", "no_show"):
            raise NotAllowed("That appointment has already taken place and can't be changed.")
        if appt["status"] == "cancelled":
            raise NotAllowed("That appointment is already cancelled.")
        if _parse_dt(appt["start_at"]) <= now:
            raise NotAllowed("That appointment has already started or is in the past, so it can't be changed. "
                             "Offer to book a new one.")
        return appt

    def cancel_appointment(self, appointment_id: int, patient_id: int, now: datetime | None = None) -> dict:
        now = now or now_qatar()
        with self._write() as conn:
            appt = self._changeable(conn, appointment_id, patient_id, now)
            late = _parse_dt(appt["start_at"]) - now < LATE_CHANGE
            conn.execute("UPDATE appointments SET status = 'cancelled', late_change = ?, updated_at = ?"
                         " WHERE appointment_id = ?", (int(late), _iso(now), appointment_id))
            return {"appointment_id": appointment_id, "late_change": late}

    def reschedule(self, appointment_id: int, patient_id: int, hold_id: int, now: datetime | None = None) -> dict:
        """Moves a booking to a slot the caller already holds. One transaction: the hold becomes the
        booking and the old appointment is cancelled together, or nothing changes at all."""
        now = now or now_qatar()
        with self._write() as conn:
            old = self._changeable(conn, appointment_id, patient_id, now)
            hold = self._take_hold(conn, hold_id, now)
            if hold["patient_id"] != patient_id:
                raise NotFound("No such hold for this patient.")
            late = _parse_dt(old["start_at"]) - now < LATE_CHANGE
            conn.execute("UPDATE appointments SET status = 'booked', hold_expires_at = NULL, updated_at = ?"
                         " WHERE appointment_id = ?", (_iso(now), hold_id))
            conn.execute("UPDATE appointments SET status = 'cancelled', late_change = ?, updated_at = ?"
                         " WHERE appointment_id = ?", (int(late), _iso(now), appointment_id))
            return {"old_appointment_id": appointment_id, "new_appointment_id": hold_id, "late_change": late}

    def list_appointments(self, patient_id: int, include_past: bool = False,
                          now: datetime | None = None) -> list[dict]:
        """The patient's booked appointments (upcoming only by default), with the doctor's name."""
        now = now or now_qatar()
        sql = ("SELECT a.*, d.name AS doctor_name, d.specialty FROM appointments a"
               " JOIN doctors d USING (doctor_id) WHERE a.patient_id = ?")
        args: list = [patient_id]
        if include_past:
            sql += " AND a.status IN ('booked', 'completed', 'no_show', 'cancelled')"
        else:
            sql += " AND a.status = 'booked' AND a.start_at > ?"
            args.append(_iso(now))
        with self._connect() as conn:
            return [_row(r) for r in conn.execute(sql + " ORDER BY a.start_at", args)]

    # --- Staff actions (used by the staff view in Stage 4c) ---

    def mark_outcome(self, appointment_id: int, status: str, now: datetime | None = None):
        """Staff record what happened: 'completed' or 'no_show', only once the start time has passed."""
        now = now or now_qatar()
        if status not in ("completed", "no_show"):
            raise ValueError("status must be 'completed' or 'no_show'")
        with self._write() as conn:
            appt = conn.execute("SELECT * FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()
            if not appt or appt["status"] != "booked":
                raise NotFound("No booked appointment with that id.")
            if _parse_dt(appt["start_at"]) > now:
                raise NotAllowed("The appointment hasn't happened yet.")
            conn.execute("UPDATE appointments SET status = ?, updated_at = ? WHERE appointment_id = ?",
                         (status, _iso(now), appointment_id))

    def count_no_shows(self, patient_id: int, since: datetime) -> int:
        """Three in six months means future bookings must be paid in advance (appointments policy)."""
        with self._connect() as conn:
            return conn.execute("SELECT count(*) FROM appointments WHERE patient_id = ? AND status = 'no_show'"
                                " AND start_at >= ?", (patient_id, _iso(since))).fetchone()[0]

    # --- Waitlist ---

    def add_to_waitlist(self, patient_id: int, date_from: date, date_to: date, doctor_id: int | None = None,
                        specialty: str | None = None, now: datetime | None = None) -> int:
        now = now or now_qatar()
        if doctor_id is None and specialty is None:
            raise ValueError("give doctor_id or specialty")
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO waitlist (patient_id, doctor_id, specialty, date_from, date_to, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (patient_id, doctor_id, specialty, date_from.isoformat(), date_to.isoformat(), _iso(now)))
            return cur.lastrowid
