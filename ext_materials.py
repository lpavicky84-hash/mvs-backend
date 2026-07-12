"""
EXTERNAL STUDY MATERIAL BRIDGE
==============================
Connects this CRM to the MVS Foundation Student Portal (Admin Console,
Backend V7.1) so that all study materials already uploaded there —
Notes, Question Bank, Syllabus, PYQs, TMA Solutions, Practical
Solutions, etc. — appear inside the Student & Teacher portals here
WITHOUT re-uploading anything.

How it works
------------
  CRM (this app)  --->  Student Portal  /api/integration/materials       (JSON list)
  CRM (this app)  --->  Student Portal  /api/integration/material/{id}/file  (file bytes)

Both calls are server-to-server with a shared secret key, so the key is
never exposed to browsers and no CORS setup is needed on either side.
The list is cached in memory for 5 minutes to keep things fast.

Railway environment variables (this CRM service)
------------------------------------------------
  STUDENT_PORTAL_URL = https://<student-portal-backend-domain>      (no trailing slash)
  STUDENT_PORTAL_KEY = <same secret that the Student Portal checks>

If these are not set, the endpoints respond with {"configured": false}
and the frontend shows a friendly "connection pending" card — nothing
breaks.
"""
import os
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from security import get_current_user

router = APIRouter(prefix="/api/ext", tags=["External Study Material"])

CACHE_SECONDS = 300          # material list cache (5 min)
FILE_CACHE_SECONDS = 3600    # small per-file cache (1 hr)
FILE_CACHE_MAX = 20          # keep at most N files in memory

_list_cache = {"data": None, "ts": 0.0}
_file_cache = {}             # id -> {"bytes":..., "ctype":..., "ts":...}


def _cfg():
    url = (os.getenv("STUDENT_PORTAL_URL") or "").strip().rstrip("/")
    key = (os.getenv("STUDENT_PORTAL_KEY") or "").strip()
    return url, key


def _normalize(raw):
    """Accept slightly different field names from the Student Portal and
    normalize into the shape the frontend expects."""
    out = []
    for m in raw or []:
        if not isinstance(m, dict):
            continue
        link = m.get("link") or m.get("external_link") or m.get("url") or ""
        kind = m.get("kind") or ("link" if link and not m.get("has_file") else "file")
        out.append({
            "id": str(m.get("id") or m.get("material_id") or ""),
            "title": m.get("title") or m.get("name") or "Untitled",
            "category": m.get("category") or m.get("tab") or m.get("section") or "Other",
            "session": m.get("session") or m.get("batch") or m.get("stream") or "",
            "class_level": str(m.get("class_level") or m.get("class") or "") or None,
            "subject": m.get("subject") or "",
            "medium": m.get("medium") or "",
            "kind": kind,
            "link": link or None,
            "filename": m.get("filename") or "",
            "updated_at": m.get("updated_at") or m.get("created_at") or "",
        })
    # newest first if timestamps present
    out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return out


@router.get("/materials")
def ext_materials(refresh: int = 0, current_user=Depends(get_current_user)):
    """List every study material from the Student Portal (any logged-in role)."""
    url, key = _cfg()
    if not url or not key:
        return {"configured": False, "materials": []}

    now = time.time()
    if not refresh and _list_cache["data"] is not None and (now - _list_cache["ts"]) < CACHE_SECONDS:
        return {"configured": True, "cached": True, "materials": _list_cache["data"]}

    try:
        r = httpx.get(url + "/api/integration/materials",
                      headers={"X-MVS-KEY": key}, timeout=30)
        r.raise_for_status()
        data = r.json()
        raw = data.get("materials") if isinstance(data, dict) else data
        mats = _normalize(raw)
        _list_cache["data"] = mats
        _list_cache["ts"] = now
        return {"configured": True, "cached": False, "materials": mats}
    except Exception as e:
        # serve stale cache if the portal is briefly down
        if _list_cache["data"] is not None:
            return {"configured": True, "cached": True, "stale": True,
                    "materials": _list_cache["data"]}
        raise HTTPException(status_code=502,
                            detail=f"Student Portal se connect nahi ho paya: {e}")


@router.get("/material/{mid}/file")
def ext_material_file(mid: str, current_user=Depends(get_current_user)):
    """Stream one material file from the Student Portal to the browser."""
    url, key = _cfg()
    if not url or not key:
        raise HTTPException(status_code=503, detail="Student Portal connection configured nahi hai")

    now = time.time()
    c = _file_cache.get(mid)
    if c and (now - c["ts"]) < FILE_CACHE_SECONDS:
        return Response(content=c["bytes"], media_type=c["ctype"],
                        headers={"Content-Disposition": c["disp"]})

    try:
        r = httpx.get(f"{url}/api/integration/material/{mid}/file",
                      headers={"X-MVS-KEY": key}, timeout=120,
                      follow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"File fetch failed: {e}")

    ctype = r.headers.get("content-type", "application/pdf")
    disp = r.headers.get("content-disposition") or f'inline; filename="material_{mid}.pdf"'

    # tiny in-memory cache so repeated opens are instant
    if len(_file_cache) >= FILE_CACHE_MAX:
        oldest = min(_file_cache, key=lambda k: _file_cache[k]["ts"])
        _file_cache.pop(oldest, None)
    _file_cache[mid] = {"bytes": r.content, "ctype": ctype, "disp": disp, "ts": now}

    return Response(content=r.content, media_type=ctype,
                    headers={"Content-Disposition": disp})


@router.get("/status")
def ext_status(current_user=Depends(get_current_user)):
    """Quick connection health check (used for debugging)."""
    url, key = _cfg()
    if not url or not key:
        return {"configured": False}
    try:
        r = httpx.get(url + "/api/integration/materials",
                      headers={"X-MVS-KEY": key}, timeout=15)
        ok = r.status_code == 200
        n = 0
        if ok:
            d = r.json()
            n = len(d.get("materials", d if isinstance(d, list) else []))
        return {"configured": True, "reachable": ok, "status_code": r.status_code, "count": n}
    except Exception as e:
        return {"configured": True, "reachable": False, "error": str(e)}
