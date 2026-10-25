"""Booking for one verified patient: availability, book, cancel, reschedule, waitlist.

Like identity.py, every rule is enforced here in code and testable without the LLM:
- every change is two steps, and step 2 is refused unless the caller spoke after step 1's read-back
- 3 no-shows in 6 months: no phone booking (advance payment needed)
- at most 3 upcoming appointments; a same-specialty duplicate triggers a warning
- age rules per specialty
- 3 slots offered at a time; times worded for speech, never ISO strings
Each function returns the text handed to the LLM as the tool result. Appointment IDs never reach it:
listed appointments get per-call reference numbers that map to IDs here.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from db import HORIZON, LATE_CHANGE, QATAR_TZ, BookingError, ClinicDB, HoldExpired, now_qatar

SLOTS_OFFERED = 3
MAX_UPCOMING = 3
NO_SHOW_LIMIT = 3
NO_SHOW_WINDOW = timedelta(days=182)   # "three missed appointments within six months" (appointments policy)
# (minimum age, maximum age exclusive) per specialty, from the knowledge base
AGE_RULES = {"pediatrics": (0, 14), "internal medicine": (16, None)}
PARTS_OF_DAY = {"morning": (0, 12), "afternoon": (12, 17), "evening": (17, 24)}
OFFER_STAFF = "Offer to transfer the caller to a member of staff (transfer_to_human)."


@dataclass
class BookingState:
    """Per-call booking state. Lives in CallData, so it survives handoffs."""
    pending: dict | None = None                        # the step-1 action waiting for the caller's yes
    listed: dict[int, int] = field(default_factory=dict)   # reference number -> appointment_id


# --- Wording for speech ---

def spoken_time(dt: datetime) -> str:
    """'Saturday 17 October at 9:20 in the morning'"""
    dt = dt.astimezone(QATAR_TZ)
    part = "in the morning" if dt.hour < 12 else "in the afternoon" if dt.hour < 17 else "in the evening"
    hour = dt.hour % 12 or 12
    return f"{dt:%A} {dt.day} {dt:%B} at {hour}:{dt:%M} {part}"


def slot_key(start: datetime) -> str:
    """The start time as the model passes it back to book a slot."""
    return f"{start.astimezone(QATAR_TZ):%Y-%m-%d %H:%M}"


# How free times are described (agreed 2026-10-22): listing "9:00, 9:20 or 9:40" made those sound like the only
# times. A day with many free times is described by its free hours ("from 9:00 to 2:40"); only a few are named.
FEW_SLOTS = 3          # this many free times on a day, or fewer: name them; more: give the free hours as ranges
DAYS_SUGGESTED = 3     # when no single day was asked for: the earliest days with free times


def spoken_clock(dt: datetime) -> str:
    """'9:20 in the morning'"""
    dt = dt.astimezone(QATAR_TZ)
    part = "in the morning" if dt.hour < 12 else "in the afternoon" if dt.hour < 17 else "in the evening"
    return f"{dt.hour % 12 or 12}:{dt:%M} {part}"


def spoken_day(day: date) -> str:
    """'Saturday 24 October'"""
    return f"{day:%A} {day.day} {day:%B}"


def free_ranges(slots: list[dict]) -> list[tuple[datetime, datetime]]:
    """Consecutive free slots merged: [(first start, last start), ...]. A taken slot splits a range."""
    ranges = []
    for s in sorted(slots, key=lambda s: s["start_at"]):
        if ranges and s["start_at"] == ranges[-1][2]:   # starts where the previous one ends
            ranges[-1] = (ranges[-1][0], s["start_at"], s["end_at"])
        else:
            ranges.append((s["start_at"], s["start_at"], s["end_at"]))
    return [(first, last) for first, last, _ in ranges]


def describe_day(day_slots: list[dict]) -> str:
    """One day's free times for one doctor: named if few, else as free hours."""
    doctor = day_slots[0]["doctor"]
    day = spoken_day(day_slots[0]["start_at"].astimezone(QATAR_TZ).date())
    if len(day_slots) <= FEW_SLOTS:
        times = "; ".join(f"{spoken_clock(s['start_at'])} [book with: doctor='{doctor}', "
                          f"start='{slot_key(s['start_at'])}']" for s in day_slots)
        return f"{day} with {doctor}, only these times: {times}"
    minutes = int((day_slots[0]["end_at"] - day_slots[0]["start_at"]).total_seconds() // 60)
    spans = " and ".join(f"from {spoken_clock(a)} to {spoken_clock(b)}" if a != b else f"at {spoken_clock(a)}"
                         for a, b in free_ranges(day_slots))
    first = day_slots[0]["start_at"].astimezone(QATAR_TZ)
    return (f"{day} with {doctor}: free {spans} (a time every {minutes} minutes; the last time in each range is "
            f"the last start) [book with: doctor='{doctor}', start='{first:%Y-%m-%d} HH:MM' on that "
            f"{minutes}-minute grid]")


def _parse_start(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=QATAR_TZ)
    except (ValueError, AttributeError):
        return None


def _parse_date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text.strip()) if text else None
    except ValueError:
        return None


def _age(dob: date, on: date) -> int:
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


# --- Lookups ---

def _patient(db: ClinicDB, patient_id: int) -> dict:
    patient = db.get_patient(patient_id)
    if patient is None:
        raise ValueError(f"no patient {patient_id}")   # can't happen: the agent is created for a verified patient
    return patient


def _doctor_by_name(db: ClinicDB, name: str) -> dict | None:
    wanted = " ".join(name.lower().replace("dr.", "").replace("dr ", "").split())
    for d in db.list_doctors():
        if " ".join(d["name"].lower().replace("dr.", "").split()) == wanted:
            return d
    return None


def doctors_text(db: ClinicDB, specialty: str | None = None, gender: str | None = None,
                 language: str | None = None) -> str:
    docs = db.list_doctors(specialty=specialty, gender=gender, language=language)
    if not docs:
        return "No doctor at the clinic matches that. Say so, and offer other options or a transfer to staff."
    return "Doctors: " + "; ".join(f"{d['name']} ({d['specialty']}, {d['gender']}, speaks {d['languages']})"
                                   for d in docs)


def _age_problem(patient: dict, specialty: str, on: date) -> str | None:
    rule = AGE_RULES.get(specialty)
    if not rule:
        return None
    age = _age(date.fromisoformat(patient["dob"]), on)
    low, high = rule
    if age < low or (high is not None and age >= high):
        limit = f"under {high}" if high is not None else f"{low} and over"
        return (f"This patient can't be booked with {specialty}: it's for patients {limit}, and the patient is "
                f"{age}. Explain, and suggest family medicine instead (it sees all ages).")
    return None


# --- Availability ---

def _parse_time(text: str | None) -> int | None:
    """'13:20' -> minutes after midnight, or None."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def check_availability(db: ClinicDB, patient_id: int, date_from_text: str, date_to_text: str | None = None,
                       doctor_name: str | None = None, specialty: str | None = None, part_of_day: str | None = None,
                       gender: str | None = None, language: str | None = None, time_text: str | None = None,
                       now: datetime | None = None) -> str:
    """Up to 3 free times. With `time_text`, that exact time first if it's free, else the nearest ones; without it,
    the earliest (it used to have no way to ask for a specific time, so "1:20" looked unavailable)."""
    now = now or now_qatar()
    d_from = _parse_date(date_from_text)
    if d_from is None:
        return "The date isn't valid. Pass dates as YYYY-MM-DD."
    d_to = _parse_date(date_to_text) or d_from
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    if part_of_day and part_of_day.lower() not in PARTS_OF_DAY:
        return "part_of_day must be morning, afternoon, or evening."
    wanted = _parse_time(time_text)
    if time_text and wanted is None:
        return "time must be HH:MM in 24-hour time, e.g. 13:20."
    if wanted is not None:
        part_of_day = None   # a specific time decides on its own
    last_day = (now + HORIZON).date()
    if d_from > last_day:
        return (f"Bookings open only {HORIZON.days} days ahead, up to {last_day:%A} {last_day.day} {last_day:%B}. "
                f"Offer an earlier date or the waitlist.")

    if doctor_name:
        doctor = _doctor_by_name(db, doctor_name)
        if doctor is None:
            return "No doctor by that name works at the clinic. Say that doctor isn't part of the team; offer list_doctors."
        doctors = [doctor]
    elif specialty:
        doctors = db.list_doctors(specialty=specialty, gender=gender, language=language)
        if not doctors:
            return ("The clinic has no doctor for that specialty with those preferences. Say so, and offer other "
                    "options or a transfer to staff.")
    else:
        return "Ask which doctor or specialty the patient wants, then call check_availability again."

    patient = _patient(db, patient_id)
    problem = _age_problem(patient, doctors[0]["specialty"], d_from)
    if problem:
        return problem

    def matching(start: date, end: date) -> list[dict]:
        found = []
        for doc in doctors:
            for s in db.free_slots(doc["doctor_id"], start, end, now=now):
                if part_of_day:
                    lo, hi = PARTS_OF_DAY[part_of_day.lower()]
                    if not lo <= s["start_at"].hour < hi:
                        continue
                found.append({**s, "doctor": doc["name"]})
        return sorted(found, key=lambda s: s["start_at"])

    slots = matching(d_from, min(d_to, last_day))
    note = ""
    one_day = d_from == d_to
    if not slots:   # nothing in the range: look for the next available dates instead
        slots = matching(d_to + timedelta(days=1), last_day)
        if not slots:
            return ("No free appointments in the next "
                    f"{HORIZON.days} days for that. Offer the waitlist (add_to_waitlist) or other doctors.")
        note = "Nothing is free in the dates asked for. Say so first. "
        one_day = False
    if wanted is None:
        return note + _describe(slots, one_day)
    # a specific time: that time first if free, then the nearest times on the same (earliest) day
    return note + _nearest(slots, wanted)


def _minutes(s: dict) -> int:
    t = s["start_at"].astimezone(QATAR_TZ)
    return t.hour * 60 + t.minute


def _nearest(slots: list[dict], wanted: int) -> str:
    """The asked-for time if free, else the nearest free times that day, with booking keys."""
    exact = [s for s in slots if _minutes(s) == wanted]
    first_day = (exact[0] if exact else slots[0])["start_at"].date()
    near = sorted([s for s in slots if s["start_at"].date() == first_day],
                  key=lambda s: (abs(_minutes(s) - wanted), s["start_at"]))[:SLOTS_OFFERED]
    lines = "; ".join(f"{spoken_time(s['start_at'])} with {s['doctor']} [book with: doctor='{s['doctor']}', "
                      f"start='{slot_key(s['start_at'])}']" for s in near)
    if exact:
        return f"That time is free: {lines.split('; ')[0]}. Book it if it's the time the caller chose."
    return (f"That exact time isn't free. Say so, then offer the nearest times (never read out the bracketed "
            f"part): {lines}.")


def _describe(slots: list[dict], one_day: bool) -> str:
    """Free times as the caller should hear them: for one day, its free hours (or the few times left); otherwise
    the earliest days, so the caller can choose a day first."""
    days: dict[tuple, list[dict]] = {}
    for s in slots:
        days.setdefault((s["start_at"].astimezone(QATAR_TZ).date(), s["doctor"]), []).append(s)
    order = sorted(days)
    if one_day:
        shown = [days[k] for k in order if k[0] == order[0][0]]
        return ("Free times (never read out the bracketed parts): " + "; ".join(describe_day(d) for d in shown) +
                ". Tell the caller the free hours as ranges, not as a list of times (name the times only when "
                "just a few are left), and ask what time suits them. When they name a time inside the free hours, "
                "call book_appointment with that time; if it isn't free, the tool offers the nearest ones.")
    first_days = sorted({k[0] for k in order})[:DAYS_SUGGESTED]
    shown = [days[k] for k in order if k[0] in first_days]
    return ("Free days (never read out the bracketed parts): " + "; ".join(describe_day(d) for d in shown) +
            f". Suggest the earliest day first ({spoken_day(first_days[0])}) and ask if it suits; describe its "
            "free hours only once the caller has chosen a day.")


# --- Two-step actions ---

def _gate(state: BookingState, kind: str, user_turns: int) -> dict | str:
    p = state.pending
    if p is None or p["kind"] != kind:
        return f"There's nothing waiting to confirm. Start with {kind}_appointment."
    if user_turns <= p["user_turns"]:
        return "The caller hasn't answered yet. Read the details back and wait for their yes before confirming."
    return p


def book(db: ClinicDB, state: BookingState, patient_id: int, doctor_name: str, start_text: str, call_id: str,
         user_turns: int, now: datetime | None = None) -> str:
    """Step 1: hold the slot and return the read-back."""
    now = now or now_qatar()
    doctor = _doctor_by_name(db, doctor_name or "")
    start = _parse_start(start_text or "")
    if doctor is None or start is None:
        return "Use a doctor and start exactly as check_availability returned them."
    patient = _patient(db, patient_id)
    if db.count_no_shows(patient_id, now - NO_SHOW_WINDOW) >= NO_SHOW_LIMIT:
        return ("This patient missed three appointments in the last six months, so new bookings need the "
                "consultation fee paid in advance, which can't be done by phone. Explain kindly, and "
                f"{OFFER_STAFF}")
    problem = _age_problem(patient, doctor["specialty"], start.date())
    if problem:
        return problem
    upcoming = db.list_appointments(patient_id, now=now)
    if len(upcoming) >= MAX_UPCOMING:
        return (f"The patient already has {len(upcoming)} upcoming appointments, the most that can be booked by "
                "phone. Offer to cancel or move one of them first.")

    try:
        hold = db.hold_slot(patient_id, doctor["doctor_id"], start, call_id=call_id, now=now)
    except BookingError as e:   # not a free slot: offer the nearest ones in the same reply, saving a round trip
        free = [{**s, "doctor": doctor["name"]}
                for s in db.free_slots(doctor["doctor_id"], start.date(), start.date(), now=now)]
        nearest = (" " + _nearest(free, start.hour * 60 + start.minute)) if free else ""
        return str(e) + nearest
    state.pending = {"kind": "book", "hold_id": hold["appointment_id"], "user_turns": user_turns}
    same = [a for a in upcoming if a["specialty"] == doctor["specialty"]]
    warning = (f" Before asking, mention they already have an appointment in {doctor['specialty']} on "
               f"{spoken_time(same[0]['start_at'])} and ask if they'd rather move that one instead."
               if same else "")
    return (f"The slot is held for a few minutes; nothing is booked yet.{warning} Read back: {doctor['name']}, "
            f"{spoken_time(start)}, and ask if that's correct. Then wait for their answer. If yes, call "
            f"confirm.")


def confirm_booking(db: ClinicDB, state: BookingState, user_turns: int, now: datetime | None = None) -> str:
    p = _gate(state, "book", user_turns)
    if isinstance(p, str):
        return p
    state.pending = None
    try:
        appt = db.confirm_hold(p["hold_id"], now=now)
    except HoldExpired:
        return "The hold ran out before the caller confirmed. Check availability again and offer times."
    doctor = db.get_doctor(appt["doctor_id"])
    print(f"[BOOK] booked appointment for patient_id={appt['patient_id']}")
    return (f"Booked: {doctor['name']}, {spoken_time(appt['start_at'])}. Confirm it to the caller. For arrival "
            f"advice, use the knowledge base.")


def list_appointments(db: ClinicDB, state: BookingState, patient_id: int, now: datetime | None = None) -> str:
    upcoming = db.list_appointments(patient_id, now=now or now_qatar())
    state.listed = {i: a["appointment_id"] for i, a in enumerate(upcoming, 1)}
    if not upcoming:
        return "The patient has no upcoming appointments."
    lines = "; ".join(f"[ref {i}] {spoken_time(a['start_at'])} with {a['doctor_name']} ({a['specialty']})"
                      for i, a in enumerate(upcoming, 1))
    return (f"Upcoming appointments: {lines}. Refer to them by date and doctor; never say the ref numbers, "
            f"they're only for the tools.")


def _listed_appointment(db: ClinicDB, state: BookingState, patient_id: int, ref: int,
                        now: datetime) -> dict | str:
    appointment_id = state.listed.get(ref)
    if appointment_id is None:
        return "Call list_my_appointments first, then pass the ref of the appointment the caller means."
    for a in db.list_appointments(patient_id, now=now):
        if a["appointment_id"] == appointment_id:
            return a
    return "That appointment can't be changed any more (it has started, passed, or was already cancelled)."


def cancel(db: ClinicDB, state: BookingState, patient_id: int, ref: int, user_turns: int,
           now: datetime | None = None) -> str:
    """Step 1: read back the appointment to cancel (with a late warning under 4 hours)."""
    now = now or now_qatar()
    appt = _listed_appointment(db, state, patient_id, ref, now)
    if isinstance(appt, str):
        return appt
    late = appt["start_at"] - now < LATE_CHANGE
    state.pending = {"kind": "cancel", "appointment_id": appt["appointment_id"], "user_turns": user_turns}
    warning = (" It's less than 4 hours away, so this is a late cancellation; tell them, and check the knowledge "
               "base for what that means (no-show fee policy)." if late else "")
    return (f"Nothing is cancelled yet. Read back: the appointment with {appt['doctor_name']} on "
            f"{spoken_time(appt['start_at'])}, and ask if they want to cancel it.{warning} Then wait for their "
            f"answer. If yes, call confirm.")


def confirm_cancellation(db: ClinicDB, state: BookingState, patient_id: int, user_turns: int,
                         now: datetime | None = None) -> str:
    p = _gate(state, "cancel", user_turns)
    if isinstance(p, str):
        return p
    state.pending = None
    try:
        result = db.cancel_appointment(p["appointment_id"], patient_id, now=now)
    except BookingError as e:
        return str(e)
    print(f"[BOOK] cancelled appointment for patient_id={patient_id}")
    return "Cancelled. Confirm it to the caller" + (" and remind them it was a late cancellation." if result[
        "late_change"] else ".")


def reschedule(db: ClinicDB, state: BookingState, patient_id: int, ref: int, doctor_name: str, start_text: str,
               call_id: str, user_turns: int, now: datetime | None = None) -> str:
    """Step 1: hold the new slot (the old appointment stays until confirmed) and return the read-back."""
    now = now or now_qatar()
    appt = _listed_appointment(db, state, patient_id, ref, now)
    if isinstance(appt, str):
        return appt
    doctor = _doctor_by_name(db, doctor_name or "")
    start = _parse_start(start_text or "")
    if doctor is None or start is None:
        return "Use a doctor and start exactly as check_availability returned them."
    try:
        hold = db.hold_slot(patient_id, doctor["doctor_id"], start, call_id=call_id,
                            replaces=appt["appointment_id"], now=now)
    except BookingError as e:
        return str(e)
    late = appt["start_at"] - now < LATE_CHANGE
    state.pending = {"kind": "reschedule", "appointment_id": appt["appointment_id"],
                     "hold_id": hold["appointment_id"], "user_turns": user_turns}
    warning = " The current appointment is less than 4 hours away, so this counts as a late change; tell them." \
        if late else ""
    return (f"Nothing is changed yet; the new time is held for a few minutes. Read back: move the appointment with "
            f"{appt['doctor_name']} on {spoken_time(appt['start_at'])} to {doctor['name']}, {spoken_time(start)}."
            f"{warning} Ask if that's correct and wait for their answer. If yes, call confirm.")


def confirm_reschedule(db: ClinicDB, state: BookingState, patient_id: int, user_turns: int,
                       now: datetime | None = None) -> str:
    p = _gate(state, "reschedule", user_turns)
    if isinstance(p, str):
        return p
    state.pending = None
    try:
        db.reschedule(p["appointment_id"], patient_id, p["hold_id"], now=now)
    except HoldExpired:
        return ("The hold on the new time ran out, so nothing changed; the original appointment stands. Check "
                "availability again.")
    except BookingError as e:
        return str(e) + " The original appointment stands."
    print(f"[BOOK] rescheduled appointment for patient_id={patient_id}")
    return "Rescheduled. Confirm the new time to the caller; the old appointment is cancelled."


def confirm(db: ClinicDB, state: BookingState, patient_id: int, user_turns: int,
            now: datetime | None = None) -> str:
    """Step 2 of whichever action is pending. The code, not the model, decides what gets confirmed."""
    kind = state.pending["kind"] if state.pending else None
    if kind == "book":
        return confirm_booking(db, state, user_turns, now)
    if kind == "cancel":
        return confirm_cancellation(db, state, patient_id, user_turns, now)
    if kind == "reschedule":
        return confirm_reschedule(db, state, patient_id, user_turns, now)
    return "There's nothing waiting to confirm. Start with book_appointment, cancel_appointment, or reschedule_appointment."


def add_to_waitlist(db: ClinicDB, patient_id: int, date_from_text: str, date_to_text: str,
                    doctor_name: str | None = None, specialty: str | None = None, now: datetime | None = None) -> str:
    d_from, d_to = _parse_date(date_from_text), _parse_date(date_to_text)
    if d_from is None or d_to is None:
        return "Pass the dates as YYYY-MM-DD."
    doctor = _doctor_by_name(db, doctor_name) if doctor_name else None
    if doctor_name and doctor is None:
        return "No doctor by that name works at the clinic."
    if doctor is None and not specialty:
        return "Say which doctor or specialty the waitlist is for."
    db.add_to_waitlist(patient_id, d_from, d_to, doctor_id=doctor["doctor_id"] if doctor else None,
                       specialty=None if doctor else specialty, now=now)
    who = doctor["name"] if doctor else f"any {specialty} doctor"
    return (f"Added to the waitlist for {who}, {d_from.day} {d_from:%B} to {d_to.day} {d_to:%B}. Tell them clinic "
            f"staff will see it and contact them if a slot frees up. Don't promise an automatic call.")
