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
                      connect_timeout=8, read_timeout=30,
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
        # browser 1 din cache kare — warna har render par 302 dobara hit hota tha (photo flood)
        return RedirectResponse(url=value, status_code=302,
                                headers={"Cache-Control": "public, max-age=86400"})
    import base64 as _b64
    from fastapi import Response
    return Response(content=_b64.b64decode(value), media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


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


def _fetch_r2_bytes(url):
    """Authenticated S3 GET by key — works even when the bucket is NOT public.
    Extracts the object key from the stored URL and fetches with credentials."""
    try:
        cli = _client()
        if not cli:
            return None
        c = _cfg()
        pub = (c.get("public_url") or "").rstrip("/")
        key = url
        if pub and url.startswith(pub):
            key = url[len(pub):].lstrip("/")
        else:
            from urllib.parse import urlparse
            path = urlparse(url).path.lstrip("/")
            # custom-domain/S3-style URL may include the bucket name as first path segment
            bkt = str(c.get("bucket") or "")
            if bkt and path.startswith(bkt + "/"):
                path = path[len(bkt) + 1:]
            key = path
        if not key:
            return None
        obj = cli.get_object(Bucket=c["bucket"], Key=key)
        return obj["Body"].read()
    except Exception:
        return None


def proxy_response(value, media_type="application/octet-stream", filename=None, download=True, sniff=False):
    """Inline viewer / same-origin ke liye: R2 URL ho to server-side fetch karke bytes
    STREAM karo (cross-origin fetch/CORS ki dikkat nahi aayegi, aur URL pe b64decode crash
    bhi nahi). Base64 ho to decode. Fetch fail ho to redirect fallback. Empty -> 404.
    sniff=True -> content-type file ke ASAL magic bytes se pakdo (migration ne galat label
    diya ho to bhi sahi type se serve ho, e.g. PDF ko image/jpeg bana diya tha)."""
    from fastapi import HTTPException, Response
    if not value:
        raise HTTPException(status_code=404, detail="Not found")
    if isinstance(value, str) and value.startswith("http"):
        # 1) Authenticated S3 GET (bucket public na ho tab bhi chalega) — sabse reliable
        data = _fetch_r2_bytes(value)
        if not data:
            # 2) Public URL se seedha fetch (agar bucket public hai)
            try:
                import urllib.request
                req = urllib.request.Request(value, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
                    "Accept": "*/*",
                })
                with urllib.request.urlopen(req, timeout=25) as r:
                    if getattr(r, "status", 200) not in (200, 206):
                        raise Exception("bad status")
                    data = r.read()
                # HTML challenge/error page mila (asli file nahi) -> galat, redirect
                if data[:20].lstrip()[:1] in (b"<",) and b"PDF" not in data[:8] and data[:3] != b"\xff\xd8\xff":
                    from fastapi.responses import RedirectResponse
                    return RedirectResponse(url=value, status_code=302)
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
    # magic bytes se ASAL type pakdo (galat label theek karne ke liye)
    if sniff and data:
        if data[:4] == b"%PDF": media_type = "application/pdf"
        elif data[:3] == b"\xff\xd8\xff": media_type = "image/jpeg"
        elif data[:8].startswith(b"\x89PNG"): media_type = "image/png"
        elif data[:4] == b"RIFF" and b"WEBP" in data[:16]: media_type = "image/webp"
        elif data[:6] in (b"GIF87a", b"GIF89a"): media_type = "image/gif"
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
        s = value.split(",")[-1]
        s = "".join(s.split())          # whitespace/newlines hatao
        s += "=" * (-len(s) % 4)        # padding theek karo (warna decode toot/kharab)
        raw = _b64.b64decode(s)
    except Exception:
        return value
    if not raw or len(raw) < 8:
        return value
    ct = (content_type or "").lower()
    # image/pdf ke liye magic bytes check — decode galat nikla to base64 hi rehne do (corrupt na ho)
    if ("image" in ct or "pdf" in ct or "jpeg" in ct or "png" in ct):
        _head = raw[:1024]
        _ok = (raw[:3] == b"\xff\xd8\xff" or raw[:8].startswith(b"\x89PNG") or b"%PDF" in _head
               or raw[:4] == b"RIFF" or raw[:6] in (b"GIF87a", b"GIF89a") or raw[:2] == b"BM"
               or raw[:4] in (b"II*\x00", b"MM\x00*") or b"ftyp" in raw[:40])
        if not _ok and len(raw) < 300:
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
