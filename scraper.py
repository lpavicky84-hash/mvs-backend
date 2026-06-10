import os
import time
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NIOS_URL = "https://sdmis.nios.ac.in/registration/check-admission-status"
RECAPTCHA_SITE_KEY = "6Lc07T4iAAAAADsnW1ZXbEz0GUissRcasTnSS4Nj"
RECAPTCHA_ACTION = ""   # NIOS doesn't use a specific action

# CapSolver API
CAPSOLVER_API_KEY = os.environ.get("CAPTCHA_API_KEY", "")
CAPSOLVER_CREATE  = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT  = "https://api.capsolver.com/getTaskResult"

STATUS_COLORS = {
    "document verification in progress": {"hex": "FFE0B2", "label": "Documents Verification In Progress"},
    "documents verification":            {"hex": "FFE0B2", "label": "Documents Verification In Progress"},
    "document verification":             {"hex": "FFE0B2", "label": "Documents Verification In Progress"},
    "admission confirmed":               {"hex": "69F0AE", "label": "Admission Confirmed"},
    "pending":                           {"hex": "FFF9C4", "label": "Pending"},
    "verified":                          {"hex": "C8E6C9", "label": "Verified"},
    "approved":                          {"hex": "B2DFDB", "label": "Approved"},
    "admitted":                          {"hex": "BBDEFB", "label": "Admitted"},
    "rejected":                          {"hex": "FFCDD2", "label": "Rejected"},
    "error":                             {"hex": "E0E0E0", "label": "Fetch Error"},
    "not found":                         {"hex": "F8BBD0", "label": "Not Found"},
    "unknown":                           {"hex": "F5F5F5", "label": "Unknown"},
}

def get_status_label(text: str) -> str:
    if not text:
        return "Unknown"
    t = text.lower()
    for key, val in STATUS_COLORS.items():
        if key in t:
            return val["label"]
    return "Unknown"

SESSION_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": NIOS_URL,
}

# ── CapSolver: solve reCAPTCHA v3 ──────────────────────────────────────────────
def solve_recaptcha_v3() -> str:
    """Get a reCAPTCHA v3 token from CapSolver. Returns token string or ''."""
    if not CAPSOLVER_API_KEY:
        logger.error("CAPTCHA_API_KEY not set!")
        return ""

    try:
        # Create task
        task = {
            "type": "ReCaptchaV3TaskProxyLess",
            "websiteURL": NIOS_URL,
            "websiteKey": RECAPTCHA_SITE_KEY,
        }
        if RECAPTCHA_ACTION:
            task["pageAction"] = RECAPTCHA_ACTION
        create_payload = {
            "clientKey": CAPSOLVER_API_KEY,
            "task": task,
        }
        r = requests.post(CAPSOLVER_CREATE, json=create_payload, timeout=30)
        data = r.json()
        if data.get("errorId") != 0:
            logger.error(f"CapSolver create error: {data.get('errorDescription')}")
            return ""

        task_id = data.get("taskId")
        logger.info(f"CapSolver task created: {task_id}")

        # Poll for result (max ~60s)
        for attempt in range(30):
            time.sleep(2)
            result_payload = {"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}
            rr = requests.post(CAPSOLVER_RESULT, json=result_payload, timeout=30)
            rdata = rr.json()
            if rdata.get("errorId") != 0:
                logger.error(f"CapSolver result error: {rdata.get('errorDescription')}")
                return ""
            if rdata.get("status") == "ready":
                token = rdata.get("solution", {}).get("gRecaptchaResponse", "")
                logger.info(f"CapSolver token received ({len(token)} chars)")
                return token

        logger.error("CapSolver timed out")
        return ""

    except Exception as e:
        logger.error(f"CapSolver error: {e}")
        return ""

# ── Get CSRF token + form fields ───────────────────────────────────────────────
def get_csrf_and_fields(session: requests.Session):
    resp = session.get(NIOS_URL, headers=SESSION_HEADERS, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")

    csrf = ""
    meta = soup.find("meta", {"name": "_csrf"}) or soup.find("meta", {"name": "csrf-token"})
    if meta:
        csrf = meta.get("content", "")
    else:
        inp = soup.find("input", {"name": "_csrf"})
        if inp:
            csrf = inp.get("value", "")

    logger.info(f"CSRF: {csrf[:20]}...")
    return csrf

# ── Fetch status for one reference ─────────────────────────────────────────────
def fetch_status_for_reference(session: requests.Session, reference_no: str,
                                csrf: str, captcha_token: str) -> dict:
    result = {
        "reference_no": reference_no,
        "status": "Fetch Error",
        "raw_text": "",
        "success": False,
    }
    try:
        payload = {
            "_csrf": csrf,
            "CheckStatus[email]": "",
            "CheckStatus[reference_no]": str(reference_no).strip(),
            "CheckStatus[enrollment_no]": "",
            "CheckStatus[google_recapcha_response]": captcha_token,
        }
        headers = {**SESSION_HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
        resp = session.post(NIOS_URL, data=payload, headers=headers, timeout=25)

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "meta", "link"]):
            tag.decompose()

        # Get clean text and find status after "Admission Status" label
        full_text = soup.get_text(separator="\n", strip=True)
        lines = [l.strip() for l in full_text.split("\n") if l.strip()]

        status_text = ""
        # Find "Admission Status" then take the NEXT meaningful line (the actual status)
        for i, line in enumerate(lines):
            if line.lower() == "admission status" and i + 1 < len(lines):
                candidate = lines[i + 1].strip()
                # Skip if next line is just a label, take the real status
                if candidate and len(candidate) > 3 and candidate.lower() != "back":
                    status_text = candidate
                    # If this matched a known status, use it
                    if get_status_label(candidate) != "Unknown":
                        break

        # Fallback: scan whole text for any known status keyword
        if not status_text or get_status_label(status_text) == "Unknown":
            for line in lines:
                if get_status_label(line) != "Unknown":
                    status_text = line
                    break

        if not status_text:
            status_text = full_text[:500]

        result["raw_text"] = status_text[:500]
        result["status"] = get_status_label(status_text)
        result["success"] = True
        logger.info(f"  {reference_no} -> {result['status']} | {status_text[:80]}")

    except Exception as e:
        logger.error(f"Error fetching {reference_no}: {e}")
        result["raw_text"] = str(e)[:200]

    return result

# ── Main scrape loop ───────────────────────────────────────────────────────────
def debug_full_response(reference_no: str) -> str:
    """Solve captcha + submit + return FULL raw response for debugging."""
    if not CAPSOLVER_API_KEY:
        return "ERROR: CAPTCHA_API_KEY not set"
    session = requests.Session()
    csrf = get_csrf_and_fields(session)
    token = solve_recaptcha_v3()
    if not token:
        return "ERROR: captcha solve failed"
    payload = {
        "_csrf": csrf,
        "CheckStatus[email]": "",
        "CheckStatus[reference_no]": str(reference_no).strip(),
        "CheckStatus[enrollment_no]": "",
        "CheckStatus[google_recapcha_response]": token,
    }
    headers = {**SESSION_HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
    resp = session.post(NIOS_URL, data=payload, headers=headers, timeout=25)
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "meta", "link"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return f"STATUS CODE: {resp.status_code}\nTOKEN LEN: {len(token)}\n\n--- RESPONSE TEXT ---\n" + "\n".join(lines[:60])

def scrape_all_students(reference_numbers: list) -> list:
    logger.info(f"Starting scrape for {len(reference_numbers)} students...")
    results = []

    if not CAPSOLVER_API_KEY:
        logger.error("CAPTCHA_API_KEY missing — set it in Railway env vars!")
        for ref in reference_numbers:
            results.append({"reference_no": ref, "status": "Fetch Error",
                            "raw_text": "No captcha API key", "success": False})
        return results

    try:
        session = requests.Session()
        csrf = get_csrf_and_fields(session)

        for i, ref_no in enumerate(reference_numbers):
            logger.info(f"[{i+1}/{len(reference_numbers)}] Checking: {ref_no}")
            # Each submission needs a fresh captcha token
            token = solve_recaptcha_v3()
            if not token:
                results.append({"reference_no": ref_no, "status": "Fetch Error",
                                "raw_text": "Captcha solve failed", "success": False})
                continue
            # Refresh CSRF occasionally
            if i > 0 and i % 15 == 0:
                csrf = get_csrf_and_fields(session)
            res = fetch_status_for_reference(session, ref_no, csrf, token)
            results.append(res)
            time.sleep(1)

    except Exception as e:
        logger.error(f"Session error: {e}")
        checked = {r["reference_no"] for r in results}
        for ref in reference_numbers:
            if ref not in checked:
                results.append({"reference_no": ref, "status": "Fetch Error",
                                "raw_text": str(e)[:200], "success": False})

    logger.info(f"Scrape complete. {len(results)} results.")
    return results
