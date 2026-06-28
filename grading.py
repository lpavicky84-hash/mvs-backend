"""
Auto-grading + AI helpers for the Exam/Test feature.
 - MCQ:        instant, compares selected option to correct_option.
 - Subjective: reads the student's HANDWRITTEN uploaded image with Gemini Vision.
 - Also: screenshot OCR, paste auto-format, Word-doc structuring.
No external pip deps (uses urllib). Needs env var GEMINI_API_KEY on the server.

NOTE: Gemini 2.0 Flash was shut down on 2026-06-01 (returns 404). Default model is
now gemini-3.5-flash (GA) with a fallback chain so grading keeps working.
"""
import os, json, urllib.request, urllib.error

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")

_DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_MODELS = []
for _m in [_DEFAULT_MODEL, "gemini-3.5-flash", "gemini-2.5-flash", "gemini-flash-latest"]:
    if _m and _m not in GEMINI_MODELS:
        GEMINI_MODELS.append(_m)

LAST_ERROR = ""


def _gemini_url(model):
    return "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model


def _gemini_generate(parts):
    """Try each model in GEMINI_MODELS until one responds. Returns text or None.
    Records the failure reason in grading.LAST_ERROR."""
    global LAST_ERROR
    if not GEMINI_KEY:
        LAST_ERROR = "GEMINI_API_KEY is not set on the server"
        return None
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096}}
    data = json.dumps(body).encode("utf-8")
    last = ""
    for model in GEMINI_MODELS:
        try:
            req = urllib.request.Request(
                _gemini_url(model) + "?key=" + GEMINI_KEY,
                data=data, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.loads(r.read().decode("utf-8"))
            LAST_ERROR = ""
            return resp["candidates"][0]["content"]["parts"][0]["text"].strip()
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")[:200]
            except Exception:
                detail = "HTTP %s" % e.code
            last = "%s -> %s" % (model, detail)
            continue
        except Exception as e:
            last = "%s -> %s" % (model, e)
            continue
    LAST_ERROR = last or "All Gemini models failed"
    return None


def _strip_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    return text


def grade_mcq(questions, mcq_answers):
    ans = mcq_answers or {}
    results, total = [], 0.0
    for q in questions:
        sel = ans.get(str(q.q_no), ans.get(q.q_no))
        correct = (q.correct_option or "").strip()
        ok = sel is not None and str(sel).strip().lower() == correct.lower()
        awarded = float(q.max_marks) if ok else 0.0
        total += awarded
        results.append({
            "q_no": q.q_no, "marks": awarded, "max": q.max_marks,
            "remark": "Correct" if ok else ("Incorrect. Correct answer: %s" % correct),
        })
    return results, total


def grade_subjective(questions, image_b64, mime_type="image/jpeg"):
    if not GEMINI_KEY:
        return None, 0.0, "GEMINI_API_KEY is not set", ""
    if not image_b64:
        return None, 0.0, "no_image", ""
    qlist = "\n".join(
        "Q%d (max %d marks): %s\nModel answer: %s"
        % (q.q_no, q.max_marks, q.question_text or "", (q.model_answer or "N/A"))
        for q in questions
    )
    prompt = (
        "You are a strict but fair exam evaluator. The attached image is a student's "
        "HANDWRITTEN answer sheet. Read the handwriting carefully, match each answer to "
        "the correct question number, and grade it against the model answer.\n"
        "For EACH question: award marks from 0 to its max (half marks allowed) and write a "
        "short remark (what was correct, what was missing or wrong). Be encouraging.\n"
        "The answers may be in Hindi (Devanagari), English, or a mix - grade in whichever "
        "language the student used; do not penalise the language choice.\n"
        "After all questions, write overall 'feedback' (2 short sentences addressed to the "
        "student) and a 'verdict' that is exactly one of: Excellent, Good, Needs Improvement.\n\n"
        "QUESTIONS:\n" + qlist + "\n\n"
        'Return ONLY valid JSON, no markdown fences, in exactly this shape: '
        '{"results":[{"q_no":1,"marks":3.5,"remark":"..."}],"feedback":"...","verdict":"Good"}'
    )
    img = image_b64.split(",")[-1] if "," in image_b64 else image_b64
    out = _gemini_generate([{"text": prompt},
                            {"inline_data": {"mime_type": mime_type or "image/jpeg", "data": img}}])
    if not out:
        return None, 0.0, (LAST_ERROR or "api_error"), ""
    try:
        data = json.loads(_strip_json(out))
    except Exception:
        return None, 0.0, "Could not parse AI response", ""
    rmap = {}
    for x in data.get("results", []):
        try:
            rmap[int(x.get("q_no"))] = x
        except Exception:
            pass
    results, total = [], 0.0
    for q in questions:
        x = rmap.get(q.q_no, {})
        try:
            mk = float(x.get("marks", 0) or 0)
        except Exception:
            mk = 0.0
        mk = max(0.0, min(mk, float(q.max_marks)))
        total += mk
        results.append({"q_no": q.q_no, "marks": mk, "max": q.max_marks,
                        "remark": x.get("remark", "")})
    return results, total, data.get("feedback", ""), (data.get("verdict", "") or "")


_LANG_NOTE = ("Preserve the original language EXACTLY (it may be Hindi in Devanagari, English, "
              "or a mix of both) - do NOT translate or change the medium. ")
_FMT_NOTE = ("Convert any mathematics to LaTeX wrapped in $...$ and any chemistry formula or reaction "
             "to \\ce{...} wrapped in $...$ (e.g. $\\ce{2H2 + O2 -> 2H2O}$). ")


def ocr_extract_question(image_b64, test_type="subjective", mime_type="image/jpeg"):
    if not GEMINI_KEY or not image_b64:
        return None
    if test_type == "mcq":
        schema = ('{"question":"...","options":["opt1","opt2","opt3","opt4"],'
                  '"correct_option":"exact text of the correct option, or empty if not shown"}')
        extra = "Extract the multiple-choice question, all answer options, and the correct option if indicated. "
    else:
        schema = '{"question":"...","model_answer":"the full answer or solution if present, else empty"}'
        extra = "Extract the question text and its answer/solution if present. "
    prompt = ("You are reading a screenshot of an exam question. " + extra + _LANG_NOTE + _FMT_NOTE +
              "Return ONLY valid JSON, no markdown fences: " + schema)
    img = image_b64.split(",")[-1] if "," in image_b64 else image_b64
    out = _gemini_generate([{"text": prompt},
                            {"inline_data": {"mime_type": mime_type or "image/jpeg", "data": img}}])
    if not out:
        return None
    try:
        return json.loads(_strip_json(out))
    except Exception:
        return None


def format_text_latex(text):
    if not GEMINI_KEY or not (text or "").strip():
        return None
    prompt = ("Reformat the following exam text for clean display. " + _FMT_NOTE + _LANG_NOTE +
              "Keep the wording identical - only add formatting where needed. "
              "Return ONLY the reformatted text, nothing else.\n\nTEXT:\n" + text)
    return _gemini_generate([{"text": prompt}])


def structure_docx_questions(full_text, test_type="subjective"):
    if not GEMINI_KEY or not (full_text or "").strip():
        return None
    if test_type == "mcq":
        schema = ('[{"question":"...","options":["a","b","c","d"],'
                  '"correct_option":"exact correct option text","max_marks":1}]')
        extra = "Each item is a multiple-choice question with its options and correct option. "
    else:
        schema = '[{"question":"...","model_answer":"...","max_marks":5}]'
        extra = "Each item is a subjective question with its model answer. "
    prompt = ("Below is text from a Word document containing exam questions. Split it into a list of "
              "questions. " + extra + _LANG_NOTE + _FMT_NOTE +
              "Infer max_marks if written, else use a sensible default. "
              "Return ONLY a JSON array: " + schema + "\n\nDOCUMENT TEXT:\n" + full_text)
    out = _gemini_generate([{"text": prompt}])
    if not out:
        return None
    try:
        data = json.loads(_strip_json(out))
        return data if isinstance(data, list) else None
    except Exception:
        return None
