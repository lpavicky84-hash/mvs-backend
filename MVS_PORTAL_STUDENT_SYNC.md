# MVS Portal → CRM : Student Auto-Onboarding Spec (Part 2)

## Context

CRM Portal (`mvs-foundation-api-production-0553.up.railway.app`) mein ab **smart
student onboarding** implement ho chuka hai: jo student MVS Portal par
**class-access UNLOCKED** hai aur Class Manager card se CRM kholta hai, uski
details (name, class, medium, subjects) **is MVS Portal se auto-fetch** hongi —
student sirf apna batch chunega. Iske liye is portal ke backend mein **2 naye
read-only integration endpoints** add karne hain — bilkul waise hi jaise pehle
`/api/integration/materials` banaya tha (same `X-MVS-KEY` header auth, same
`MVS_CRM_KEY` secret, koi CORS change nahi, koi existing feature touch nahi).

## Endpoint 1 — Ek student ki details

```
GET /api/integration/student?phone=<10-digit-mobile>
Header: X-MVS-KEY: <secret>
```

Response `200` JSON:

```json
{
  "found": true,
  "name": "Harshit Sharma",
  "phone": "9876543210",
  "class_level": "12",
  "session": "April 2027",
  "medium": "English",
  "subjects": ["English", "Mathematics", "Physics", "Chemistry", "Physical Education"],
  "class_access_unlocked": true
}
```

Rules:
- Phone match 10-digit par karo (country code strip kar ke).
- `found: false` agar student hi nahi hai.
- `class_level`: `"10"` ya `"12"` (string).
- `subjects`: student ke **actual chosen subjects ke naam** (Study Materials
  dashboard mein jo dikhte hain — codes optional, CRM khud strip kar leta hai).
- `medium`: `"Hindi"` / `"English"` — jo student ka medium hai.
- `class_access_unlocked`: **sabse important** — sirf tab `true` jab is student
  ka Class Access section unlocked hai (batch purchase confirmed). CRM sirf
  unlocked students ko hi onboard karta hai.

## Endpoint 2 — Sabhi unlocked students ki list

```
GET /api/integration/unlocked-students
Header: X-MVS-KEY: <secret>
```

Response `200` JSON:

```json
{
  "students": [
    {"name": "Harshit Sharma", "phone": "9876543210", "class_level": "12", "session": "April 2027"},
    {"name": "Riya Gupta",     "phone": "9123456780", "class_level": "10", "session": "April 2027"}
  ]
}
```

Rules:
- Sirf **class_access_unlocked = true** wale students.
- CRM iska use admin ko yeh dikhane ke liye karta hai ki kitne unlocked
  students ne abhi tak Class Manager se batch select nahi kiya ("Portal
  Pending" list). Bade lists theek hain — ek hi response mein bhejo.

## SSO token verification (optional but recommended)

CRM ab `#sso=<token>` ko **server-side verify** bhi kar sakta hai. Iske liye:
- Jo secret tum token sign karne ke liye use karte ho (HMAC-SHA256 of the
  base64url payload string), uski value owner ko do — woh CRM Railway par
  `CRM_SSO_SECRET` env var mein set karega.
- Agar secret set nahi hoga to CRM signature check skip karta hai (flow phir
  bhi kaam karega, kyunki asli gate to Endpoint 1 ka server-to-server
  verification hai — student portal par exist + unlocked hona zaroori hai).

## Test commands

```bash
curl -H "X-MVS-KEY: <secret>" "https://<backend>/api/integration/student?phone=9876543210"
curl -H "X-MVS-KEY: <secret>" "https://<backend>/api/integration/unlocked-students"
curl -H "X-MVS-KEY: wrong"    "https://<backend>/api/integration/student?phone=9876543210"   # 401 aana chahiye
```

## Deliverables

1. Dono endpoints implement + deploy (same X-MVS-KEY protection).
2. Teeno curl tests ke results.
3. (Optional) SSO signing secret ki value, owner `CRM_SSO_SECRET` set karega.
