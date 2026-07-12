IS UPDATE MEIN KYA HUA (Study Material Integration + Renames)
=============================================================

BADLE HUE FILES (GitHub repo root mein replace karo)
----------------------------------------------------
  mvs_portal_connected.html   <- renames + naya connected Study Material section
  main.py                     <- ext_materials router include hua (2 lines)

NAYI FILE (repo root mein ADD karo)
-----------------------------------
  ext_materials.py            <- Student Portal se materials laane wala bridge

BAAKI SAB FILES UNCHANGED (kuch replace karne ki zaroorat nahi).

1) RENAMES
----------
  Teacher portal : "Study Material"  -> "Classes Material"  (class notes/DPP wala)
                   + NAYA "Study Material" nav item (Student Portal se connected)
  Admin portal   : "Study Material"  -> "Classes Material"
                   "Question Bank"   -> "Study Material" (connected view)
  Student portal : "Materials"       -> "Classes Material"
                   "Question Bank"   -> "Study Material" (connected view)
  Student dashboard quick-actions bhi update ho gaye.

2) QUESTION BANK HATAYA
-----------------------
  Purana Question Bank (admin upload + student view) UI se hata diya.
  Uski jagah har portal mein "Study Material" hai jo MVS Student Portal
  (Admin Console V7.1) se LIVE connected hai:
   - Session tabs (April/October/Stream 2, On Demand/SYC)
   - Class / Medium / Category filters
   - Category-wise grouping (TMA Solutions, PYQs, Syllabus, jo bhi wahan hai)
   - PDF proxy se khulta hai, links naye tab mein
  NOTE: Purana admin-uploaded question bank data DB mein safe hai, bas UI
  se dikhna band hua. Naya material ab Student Portal se hi manage hoga.

3) CONNECTION SETUP (2 steps)
-----------------------------
  STEP A — Student Portal side:
    STUDENT_PORTAL_INTEGRATION.md file us portal ke chat/developer ko de do.
    Woh 2 read-only endpoints + 1 secret key add karega aur tumhe
    (a) secret key aur (b) backend ka URL wapas dega.

  STEP B — CRM side (Railway > is service > Variables):
    STUDENT_PORTAL_URL = https://<student-portal-backend-url>   (no trailing /)
    STUDENT_PORTAL_KEY = <wahi secret key>
    Save karte hi Railway redeploy karega — Study Material live ho jayega.

  Jab tak variables set nahi hain, section "connection pending" card dikhata
  hai — kuch break nahi hota.

  Health check (login karke browser console ya curl):
    GET /api/ext/status   -> {"configured":true,"reachable":true,"count":N}

4) CACHING
----------
  Material list 5 min cache hoti hai (Refresh button turant fresh laata hai).
  Files 1 ghante ke liye chhote memory cache mein rehti hain (max 20 files),
  taaki bachon ke repeated opens instant hon aur Student Portal par load na aaye.
