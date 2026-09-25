# community_routes.py — Admin COMMUNITY
# Batch-wise student groups + broadcasts + targeted popups.
# Every post can carry image/pdf attachments + a video/link, notifies all recipients
# at send time, and tracks per-recipient delivery / seen / click for a read-report.
import base64
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Body, Request
from sqlalchemy.orm import Session
from sqlalchemy import or_

from database import get_db
from security import get_admin, get_student, get_current_user
from models import (User, UserRole, StudentProfile, StudentBatch, Batch, Notification,
                    CommunityGroup, CommunityGroupMember, CommunityPost,
                    CommunityAttachment, CommunityRead)

try:
    from admin_routes import admin_section_guard as _admin_guard
except Exception:  # pragma: no cover
    def _admin_guard():
        return None

import r2_storage as r2

router = APIRouter(prefix="/api", tags=["Community"])

IST = timedelta(hours=5, minutes=30)
_MAX_BYTES = 22 * 1024 * 1024   # ~22 MB per attachment


# ------------------------------------------------------------------ helpers
def _ist_str(dt):
    if not dt:
        return ""
    try:
        y = dt.replace(tzinfo=timezone.utc).astimezone(timezone(IST))
        return y.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        try:
            return dt.strftime("%d %b %Y, %I:%M %p")
        except Exception:
            return ""


def _decode_datauri(s):
    """Return (raw_bytes, mime) from a data: URI or bare base64 string."""
    if not s or not isinstance(s, str):
        return None, ""
    mime = ""
    b64 = s
    m = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", s, re.DOTALL)
    if m:
        mime = (m.group(1) or "").strip()
        b64 = m.group(3) or ""
    try:
        raw = base64.b64decode(b64 + "===")
    except Exception:
        return None, ""
    if not raw or len(raw) > _MAX_BYTES:
        return None, ""
    return raw, mime


def _ext_for(mime, name=""):
    mime = (mime or "").lower()
    if "pdf" in mime or (name or "").lower().endswith(".pdf"):
        return "pdf"
    if "png" in mime:
        return "png"
    if "webp" in mime:
        return "webp"
    if "gif" in mime:
        return "gif"
    return "jpg"


def _save_attachments(db, post_id, images, files):
    """images: list of data-URI strings (photos). files: list of {name,data,mime} (pdf/other)."""
    n = 0
    for im in (images or [])[:12]:
        raw, mime = _decode_datauri(im)
        if not raw:
            continue
        mime = mime or "image/jpeg"
        try:
            url = r2.store_file_value(r2.new_key("community/img", "a." + _ext_for(mime)), raw, mime)
        except Exception:
            continue
        db.add(CommunityAttachment(post_id=post_id, url=url, mime=mime, name="", kind="image"))
        n += 1
    for f in (files or [])[:12]:
        if not isinstance(f, dict):
            continue
        raw, mime = _decode_datauri(f.get("data") or "")
        if not raw:
            continue
        name = (f.get("name") or "file").strip()[:230]
        mime = (f.get("mime") or mime or "application/octet-stream")
        kind = "pdf" if ("pdf" in mime.lower() or name.lower().endswith(".pdf")) else \
               ("image" if mime.lower().startswith("image") else "file")
        try:
            url = r2.store_file_value(r2.new_key("community/file", name or ("f." + _ext_for(mime, name))), raw, mime)
        except Exception:
            continue
        db.add(CommunityAttachment(post_id=post_id, url=url, mime=mime, name=name, kind=kind))
        n += 1
    return n


def _att_out(db, post_id):
    out = []
    for a in (db.query(CommunityAttachment)
              .filter(CommunityAttachment.post_id == post_id)
              .order_by(CommunityAttachment.id.asc()).all()):
        out.append({"id": a.id, "kind": a.kind or "image", "name": a.name or "",
                    "mime": a.mime or "", "url": "/api/community-file?id=" + str(a.id)})
    return out


def _batch_label(b):
    """Batch display name WITH its session, so duplicate names never mix up."""
    nm = (b.name or b.code or "Batch").strip()
    ss = (getattr(b, "session", "") or "").strip()
    return (nm + " — " + ss) if ss and ss.lower() not in nm.lower() else nm


def _batch_member_uids(db, batch_id):
    """Student User.id enrolled in a batch — DISTINCT, counted exactly like the admin
    Batches page: primary link (StudentProfile.batch_id) UNION add-on (StudentBatch)."""
    sids = set()
    try:
        for (sid,) in db.query(StudentProfile.id).filter(StudentProfile.batch_id == batch_id).all():
            if sid:
                sids.add(sid)
        for (sid,) in db.query(StudentBatch.student_id).filter(StudentBatch.batch_id == batch_id).all():
            if sid:
                sids.add(sid)
    except Exception:
        pass
    uids = set()
    if sids:
        for sp in db.query(StudentProfile).filter(StudentProfile.id.in_(list(sids))).all():
            if sp.user_id:
                uids.add(sp.user_id)
    return uids


def _batch_count_map(db):
    """{batch_id: distinct student count} — same definition as the admin Batches page,
    so Community numbers always match what batch managers see."""
    from collections import defaultdict
    prim = defaultdict(set)
    enr = defaultdict(set)
    try:
        for sid, bid in (db.query(StudentProfile.id, StudentProfile.batch_id)
                         .filter(StudentProfile.batch_id != None).all()):  # noqa: E711
            if bid:
                prim[bid].add(sid)
        for sid, bid in db.query(StudentBatch.student_id, StudentBatch.batch_id).all():
            if bid and sid:
                enr[bid].add(sid)
    except Exception:
        pass
    out = {}
    for bid in set(list(prim.keys()) + list(enr.keys())):
        out[bid] = len(prim.get(bid, set()) | enr.get(bid, set()))
    return out


def _group_member_uids(db, group):
    """Set of student User.id in a group:
    - source=session: students whose exam_session matches session_key ('__none__' = not set)
    - batch group: students enrolled in that batch (dynamic)
    - plus any explicit members."""
    uids = set()
    try:
        src = (getattr(group, "source", "") or "").lower()
        skey = getattr(group, "session_key", None)
        if src == "session" or skey:
            if skey == "__none__":
                q = db.query(StudentProfile).filter(
                    or_(StudentProfile.exam_session == None,  # noqa: E711
                        StudentProfile.exam_session == ""))
            else:
                q = db.query(StudentProfile).filter(StudentProfile.exam_session == skey)
            for sp in q.all():
                if sp.user_id:
                    uids.add(sp.user_id)
        elif getattr(group, "batch_id", None):
            uids |= _batch_member_uids(db, group.batch_id)
        for m in db.query(CommunityGroupMember).filter(CommunityGroupMember.group_id == group.id).all():
            if m.user_id:
                uids.add(m.user_id)
    except Exception:
        pass
    return uids


def _resolve_targets(db, target):
    """target dict -> set of student User.id.
    {scope:'all'} | {scope:'batches', batch_ids:[...]} | {scope:'students', user_ids:[...]}"""
    uids = set()
    target = target or {}
    scope = (target.get("scope") or "all").lower()
    try:
        if scope == "students":
            want = set(int(x) for x in (target.get("user_ids") or []) if str(x).strip())
            if want:
                for u in db.query(User).filter(User.role == UserRole.student, User.id.in_(list(want))).all():
                    uids.add(u.id)
        elif scope == "batches":
            bids = [int(x) for x in (target.get("batch_ids") or []) if str(x).strip()]
            for bid in bids:
                uids |= _batch_member_uids(db, bid)
        else:  # all students
            for u in db.query(User).filter(User.role == UserRole.student, User.is_active == True).all():  # noqa: E712
                uids.add(u.id)
    except Exception:
        pass
    return uids


def _target_label(db, target):
    target = target or {}
    scope = (target.get("scope") or "all").lower()
    if scope == "all":
        return "All students"
    if scope == "students":
        return str(len(target.get("user_ids") or [])) + " selected students"
    if scope == "batches":
        names = []
        try:
            bids = [int(x) for x in (target.get("batch_ids") or [])]
            for b in db.query(Batch).filter(Batch.id.in_(bids)).all():
                names.append(_batch_label(b))
        except Exception:
            pass
        return ", ".join(names[:4]) + (" +" + str(len(names) - 4) if len(names) > 4 else "") if names else "Selected batches"
    return "Students"


def _fanout(db, post, uids, sender, link=None, image_url=None):
    """Create a CommunityRead + Notification per recipient — BULK insert (ek hi INSERT
    har table ke liye), taaki 2000+ students par bhi broadcast turant ho (pehle per-row
    add() se 30-40s lagta tha). created_at = DB ka NOW() taaki baaki notifications ke saath
    exact same time-base rahe aur ordering sahi rahe."""
    from sqlalchemy import text as _text
    try:
        db_now = db.execute(_text("SELECT NOW()")).scalar() or datetime.utcnow()
    except Exception:
        db_now = datetime.utcnow()
    ttl = (post.title or "").strip() or ("New broadcast" if post.kind == "broadcast"
                                         else ("Important" if post.kind == "popup" else "New message"))
    body = (post.body or "")[:180]
    ntype = "community_" + (post.kind or "group")
    prefix = "" if post.kind == "group" else "\U0001F4E2 "
    full_title = (prefix + ttl)[:190]
    blabel = (post.target_label or "")[:150]
    sid = getattr(sender, "id", None)
    uids = [u for u in uids if u]
    if uids:
        db.bulk_insert_mappings(CommunityRead, [
            {"post_id": post.id, "user_id": uid, "created_at": db_now} for uid in uids])
        db.bulk_insert_mappings(Notification, [
            {"user_id": uid, "title": full_title, "message": body, "notif_type": ntype,
             "link": (link or None), "image_url": (image_url or None),
             "sender_id": sid, "sender_role": "admin", "is_read": False,
             "batch_key": "cp_" + str(post.id), "batch_label": blabel,
             "created_at": db_now} for uid in uids])
    post.target_count = len(uids)
    return len(uids)


def _first_image_url(db, post_id):
    a = (db.query(CommunityAttachment)
         .filter(CommunityAttachment.post_id == post_id, CommunityAttachment.kind == "image")
         .order_by(CommunityAttachment.id.asc()).first())
    return ("/api/community-file?id=" + str(a.id)) if a else None


def _post_out(db, p, with_counts=False):
    out = {"id": p.id, "kind": p.kind, "group_id": p.group_id, "title": p.title or "",
           "body": p.body or "", "link": p.link or "", "popup_style": p.popup_style or "info",
           "sender_name": p.sender_name or "", "at": _ist_str(p.created_at),
           "pinned": bool(getattr(p, "is_pinned", False)),
           "target_label": p.target_label or "", "target_count": p.target_count or 0,
           "attachments": _att_out(db, p.id)}
    if with_counts:
        seen = db.query(CommunityRead).filter(CommunityRead.post_id == p.id,
                                              CommunityRead.seen_at.isnot(None)).count()
        clicked = db.query(CommunityRead).filter(CommunityRead.post_id == p.id,
                                                 CommunityRead.clicked_at.isnot(None)).count()
        sent = db.query(CommunityRead).filter(CommunityRead.post_id == p.id).count()
        out["sent"] = sent or (p.target_count or 0)
        out["seen"] = seen
        out["clicked"] = clicked
    return out


# =========================================================== ADMIN: GROUPS
@router.get("/admin/community/targets", dependencies=[Depends(_admin_guard)])
def community_targets(db: Session = Depends(get_db), _=Depends(get_admin)):
    batches = []
    try:
        counts = _batch_count_map(db)
        for b in db.query(Batch).filter(Batch.active == True).order_by(Batch.name.asc()).all():  # noqa: E712
            batches.append({"id": b.id, "name": _batch_label(b), "type": b.type or "",
                            "session": (getattr(b, "session", "") or ""),
                            "count": counts.get(b.id, 0)})
    except Exception:
        pass
    total = db.query(User).filter(User.role == UserRole.student, User.is_active == True).count()  # noqa: E712
    return {"batches": batches, "total_students": total}


@router.get("/admin/community/batch-audit", dependencies=[Depends(_admin_guard)])
def community_batch_audit(name: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Live data-integrity report: unique students per batch, duplicate rows, ghost rows,
    and same-name cross-session overlap (e.g. a student wrongly in BOTH Oct 2026 & April 2027)."""
    from collections import defaultdict
    out = {"batches": [], "overlaps": [], "totals": {}}
    try:
        valid_sids = set()
        sp_user = {}
        for spid, uid in db.query(StudentProfile.id, StudentProfile.user_id).all():
            valid_sids.add(spid)
            sp_user[spid] = uid
        prim = defaultdict(set)      # batch_id -> set(student_id) via StudentProfile.batch_id
        prim_of = {}
        for spid, bid in (db.query(StudentProfile.id, StudentProfile.batch_id)
                          .filter(StudentProfile.batch_id != None).all()):  # noqa: E711
            if bid:
                prim[bid].add(spid); prim_of[spid] = bid
        # StudentBatch — detect duplicates + ghosts
        pairs = set(); dup_rows = 0; ghost_rows = 0; enr = defaultdict(set)
        batch_ids = set(b.id for b in db.query(Batch.id).all())
        for sid, bid in db.query(StudentBatch.student_id, StudentBatch.batch_id).all():
            if (sid, bid) in pairs:
                dup_rows += 1
                continue
            pairs.add((sid, bid))
            if sid not in valid_sids or bid not in batch_ids:
                ghost_rows += 1
                continue
            enr[bid].add(sid)
        bq = db.query(Batch)
        if (name or "").strip():
            bq = bq.filter(Batch.name.like("%" + name.strip() + "%"))
        blist = bq.order_by(Batch.name.asc(), Batch.session.asc()).all()
        by_name = defaultdict(list)
        allsets = {}
        for b in blist:
            members = prim.get(b.id, set()) | enr.get(b.id, set())
            allsets[b.id] = members
            addon = sum(1 for s in enr.get(b.id, set()) if prim_of.get(s) != b.id)
            row = {"id": b.id, "name": b.name or "", "session": (getattr(b, "session", "") or ""),
                   "active": bool(b.active), "distinct": len(members),
                   "primary": len(prim.get(b.id, set())), "addon": addon,
                   "sb_rows": len(enr.get(b.id, set()))}
            out["batches"].append(row)
            by_name[b.name or ""].append(b)
        # same-name cross-session overlap
        for nm, cards in by_name.items():
            if len(cards) < 2:
                continue
            seen = defaultdict(int)
            for b in cards:
                for s in allsets.get(b.id, set()):
                    seen[s] += 1
            overlap = sum(1 for s, c in seen.items() if c > 1)
            if overlap:
                out["overlaps"].append({
                    "name": nm, "overlap": overlap,
                    "cards": [{"id": b.id, "session": (getattr(b, "session", "") or ""),
                               "distinct": len(allsets.get(b.id, set()))} for b in cards]})
        out["totals"] = {
            "batches": len(blist),
            "studentbatch_pairs": len(pairs),
            "duplicate_rows_removed_at_read": dup_rows,
            "ghost_rows": ghost_rows,
            "students_with_primary_batch": len(prim_of),
        }
    except Exception as e:
        out["error"] = str(e)
    out["overlaps"].sort(key=lambda x: -x["overlap"])
    return out


@router.get("/admin/community/students", dependencies=[Depends(_admin_guard)])
def community_students(q: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    qq = (q or "").strip()
    rows = []
    query = db.query(User, StudentProfile).join(
        StudentProfile, StudentProfile.user_id == User.id).filter(User.role == UserRole.student)
    if qq:
        like = "%" + qq + "%"
        query = query.filter(or_(User.name.like(like), StudentProfile.phone.like(like),
                                 User.user_id.like(like)))
    for u, sp in query.order_by(User.name.asc()).limit(400).all():
        rows.append({"user_id": u.id, "profile_id": sp.id, "name": u.name,
                     "phone": sp.phone or "", "class_level": sp.class_level or "",
                     "batch": sp.batch_name or ""})
    return {"students": rows}


@router.get("/admin/community/groups", dependencies=[Depends(_admin_guard)])
def community_groups(db: Session = Depends(get_db), _=Depends(get_admin)):
    out = []
    for g in (db.query(CommunityGroup).filter(CommunityGroup.is_active == True)  # noqa: E712
              .order_by(CommunityGroup.id.desc()).all()):
        members = len(_group_member_uids(db, g))
        last = (db.query(CommunityPost).filter(CommunityPost.kind == "group",
                                               CommunityPost.group_id == g.id, CommunityPost.is_active == True)  # noqa: E712
                .order_by(CommunityPost.id.desc()).first())
        out.append({"id": g.id, "name": g.name or "", "description": g.description or "",
                    "batch_id": g.batch_id, "icon_color": g.icon_color or "",
                    "members": members,
                    "last": (last.title or last.body or "")[:80] if last else "",
                    "last_at": _ist_str(last.created_at) if last else ""})
    return {"groups": out}


@router.post("/admin/community/groups", dependencies=[Depends(_admin_guard)])
def community_group_create(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_admin)):
    name = (payload.get("name") or "").strip()
    batch_id = payload.get("batch_id")
    try:
        batch_id = int(batch_id) if batch_id not in (None, "", "0", 0) else None
    except Exception:
        batch_id = None
    if batch_id and not name:
        b = db.query(Batch).filter(Batch.id == batch_id).first()
        name = _batch_label(b) if b else "Group"
    if not name:
        raise HTTPException(400, "Group name required")
    g = CommunityGroup(name=name[:160], description=(payload.get("description") or "")[:600],
                       batch_id=batch_id, source=("batch" if batch_id else "custom"),
                       icon_color=(payload.get("icon_color") or "")[:16],
                       created_by=getattr(me, "id", None), is_active=True)
    db.add(g); db.flush()
    for uid in (payload.get("member_ids") or [])[:2000]:
        try:
            db.add(CommunityGroupMember(group_id=g.id, user_id=int(uid)))
        except Exception:
            pass
    db.commit()
    return {"ok": True, "id": g.id, "members": len(_group_member_uids(db, g))}


@router.post("/admin/community/groups/auto-sync", dependencies=[Depends(_admin_guard)])
def community_groups_autosync(db: Session = Depends(get_db), me=Depends(get_admin)):
    """Auto-create a group for every active batch (session in the name), plus a
    'No Session' group for students who haven't set their exam session yet.
    Membership is dynamic, so new students auto-join and moving to a session
    updates their groups automatically. Idempotent — safe to run any time."""
    created = 0
    try:
        existing_bids = set(g.batch_id for g in db.query(CommunityGroup)
                            .filter(CommunityGroup.batch_id.isnot(None), CommunityGroup.is_active == True).all())  # noqa: E712
        for b in db.query(Batch).filter(Batch.active == True).all():  # noqa: E712
            if b.id in existing_bids:
                continue
            db.add(CommunityGroup(name=_batch_label(b)[:160], batch_id=b.id, source="batch",
                                  created_by=getattr(me, "id", None), is_active=True))
            created += 1
        # 'No Session' catch-all group
        has_none = db.query(CommunityGroup).filter(CommunityGroup.session_key == "__none__",
                                                   CommunityGroup.is_active == True).first()  # noqa: E712
        if not has_none:
            db.add(CommunityGroup(name="No Session (setup pending)", source="session",
                                  session_key="__none__", icon_color="#8a8578",
                                  created_by=getattr(me, "id", None), is_active=True))
            created += 1
        db.commit()
    except Exception:
        db.rollback()
    return {"ok": True, "created": created}


@router.delete("/admin/community/groups/{gid}", dependencies=[Depends(_admin_guard)])
def community_group_delete(gid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    g = db.query(CommunityGroup).filter(CommunityGroup.id == gid).first()
    if g:
        g.is_active = False
        db.commit()
    return {"ok": True}


@router.get("/admin/community/groups/{gid}/posts", dependencies=[Depends(_admin_guard)])
def community_group_posts_admin(gid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    g = db.query(CommunityGroup).filter(CommunityGroup.id == gid).first()
    if not g:
        raise HTTPException(404, "Group not found")
    posts = [_post_out(db, p, with_counts=True) for p in
             (db.query(CommunityPost).filter(CommunityPost.kind == "group",
                                             CommunityPost.group_id == gid, CommunityPost.is_active == True)  # noqa: E712
              .order_by(CommunityPost.is_pinned.desc(), CommunityPost.id.asc()).all())]
    return {"group": {"id": g.id, "name": g.name, "members": len(_group_member_uids(db, g))},
            "posts": posts}


@router.post("/admin/community/groups/{gid}/post", dependencies=[Depends(_admin_guard)])
def community_group_post(gid: int, payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_admin)):
    g = db.query(CommunityGroup).filter(CommunityGroup.id == gid).first()
    if not g:
        raise HTTPException(404, "Group not found")
    body = (payload.get("body") or "").strip()
    link = (payload.get("link") or "").strip()
    images = payload.get("images") or []
    files = payload.get("files") or []
    if not body and not link and not images and not files:
        raise HTTPException(400, "Message is empty")
    p = CommunityPost(kind="group", group_id=gid, title=(payload.get("title") or "")[:240],
                      body=body, link=link[:600], sender_id=getattr(me, "id", None),
                      sender_name=getattr(me, "name", "Admin"), target_label=g.name or "Group")
    db.add(p); db.flush()
    _save_attachments(db, p.id, images, files)
    uids = _group_member_uids(db, g)
    _fanout(db, p, uids, me, link=(link if link.startswith("http") else None),
            image_url=_first_image_url(db, p.id))
    db.commit()
    return {"ok": True, "post": _post_out(db, p, with_counts=True)}


# ===================================================== ADMIN: BROADCAST / POPUP
@router.post("/admin/community/broadcast", dependencies=[Depends(_admin_guard)])
def community_broadcast(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_admin)):
    body = (payload.get("body") or "").strip()
    title = (payload.get("title") or "").strip()
    link = (payload.get("link") or "").strip()
    images = payload.get("images") or []
    files = payload.get("files") or []
    target = payload.get("target") or {"scope": "all"}
    if not title and not body and not images and not files:
        raise HTTPException(400, "Nothing to send")
    p = CommunityPost(kind="broadcast", title=title[:240], body=body, link=link[:600],
                      sender_id=getattr(me, "id", None), sender_name=getattr(me, "name", "Admin"),
                      target_label=_target_label(db, target))
    db.add(p); db.flush()
    _save_attachments(db, p.id, images, files)
    uids = _resolve_targets(db, target)
    if not uids:
        db.rollback()
        raise HTTPException(400, "No students matched the target")
    n = _fanout(db, p, uids, me, link=(link if link.startswith("http") else None),
                image_url=_first_image_url(db, p.id))
    db.commit()
    return {"ok": True, "sent": n, "post": _post_out(db, p, with_counts=True)}


@router.post("/admin/community/popup", dependencies=[Depends(_admin_guard)])
def community_popup(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_admin)):
    title = (payload.get("title") or "").strip()
    body = (payload.get("body") or "").strip()
    link = (payload.get("link") or "").strip()
    images = payload.get("images") or []
    style = (payload.get("popup_style") or "info").strip().lower()
    if style not in ("info", "success", "alert"):
        style = "info"
    target = payload.get("target") or {"scope": "all"}
    if not title and not body and not images:
        raise HTTPException(400, "Popup is empty")
    p = CommunityPost(kind="popup", title=title[:240], body=body, link=link[:600],
                      popup_style=style, sender_id=getattr(me, "id", None),
                      sender_name=getattr(me, "name", "Admin"), target_label=_target_label(db, target))
    db.add(p); db.flush()
    _save_attachments(db, p.id, images, [])
    uids = _resolve_targets(db, target)
    if not uids:
        db.rollback()
        raise HTTPException(400, "No students matched the target")
    n = _fanout(db, p, uids, me, link=(link if link.startswith("http") else None),
                image_url=_first_image_url(db, p.id))
    db.commit()
    return {"ok": True, "sent": n, "post": _post_out(db, p, with_counts=True)}


@router.get("/admin/community/posts", dependencies=[Depends(_admin_guard)])
def community_posts_admin(kind: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    q = db.query(CommunityPost).filter(CommunityPost.is_active == True)  # noqa: E712
    k = (kind or "").strip().lower()
    if k in ("broadcast", "popup", "group"):
        q = q.filter(CommunityPost.kind == k)
    else:
        q = q.filter(CommunityPost.kind.in_(["broadcast", "popup"]))
    posts = [_post_out(db, p, with_counts=True) for p in
             q.order_by(CommunityPost.id.desc()).limit(100).all()]
    return {"posts": posts}


@router.get("/admin/community/posts/{pid}/receipts", dependencies=[Depends(_admin_guard)])
def community_receipts(pid: int, filter: str = "all", db: Session = Depends(get_db), _=Depends(get_admin)):
    p = db.query(CommunityPost).filter(CommunityPost.id == pid).first()
    if not p:
        raise HTTPException(404, "Not found")
    reads = db.query(CommunityRead).filter(CommunityRead.post_id == pid).all()
    uids = [r.user_id for r in reads]
    umap = {}
    if uids:
        for u in db.query(User).filter(User.id.in_(uids)).all():
            umap[u.id] = u
    spmap = {}
    if uids:
        for sp in db.query(StudentProfile).filter(StudentProfile.user_id.in_(uids)).all():
            spmap[sp.user_id] = sp
    f = (filter or "all").lower()
    out = []
    for r in reads:
        seen = r.seen_at is not None
        clicked = r.clicked_at is not None
        if f == "seen" and not seen:
            continue
        if f == "clicked" and not clicked:
            continue
        if f == "pending" and seen:
            continue
        u = umap.get(r.user_id)
        sp = spmap.get(r.user_id)
        out.append({"user_id": r.user_id, "profile_id": (sp.id if sp else None),
                    "name": (u.name if u else "Student"),
                    "mvs_id": (u.user_id if u else ""),
                    "class": (sp.class_level if sp else "") or "",
                    "phone": (sp.phone if sp else "") or "",
                    "seen": seen, "seen_at": _ist_str(r.seen_at),
                    "clicked": clicked, "clicked_at": _ist_str(r.clicked_at)})
    out.sort(key=lambda x: (0 if x["seen"] else 1, x["name"].lower()))
    seen_n = sum(1 for r in reads if r.seen_at is not None)
    clicked_n = sum(1 for r in reads if r.clicked_at is not None)
    return {"post": _post_out(db, p, with_counts=True),
            "sent": len(reads), "seen": seen_n, "clicked": clicked_n, "recipients": out}


@router.delete("/admin/community/posts/{pid}", dependencies=[Depends(_admin_guard)])
def community_post_delete(pid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Delete for everyone: removes the message from the group thread AND every
    student's bell notification / seen-tracking. Cannot be undone."""
    p = db.query(CommunityPost).filter(CommunityPost.id == pid).first()
    if not p:
        return {"ok": True}
    # 1) remove the per-recipient bell notifications for this post
    try:
        db.query(Notification).filter(Notification.batch_key == ("cp_" + str(pid))).delete(synchronize_session=False)
    except Exception:
        db.rollback()
    # 2) remove read / seen / clicked tracking rows
    try:
        db.query(CommunityRead).filter(CommunityRead.post_id == pid).delete(synchronize_session=False)
    except Exception:
        db.rollback()
    # 3) remove attachment rows
    try:
        db.query(CommunityAttachment).filter(CommunityAttachment.post_id == pid).delete(synchronize_session=False)
    except Exception:
        db.rollback()
    # 4) soft-delete the post so it disappears from every admin + student thread
    p.is_active = False
    db.commit()
    return {"ok": True}


@router.post("/admin/community/posts/{pid}/pin", dependencies=[Depends(_admin_guard)])
def community_post_pin(pid: int, payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    """Pin/unpin a message so it stays at the TOP of the group for everyone."""
    p = db.query(CommunityPost).filter(CommunityPost.id == pid).first()
    if not p:
        raise HTTPException(404, "Not found")
    p.is_pinned = bool(payload.get("pinned", True))
    db.commit()
    return {"ok": True, "pinned": bool(p.is_pinned)}


# =========================================================== STUDENT SIDE
@router.get("/student/community/groups")
def student_community_groups(db: Session = Depends(get_db), me=Depends(get_student)):
    sp = db.query(StudentProfile).filter(StudentProfile.user_id == me.id).first()
    if not sp:
        return {"groups": []}
    my_bids = set(e.batch_id for e in db.query(StudentBatch).filter(StudentBatch.student_id == sp.id).all())
    if getattr(sp, "batch_id", None):
        my_bids.add(sp.batch_id)   # primary link always counts (sales-imported students)
    my_gids = set(m.group_id for m in db.query(CommunityGroupMember)
                  .filter(CommunityGroupMember.user_id == me.id).all())
    my_sess = (getattr(sp, "exam_session", "") or "").strip()
    out = []
    for g in (db.query(CommunityGroup).filter(CommunityGroup.is_active == True)  # noqa: E712
              .order_by(CommunityGroup.id.desc()).all()):
        _in = (g.batch_id and g.batch_id in my_bids) or (g.id in my_gids)
        if not _in and (getattr(g, "source", "") == "session" or getattr(g, "session_key", None)):
            sk = getattr(g, "session_key", None)
            if sk == "__none__":
                _in = (my_sess == "")
            elif sk:
                _in = (my_sess == sk)
        if _in:
            last = (db.query(CommunityPost).filter(CommunityPost.kind == "group",
                                                   CommunityPost.group_id == g.id, CommunityPost.is_active == True)  # noqa: E712
                    .order_by(CommunityPost.id.desc()).first())
            # unread = group posts newer than the student's last-seen read row
            unread = 0
            try:
                seen_ids = set(r.post_id for r in db.query(CommunityRead)
                               .filter(CommunityRead.user_id == me.id, CommunityRead.seen_at.isnot(None)).all())
                for pp in db.query(CommunityPost.id).filter(CommunityPost.kind == "group",
                                                            CommunityPost.group_id == g.id, CommunityPost.is_active == True).all():  # noqa: E712
                    if pp.id not in seen_ids:
                        unread += 1
            except Exception:
                unread = 0
            out.append({"id": g.id, "name": g.name or "", "description": g.description or "",
                        "icon_color": g.icon_color or "",
                        "last": (last.title or last.body or "\U0001F4CE Attachment")[:80] if last else "",
                        "last_at": _ist_str(last.created_at) if last else "", "unread": unread})
    return {"groups": out}


def _student_in_group(db, me, g):
    if not g or not g.is_active:
        return False
    if db.query(CommunityGroupMember).filter(CommunityGroupMember.group_id == g.id,
                                             CommunityGroupMember.user_id == me.id).first():
        return True
    sp = db.query(StudentProfile).filter(StudentProfile.user_id == me.id).first()
    if not sp:
        return False
    if getattr(g, "source", "") == "session" or getattr(g, "session_key", None):
        sk = getattr(g, "session_key", None)
        my = (getattr(sp, "exam_session", "") or "").strip()
        if sk == "__none__":
            return my == ""
        return bool(sk) and my == sk
    if g.batch_id:
        if db.query(StudentBatch).filter(StudentBatch.student_id == sp.id,
                                         StudentBatch.batch_id == g.batch_id).first():
            return True
        if getattr(sp, "batch_id", None) == g.batch_id:
            return True
    return False


@router.get("/student/community/groups/{gid}/posts")
def student_community_group_posts(gid: int, db: Session = Depends(get_db), me=Depends(get_student)):
    g = db.query(CommunityGroup).filter(CommunityGroup.id == gid).first()
    if not g or not _student_in_group(db, me, g):
        raise HTTPException(403, "Not in this group")
    posts = [_post_out(db, p) for p in
             (db.query(CommunityPost).filter(CommunityPost.kind == "group",
                                             CommunityPost.group_id == gid, CommunityPost.is_active == True)  # noqa: E712
              .order_by(CommunityPost.is_pinned.desc(), CommunityPost.id.asc()).all())]
    # mark seen for this student
    _mark_seen(db, me, [p["id"] for p in posts])
    return {"group": {"id": g.id, "name": g.name}, "posts": posts}


def _mark_seen(db, me, post_ids):
    if not post_ids:
        return
    now = datetime.utcnow()
    try:
        rows = db.query(CommunityRead).filter(CommunityRead.user_id == me.id,
                                              CommunityRead.post_id.in_(post_ids)).all()
        have = set()
        for r in rows:
            have.add(r.post_id)
            if r.seen_at is None:
                r.seen_at = now
        for pid in post_ids:
            if pid not in have:
                db.add(CommunityRead(post_id=pid, user_id=me.id, seen_at=now, created_at=now))
        # also mark the matching bell notifications read
        for n in db.query(Notification).filter(Notification.user_id == me.id,
                                               Notification.batch_key.in_(["cp_" + str(p) for p in post_ids]),
                                               Notification.is_read == False).all():  # noqa: E712
            n.is_read = True
            n.read_at = now
        db.commit()
    except Exception:
        db.rollback()


@router.get("/student/community/popups")
def student_community_popups(db: Session = Depends(get_db), me=Depends(get_student)):
    """Unseen popups targeted at this student (shown once on portal open)."""
    out = []
    try:
        seen_ids = set(r.post_id for r in db.query(CommunityRead)
                       .filter(CommunityRead.user_id == me.id, CommunityRead.seen_at.isnot(None)).all())
        my_ids = set(r.post_id for r in db.query(CommunityRead)
                     .filter(CommunityRead.user_id == me.id).all())
        pend = [pid for pid in my_ids if pid not in seen_ids]
        if pend:
            for p in (db.query(CommunityPost).filter(CommunityPost.kind == "popup",
                                                     CommunityPost.id.in_(list(pend)), CommunityPost.is_active == True)  # noqa: E712
                      .order_by(CommunityPost.id.desc()).limit(5).all()):
                out.append(_post_out(db, p))
    except Exception:
        pass
    return {"popups": out}


@router.post("/student/community/posts/{pid}/seen")
def student_community_seen(pid: int, db: Session = Depends(get_db), me=Depends(get_student)):
    _mark_seen(db, me, [pid])
    return {"ok": True}


@router.post("/student/community/posts/{pid}/click")
def student_community_click(pid: int, db: Session = Depends(get_db), me=Depends(get_student)):
    now = datetime.utcnow()
    try:
        r = db.query(CommunityRead).filter(CommunityRead.post_id == pid,
                                           CommunityRead.user_id == me.id).first()
        if r:
            if r.seen_at is None:
                r.seen_at = now
            r.clicked_at = now
        else:
            db.add(CommunityRead(post_id=pid, user_id=me.id, seen_at=now, clicked_at=now, created_at=now))
        db.commit()
    except Exception:
        db.rollback()
    return {"ok": True}


# =========================================================== FILE SERVE (any logged-in user)
@router.get("/community-file")
def community_file(id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    a = db.query(CommunityAttachment).filter(CommunityAttachment.id == id).first()
    if not a or not a.url:
        raise HTTPException(404, "Not found")
    mime = a.mime or ("application/pdf" if (a.kind == "pdf") else "image/jpeg")
    download = (a.kind != "image")
    try:
        return r2.proxy_response(a.url, mime, a.name or None, download, sniff=True)
    except Exception:
        raise HTTPException(404, "File unavailable")
