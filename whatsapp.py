"""
whatsapp.py — WhatsApp template sending for MVS Portal (Combirds / generic BSP).

admin_routes.py isko `import whatsapp as W` karke use karta hai. Isliye ye poora
interface deta hai:  cfg(), is_configured(), missing(), build_message(),
build_params(), send()  +  send_announce().

CONFIG (do me se koi bhi tarika — dono chalte hain):
  1) Portal ke Admin -> WhatsApp Settings me daalo (DB me app_settings me save hota).
  2) Ya Railway Environment Variables me (WA_API_URL, WA_API_KEY, ...).
  Portal-settings ki value pehle use hoti hai, warna env var, warna default.

Keys:
  wa_api_url / WA_API_URL     -> Combirds send-message endpoint (POST)
  wa_api_key / WA_API_KEY     -> Combirds API key/token
  wa_format  / WA_FORMAT      -> "campaign" (AiSensy/Interakt style) | "meta" (Cloud API)
  wa_welcome / WA_WELCOME     -> approved WELCOME template name (Template 1)
  wa_announce/ WA_ANNOUNCE    -> approved ANNOUNCEMENT template name (Template 2)
  wa_lang    / WA_LANG        -> template language code (default "en")
  wa_link    / WA_LINK        -> portal link (default https://app.mvsfoundation.in)
  wa_sender  / WA_SENDER      -> optional sender/channel/from id (agar API maange)

Sab kuch guarded hai — koi bhi galti pe (False, "reason") return hota hai, kabhi
crash nahi. Network ke liye stdlib urllib use hota hai (koi extra dependency nahi).
"""

import os
import json
import urllib.request
import urllib.parse

PORTAL_LINK_DEFAULT = "https://app.mvsfoundation.in"

# ---- config resolution: app_settings (DB) > env var > default -------------
_ENV = {
    "wa_api_url":  "WA_API_URL",
    "wa_api_key":  "WA_API_KEY",
    "wa_format":   "WA_FORMAT",
    "wa_welcome":  "WA_WELCOME",
    "wa_announce": "WA_ANNOUNCE",
    "wa_lang":     "WA_LANG",
    "wa_link":     "WA_LINK",
    "wa_sender":   "WA_SENDER",
}


def _db_get(key):
    """app_settings se ek value padho (bina session ke, engine se). None agar na mile."""
    try:
        from database import engine
        from sqlalchemy import text as _t
        with engine.connect() as conn:
            row = conn.execute(_t("SELECT value FROM app_settings WHERE `key`=:k"),
                               {"k": key}).fetchone()
            if row and row[0] is not None:
                v = str(row[0]).strip()
                return v or None
    except Exception:
        pass
    return None


def _val(key, default=""):
    v = _db_get(key)
    if v:
        return v
    v = os.environ.get(_ENV.get(key, ""), "")
    return (v.strip() if v else default)


def cfg():
    return {
        "provider": "combirds",
        "api_url":  _val("wa_api_url"),
        "api_key":  _val("wa_api_key"),
        "format":   (_val("wa_format", "campaign") or "campaign").lower(),
        "campaign": _val("wa_welcome"),          # welcome template/campaign name
        "template": _val("wa_welcome"),          # (alias — admin_routes cfg() me dono padha jaata)
        "announce": _val("wa_announce"),
        "lang":     _val("wa_lang", "en"),
        "link":     _val("wa_link", PORTAL_LINK_DEFAULT),
        "sender":   _val("wa_sender"),
        "params":   ["name", "batch"],           # welcome template variables order
    }


def missing():
    c = cfg()
    need = []
    if not c["api_url"]:
        need.append("WA_API_URL (Combirds send endpoint)")
    if not c["api_key"]:
        need.append("WA_API_KEY (Combirds API key)")
    if not c["campaign"]:
        need.append("WA_WELCOME (approved welcome template name)")
    return need


def is_configured():
    return len(missing()) == 0


# ---- phone helpers --------------------------------------------------------
def _digits(p):
    return "".join(ch for ch in str(p or "") if ch.isdigit())


def _phone_intl(p, plus=False):
    """India: 10-digit -> 91XXXXXXXXXX (ya +91...). Pehle se 91/0 ho to handle."""
    d = _digits(p)
    if len(d) > 10 and d.startswith("91"):
        d = d[2:]
    d = d[-10:]
    if len(d) != 10:
        return ""
    return (("+91" if plus else "91") + d)


# ---- message / params builders (preview + send) ---------------------------
def build_params(name, batch, phone, message=None):
    """Template variables. Welcome: [name, batch]. Announce: [name, message]."""
    if message is not None:
        return [str(name or "Student"), str(message or "")]
    return [str(name or "Student"), str(batch or "")]


def build_message(name, batch, phone, template=None, message=None):
    """Preview text (portal me dikhane ke liye). Actual send template-based hota hai."""
    link = cfg()["link"]
    nm = name or "Student"
    if message:
        return (f"Hi {nm}, an update from MVS Foundation: {message} "
                f"For any help, open your Class Manager and use the Support section. "
                f"Open: {link} — Team MVS Foundation")
    bt = batch or "your"
    return (f"Hi {nm}, welcome to MVS Foundation's {bt} batch. Your Class Manager is now "
            f"active — Notes, PPT, DPP submissions, Test Series, Doubts and Time Table are "
            f"all in one place. Login with your registered mobile number at {link}. "
            f"Note: replies to this number are not monitored — for any help use the Support "
            f"section inside your Class Manager. — Team MVS Foundation")


# ---- the send -------------------------------------------------------------
def _post(url, headers, payload, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
        return r.status, body


def _send_raw(phone, template, params):
    """Ek number pe template message bhejo. -> (ok, detail)."""
    c = cfg()
    if not (c["api_url"] and c["api_key"] and template):
        return False, "WhatsApp not configured (api_url / api_key / template missing)"
    fmt = c["format"]
    p10 = _phone_intl(phone, plus=False)
    if not p10:
        return False, "Invalid phone"

    if fmt == "meta":
        # Meta Cloud API / most BSP passthrough style
        headers = {"Authorization": "Bearer " + c["api_key"]}
        payload = {
            "messaging_product": "whatsapp",
            "to": p10,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": c["lang"] or "en"},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(x)} for x in (params or [])],
                }],
            },
        }
    else:
        # "campaign" style (AiSensy / Interakt / many Indian BSPs incl. Combirds)
        headers = {}
        payload = {
            "apiKey": c["api_key"],
            "campaignName": template,
            "destination": p10,
            "userName": (params[0] if params else "Student"),
            "templateParams": [str(x) for x in (params or [])],
        }
        if c["sender"]:
            payload["source"] = c["sender"]

    try:
        status, body = _post(c["api_url"], headers, payload)
        ok = (200 <= status < 300)
        # kuch BSP 200 ke saath body me success:false bhejte hain
        low = (body or "").lower()
        if ok and ('"success":false' in low.replace(" ", "") or '"status":"error"' in low.replace(" ", "")):
            ok = False
        return ok, f"HTTP {status}: {body[:280]}"
    except urllib.error.HTTPError as e:
        try:
            eb = e.read().decode("utf-8", "replace")
        except Exception:
            eb = ""
        return False, f"HTTP {e.code}: {eb[:280]}"
    except Exception as e:
        return False, f"Send failed: {str(e)[:200]}"


def send(phone, text=None, name="", batch="", template=None, params=None):
    """admin_routes yahi call karta hai (welcome). template/params na ho to welcome."""
    c = cfg()
    tmpl = template or c["campaign"]
    prm = params if params is not None else build_params(name, batch, phone)
    return _send_raw(phone, tmpl, prm)


def send_announce(phone, name, message):
    """Announcement (Template 2) — {{1}}=name, {{2}}=custom message."""
    c = cfg()
    tmpl = c["announce"] or c["campaign"]
    return _send_raw(phone, tmpl, build_params(name, None, phone, message=message))
