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

def test_a_full_day_is_described_by_its_free_hours_not_a_list(c):
    # "9:00, 9:20 or 9:40" sounded like the only times (2026-10-22): a busy-free day is told as its free hours
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="Dr. Omar Benali", now=NOW)
    assert "free from 9:00 in the morning to 2:40 in the afternoon" in msg and "every 20 minutes" in msg
    assert "9:20" not in msg and "ask what time suits" in msg


def test_a_gap_splits_the_free_hours(c):
    book_and_confirm(c, c["other"], "2026-10-17 11:00")
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="Dr. Omar Benali", now=NOW)
    assert "from 9:00 in the morning to 10:40 in the morning and from 11:20 in the morning to 2:40" in msg


def test_a_day_with_few_times_left_names_them(c):
    for t in ["09:00", "09:20", "09:40", "10:00", "10:20", "10:40", "11:00", "11:20", "11:40", "12:00", "12:20",
              "12:40", "13:00", "13:20", "13:40"]:
        c["db"].book_slot(c["other"], c["benali"], at(date(2026, 10, 17), t), "reception", now=NOW)
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="Dr. Omar Benali", now=NOW)
    assert "only these times" in msg and msg.count("[book with:") == 3
    assert "start='2026-10-17 14:00'" in msg and "start='2026-10-17 14:40'" in msg


def test_a_range_of_days_suggests_the_earliest_day_first(c):
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-13", "2026-10-31",
                                     doctor_name="Dr. Omar Benali", now=NOW)
    assert "Suggest the earliest day first (Wednesday 14 October)" in msg
    assert "describe its free hours only once the caller has chosen a day" in msg


def test_booking_a_time_inside_the_free_hours(c):
    msg = booking.book(c["db"], BookingState(), c["adult"], "Dr. Omar Benali", "2026-10-17 10:20", CALL,
                       user_turns=1, now=NOW)
    assert "nothing is booked yet" in msg and "10:20" in msg


def test_booking_a_taken_time_offers_the_nearest_ones_in_the_same_reply(c):
    book_and_confirm(c, c["other"], "2026-10-17 13:20")
    msg = booking.book(c["db"], BookingState(), c["adult"], "Dr. Omar Benali", "2026-10-17 13:20", CALL,
                       user_turns=1, now=NOW)
    assert "isn't free" in msg and "start='2026-10-17 13:00'" in msg and "start='2026-10-17 13:40'" in msg


def test_doctor_names_are_matched_without_the_title_or_case(c):
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="omar benali", now=NOW)
    assert "[book with:" in msg


def test_unknown_doctor(c):
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="Dr. Okafo", now=NOW)
    assert "isn't part of the team" in msg


def test_part_of_day_filters_slots(c):
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="Dr. Omar Benali",
                                     part_of_day="afternoon", now=NOW)
    assert "free from 12:00 in the afternoon to 2:40 in the afternoon" in msg and "in the morning" not in msg


def test_nothing_in_range_offers_the_next_available_dates(c):
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-18", doctor_name="Dr. Omar Benali", now=NOW)
    assert "Nothing is free in the dates asked for" in msg and "Monday 19 October" in msg


def test_a_specific_time_is_offered_even_when_it_is_not_among_the_earliest(c):
    # the call that found this: afternoon gave 12:00, 12:20, 12:40, and 13:20 looked unavailable
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="Dr. Omar Benali",
                                     part_of_day="afternoon", time_text="13:20", now=NOW)
    assert msg.startswith("That time is free") and "start='2026-10-17 13:20'" in msg.split(";")[0]


def test_a_taken_time_offers_the_nearest_ones(c):
    booking.book(c["db"], BookingState(), c["other"], "Dr. Omar Benali", "2026-10-17 13:20", "other-call",
                 user_turns=1, now=NOW)   # someone else is holding 13:20
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="Dr. Omar Benali",
                                     time_text="13:20", now=NOW)
    assert "isn't free" in msg and "13:20'" not in msg
    assert "start='2026-10-17 13:00'" in msg and "start='2026-10-17 13:40'" in msg


def test_a_badly_written_time_is_refused(c):
    msg = booking.check_availability(c["db"], c["adult"], "2026-10-17", doctor_name="Dr. Omar Benali",
                                     time_text="1:20 pm", now=NOW)
    assert "HH:MM" in msg


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
