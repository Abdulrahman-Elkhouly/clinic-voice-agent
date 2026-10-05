# Clinic Voice Agent: Project Plan

The voice agent is moving from the Nova Mobile telecom demo to a **clinic receptionist** that answers questions, identifies patients, and books appointments, first in the browser and later over real phone calls.

**Rule for every stage:** before writing code, list every possible event, edge case, and requirement for that stage, and decide how each is handled.

## Decisions so far

| Topic | Decision |
|---|---|
| Language | English first. Arabic later. |
| Scope | One clinic for now. |
| Patient ID | Depends on the clinic, so the format must be configurable. Patients must know their ID for future reference. |
| New callers | The agent registers them and assigns an ID. |
| Returning callers | Asked for their ID, with a recovery path if they forgot it. |
| Database | Mock SQLite now, replaced by the clinic's real system later. |

## Stages

### Stage 0: Save the current version
- `git init` and commit the working Nova Mobile version.
- One commit per stage from here on.

### Stage 1: Switch the domain to a clinic
- **Knowledge base:** services, doctors and specialties, opening hours, insurance accepted, visit preparation, cancellation policy, location and parking.
- **Prompts:** clinic tone, clinic name in the greeting.
- **Safety rules:** no medical advice or diagnosis. For urgent symptoms, tell the caller to contact emergency services or go to the ER.
- **New `eval/test_set.md`:** answerable questions, questions outside the knowledge base, and medical-advice questions the agent must decline.
- **Reused unchanged:** `rag.py`, `eval_rag.py`.

### Stage 2: Mock database
- **SQLite** file, no server to install.
- **Tables:** `patients`, `doctors`, `slots` (doctor availability), `appointments`.
- **`db.py`** is the only code that touches SQL. Switching to a real clinic system means rewriting this one file.
- **Seed script** with fake doctors, schedules, and patients.
- **Principle:** policies and how-to text come from RAG. Schedules and appointments (facts that change and must be exact) come from the database.

### Stage 3: Patient identification
See the full design below.

### Stage 4: Booking tools
- **Tools:** `check_availability`, `book_appointment`, `cancel_appointment`, `reschedule_appointment`, `list_my_appointments`.
- **Dates:** "next Tuesday afternoon" must become an exact time. The LLM needs today's date and the time zone in its instructions, and the code validates the result.
- **Confirm before acting:** read back the doctor, day, and time, then book.
- **No double booking:** enforced by a unique constraint in the database, not by the prompt.
- **Booking tools only exist after verification** (handoff from the reception agent).
- **Agent structure:** reception (identify) → booking agent / information agent (RAG).
- *Events table to be written before implementation.*

### Stage 5: Real phone calls (Twilio)
- **Path:** Twilio phone number → SIP trunk → LiveKit SIP → room → agent. A phone caller is just another participant in the room.
- **Caller ID** arrives as a participant attribute and can suggest a patient match. Date of birth is still verified, since numbers are shared and can be spoofed.
- **SMS** to send new patients their file number.
- **Expect:** lower audio quality (8 kHz) and more STT errors, a small monthly cost per number, and the ICENTER network block.
- *Events table to be written before implementation.*

### Stage 6: Transfer to a human
- **Cold transfer:** hand the call straight to a human's number (SIP transfer).
- **Warm transfer:** the agent calls the human first, briefs them, then connects the caller.
- **Triggers:** the caller asks for a person, the agent fails twice in a row, urgent or sensitive topics, 3 failed verification attempts.
- **After hours:** take a message or offer a callback.
- *Events table to be written before implementation.*

### Throughout every stage
- Extend the eval set with that stage's scenarios.
- **Privacy:** this is health data. No real patient data in logs or git. Mention recording consent if calls are recorded.

---

## Stage 3 design: patient identification

### Two IDs

| | `patient_id` (internal) | `file_number` (what the patient knows) |
|---|---|---|
| Who sees it | Only the database and code | The patient and clinic staff |
| Format | Auto-increment or UUID | Set by the clinic, configurable |
| Changes? | Never | Could change if the clinic switches systems |

All tables link through `patient_id`. A clinic's own format only affects how `file_number` is generated and checked.

### Default file number format (designed for voice)
- **Digits only**, e.g. `48213`. Letters are unreliable over STT ("B" vs "D", "M" vs "N").
- **Short:** 5–6 digits are enough for one clinic.
- **Check digit:** the last digit is calculated from the others (Luhn algorithm). If STT mishears one digit, the check fails and the agent asks again instead of looking up the wrong patient.

### Identification vs verification
A file number is written on cards and papers, and family members know each other's. Knowing it doesn't prove identity.

- **Identification:** file number (who you say you are)
- **Verification:** date of birth (proof)

Nothing is revealed (name, appointments, anything) until both match.

### Entry paths

```
"Have you visited us before?"
   │
   ├─ No (new) ──────► collect name, DOB, phone ──► duplicate check ──► create ──► give file number
   │
   ├─ Yes, has ID ───► file number ──► verify DOB ──► identified
   │
   └─ Yes, forgot ID ► name + DOB + phone ──► search ──► exactly 1 match ──► identified + remind ID
```

### Events: new patients

| Event | Handling |
|---|---|
| Says "new" but is already registered | Before creating, search name + DOB. If found: "It looks like you may already have a file with us," then switch to the forgot-ID path. Prevents duplicate records. |
| Not sure if they've been before | Treat as forgot-ID: search first, register only if nothing is found. |
| Hangs up mid-registration | Create the record only at the end, after everything is confirmed. A dropped call leaves nothing half-created. |
| Name misheard | Ask them to spell it, then read it back. |
| Hears their file number once and forgets it | Read it back digit by digit, offer to repeat, send it by SMS (Stage 5), and the clinic can tell them in person. |

### Events: returning patients with an ID

| Event | Handling |
|---|---|
| ID fails the check digit | "I didn't catch that correctly. Could you repeat it slowly?" (likely an STT error) |
| ID is valid but not found | Ask again once, then offer the forgot-ID path. |
| ID found, DOB doesn't match | Don't say which one was wrong, since that would confirm the ID exists. Say "Those details don't match our records." |
| Repeated mismatches | Limit to 3 attempts, then transfer to a human (Stage 6) or ask them to visit. Prevents guessing birth dates. |

### Events: forgot their ID

| Event | Handling |
|---|---|
| Exactly one match on name + DOB (+ phone) | Identified. Tell them their file number and offer SMS. |
| Multiple matches (same name and birthday) | Ask for one more detail: phone number or last visit date. |
| No match | Maybe a different spelling or phone. Offer: spell the name again → register as new → transfer to a human. |

### Events: who is the patient?

| Event | Handling |
|---|---|
| Parent calling for a child | Identify the patient (the child), not the caller. Ask: "Is this appointment for you or someone else?" |
| Caller booking for an elderly parent | Same. Optionally record who called. |
| One phone number shared by several patients | Fine, because the file number is the identifier, not the phone. |
| Caller refuses to give details | Answer general questions (RAG only) without identification. Booking requires identification. |

### Privacy rules
- Never read out a name, appointment, or other detail before verification.
- Never say "that ID belongs to someone else."
- Log `patient_id`, never names or birth dates.

### Effects on other stages
- **Stage 2:** `patients` table has `file_number` (unique), `name`, `dob`, `phone`, `created_at`. Lookups: `find_by_file_number`, `find_by_details(name, dob, phone)`.
- **Stage 4:** booking tools become available only after verification.
- **Stage 5:** caller ID suggests a match but never replaces DOB verification.

---

## Open questions
1. Is date of birth enough for verification, or does the clinic want something else (national ID, last 4 digits of phone)?
2. Plan for SMS with the file number in Stage 5? (small per-message Twilio cost)
3. Spelling Arabic names in English over the phone: is "best effort spelling + DOB match" acceptable for now?
4. Stage 1 knowledge base: written by the user (as for Nova Mobile) or drafted by Claude?
