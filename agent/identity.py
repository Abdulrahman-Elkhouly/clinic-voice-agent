"""Patient identification for one call: verify, find (forgot file number), register.

Kept separate from the agent so every rule is enforced in code and testable without the conversation LLM:
attempt limits, the cross-call lockout, privacy-safe messages, and registration that is never half-done.
Each function returns the text handed to the LLM as the tool result.

Names are compared by an isolated LLM judge (name_judge.py), passed in as `judge` so tests can replace it.
"""
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from db import ClinicDB, is_valid_file_number, now_qatar
from name_judge import NameJudgeError, pick_matching_name

MAX_FAILED_ATTEMPTS = 3   # per call
PHONE_DIGITS = 8          # Qatar mobile numbers (fictional setting; see the switch checklist)

Judge = Callable[[str, list[str]], "int | None"]

OFFER_STAFF = "Offer to transfer the caller to a member of staff (transfer_to_human)."
LOCKED_FOR_CALL = ("Identification is locked for the rest of this call after too many failed attempts. "
                   f"Do not try again. {OFFER_STAFF} You can still answer general questions.")
LOCKED_FILE = (f"Phone verification isn't possible for these details right now. Do not try again on this call. "
               f"{OFFER_STAFF}")
CANT_CONFIRM = f"I can't confirm the right file by phone. Don't reveal anything. {OFFER_STAFF}"
NO_MATCH = ("Those details don't match our records. Say exactly that. Never say which detail was wrong "
            "or whether such a file exists.")


@dataclass
class CallIdentity:
    """Identification state for one call. One active patient at a time."""
    failed_attempts: int = 0
    patient_id: int | None = None
    patient_first_name: str | None = None
    pending_registration: dict | None = None   # details read back, waiting for the caller's yes

    @property
    def locked(self) -> bool:
        return self.failed_attempts >= MAX_FAILED_ATTEMPTS

    def set_patient(self, patient: dict) -> str:
        switched = self.patient_id is not None and self.patient_id != patient["patient_id"]
        self.patient_id = patient["patient_id"]
        self.patient_first_name = first_name(patient["full_name"])
        print(f"[ID] verified patient_id={self.patient_id}")   # never names, DOBs, or file numbers
        return " The active patient is now this person; earlier details belonged to someone else." if switched else ""


def first_name(full_name: str) -> str:
    return full_name.split()[0] if full_name.split() else full_name


def _judge_one(judge: Judge, heard: str, patients: list[dict]) -> dict | None | str:
    """The patient whose name the judge matched, None if no match, or "error" if the judge failed."""
    try:
        i = judge(heard, [p["full_name"] for p in patients])
    except NameJudgeError as e:
        print(f"[ID] name judge failed: {type(e).__name__}")
        return "error"
    return patients[i] if i is not None else None


def spoken_digits(number: str) -> str:
    return " ".join(number)


def parse_dob(text: str, today: date) -> date | None:
    """'YYYY-MM-DD' -> date, or None if missing, malformed, in the future, or over 120 years ago."""
    try:
        d = date.fromisoformat(text.strip())
    except (ValueError, AttributeError):
        return None
    return d if date(today.year - 120, 1, 1) <= d <= today else None


def parse_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text or "")
    if digits.startswith("974") and len(digits) == PHONE_DIGITS + 3:   # +974 country code
        digits = digits[3:]
    return digits if len(digits) == PHONE_DIGITS else None


BAD_DOB = "That date of birth isn't a valid past date. Ask for it again. Pass it as YYYY-MM-DD."
BAD_PHONE = f"That phone number isn't {PHONE_DIGITS} digits. Ask for it again and read it back digit by digit."


def _failed(state: CallIdentity) -> str:
    state.failed_attempts += 1
    if state.locked:
        return NO_MATCH + " " + LOCKED_FOR_CALL
    left = MAX_FAILED_ATTEMPTS - state.failed_attempts
    return NO_MATCH + f" The caller may try {left} more time{'s' if left != 1 else ''}."


def verify(db: ClinicDB, state: CallIdentity, file_number: str, dob_text: str,
           now: datetime | None = None) -> str:
    """Returning patient who knows their file number."""
    now = now or now_qatar()
    if state.locked:
        return LOCKED_FOR_CALL
    digits = re.sub(r"\D", "", file_number or "")
    if not is_valid_file_number(digits):   # usually a misheard digit: not counted as an attempt
        return ("That file number doesn't look right (it fails our check). Ask the caller to repeat it "
                "slowly, digit by digit. Don't say whether the file exists.")
    dob = parse_dob(dob_text, now.date())
    if dob is None:
        return BAD_DOB
    if db.verification_locked(digits, now):
        return LOCKED_FILE

    patient = db.verify_patient(digits, dob)
    db.record_verification(digits, patient is not None, now)
    if patient is None:
        return _failed(state)
    switched = state.set_patient(patient)
    return (f"Verified. The patient's first name is {state.patient_first_name}. Greet them by first name. "
            f"Don't read out any other personal details.{switched}")


def find(db: ClinicDB, state: CallIdentity, dob_text: str, phone_text: str, full_name: str | None = None,
         now: datetime | None = None, judge: Judge = pick_matching_name) -> str:
    """Returning patient who forgot their file number: date of birth + phone; name only to break a tie."""
    now = now or now_qatar()
    if state.locked:
        return LOCKED_FOR_CALL
    dob = parse_dob(dob_text, now.date())
    if dob is None:
        return BAD_DOB
    phone = parse_phone(phone_text)
    if phone is None:
        return BAD_PHONE

    matches = db.find_by_dob_phone(dob, phone)
    if not matches:
        return _failed(state)
    if len(matches) > 1:
        if not full_name:
            return ("More than one file matches. Ask for the patient's full name, then call find_patient "
                    "again with the same date of birth and phone plus the name. Don't reveal anything yet.")
        picked = _judge_one(judge, full_name, matches)
        if not isinstance(picked, dict):   # no match, or the judge failed: never guess
            return CANT_CONFIRM
        matches = [picked]

    patient = matches[0]
    switched = state.set_patient(patient)
    return (f"Verified. The patient's first name is {state.patient_first_name}. Their file number is "
            f"{spoken_digits(patient['file_number'])}. Read it out digit by digit and offer to repeat it. "
            f"Don't read out any other personal details.{switched}")


def spell(name: str) -> str:
    """'Sara Mansour' -> 'S-A-R-A, M-A-N-S-O-U-R'"""
    return ", ".join("-".join(part.upper()) for part in name.split())


def prepare_registration(state: CallIdentity, full_name: str, dob_text: str, phone_text: str,
                         guardian_phone_text: str | None, user_turns: int, now: datetime | None = None) -> str:
    """Step 1 of 2: validate and hold the details, and say what to read back. Creates nothing.
    `user_turns` is how many times the caller has spoken so far; step 2 requires that they speak again."""
    now = now or now_qatar()
    name = " ".join((full_name or "").split())
    if len(name) < 2:
        return "The patient's full name is missing. Ask for it and ask them to spell it."
    dob = parse_dob(dob_text, now.date())
    if dob is None:
        return BAD_DOB
    phone = parse_phone(phone_text)
    if phone is None:
        return BAD_PHONE
    guardian = None
    if guardian_phone_text:
        guardian = parse_phone(guardian_phone_text)
        if guardian is None:
            return "The guardian's phone number isn't valid. Ask for it again and read it back digit by digit."

    state.pending_registration = {"full_name": name, "dob": dob, "phone": phone, "guardian": guardian,
                                  "user_turns": user_turns}
    guardian_line = f", guardian's number {spoken_digits(guardian)}" if guardian else ""
    # Registration is the one place a birth date is read back: there is no stored date yet, and a wrong one
    # in a new file would make every future verification fail
    return (f"Nothing is registered yet. Read these details back to the caller and ask if they're all correct: "
            f"name {name}, spelled {spell(name)}; born {dob.day} {dob:%B %Y}; mobile {spoken_digits(phone)}"
            f"{guardian_line}. Then stop and wait for their answer. If they confirm, call confirm_registration. "
            f"If something is wrong, call register_patient again with the corrected details.")


def confirm_registration(db: ClinicDB, state: CallIdentity, user_turns: int, now: datetime | None = None,
                         judge: Judge = pick_matching_name) -> str:
    """Step 2 of 2: create the file, only if the caller has spoken since the details were read back.
    Enforced here, in code, because the model was seen registering without any read-back."""
    pending = state.pending_registration
    if pending is None:
        return "There's no registration waiting. Collect the details and call register_patient first."
    if user_turns <= pending["user_turns"]:
        return ("The caller hasn't confirmed yet. Read the details back, then wait for their answer before "
                "calling confirm_registration.")
    state.pending_registration = None
    return register(db, state, pending["full_name"], pending["dob"].isoformat(), pending["phone"],
                    pending["guardian"], now=now, judge=judge)


def register(db: ClinicDB, state: CallIdentity, full_name: str, dob_text: str, phone_text: str,
             guardian_phone_text: str | None = None, now: datetime | None = None,
             judge: Judge = pick_matching_name) -> str:
    """Creates the file (after the caller confirmed; see confirm_registration). One step at the end,
    so a dropped call never leaves a half-made record."""
    now = now or now_qatar()
    name = " ".join((full_name or "").split())
    if len(name) < 2:
        return "The patient's full name is missing. Ask for it and ask them to spell it."
    dob = parse_dob(dob_text, now.date())
    if dob is None:
        return BAD_DOB
    phone = parse_phone(phone_text)
    if phone is None:
        return BAD_PHONE
    guardian = None
    if guardian_phone_text:
        guardian = parse_phone(guardian_phone_text)
        if guardian is None:
            return "The guardian's phone number isn't valid. Ask for it again and read it back digit by digit."

    # Duplicate check on the digits; the judge decides from the (already collected) name if it's the same person
    existing = db.find_by_dob_phone(dob, phone)
    if existing:
        same = _judge_one(judge, name, existing)
        if same == "error":
            return f"Registration can't be completed by phone right now. Don't reveal anything. {OFFER_STAFF}"
        if same is not None:
            # Name, birthday and phone all match: this caller already has a file. They proved the same
            # details the forgot-file-number path asks for, so they count as verified.
            switched = state.set_patient(same)
            return (f"This patient already has a file, so no new one was created. They are now verified. Tell "
                    f"them kindly that they're already registered, and give their file number digit by digit: "
                    f"{spoken_digits(same['file_number'])}. Offer to repeat it.{switched}")
        # Same birthday and phone but a different person (e.g. a twin): register them

    created = db.create_patient(name, dob, phone, guardian, now=now)
    switched = state.set_patient({"patient_id": created["patient_id"], "full_name": name})
    return (f"Registered. The new file number is {spoken_digits(created['file_number'])}. Read it out digit by "
            f"digit, ask them to keep it for every future booking, and offer to repeat it.{switched}")
