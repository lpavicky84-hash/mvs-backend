# MVS Foundation Portal — Updated Code (for handoff)

Version: 7968bae (latest)
Date: 2026-07-21

## Contents
- frontend/mvs_portal_connected.html — complete single-file portal (student + teacher + admin). Deploy as-is; connects to Railway backend.
- backend/admin_routes.py, teacher_routes.py — UPDATED (deploy these)
- backend/student_routes.py, models.py — unchanged in recent updates, included for completeness

## Update history (newest first)

### 7968bae — Demo timetable docx: all subjects
- Portal-embedded demo Word doc now covers 9 subjects (English, Hindi, Maths, Physics, Chemistry, Biology, Accountancy, Business Studies, Economics), each with sample rows in real NIOS lesson/chapter naming.
- Download: window._DEMO_TT_B64 + downloadDemoTT() -> MVS_Timetable_Demo_All_Subjects.docx. Links in both PDF-upload modals. PORTAL-ONLY deploy.

### a3a53cd — PDF preview Chapter/Grammar toggle + demo doc
- Preview modal rows get Chapter|Grammar segmented toggle (pvwType); grammar stored via "Grammar: " prefix; header shows "Y chapters + Z grammar".
- _isNonBookChapter keyword list extended (direct/indirect speech etc.). PORTAL-ONLY deploy.

### 4d6ad48 — Chapter counting + class-aware duplicate + PDF preview (BIG)
Frontend:
- _isNonBookChapter(name): grammar/writing keywords excluded from chapter counts.
- _expandMergedChapter(name): "Chapter 15 & 16" -> 2, "Chapter 25 to 28" -> 4, "Chapter - 3, 6, 9" -> 3. Used in _chapAll, dropdown counts, renderStudentChapters.
- Post-upload preview modal: openPdfPreview/renderPdfPreview/pvwSplit/pvwDel/pvwEdit/commitPdfPreview; split merged rows, delete rows, then commit.
- pdfReplaceWarn(subj, entries, warnId, cbId, cls) — class-aware duplicate warning (digit-normalized class compare).
Backend (DEPLOY admin_routes.py + teacher_routes.py):
- Both PDF upload endpoints accept preview=Form("false") -> returns parsed rows without saving.
- New POST /timetable-pdf-commit (admin + teacher): {rows, class_name, replace}; class-aware replace (admin: class_name match; teacher: teacher_id + class_name match). Fixes: Class 10 English upload no longer wipes Class 12 English.

### 0ef83b5 — Live presence accuracy + user detail modal
- _pingNow heartbeat gated on document.visibilityState==='visible'; ping on visibilitychange return. Hidden tabs no longer stay "live" (backend LIVE_WINDOW_MIN=3).
- Admin Live Users: click a user -> full detail modal (batch, phone tel: link, class, medium, subjects, email) + login history. Backend admin_user_sessions returns detail dict from StudentProfile/TeacherProfile. Deploy admin_routes.py.

### c7e5d6b — Teacher photo on test cards
- Teacher-portal test cards show 46px rounded teacher photo (.tx-tlogo-self) with initials fallback. PORTAL-ONLY.

### 813abbc — Test window, PDF formatting, answer-leak fix
- Earlier base.

## Deploy notes
- Frontend: replace the single HTML file.
- Backend: overwrite admin_routes.py + teacher_routes.py on Railway and restart. student_routes.py/models.py unchanged since 813abbc era.
