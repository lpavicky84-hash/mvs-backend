# MVS CLASS MANAGER
# PUBLIC TEST + PORTAL OBJECTIVE TEST — SECURE ONLINE EXAM, COMPETITIVE SCORING & FAIR RANKING
## Code-audited Claude implementation specification
### Normal Tests preserved · Competitive/JEE-style mode added · Server-authoritative timing · Configurable integrity controls

---

# 0. SOURCE, SCOPE & HONEST AUDIT BOUNDARY

The latest user upload is named `mvs-backend-main (1).zip`.

The raw bytes of that upload were not available to the audit container, so the detailed source audit below was performed against the nearest available current archive:

`mvs-backend-main(20261103-073919).zip`

Claude MUST verify every referenced symbol and finding against the CURRENT repository before editing. Do not blindly apply old line numbers.

Static validation of the audited archive:

- 46 Python files parsed successfully.
- `mvs_app.js` passes `node --check`.

These are syntax checks only, not runtime certification.

This implementation must cover BOTH:

1. **Portal Objective Tests**
   - logged-in students
   - Teacher/Admin test creation
   - Student Test Player
   - ExamAttempt / result / ranking

2. **Public Shareable Objective Tests**
   - guest registration
   - `/t/{token}` public player
   - PublicExamAttempt
   - Admin public-test tracker
   - Teacher public responses

Do NOT create two unrelated exam engines. Reuse a shared scoring, attempt, timing, ranking and integrity service wherever safe.

---

# 1. OWNER'S REQUIRED EXPERIENCE

Preserve the existing Normal Objective Test.

Add a second selectable mode:

## NORMAL TEST

- Current objective-test behavior remains available.
- No negative marking by default.
- Existing tests remain compatible.

## COMPETITIVE TEST

JEE/Main-style configurable scoring:

- Correct answer: configurable positive marks, default template `+4`.
- Wrong answer: configurable negative marks, default template `-1`.
- Unanswered: configurable, default `0`.
- Teacher sees this choice during objective-test creation.
- Result clearly shows positive marks, negative deduction and final score.
- Ranking uses marks first, then actual completion duration.

Add configurable Exam Integrity / Secure Mode per test.

When secure mode is enabled:

- Starting the attempt creates one server-owned attempt session.
- Timer continues even if browser/app is closed.
- Tab/app/fullscreen violations are detected and logged.
- The configured policy may warn, flag or auto-submit.
- Refresh/reopen resumes the SAME attempt while time remains.
- No manual refresh should restart the timer.
- No normal browser implementation may falsely claim it can physically prevent every OS-level app switch, second device or AI use.

---

# 2. IMPORTANT BROWSER LIMITATION — DO NOT MAKE FALSE CLAIMS

A normal web browser CANNOT guarantee:

- physically preventing Alt+Tab;
- physically preventing phone app switching;
- preventing minimize at OS level;
- preventing a second phone/laptop;
- preventing screenshots on every device;
- preventing external AI usage;
- preventing developer tools with certainty.

A browser CAN:

- request fullscreen after a user gesture;
- detect `visibilitychange`;
- detect focus/blur;
- detect `fullscreenchange`;
- detect pagehide/unload;
- block or log copy/paste/context-menu/print shortcuts on the exam page;
- detect concurrent sessions;
- keep a server-authoritative timer;
- autosave answers;
- record integrity events;
- warn or auto-submit according to an explicit policy.

Build the strongest honest browser-based integrity system.

Never tell Admin/Teacher that a test is “100% cheat proof”.

Use neutral terms:

- Integrity event
- Focus lost
- Fullscreen exited
- Concurrent session detected
- Needs review

Do not automatically label a student a cheater.

Do not add covert camera, microphone, biometric recognition or continuous surveillance in this task. Any future media proctoring requires a separate explicit privacy specification, consent, retention policy and legal review.

---

# 3. VERIFIED CURRENT ARCHITECTURE

Current models include:

- `Exam`
- `ExamQuestion`
- `ExamAttempt`
- `ExamResult`
- `PublicExamAttempt`

Current flows include:

- Teacher creates Objective/MCQ tests.
- Teacher enables a shareable public link.
- Portal students attempt tests through `mvs_app.js`.
- Public guests attempt through `public_test.html`.
- Current MCQ grading uses `grading.grade_mcq`.
- Student/Admin/Teacher ranking has separate duplicated implementations.

Preserve:

- English/Hindi/Bilingual questions;
- question images;
- explanations;
- batch-aware test lists;
- existing result review;
- existing PDF/subjective tests;
- teacher public-response privacy;
- current notifications;
- existing test cards and normal mode.

---

# 4. CRITICAL FINDINGS FROM CURRENT CODE

## PT-01 — Public timer is client-memory only

`public_test.html` uses:

- `STARTMS = Date.now()`
- client `setInterval`
- client-side auto-submit

There is no server-owned public attempt start before submission.

Consequences:

- Reload/new browser context can restart the timer.
- Client clock can be changed.
- Direct submit API has no trusted elapsed-time proof.
- Browser close has no server finalization.
- Ranking cannot use trusted duration.

Fix with a server-owned attempt session.

---

## PT-02 — Public submit does not enforce schedule/duration on the server

`public_routes.py public_submit()`:

- validates guest details;
- grades submitted answers;
- stores one PublicExamAttempt;

but does not validate:

- attempt started_at;
- expires_at;
- elapsed time;
- scheduled start/end;
- server attempt token;
- secure-mode state;
- integrity violations.

Client timer is not security.

---

## PT-03 — Public questions can be fetched whenever link is enabled

`public_get_exam()` returns the questions if:

- token exists;
- public_enabled;
- Exam active;
- MCQ.

It does not enforce scheduled start or a server-created attempt session.

A competitive public exam must not expose questions before the allowed start.

---

## PT-04 — Public answer review is always returned immediately after submit

`public_submit()` returns:

- correct answer;
- explanation;
- full review

immediately after submission.

For a live competitive exam, an early submitter can share answers with others.

Add configurable result/solution release policy:

- Immediately after submit
- After this student's deadline
- After exam window closes
- Manual Teacher/Admin release

Competitive secure template should default to exam-close/manual release, not immediate answer reveal.

---

## PT-05 — Public one-attempt rule is application-only phone matching

Current code checks:

`exam_id + normalized phone`

before insert.

There is no demonstrated database uniqueness constraint or signed start session.

Concurrent requests can race, and a different phone number bypasses the rule.

Implement:

- database uniqueness/idempotency;
- server attempt token;
- rate limiting;
- optional OTP verification if existing approved infrastructure supports it;
- optional CAPTCHA/bot protection;
- do not require OTP by default without owner configuration.

---

## PT-06 — Public views can be inflated

Every public GET increments `public_views`.

Reloads, bots and previews may inflate opens.

Track:

- raw opens;
- deduplicated sessions;
- started attempts;
- completed attempts;

as separate metrics.

Do not call raw opens unique students.

---

## ST-01 — Portal timed attempt start is client-controlled

`mvs_app.js` uses localStorage:

`mvs_start_{exam_id}`

for unscheduled timed tests.

The user can clear/change localStorage.

The backend does not persist a per-student started_at/deadline before submission.

Replace with server-authoritative attempt start.

---

## ST-02 — Portal timer does not reliably server-auto-submit

The current portal `_plTimerStart()` updates the timer display.

It does not create a trusted server deadline or a server job that finalizes the attempt.

For unscheduled tests, server submit does not enforce elapsed duration.

Implement server expiry and auto-finalization.

---

## ST-03 — Reopening the portal player resets answer memory

`openExamPlayer()` resets:

- `_plAns = {}`
- `_plRev = {}`

while the local timer can continue.

Refresh/reopen can therefore lose answers but not time.

Implement server autosave + local recovery.

---

## ST-04 — Scheduled exam questions can be obtained before start via direct API

`student_get_exam()` fetches Exam by ID and returns questions.

It does not enforce:

- scheduled start;
- batch authorization;
- subject authorization;
- class authorization.

The UI checks future schedule, but direct API access can bypass UI.

This is a critical authorization/content-leak issue.

---

## ST-05 — Portal submit can target an unauthorized exam ID

`student_submit_exam()` checks:

- Exam exists and active;
- already submitted;
- scheduled end.

It does not apply the same batch/subject/class access checks used in the exam list.

A student who discovers an exam ID may directly submit to another batch/subject test.

Use one shared `authorize_student_exam()` service for:

- detail;
- start;
- autosave;
- submit;
- result;
- paper;
- ranking;
- solution.

---

## ST-06 — Portal scheduled start is not enforced server-side on submit

Current submit rejects after the scheduled end, but does not reject before scheduled start.

UI-only start restrictions are insufficient.

---

## ST-07 — Question paper endpoint is not fully batch/schedule scoped

`student_exam_paper()` checks subject and class, but the inspected path does not use the same strict selected-batch filter and start gate as the test list.

Unify authorization.

---

## ST-08 — Ranking endpoint is not student-scoped

`student_exam_ranking()` fetches an exam by ID without applying batch/subject/class visibility rules.

A student should not access an unauthorized exam's ranking by changing the ID.

---

## ST-09 — Teacher ranking endpoint does not verify ownership

`teacher_exam_ranking()` resolves the teacher profile but the inspected function then calls a generic ranking helper without proving that the exam belongs to that teacher.

Teacher must see only authorized tests.

---

## ST-10 — Attempts are destructively deleted before resubmission

Current submit deletes existing ExamAttempt rows for the student/exam, then creates a new row.

Risks:

- attempt history disappears;
- old ExamResult rows can become orphaned because `attempt_id` is not shown as a protected FK;
- audit/timing/integrity history is lost;
- concurrent submits can create inconsistent data.

Do not delete attempt history.

Use attempt version/revision and one active attempt constraint.

---

## ST-11 — No trusted duration exists

`ExamAttempt` has submitted_at but no:

- started_at;
- expires_at;
- elapsed_seconds;
- last heartbeat;
- submission reason;
- auto-submitted flag.

`PublicExamAttempt` similarly lacks trusted attempt timing.

The requested ranking cannot be implemented correctly until this is fixed.

---

## GR-01 — Current grading has no negative marking

`grading.grade_mcq()` gives:

- correct → `max_marks`
- wrong/unanswered → `0`

Add a central scoring engine supporting both Normal and Competitive policies.

---

## GR-02 — Correct answer is stored by option text

Current `correct_option` and submitted selection use option text.

This is fragile for:

- repeated option text;
- translated option text;
- randomized option order;
- punctuation/format edits.

Introduce stable option IDs/keys while preserving legacy tests.

---

## RK-01 — Current ranking ignores completion time

Student, Teacher and Admin each contain a duplicated `_exam_ranking_rows()`.

They currently sort by:

1. marks descending
2. student name alphabetically

This does not meet the requested rule.

---

## RK-02 — Ranking logic is duplicated and can drift

Ranking is independently implemented in:

- student routes;
- teacher routes;
- admin routes.

Create one shared ranking service.

---

## RK-03 — Ranking has an N+1 student lookup

Current ranking queries StudentProfile inside the attempt loop.

Batch-load students/users instead.

---

## EDIT-01 — Live test questions can be fully replaced after attempts exist

Current Teacher UI says previous submissions remain safe.

Current PATCH can delete all ExamQuestion rows and recreate them even when attempts already exist.

That can invalidate:

- old answer text mapping;
- marks;
- total marks;
- rankings;
- result explanations.

After any attempt starts:

- lock questions;
- lock scoring;
- lock security policy;
- lock timing/ranking policy;
- allow safe metadata edits only;
- use Duplicate/Create Revision for substantive changes.

Attempts must retain the exact exam revision they attempted.

---

# 5. PRESERVE NORMAL TESTS

Do not force the competitive system onto existing tests.

Migration defaults:

- `exam_mode = NORMAL`
- wrong mark = 0
- unanswered mark = 0
- integrity mode = OFF
- current result release behavior preserved for legacy tests
- current ranking legacy rows remain labeled Legacy where duration is unavailable

Existing tests and attempts must remain viewable.

Do not fabricate elapsed times for historical attempts.

---

# 6. EXAM CREATION — PREMIUM MODE SELECTOR

When Teacher clicks **New Objective Test**, first ask:

## Choose Test Mode

### Normal Objective Test

- Existing auto-grading
- No negative marking by default
- Flexible normal practice/class test

### Competitive Exam

- JEE-style scoring
- Server timer
- Exam integrity settings
- Competitive rank and time tie-break
- NTA-style player

The choice must be stored as a stable internal key, not inferred from title.

Suggested internal values:

- `NORMAL`
- `COMPETITIVE`

Display labels can be configurable.

---

# 7. COMPETITIVE SCORING CONFIGURATION

Provide a preset:

## JEE-style preset

- Correct: `+4`
- Wrong: `-1`
- Unanswered: `0`

Teacher can configure:

- correct marks;
- wrong marks/deduction;
- unanswered marks;
- optional per-question override;
- whether score can go below zero;
- display precision.

Use Decimal/fixed precision or integer minor mark units.

Do not use binary float for authoritative scoring if decimal marks are supported.

Show preview:

```
Example:
10 correct  = +40
3 incorrect = -3
2 unanswered = 0
Final score = 37
Maximum positive score = 60
```

Normal mode continues to use current per-question max marks and zero wrong penalty unless configured otherwise.

---

# 8. CENTRAL SCORING SERVICE

Create one server-authoritative objective scoring service.

Input:

- exam revision;
- scoring policy;
- immutable question/option keys;
- submitted answers.

Output:

- correct count;
- incorrect count;
- unanswered count;
- positive marks;
- negative deduction;
- unanswered adjustment;
- final score;
- maximum score;
- minimum possible score;
- per-question score/reason.

Use the same service for:

- portal students;
- public guests;
- regrading;
- result view;
- ranking;
- exports.

Never calculate authoritative marks in JavaScript.

---

# 9. STABLE QUESTION & OPTION IDENTITY

For new tests, represent options conceptually as:

```json
[
  {"key":"A","text":"...","text_hi":"..."},
  {"key":"B","text":"...","text_hi":"..."}
]
```

Store `correct_option_key`, not only display text.

For legacy tests:

- migrate/adapter preserves current text behavior;
- do not silently change old answers;
- generate stable legacy keys where unambiguous.

Randomized option order must never change the option's stable key.

---

# 10. SERVER-AUTHORITATIVE ATTEMPT LIFECYCLE

Implement a shared attempt service for portal/public contexts.

Conceptual lifecycle:

```
NOT_STARTED
→ STARTED
→ IN_PROGRESS
→ SUBMITTING
→ SUBMITTED / AUTO_SUBMITTED
→ GRADED
→ RELEASED
```

Start endpoint:

- authorizes participant;
- checks schedule;
- checks attempt limit;
- creates/resumes one attempt;
- stores server `started_at`;
- stores `expires_at`;
- locks exam revision;
- locks question/option order;
- returns signed attempt/session token;
- returns server time and deadline;
- returns questions only after successful start.

Timer must be derived from:

`expires_at - server_now`

not localStorage.

---

# 11. START MEANS FINISH

For a timed secure test:

- After Start, the timer continues on the server.
- Closing/minimizing browser does not pause the timer.
- Reopening resumes the same attempt if time remains and policy allows.
- A second Start does not create another full-duration attempt.
- At expiry, the server finalizes/autosubmits the latest saved answers.
- The student cannot reset time by clearing storage.
- Teacher/Admin can authorize a controlled reattempt through a new attempt revision.

This is the correct implementation of “Start means finish”.

---

# 12. AUTOSAVE & HEARTBEAT

Autosave every meaningful answer change with debounce.

Store:

- selected option key;
- question status;
- marked-for-review status;
- last saved time;
- optional time spent on question.

Heartbeat:

- sends attempt/session token;
- receives server time;
- receives remaining time;
- confirms session validity;
- reports authorized integrity-event deltas.

Requirements:

- offline/local recovery is allowed;
- server remains authoritative;
- reconnect syncs safely;
- stale client responses cannot overwrite newer answers;
- submission is idempotent;
- no answer loss on refresh;
- no one-request-per-second design.

---

# 13. EXAM INTEGRITY / SECURE MODE

Per-test backend options:

- Integrity Mode: OFF / STANDARD / STRICT / CUSTOM
- Require fullscreen
- Maximum visibility violations
- Violation action:
  - LOG_ONLY
  - WARN
  - AUTO_SUBMIT_AFTER_LIMIT
  - AUTO_SUBMIT_IMMEDIATELY
- Block copy
- Block paste
- Block context menu
- Block print shortcut
- Detect fullscreen exit
- Detect page hidden/minimized
- Detect focus loss
- One active device/session
- Randomize questions
- Randomize options
- Resume allowed while timer continues
- Result release policy
- Ranking visibility

Do not put these only in frontend.

Server owns policy.

---

# 14. INTEGRITY EVENT DETECTION

Use a shared exam integrity controller.

Possible events:

- `VISIBILITY_HIDDEN`
- `WINDOW_BLUR`
- `FULLSCREEN_EXIT`
- `COPY_ATTEMPT`
- `PASTE_ATTEMPT`
- `CONTEXT_MENU_ATTEMPT`
- `PRINT_SHORTCUT_ATTEMPT`
- `PAGE_EXIT`
- `CONCURRENT_SESSION`
- `DEVICE_SESSION_CHANGED`
- `HEARTBEAT_MISSED`
- `NETWORK_DISCONNECTED`

Each event stores:

- attempt ID;
- event type;
- server timestamp;
- client sequence;
- relevant metadata;
- policy action;
- deduplication ID.

Use debounce/coalescing:

A single app switch can trigger multiple browser events.

Do not count blur + visibility + pagehide as three violations for one transition unless policy explicitly defines it.

Do not auto-penalize ordinary network failure.

---

# 15. FULLSCREEN FLOW

Secure attempt Start screen:

1. Show instructions.
2. Show integrity policy.
3. Ask student to confirm.
4. User presses **Enter Fullscreen & Start**.
5. Browser fullscreen is requested from this user gesture.
6. Server attempt starts only after valid start request.
7. Fullscreen exit is logged according to policy.

If fullscreen is unsupported:

- show clear fallback;
- allow or block according to configured test policy;
- do not silently pretend fullscreen is active.

Mobile browser behavior must be tested honestly.

---

# 16. FALSE-POSITIVE SAFETY

Phone calls, notifications, accessibility tools, browser UI and permission dialogs may trigger focus/visibility changes.

Provide:

- short debounce/grace threshold;
- clear first warning;
- visible violation counter;
- reason in audit;
- Admin review.

Strict auto-submit is allowed only if Teacher explicitly enables it for that test.

Default STANDARD mode should log/warn before punitive action.

---

# 17. ONE ACTIVE SESSION

For secure mode:

- one active attempt session per participant/exam;
- second device/tab receives a controlled conflict message;
- existing session can be resumed or invalidated according to policy;
- no silent duplicate attempt;
- session token is signed/unguessable;
- session ownership validated on every autosave/submit/heartbeat.

Do not use IP alone as identity; mobile networks change.

IP/device data can be review signals only.

---

# 18. PUBLIC TEST START FLOW

Current public flow should become:

```
Open public link
→ Registration
→ Optional identity verification
→ Instructions / mode / integrity policy
→ Start endpoint
→ Signed attempt token
→ Questions + server deadline
→ Autosave/heartbeat
→ Submit/autosubmit
→ Result according to release policy
```

Do not create the final PublicExamAttempt only at submit.

Create a durable attempt/session at start.

Phone and personal details remain Admin-only according to current privacy split.

---

# 19. PUBLIC LINK SECURITY

Add configurable:

- link enabled;
- start/end availability;
- link expiry;
- rotate token;
- optional access code;
- optional OTP if approved infrastructure exists;
- rate limit;
- bot/CAPTCHA option;
- one attempt policy;
- result release policy;
- public leaderboard visibility.

Do not expose correct answers before release.

Do not show phone/email/city/NIOS reference in a public leaderboard.

---

# 20. PORTAL EXAM AUTHORIZATION — ONE SHARED SERVICE

Create:

`authorize_student_exam(db, student, exam, selected_batch, action)`

Use it for:

- exam list;
- detail;
- start;
- question paper;
- autosave;
- heartbeat;
- submit;
- result;
- solution;
- ranking.

Validate:

- active exam;
- student class;
- subject enrollment;
- selected/enrolled batch;
- scheduled start;
- scheduled end;
- attempt state;
- result release policy.

Direct API calls must not bypass the portal UI.

---

# 21. TIMEZONE SAFETY

Current code mixes naive times, `datetime.utcnow()`, `datetime.now()` and manual 5:30 subtraction in some paths.

Replace with a centralized timezone-aware service.

Store authoritative timestamps in UTC.

Convert for display using configured application timezone.

Test:

- scheduled start;
- scheduled end;
- result release;
- daylight/offset assumptions;
- public and portal consistency.

Do not use browser clock as authority.

---

# 22. AUTO-SUBMISSION

At expiry:

- lock further answer changes;
- use latest successfully saved answers;
- server finalizes idempotently;
- set `submission_reason = TIME_EXPIRED`;
- set `auto_submitted = true`;
- calculate trusted elapsed time;
- grade objective attempt;
- notify client through event/polling;
- allow result only according to release policy.

A client-side timer callback alone is not sufficient.

Use a reliable server-side expiry/reconciliation process compatible with current deployment.

---

# 23. RANKING — REQUIRED ORDER

Default competitive ranking order:

1. `final_score` descending
2. `elapsed_seconds` ascending
3. `submitted_at` ascending
4. stable attempt ID ascending

This satisfies the owner's requirement:

- Student A: 60 marks, completed in 20 minutes, submitted earlier.
- Student B: 60 marks, completed in 19 minutes, submitted later.

Student B ranks above Student A because the score is equal and B completed faster.

Do not use alphabetical name as a performance tie-break.

---

# 24. RANKING REASON

Return structured tie-break information.

Examples:

- `Higher score`
- `Same score; completed 1 minute 12 seconds faster`
- `Same score and completion time; submitted earlier`
- `Exact tie resolved by stable attempt order`

Student Result should show:

- Rank
- Score
- Time Taken
- Ranking Reason

Teacher/Admin ranking should show:

- student;
- score;
- correct;
- incorrect;
- unanswered;
- time taken;
- submit time;
- submission reason;
- integrity status;
- rank reason.

Use human-readable duration.

---

# 25. PROVISIONAL VS FINAL RANKING

During an active exam window:

- Ranking can be hidden or marked `PROVISIONAL`.
- Avoid exposing answers/competitor detail that helps active candidates.

After the exam closes/release occurs:

- Ranking becomes `FINAL`.

Make this configurable.

Competitive secure template should default to final ranking after window close.

---

# 26. SHARED RANKING SERVICE

Remove the three drifting implementations.

Create one shared service used by:

- Student ranking API
- Teacher ranking API
- Admin ranking API
- optional Public ranking API
- exports

Batch-load participant display data.

Eliminate N+1 StudentProfile queries.

Apply authorization before returning ranking.

---

# 27. LEGACY RANKING

Historical attempts have no trusted elapsed_seconds.

Do not invent time.

For legacy exams:

- keep legacy marks/submission ordering;
- label ranking rule `LEGACY`;
- do not mix legacy unknown-time attempts into a new time-based leaderboard without a documented fallback.

Possible fallback:

1. marks desc
2. trusted elapsed if all tied attempts have it
3. submitted_at asc
4. attempt ID

Return the rule used.

---

# 28. RESULT RELEASE POLICY

Per test:

- `IMMEDIATE`
- `AFTER_ATTEMPT_DEADLINE`
- `AFTER_EXAM_CLOSE`
- `MANUAL_RELEASE`

Separately configure:

- score release;
- correct-answer review;
- explanations;
- ranking.

A competitive test can release score before solutions if desired.

Public early submitters must not automatically receive answer keys while others are still attempting.

---

# 29. PREMIUM COMPETITIVE EXAM PLAYER

Keep Normal mode working.

For Competitive mode create a dedicated NTA/JEE-style player:

- Instructions screen
- Candidate/test summary
- Server timer
- One question at a time
- Save & Next
- Mark for Review & Next
- Clear Response
- Previous
- Question palette
- Status legend:
  - Not Visited
  - Not Answered
  - Answered
  - Marked for Review
  - Answered & Marked for Review
- Fullscreen/integrity status
- Autosave indicator
- Connection indicator
- Final review summary
- Submit confirmation

Do not require every question to be answered.

Unanswered questions are valid and score according to policy.

---

# 30. NORMAL TEST PLAYER

Preserve current long-form/palette behavior if desired.

However, move timer and answer persistence to the same server-owned attempt engine.

Normal mode must also benefit from:

- server timer;
- autosave;
- resume;
- authorization;
- safe submit;
- trusted duration.

Integrity controls remain OFF unless enabled.

---

# 31. RESULT UI — PREMIUM

Competitive result card:

```
Final Score         60 / 100
Positive Marks      +68
Negative Marks      -8
Correct             17
Incorrect           8
Unanswered          0
Accuracy            68%
Time Taken          19m 08s
Rank                 #4 of 238
Ranking Reason       Same score; completed 52s faster
Integrity Status     Clean / Needs Review
```

Show:

- score breakdown;
- time;
- rank;
- rank rule;
- release status;
- question review when permitted;
- teacher feedback if relevant.

Do not calculate percentage incorrectly for negative scores.

---

# 32. TEACHER/ADMIN ANALYTICS

Add useful, real analytics:

- attempts started;
- completed;
- auto-submitted;
- average score;
- average duration;
- fastest completion;
- correct/incorrect/unanswered distribution;
- question-wise correct percentage;
- average time per question where tracked;
- integrity-event distribution;
- result-release status;
- ranking policy.

Do not expose private guest details to Teacher beyond current authorized fields.

Admin retains full public-test details.

---

# 33. INTEGRITY REPORT

Teacher/Admin authorized view:

- event timeline;
- violation count;
- event types;
- fullscreen status;
- concurrent-session events;
- network interruption markers;
- submission reason;
- review status.

Use badges:

- CLEAN
- REVIEW RECOMMENDED
- POLICY AUTO-SUBMITTED

Do not use accusatory labels.

Allow authorized Admin/Teacher to add review note.

Do not silently alter score merely because an event exists unless the published test policy explicitly defines that action.

---

# 34. QUESTION RANDOMIZATION

Optional per test:

- randomize question order;
- randomize option order.

At attempt start:

- generate deterministic per-attempt order;
- store it server-side;
- resume uses same order;
- grading uses stable option keys;
- result review reconstructs attempted order.

Do not randomize after the attempt starts.

---

# 35. EXAM VERSIONING / LIVE EDIT LOCK

Before any attempt starts:

- Teacher can edit.

After first attempt starts:

Lock:

- questions;
- options;
- correct answers;
- scoring;
- duration;
- integrity policy;
- ranking policy;
- randomization.

Allow safe metadata edits only where they do not alter fairness.

For substantive change:

`Duplicate / Create New Revision`

Existing attempts remain tied to the old revision.

Do not delete ExamQuestion rows under live attempts.

---

# 36. ATTEMPT DATA MODEL — CONCEPTUAL

Adapt names to current conventions.

Extend Exam:

- exam_mode
- scoring_policy JSON or explicit fields
- integrity_mode
- integrity_config JSON/validated structure
- ranking_policy
- result_release_policy
- solution_release_policy
- ranking_release_policy
- randomize_questions
- randomize_options
- revision
- locked_at
- public_start_at / public_end_at where needed

Extend/create attempt session:

- id / UUID
- exam_id
- exam_revision
- participant type
- student_id or public-attempt identity
- started_at
- expires_at
- submitted_at
- elapsed_seconds
- status
- auto_submitted
- submission_reason
- question_order
- option_order
- last_heartbeat_at
- session/device hash
- correct_count
- incorrect_count
- unanswered_count
- positive_marks
- negative_marks
- final_score
- integrity_status
- violation_count
- created_at / updated_at

Attempt answers:

- attempt_id
- question identity
- selected option key
- marked_for_review
- answer revision
- saved_at
- optional time_spent_seconds

Integrity event:

- attempt_id
- event type
- timestamp
- sequence
- metadata
- policy action

Use indexes and constraints.

---

# 37. DATA INTEGRITY & IDEMPOTENCY

Add database protections for:

- one active attempt per participant/exam/revision;
- idempotent start;
- idempotent autosave revision;
- idempotent final submit;
- unique public identity rule where configured;
- no duplicate ranking entry;
- no orphan ExamResult;
- no destructive history deletion;
- controlled reattempt.

Concurrent submit requests must return the same finalized result, not create duplicates.

---

# 38. PUBLIC DATA PRIVACY

Preserve current split:

Teacher public response:
- name
- score
- authorized exam stats

Admin:
- authorized personal details.

Public leaderboard:

- disabled by default;
- if enabled, use configurable masked/display name;
- never expose phone, email, city, NIOS reference, IP or custom private fields.

Integrity metadata is authorized staff-only.

---

# 39. RATE LIMITS & ABUSE CONTROLS

Public endpoints need:

- rate limit by token/session/IP as a signal;
- payload size limits;
- answer-key validation;
- field length limits;
- bot protection option;
- brute-force token safeguards;
- start/submit idempotency.

Do not treat IP as the sole participant identity.

---

# 40. FRONTEND INTEGRITY CONTROLLER

Use a single controller with lifecycle cleanup.

On attempt start:

- attach listeners;
- request fullscreen if configured;
- start heartbeat;
- start autosave;
- show policy state.

On submit/exit:

- detach listeners;
- clear heartbeat/autosave timers;
- release wake lock if used;
- avoid duplicate events.

Do not attach listeners repeatedly on rerender.

---

# 41. MOBILE, LIGHT/DARK & ACCESSIBILITY

Test at:

- 320
- 360
- 390
- 430
- 768
- 1024
- 1440
- 1920 px

Requirements:

- no horizontal page overflow;
- keyboard/phone orientation safe;
- fullscreen fallback message;
- palette usable;
- sticky action bar does not cover content;
- dark inputs/cards readable;
- semantic tokens;
- visible focus;
- keyboard navigation;
- screen-reader labels;
- reduced motion;
- no color-only status;
- connection/autosave state readable.

---

# 42. PERFORMANCE

Do not make autosave/realtime slow.

- Debounce answer saves.
- Batch heartbeat/event writes.
- Use compact payloads.
- Index attempt/exam/student/status fields.
- Ranking uses aggregate/batched queries.
- Do not query StudentProfile once per row.
- Load result detail on demand.
- Do not preload every integrity event in exam list.
- Cache immutable exam revision/questions safely after authorization.
- Targeted invalidation only.

---

# 43. MIGRATION & ROLLOUT

1. Verify current repository.
2. Add versioned migrations.
3. Backfill existing exams:
   - NORMAL
   - integrity OFF
   - legacy release behavior
4. Preserve historical attempts.
5. Do not fabricate durations.
6. Add new attempt engine behind feature flag.
7. Enable one staging Normal test.
8. Enable one staging Competitive test.
9. Enable one staging Public Competitive test.
10. Run security/direct API tests.
11. Canary production rollout.
12. Monitor event duplication, autosave errors and timer mismatches.
13. Expand rollout after evidence passes.

No import-time ALTER TABLE hacks.

---

# 44. REQUIRED ACCEPTANCE TESTS

## Normal compatibility

1. Existing Normal MCQ creates successfully.
2. Existing scoring remains correct.
3. Existing result view works.
4. Existing public link works with default legacy settings.
5. Existing subjective/PDF tests remain unchanged.

## Competitive scoring

6. `+4/-1/0`: 10 correct, 3 wrong, 2 unanswered = 37.
7. All wrong can produce a negative score if configured.
8. Unanswered is not treated as wrong.
9. Client-supplied score is ignored.
10. Decimal rule uses exact fixed precision.
11. Per-question override works if enabled.
12. Normal mode wrong answer remains zero unless configured.

## Start/timer

13. Attempt start returns server deadline.
14. Refresh does not reset timer.
15. Clearing localStorage does not reset timer.
16. Closing browser does not pause timer.
17. Reopen resumes same attempt.
18. Second tab cannot create a new full-duration attempt.
19. Server expiry auto-submits.
20. Client clock change does not extend time.
21. Pre-start direct API cannot fetch questions.
22. Pre-start direct submit is denied.
23. Unauthorized batch exam detail is denied.
24. Unauthorized exam paper is denied.
25. Unauthorized ranking is denied.

## Autosave

26. Selected answer survives rerender.
27. Selected answer survives refresh.
28. Last newer answer wins over stale response.
29. Temporary network loss preserves local answers.
30. Reconnect syncs without duplication.
31. Autosave failure is visible.
32. Final submit is idempotent.

## Integrity

33. Visibility hidden creates one coalesced event.
34. Blur + visibility for one switch does not triple-count.
35. Fullscreen exit is logged.
36. Copy attempt follows configured policy.
37. Standard mode warns/logs.
38. Strict configured threshold auto-submits.
39. Network disconnect does not automatically accuse/fail.
40. Concurrent session is detected.
41. Unsupported fullscreen follows fallback policy.
42. Event listeners are cleaned after submit.

## Public

43. Public questions unavailable before allowed start.
44. Public timer is server-authoritative.
45. Public reload cannot restart attempt.
46. Concurrent duplicate public submit creates one result.
47. Public result does not reveal answers before release.
48. Teacher public view never receives private details.
49. Public leaderboard, if enabled, hides private fields.
50. Raw view count and unique starts are separate.
51. Token rotation invalidates old link.
52. Rate limit handles abuse without blocking normal user.

## Ranking

53. Higher score ranks first.
54. Same score: faster elapsed ranks first.
55. Student B: 60 marks/19m ranks above A: 60 marks/20m even if B submitted later.
56. Same score and elapsed: earlier submitted_at ranks first.
57. Exact tie resolves deterministically.
58. Ranking reason says why.
59. Admin sees time taken.
60. Student result shows own time and rank reason.
61. Ranking is provisional during active window if configured.
62. Final ranking freezes/labels after close.
63. Legacy attempts show legacy ranking rule without fabricated time.
64. Student/Teacher/Admin use the same ranking service.

## Versioning

65. Questions can be edited before attempts.
66. Questions cannot be destructively replaced after first start.
67. Duplicate/revision creates a new fair exam.
68. Old results remain tied to old revision.

## UI

69. Normal/Competitive selector is clear.
70. JEE preset preview is accurate.
71. Secure settings explain limitations.
72. Competitive player works at 320px.
73. Dark mode readable.
74. Light mode readable.
75. Keyboard navigation works.
76. Mark for Review palette status works.
77. Final review shows unanswered count.
78. Submit confirmation shows score rule, not answers.

## Data integrity

79. No attempt history deletion.
80. No orphan result rows.
81. Authorized reattempt creates a controlled revision.
82. No duplicate XP/rewards on retry.
83. Audit trail stores start, submit and policy actions.

---

# 45. REQUIRED CLAUDE REPORT

Before implementation:

1. Verify current source version.
2. Confirm/refute every finding.
3. Map all test routes and UI functions.
4. List current data-model gaps.
5. Produce migration and rollback plan.

After implementation:

1. Files changed.
2. Models/migrations.
3. Shared attempt service.
4. Scoring service.
5. Ranking service.
6. Integrity controller.
7. Public-test changes.
8. Portal-test changes.
9. Authorization fixes.
10. Result-release behavior.
11. UI/mobile/dark changes.
12. Performance/index changes.
13. Executed tests with pass/fail.
14. Browser/device limitations still present.
15. Remaining policy decisions.

Classify every item:

- FIXED & TESTED
- FIXED, DEPLOY VERIFICATION REQUIRED
- POLICY DECISION REQUIRED
- NOT APPLICABLE
- STILL OPEN

Do not claim “cheat proof”.

Do not claim completion from syntax checks.

---

# 46. FINAL INTENDED FLOW

Teacher creates Objective Test.

Teacher chooses:

```
Normal Test
or
Competitive Exam
```

Competitive Exam:

```
Scoring: +4 / -1 / 0
Integrity: Strict
Fullscreen required
One active session
Question/order randomization
Result after exam closes
```

Student/Public Guest:

```
Register / authenticate
→ Read instructions
→ Enter Fullscreen & Start
→ Server creates attempt and deadline
→ Questions load
→ Answers autosave
→ Integrity events are logged
→ Timer continues through refresh/close
→ Submit or server autosubmit
→ Result releases by policy
→ Ranking: score first, faster completion second
→ Result explains ranking reason
```

Admin/Teacher:

```
See score breakdown
See time taken
See rank and reason
See integrity event summary
See public/portal attempt analytics
```

Build this on top of the existing MVS test system without breaking Normal tests, subjective tests, PDF tests, bilingual content, batches, results or current privacy boundaries.
