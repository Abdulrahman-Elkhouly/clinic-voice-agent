"""Tests for the booking rules (agent/booking.py). No LLM involved.

    python -m pytest tests -v
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import booking  # noqa: E402
from booking import BookingState  # noqa: E402
from db import QATAR_TZ, ClinicDB  # noqa: E402

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=QATAR_TZ)   # Monday morning
CALL = "call-1"


@pytest.fixture
def c(tmp_path):
    db = ClinicDB(tmp_path / "test.db")
    benali = db.add_doctor("Dr. Omar Benali", "orthopedics", "male", ["Arabic", "English", "French"])
    for wd in (SAT, MON, WED):
        db.add_schedule(benali, wd, "09:00", "15:00", 20, date(2026, 1, 1))
    haddad = db.add_doctor("Dr. Layla Haddad", "pediatrics", "female", ["Arabic", "English", "French"])
    for wd in (SAT, SUN, MON, TUE, WED):
        db.add_schedule(haddad, wd, "08:00", "14:00", 15, date(2026, 1, 1))
    nasser = db.add_doctor("Dr. Samir Nasser", "internal medicine", "male", ["Arabic", "English"])
    for wd in (SUN, TUE, THU):
        db.add_schedule(nasser, wd, "08:00", "14:00", 20, date(2026, 1, 1))
    adult = db.create_patient("Fatima Al-Naimi", date(1990, 3, 14), "5512 3456", now=NOW)["patient_id"]
    child = db.create_patient("Yousef Al-Kaabi", date(2019, 5, 10), "3345 6789", now=NOW)["patient_id"]
    other = db.create_patient("Joseph Mathew", date(1969, 1, 30), "5590 4433", now=NOW)["patient_id"]
    return {"db": db, "benali": benali, "haddad": haddad, "adult": adult, "child": child, "other": other}


def at(d: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=QATAR_TZ)


def book_and_confirm(c, patient, start="2026-10-17 09:00", doctor="Dr. Omar Benali", state=None, now=NOW):
    state = state or BookingState()
    booking.book(c["db"], state, patient, doctor, start, CALL, user_turns=1, now=now)
    return booking.confirm_booking(c["db"], state, user_turns=2, now=now), state


# --- Wording ---

def test_times_are_worded_for_speech():
    assert booking.spoken_time(at(date(2026, 10, 17), "09:20")) == "Saturday 17 October at 9:20 in the morning"
    assert booking.spoken_time(at(date(2026, 10, 17), "15:00")) == "Saturday 17 October at 3:00 in the afternoon"
    assert booking.spoken_time(at(date(2026, 10, 17), "19:45")) == "Saturday 17 October at 7:45 in the evening"


# --- Availability ---

def test_offers_three_earliest_slots_with_booking_keys(c):
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="Dr. Omar Benali", now=NOW)
    assert "Saturday 17 October at 9:00 in the morning" in msg
    assert msg.count("[book with:") == 3 and "start='2026-10-17 09:40'" in msg and "More times exist" in msg


def test_doctor_names_are_matched_without_the_title_or_case(c):
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="omar benali", now=NOW)
    assert "[book with:" in msg


def test_unknown_doctor(c):
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="Dr. Okafo", now=NOW)
    assert "isn't part of the team" in msg


def test_part_of_day_filters_slots(c):
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="Dr. Omar Benali",
                                     part_of_day="afternoon", now=NOW)
    assert "at 12:00 in the afternoon" in msg and "in the morning" not in msg


def test_nothing_in_range_offers_the_next_available_dates(c):
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-18", doctor_name="Dr. Omar Benali", now=NOW)
    assert "Nothing is free in the dates asked for" in msg and "Monday 19 October" in msg


def test_specialty_search_with_gender(c):
    msg = booking.check_availability(c["db"], c["child"], "2026-10-17", specialty="pediatrics", gender="female",
                                     now=NOW)
    assert "Dr. Layla Haddad" in msg
    msg = booking.check_availability(c["db"], c["child"], "2026-10-17", specialty="pediatrics", gender="male",
                                     now=NOW)
    assert "no doctor for that specialty" in msg


def test_beyond_the_horizon(c):
    msg = booking.check_availability(c["db"], c["adult"], "2027-01-10", doctor_name="Dr. Omar Benali", now=NOW)
    assert "60 days ahead" in msg


@pytest.mark.parametrize("who, specialty, allowed", [
    ("adult", "pediatrics", False), ("child", "pediatrics", True),
    ("child", "internal medicine", False), ("adult", "internal medicine", True),
])
def test_age_rules(c, who, specialty, allowed):
    msg = booking.check_availability(c["db"], c[who], "2026-10-18", date_to_text="2026-10-20",
                                     specialty=specialty, now=NOW)
    assert ("[book with:" in msg) == allowed
    if not allowed:
        assert "family medicine" in msg


# --- Booking: two steps with the confirmation gate ---

def test_booking_needs_the_caller_to_answer_the_read_back(c):
    db, state = c["db"], BookingState()
    msg = booking.book(db, state, c["adult"], "Dr. Omar Benali", "2026-10-17 09:00", CALL, user_turns=3, now=NOW)
    assert "nothing is booked yet" in msg and "Saturday 17 October at 9:00 in the morning" in msg
    assert "hasn't answered" in booking.confirm_booking(db, state, user_turns=3, now=NOW)   # same turn: refused
    assert db.list_appointments(c["adult"], now=NOW) == []
    assert booking.confirm_booking(db, state, user_turns=4, now=NOW).startswith("Booked")
    assert len(db.list_appointments(c["adult"], now=NOW)) == 1


def test_confirm_with_nothing_pending(c):
    assert "nothing waiting" in booking.confirm_booking(c["db"], BookingState(), user_turns=5, now=NOW)
    assert "nothing waiting" in booking.confirm(c["db"], BookingState(), c["adult"], user_turns=5, now=NOW)


def test_single_confirm_runs_whichever_action_is_pending(c):
    db, state = c["db"], BookingState()
    booking.book(db, state, c["adult"], "Dr. Omar Benali", "2026-10-17 09:00", CALL, user_turns=1, now=NOW)
    assert "hasn't answered" in booking.confirm(db, state, c["adult"], user_turns=1, now=NOW)   # gate still holds
    assert booking.confirm(db, state, c["adult"], user_turns=2, now=NOW).startswith("Booked")
    booking.list_appointments(db, state, c["adult"], now=NOW)
    booking.cancel(db, state, c["adult"], 1, user_turns=3, now=NOW)
    assert booking.confirm(db, state, c["adult"], user_turns=4, now=NOW).startswith("Cancelled")
    assert db.list_appointments(c["adult"], now=NOW) == []


def test_held_slot_is_not_offered_or_bookable_by_another_call(c):
    db = c["db"]
    booking.book(db, BookingState(), c["adult"], "Dr. Omar Benali", "2026-10-17 09:00", CALL, user_turns=1, now=NOW)
    msg = booking.book(db, BookingState(), c["other"], "Dr. Omar Benali", "2026-10-17 09:00", "call-2",
                       user_turns=1, now=NOW)
    assert "just taken" in msg


def test_expired_hold_cannot_be_confirmed(c):
    db, state = c["db"], BookingState()
    booking.book(db, state, c["adult"], "Dr. Omar Benali", "2026-10-17 09:00", CALL, user_turns=1, now=NOW)
    msg = booking.confirm_booking(db, state, user_turns=2, now=NOW + timedelta(minutes=6))
    assert "ran out" in msg and db.list_appointments(c["adult"], now=NOW) == []


def test_slots_not_returned_by_availability_are_refused(c):
    msg = booking.book(c["db"], BookingState(), c["adult"], "Dr. Omar Benali", "2026-10-17 09:10", CALL,
                       user_turns=1, now=NOW)
    assert "no appointment at that time" in msg


def test_three_no_shows_block_phone_booking(c):
    db = c["db"]
    past = NOW - timedelta(days=40)
    for day in ("2026-09-05", "2026-09-07", "2026-09-09"):   # Sat, Mon, Wed in September
        appt = db.book_slot(c["adult"], c["benali"], at(date.fromisoformat(day), "09:00"), "reception", now=past)
        db.mark_outcome(appt["appointment_id"], "no_show", now=NOW)
    msg = booking.book(db, BookingState(), c["adult"], "Dr. Omar Benali", "2026-10-17 09:00", CALL, user_turns=1,
                       now=NOW)
    assert "paid in advance" in msg and "transfer" in msg


def test_at_most_three_upcoming_appointments(c):
    for start in ("2026-10-17 09:00", "2026-10-19 09:00", "2026-10-21 09:00"):
        book_and_confirm(c, c["adult"], start)
    msg = booking.book(c["db"], BookingState(), c["adult"], "Dr. Omar Benali", "2026-10-24 09:00", CALL,
                       user_turns=1, now=NOW)
    assert "most that can be booked" in msg


def test_same_specialty_appointment_triggers_a_warning(c):
    book_and_confirm(c, c["adult"], "2026-10-17 09:00")
    msg = booking.book(c["db"], BookingState(), c["adult"], "Dr. Omar Benali", "2026-10-19 09:00", CALL,
                       user_turns=1, now=NOW)
    assert "already have an appointment in orthopedics on Saturday 17 October" in msg and "rather move" in msg


# --- List, cancel, reschedule ---

def test_list_uses_reference_numbers_not_ids(c):
    book_and_confirm(c, c["adult"], "2026-10-17 09:00")
    state = BookingState()
    msg = booking.list_appointments(c["db"], state, c["adult"], now=NOW)
    assert "[ref 1] Saturday 17 October at 9:00 in the morning with Dr. Omar Benali" in msg
    assert list(state.listed) == [1]


def test_cancel_needs_a_listed_appointment_and_the_gate(c):
    db = c["db"]
    book_and_confirm(c, c["adult"], "2026-10-17 09:00")
    state = BookingState()
    assert "list_my_appointments first" in booking.cancel(db, state, c["adult"], 1, user_turns=1, now=NOW)
    booking.list_appointments(db, state, c["adult"], now=NOW)
    msg = booking.cancel(db, state, c["adult"], 1, user_turns=1, now=NOW)
    assert "Nothing is cancelled yet" in msg and "late" not in msg
    assert "hasn't answered" in booking.confirm_cancellation(db, state, c["adult"], user_turns=1, now=NOW)
    assert booking.confirm_cancellation(db, state, c["adult"], user_turns=2, now=NOW).startswith("Cancelled")
    assert db.list_appointments(c["adult"], now=NOW) == []


def test_cancel_within_four_hours_warns_it_is_late(c):
    db = c["db"]
    book_and_confirm(c, c["adult"], "2026-10-17 12:00")
    state, later = BookingState(), at(date(2026, 10, 17), "09:00")
    booking.list_appointments(db, state, c["adult"], now=later)
    assert "late cancellation" in booking.cancel(db, state, c["adult"], 1, user_turns=1, now=later)


def test_another_patients_appointment_cannot_be_cancelled_through_a_stale_list(c):
    db, state = c["db"], BookingState()
    book_and_confirm(c, c["adult"], "2026-10-17 09:00")
    booking.list_appointments(db, state, c["adult"], now=NOW)   # listed for Fatima...
    msg = booking.cancel(db, state, c["other"], 1, user_turns=1, now=NOW)   # ...then used for another patient
    assert "can't be changed" in msg
    assert len(db.list_appointments(c["adult"], now=NOW)) == 1


def test_reschedule_moves_the_appointment(c):
    db = c["db"]
    book_and_confirm(c, c["adult"], "2026-10-17 09:00")
    state = BookingState()
    booking.list_appointments(db, state, c["adult"], now=NOW)
    msg = booking.reschedule(db, state, c["adult"], 1, "Dr. Omar Benali", "2026-10-19 10:00", CALL,
                             user_turns=1, now=NOW)
    assert "Nothing is changed yet" in msg and "Monday 19 October at 10:00 in the morning" in msg
    assert booking.confirm_reschedule(db, state, c["adult"], user_turns=2, now=NOW).startswith("Rescheduled")
    [appt] = db.list_appointments(c["adult"], now=NOW)
    assert appt["start_at"] == at(date(2026, 10, 19), "10:00")


def test_failed_reschedule_keeps_the_original(c):
    db = c["db"]
    book_and_confirm(c, c["adult"], "2026-10-17 09:00")
    state = BookingState()
    booking.list_appointments(db, state, c["adult"], now=NOW)
    booking.reschedule(db, state, c["adult"], 1, "Dr. Omar Benali", "2026-10-19 10:00", CALL, user_turns=1, now=NOW)
    msg = booking.confirm_reschedule(db, state, c["adult"], user_turns=2, now=NOW + timedelta(minutes=10))
    assert "original appointment stands" in msg
    [appt] = db.list_appointments(c["adult"], now=NOW)
    assert appt["start_at"] == at(date(2026, 10, 17), "09:00")


def test_waitlist(c):
    msg = booking.add_to_waitlist(c["db"], c["adult"], "2026-10-17", "2026-10-24", doctor_name="Dr. Omar Benali",
                                  now=NOW)
    assert "Added to the waitlist for Dr. Omar Benali" in msg and "automatic" in msg
