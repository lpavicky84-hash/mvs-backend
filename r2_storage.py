"""Cloudflare R2 storage helper (S3-compatible).

Env vars (Railway):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_URL

Usage:
  import r2_storage as R2
  if R2.is_configured():
      url = R2.upload_bytes("photos/teacher/5.jpg", data, "image/jpeg")
"""
import os

_STATE = {"client": None}


def _cfg():
    return {
        "account_id": (os.getenv("R2_ACCOUNT_ID") or "").strip(),
        "access_key": (os.getenv("R2_ACCESS_KEY_ID") or "").strip(),
        "secret_key": (os.getenv("R2_SECRET_ACCESS_KEY") or "").strip(),
        "bucket":     (os.getenv("R2_BUCKET") or "").strip(),
        "public_url": (os.getenv("R2_PUBLIC_URL") or "").strip().rstrip("/"),
    }


def is_configured():
    c = _cfg()
    return bool(c["account_id"] and c["access_key"] and c["secret_key"] and c["bucket"])


def _client():
    """Lazy boto3 client (import inside so app boots even if boto3 missing)."""
    if _STATE["client"] is not None:
        return _STATE["client"]
    if not is_configured():
        return None
    c = _cfg()
    import boto3
    from botocore.config import Config
    endpoint = "https://%s.r2.cloudflarestorage.com" % c["account_id"]
    _STATE["client"] = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=c["access_key"],
        aws_secret_access_key=c["secret_key"],
        config=Config(signature_version="s3v4", region_name="auto",
                      retries={"max_attempts": 3, "mode": "standard"}),
    )
    return _STATE["client"]


def public_url(key):
    c = _cfg()
    return "%s/%s" % (c["public_url"], str(key).lstrip("/"))


def upload_bytes(key, data, content_type="application/octet-stream", cache_seconds=31536000):
    """Upload bytes -> return public URL. key e.g. 'photos/teacher/5.jpg'."""
    cli = _client()
    if not cli:
        raise RuntimeError("R2 not configured")
    c = _cfg()
    cli.put_object(
        Bucket=c["bucket"], Key=str(key).lstrip("/"), Body=data,
        ContentType=content_type,
        CacheControl="public, max-age=%d, immutable" % int(cache_seconds),
    )
    return public_url(key)


def delete_key(key):
    cli = _client()
    if not cli:
        return False
    try:
        cli.delete_object(Bucket=_cfg()["bucket"], Key=str(key).lstrip("/"))
        return True
    except Exception:
        return False


def store_photo_value(key, raw, content_type="image/jpeg"):
    """Photo ko R2 par upload karke URL return karo (photo_b64 field me yehi URL
    save hoga). R2 na ho ya fail ho -> base64 string (purana tarika, fallback)."""
    import base64 as _b64
    if is_configured():
        try:
            return upload_bytes(key, raw, content_type or "image/jpeg")
        except Exception:
            pass
    return _b64.b64encode(raw).decode("ascii")


def photo_response(value):
    """photo_b64 field ka value -> image response. 'http' se shuru (R2 URL) ho to
    R2 par redirect (free egress), warna base64 decode karke serve."""
    from fastapi import HTTPException
    if not value:
        raise HTTPException(status_code=404, detail="No photo")
    if isinstance(value, str) and value.startswith("http"):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=value, status_code=302)
    import base64 as _b64
    from fastapi import Response
    return Response(content=_b64.b64decode(value), media_type="image/jpeg")


def new_key(prefix, filename=""):
    """Unique R2 key banao, e.g. 'materials/ab12cd34ef56.pdf'."""
    import uuid
    ext = ""
    if filename and "." in str(filename):
        ext = "." + str(filename).rsplit(".", 1)[-1].lower()[:8]
    return "%s/%s%s" % (str(prefix).strip("/"), uuid.uuid4().hex[:16], ext)


def store_file_value(key, raw, content_type="application/octet-stream"):
    """File R2 par upload -> URL return (DB field me yehi save hoga). R2 na ho/fail
    ho -> base64 string (purana tarika, fallback)."""
    import base64 as _b64
    if is_configured():
        try:
            return upload_bytes(key, raw, content_type or "application/octet-stream")
        except Exception:
            pass
    return _b64.b64encode(raw).decode("ascii")


def file_response(value, media_type="application/octet-stream", filename=None, download=True):
    """DB field ka value -> file response. R2 URL ho to redirect (free egress),
    warna base64 decode karke serve (Content-Disposition ke saath)."""
    from fastapi import HTTPException
    if not value:
        raise HTTPException(status_code=404, detail="Not found")
    if isinstance(value, str) and value.startswith("http"):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=value, status_code=302)
    import base64 as _b64
    from fastapi import Response
    v = value.split(",")[-1] if isinstance(value, str) else value
    headers = {}
    if filename:
        disp = "attachment" if download else "inline"
        headers["Content-Disposition"] = '%s; filename="%s"' % (disp, filename)
    return Response(content=_b64.b64decode(v), media_type=media_type, headers=headers)


def proxy_response(value, media_type="application/octet-stream", filename=None, download=True):
    """Inline viewer / same-origin ke liye: R2 URL ho to server-side fetch karke bytes
    STREAM karo (cross-origin fetch/CORS ki dikkat nahi aayegi, aur URL pe b64decode crash
    bhi nahi). Base64 ho to decode. Fetch fail ho to redirect fallback. Empty -> 404."""
    from fastapi import HTTPException, Response
    if not value:
        raise HTTPException(status_code=404, detail="Not found")
    if isinstance(value, str) and value.startswith("http"):
        try:
            import urllib.request
            req = urllib.request.Request(value, headers={"User-Agent": "mvs-proxy"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = r.read()
        except Exception:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=value, status_code=302)
    else:
        import base64 as _b64
        v = value.split(",")[-1] if isinstance(value, str) else value
        try:
            data = _b64.b64decode(v)
        except Exception:
            raise HTTPException(status_code=400, detail="File could not be read")
    headers = {}
    if filename:
        disp = "attachment" if download else "inline"
        headers["Content-Disposition"] = '%s; filename="%s"' % (disp, filename)
    return Response(content=data, media_type=media_type, headers=headers)


def normalize(value, prefix, content_type="application/octet-stream"):
    """Store-time helper: base64/dataURL -> R2 par upload -> URL return.
    Agar pehle se http URL hai -> waise hi. None/empty -> waise hi.
    R2 na ho ya fail -> base64 waise ka waisa (data loss nahi)."""
    if not value or not isinstance(value, str):
        return value
    if value.startswith("http"):
        return value
    if not is_configured():
        return value
    try:
        import base64 as _b64
        raw = _b64.b64decode(value.split(",")[-1])
    except Exception:
        return value
    ct = (content_type or "").lower()
    ext = ".bin"
    if "pdf" in ct: ext = ".pdf"
    elif "png" in ct: ext = ".png"
    elif "webm" in ct or "ogg" in ct: ext = ".webm"
    elif "mp3" in ct or "mpeg" in ct: ext = ".mp3"
    elif "jpeg" in ct or "jpg" in ct or "image" in ct: ext = ".jpg"
    try:
        return upload_bytes(new_key(prefix, "f" + ext), raw, content_type or "application/octet-stream")
    except Exception:
        return value
