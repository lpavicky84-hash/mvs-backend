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
    "wa_welcome_msg": "WA_WELCOME_MSG",
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
        "welcome_msg": _val("wa_welcome_msg"),   # welcome ka message text (variable {{2}} me jata hai)
        "lang":     _val("wa_lang", "en"),
        "link":     _val("wa_link", PORTAL_LINK_DEFAULT),
        "sender":   _val("wa_sender"),
        "params":   ["name", "message", "phone"],  # {{1}}=name, {{2}}=message+link, {{3}}=registered mobile
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
def _resolve(text, name, batch, link):
    """{name}/{batch}/{link} tokens replace + newline/tab hata (WhatsApp variable rule)."""
    t = str(text or "")
    t = (t.replace("{name}", str(name or ""))
          .replace("{batch}", str(batch or ""))
          .replace("{link}", str(link or "")))
    t = t.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    while "     " in t:            # 4+ consecutive spaces not allowed
        t = t.replace("     ", " ")
    return t.strip()


def _welcome_body(name, batch, link):
    from_cfg = cfg().get("welcome_msg")
    if from_cfg:
        return _resolve(from_cfg, name, batch, link)
    # default agar admin ne message set nahi kiya
    return _resolve("welcome to {batch} batch. Your Class Manager is now active — "
                    "login with your registered mobile at {link}", name, batch or "your", link)


def build_params(name, batch, phone, message=None):
    """Template variables (3):
      {{1}} = student ka naam
      {{2}} = message (admin ka likha) + Class Manager link
      {{3}} = student ka registered mobile number
    Welcome ka message text portal se editable hai."""
    c = cfg()
    link = c["link"] or ""
    if message is not None:
        body = _resolve(message, name, batch, link)
    else:
        body = _welcome_body(name, batch, link)
    # {{2}} ke saath Class Manager link (agar message me pehle se na ho to jod do)
    if link and link not in body:
        body = (body.rstrip() + " " + link).strip()
    ph = _phone_intl(phone, plus=True) or str(phone or "").strip()
    return [str(name or "Student"), body, ph]


def build_message(name, batch, phone, template=None, message=None):
    """Preview text — jaisa template render hoga (Hi {{1}}, {{2}} ... mobile: {{3}})."""
    p = build_params(name, batch, phone, message=message)
    ph = p[2] if len(p) > 2 else ""
    return "Hi " + p[0] + ",\n\n" + p[1] + "\n\nYour registered mobile number: " + ph


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
