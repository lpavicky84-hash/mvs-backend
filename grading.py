"""
Auto-grading + AI helpers for the Exam/Test feature.
 - MCQ:        instant, compares selected option to correct_option.
 - Subjective: reads the student's HANDWRITTEN uploaded image with Gemini Vision.
 - Also: screenshot OCR, paste auto-format, Word-doc structuring.
No external pip deps (uses urllib). Needs env var GEMINI_API_KEY on the server.

NOTE: Gemini 2.0 Flash was shut down on 2026-06-01 (returns 404). Default model is
now gemini-3.5-flash (GA) with a fallback chain so grading keeps working.
"""
import os, json, re, time, urllib.request, urllib.error

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")

_DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_MODELS = []
for _m in [_DEFAULT_MODEL, "gemini-3.5-flash", "gemini-2.5-flash", "gemini-flash-latest"]:
    if _m and _m not in GEMINI_MODELS:
        GEMINI_MODELS.append(_m)

LAST_ERROR = ""


def _gemini_url(model):
    return "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model


# HTTP codes worth retrying (transient: overload / rate-limit / server hiccup).
_RETRY_CODES = (429, 500, 502, 503)
_MAX_TRIES = 3


def _gemini_generate(parts, max_tokens=4096, timeout=90):
    """Try each model in GEMINI_MODELS until one responds, retrying transient
    errors (high demand / rate limit) with backoff. Returns text or None.
    Records the failure reason in grading.LAST_ERROR."""
    global LAST_ERROR
    if not GEMINI_KEY:
        LAST_ERROR = "GEMINI_API_KEY is not set on the server"
        return None
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens}}
    data = json.dumps(body).encode("utf-8")
    last = ""
    for model in GEMINI_MODELS:
        for attempt in range(_MAX_TRIES):
            try:
                req = urllib.request.Request(
                    _gemini_url(model) + "?key=" + GEMINI_KEY,
                    data=data, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    resp = json.loads(r.read().decode("utf-8"))
                LAST_ERROR = ""
                return resp["candidates"][0]["content"]["parts"][0]["text"].strip()
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8")[:200]
                except Exception:
                    detail = "HTTP %s" % e.code
                last = "%s -> %s" % (model, detail)
                if e.code in _RETRY_CODES and attempt < _MAX_TRIES - 1:
                    time.sleep(1.5 * (attempt + 1))   # 1.5s, 3s backoff
                    continue
                break  # non-retryable or out of tries -> next model
            except Exception as e:
                last = "%s -> %s" % (model, e)
                if attempt < _MAX_TRIES - 1:
                    time.sleep(1.0)
                    continue
                break
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


def _split_image(image_b64, fallback_mime="image/jpeg"):
    """Return (clean_base64, mime). Auto-detects mime from a data URL prefix so we
    never send PNG/PDF bytes labelled as JPEG (which Gemini rejects)."""
    if image_b64 and image_b64.startswith("data:") and "," in image_b64:
        header, b = image_b64.split(",", 1)
        try:
            mime = header.split(":", 1)[1].split(";", 1)[0]
        except Exception:
            mime = fallback_mime
        return b.strip(), (mime or fallback_mime)
    return (image_b64 or "").strip(), fallback_mime


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
    img, mime = _split_image(image_b64, mime_type or "image/jpeg")
    out = _gemini_generate([{"text": prompt},
                            {"inline_data": {"mime_type": mime, "data": img}}])
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

# Shared instruction: force the model to emit REAL line breaks between structural
# parts instead of one run-on paragraph. This is what makes the textarea preview
# and the exported PDF display cleanly, one logical line at a time - matching how
# a student would see it on a properly formatted worksheet.
_STRUCT_RULES = (
    "Statement: <the question statement> ; then a blank heading 'Given Data:' on its "
    "OWN line ; then EACH given value on its OWN line as '- item' ; then 'Step 1:' "
    "(short title) on its OWN line, its explanation sentence on its OWN line, then the "
    "equation ALONE on its OWN line, then 'Substitute the values:' on its OWN line "
    "with the substituted equation ALONE on its OWN line ; repeat Step 2, Step 3 etc. "
    "the same way ; then 'Final Answer:' on its OWN line with each result as '- item' "
    "on its OWN line. Never write two sentences, a heading plus its content, or two "
    "bullet items on the same line. Never merge two words together without a space."
)
_STRUCT_NOTE = (
    "FORMAT WITH REAL LINE BREAKS (very important): insert an actual newline character "
    "between every distinct part so it reads cleanly line-by-line, in this structure - " +
    _STRUCT_RULES + " "
)
_STRUCT_NOTE_JSON = (
    "FORMAT WITH LINE BREAKS INSIDE THE JSON STRING (very important): within each JSON "
    "string value, separate distinct parts using an escaped newline \\n (backslash-n, "
    "valid inside a JSON string) so the text displays cleanly line-by-line, in this "
    "structure - " + _STRUCT_RULES + " "
)


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
              _STRUCT_NOTE_JSON +
              "Return ONLY valid JSON, no markdown fences: " + schema)
    img, mime = _split_image(image_b64, mime_type or "image/jpeg")
    out = _gemini_generate([{"text": prompt},
                            {"inline_data": {"mime_type": mime, "data": img}}])
    if not out:
        return None
    try:
        return json.loads(_strip_json(out))
    except Exception:
        return None


def format_text_latex(text):
    if not GEMINI_KEY or not (text or "").strip():
        return None
    prompt = ("Reformat the following exam text for clean display. " + _FMT_NOTE + _LANG_NOTE + _STRUCT_NOTE +
              "Keep the wording identical - only add formatting (including line breaks) where needed. "
              "Return ONLY the reformatted text, nothing else - no markdown fences.\n\nTEXT:\n" + text)
    return _gemini_generate([{"text": prompt}])


def _parse_json_array(text):
    """Parse a JSON array from AI output. Salvages the common failure modes:
    markdown fences, prose around the array, trailing commas, and a TRUNCATED
    array (output hit the token limit) - in that case the complete leading
    items are recovered instead of losing everything."""
    t = _strip_json(text)
    i = t.find("[")
    if i < 0:
        return None
    t = t[i:]
    j = t.rfind("]")
    if j >= 0:
        cand = re.sub(r",\s*([\]}])", r"\1", t[:j + 1])
        try:
            data = json.loads(cand)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    # truncated: cut back to the last complete object and close the array
    k = t.rfind("}")
    while k > 0:
        cand = re.sub(r",\s*([\]}])", r"\1", t[:k + 1].rstrip().rstrip(",") + "]")
        try:
            data = json.loads(cand)
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
        k = t.rfind("}", 0, k)
    return None


_QSTART_RE = re.compile(
    r"(?m)^\s*(?:Q\.?\s*(?:No\.?\s*)?)?(\d{1,3})\s*[\.\)]\s+\S|^\s*Q\s*(\d{1,3})\b")


def _chunk_exam_text(full_text, budget=9000):
    """Split a long question paper into chunks small enough that each chunk's
    JSON output fits comfortably in the model's output-token limit. Splits at
    question boundaries so no question straddles two chunks; falls back to
    overlapping fixed-size windows if no question markers are found."""
    text = full_text or ""
    if len(text) <= budget:
        return [text]
    starts = [m.start() for m in _QSTART_RE.finditer(text)]
    if len(starts) >= 3:
        pieces, chunk_start = [], 0
        prev = starts[0]
        for pos in starts[1:] + [len(text)]:
            if pos - chunk_start > budget and prev > chunk_start:
                pieces.append(text[chunk_start:prev])
                chunk_start = prev
            prev = pos
        pieces.append(text[chunk_start:])
    else:
        pieces, i = [], 0
        while i < len(text):
            pieces.append(text[i:i + budget])
            i += budget - 600
    # a piece can still be oversize (e.g. dense marking-scheme tables) - window it
    out = []
    for p in pieces:
        if len(p) <= budget * 1.4:
            if p.strip():
                out.append(p)
        else:
            i = 0
            while i < len(p):
                out.append(p[i:i + budget])
                i += budget - 600
    return out


_KEY_SPLIT_RE = re.compile(
    r"(?i)marking\s+scheme|answer\s+key|\u0909\u0924\u094d\u0924\u0930\s*"
    r"\u0915\u0941\u0902\u091c\u0940|\u0905\u0902\u0915\u0928\s*\u092f\u094b\u091c\u0928\u093e")


def _split_answer_key(full_text):
    """If the paper carries its marking scheme / answer key after the questions,
    split it off so questions and answers can be handled in two passes (chunked
    single-pass processing would otherwise separate a question from its answer)."""
    text = full_text or ""
    m = _KEY_SPLIT_RE.search(text, int(len(text) * 0.30))
    if m:
        return text[:m.start()], text[m.start():]
    return text, ""


def _fill_answers_from_key(questions, key_text):
    """Second pass: read the marking-scheme text and fill model_answer for the
    already-extracted questions by question number. Best-effort - on any failure
    the questions are returned as-is."""
    try:
        for chunk in _chunk_exam_text(key_text):
            prompt = (
                "Below is the MARKING SCHEME / ANSWER KEY portion of an exam paper. For every "
                "question number answered in it, produce its model answer. " + _LANG_NOTE +
                _FMT_NOTE + _STRUCT_NOTE_JSON +
                "For MCQ answers, write the correct option letter and text plus the brief "
                "explanation if given. Return ONLY a JSON array: "
                '[{"q_no": 1, "model_answer": "..."}]\n\nMARKING SCHEME TEXT:\n' + chunk)
            out = _gemini_generate([{"text": prompt}], max_tokens=8192, timeout=150)
            for item in (_parse_json_array(out) or []):
                try:
                    qn = int(item.get("q_no"))
                except Exception:
                    continue
                ans = (item.get("model_answer") or "").strip()
                if 1 <= qn <= len(questions) and ans:
                    cur = (questions[qn - 1].get("model_answer") or "").strip()
                    if not cur or len(ans) > len(cur):
                        questions[qn - 1]["model_answer"] = ans
    except Exception:
        pass
    return questions


_MIXED_NOTE = (
    "The document may be a MIXED question paper containing several sections: multiple-choice "
    "questions, objective questions (fill in the blanks, match the following, true/false, "
    "passage-based sub-questions), short-answer and long-answer questions, and it may also "
    "contain a marking scheme / answer key at the end. Handle it as follows: "
    "(1) Extract EVERY question in order, whatever its type. "
    "(2) For an MCQ or objective question, include its options / blanks / match-columns as "
    "separate lines INSIDE the question text (e.g. '(A) ...' on its own line), and put the "
    "correct answer (with a brief one-line working or reason if numerical/conceptual) in "
    "model_answer. "
    "(3) If the document includes an answer key or marking scheme, USE it to fill model_answer "
    "for the matching questions - and do NOT output the marking-scheme entries as separate questions. "
    "(4) A question with an internal choice ('OR' / 'अथवा') is ONE question - keep both "
    "alternatives inside the same question text separated by a line 'OR'. "
    "(5) Skip page headers/footers, section headings, and general instructions - they are not questions. "
    "(6) Take max_marks from markers like [1], (2), '1 X 2', 'carrying 3 marks each'. "
    "(7) If some text is garbled/mojibake (broken font encoding, e.g. random symbols where Hindi "
    "should be), IGNORE the unreadable parts and reconstruct the question from the readable text - "
    "never copy garbage characters into the output. ")


def _structure_chunk(chunk_text, test_type, part_note):
    if test_type == "mcq":
        schema = ('[{"question":"...","options":["a","b","c","d"],'
                  '"correct_option":"exact correct option text","max_marks":1}]')
        extra = "Each item is a multiple-choice question with its options and correct option. "
    else:
        schema = '[{"question":"...","model_answer":"...","max_marks":5}]'
        extra = ("Each item has the full question text and a model answer. " + _MIXED_NOTE)
    prompt = ("Below is text extracted from an exam question paper (PDF/Word). Split it into a "
              "list of questions. " + part_note + extra + _LANG_NOTE + _FMT_NOTE + _STRUCT_NOTE_JSON +
              "Infer max_marks if written, else use a sensible default. "
              "Return ONLY a JSON array: " + schema + "\n\nDOCUMENT TEXT:\n" + chunk_text)
    out = _gemini_generate([{"text": prompt}], max_tokens=8192, timeout=150)
    if not out:
        return None
    return _parse_json_array(out)


def structure_docx_questions(full_text, test_type="subjective"):
    """Structure a question paper (possibly long, possibly mixed-type) into a
    question list. Long papers are processed in question-aligned chunks so the
    JSON reply of each call stays within the model's output limit - this is what
    used to silently fail on big papers (truncated JSON -> parse error)."""
    global LAST_ERROR
    if not GEMINI_KEY:
        LAST_ERROR = "GEMINI_API_KEY is not set on the server"
        return None
    if not (full_text or "").strip():
        LAST_ERROR = "No text to structure"
        return None
    qtext, key_text = _split_answer_key(full_text) if test_type != "mcq" else (full_text, "")
    chunks = _chunk_exam_text(qtext)
    merged, ok_chunks = [], 0
    for ci, chunk in enumerate(chunks):
        note = ("This is PART %d of %d of the same paper - extract only the questions "
                "visible in this part. " % (ci + 1, len(chunks))) if len(chunks) > 1 else ""
        qs = _structure_chunk(chunk, test_type, note)
        if qs:
            merged.extend(q for q in qs if isinstance(q, dict) and (q.get("question") or "").strip())
            ok_chunks += 1
    if not merged:
        if not LAST_ERROR:
            LAST_ERROR = "AI reply could not be parsed as a question list"
        return None
    if key_text.strip():
        merged = _fill_answers_from_key(merged, key_text)
    if ok_chunks < len(chunks):
        LAST_ERROR = "partial: %d of %d parts imported" % (ok_chunks, len(chunks))
    else:
        LAST_ERROR = ""
    return merged


def _subject_hint(subject):
    """Return NIOS Hindi-medium terminology guidance for the given subject so the
    translation uses the words students actually see in their Hindi textbooks."""
    s = (subject or "").lower()
    hints = {
        ("physics", "भौतिक"): "Physics (भौतिक विज्ञान): use standard NIOS Hindi terms e.g. force=बल, velocity=वेग, acceleration=त्वरण, momentum=संवेग, energy=ऊर्जा, work=कार्य, power=शक्ति, mass=द्रव्यमान, displacement=विस्थापन, dipole moment=द्विध्रुव आघूर्ण, charge=आवेश, field=क्षेत्र.",
        ("chemistry", "रसायन"): "Chemistry (रसायन विज्ञान): reaction=अभिक्रिया, compound=यौगिक, element=तत्व, bond=आबंध, mole=मोल, oxidation=ऑक्सीकरण, reduction=अपचयन, acid=अम्ल, base=क्षार, salt=लवण. Keep chemical formulae and element symbols in English.",
        ("bio", "जीव"): "Biology (जीव विज्ञान): cell=कोशिका, tissue=ऊतक, gene=जीन, respiration=श्वसन, photosynthesis=प्रकाश संश्लेषण, enzyme=एंजाइम, organism=जीव. Keep scientific (Latin) names in English.",
        ("math", "गणित"): "Mathematics (गणित): equation=समीकरण, function=फलन, derivative=अवकलज, integral=समाकल, probability=प्रायिकता, matrix=आव्यूह, triangle=त्रिभुज, ratio=अनुपात. Keep all symbols/numbers in English.",
        ("history", "इतिहास"): "History (इतिहास): use standard Hindi historical terms; keep proper nouns (people, dynasties, places, treaties) as commonly written in Hindi. revolution=क्रांति, empire=साम्राज्य, civilization=सभ्यता, movement=आंदोलन.",
        ("geograph", "भूगोल"): "Geography (भूगोल): climate=जलवायु, river=नदी, plateau=पठार, latitude=अक्षांश, longitude=देशांतर, monsoon=मानसून, plain=मैदान.",
        ("politic", "राजन"): "Political Science (राजनीति विज्ञान): democracy=लोकतंत्र, constitution=संविधान, fundamental rights=मौलिक अधिकार, parliament=संसद, federalism=संघवाद.",
        ("econom", "अर्थ"): "Economics (अर्थशास्त्र): demand=मांग, supply=पूर्ति, market=बाजार, inflation=मुद्रास्फीति, poverty=गरीबी. Keep GDP, GNP as English abbreviations.",
        ("account", "लेखा"): "Accountancy (लेखाशास्त्र): debit=नामे, credit=जमा, ledger=खाताबही, balance sheet=तुलन पत्र, journal=रोजनामचा, capital=पूंजी.",
        ("business",): "Business Studies (व्यवसाय अध्ययन): management=प्रबंधन, marketing=विपणन, partnership=साझेदारी, organisation=संगठन.",
        ("home science", "गृह विज्ञान"): "Home Science (गृह विज्ञान): nutrition=पोषण, vitamin=विटामिन, hygiene=स्वच्छता, balanced diet=संतुलित आहार.",
        ("psycholog", "मनोविज्ञान"): "Psychology (मनोविज्ञान): behaviour=व्यवहार, memory=स्मृति, perception=प्रत्यक्ष, motivation=अभिप्रेरणा, learning=अधिगम.",
        ("computer", "data entry", "कंप्यू"): "Computer/Data Entry: keep technical computing terms (software, hardware, keyboard, file, database, spreadsheet, operating system) in English; translate only the surrounding explanation into simple Hindi.",
        ("english", "hindi", "language"): "Language subject: translate naturally; keep grammar terms standard.",
    }
    for keys, hint in hints.items():
        if any(k in s for k in keys):
            return hint
    return ("Use standard NIOS Hindi-medium textbook terminology for this subject; keep "
            "technical terms exactly as they are commonly written in Hindi textbooks.")


def translate_question_to_hindi(question_text, model_answer="", options=None, subject=""):
    """Translate an exam question (question + model answer + mcq options) into accurate,
    subject-aware Hindi for NIOS bilingual tests. Keeps LaTeX/chemistry/numbers/units
    intact and uses the correct Hindi technical terminology for the subject.
    Returns {"question","answer","options":[...]} or None."""
    options = options or []
    payload = {
        "question": question_text or "",
        "answer": model_answer or "",
        "options": [str(o) for o in options],
    }
    prompt = (
        "You are an expert NIOS (National Institute of Open Schooling) Hindi-medium "
        "teacher and translator for the subject '" + (subject or "this subject") + "'. "
        "Translate the exam content from English into accurate, natural, exam-appropriate "
        "Hindi (Devanagari), exactly as it would appear in an official NIOS Hindi-medium "
        "textbook or question paper.\n\n"
        "SUBJECT TERMINOLOGY GUIDE:\n" + _subject_hint(subject) + "\n\n"
        "STRICT RULES:\n"
        "1. Use the correct, standard Hindi technical term for every concept in THIS "
        "subject. Do NOT translate literally word-by-word - translate the MEANING using "
        "the terminology a NIOS Hindi-medium student expects. Avoid wrong or awkward "
        "coined words.\n"
        "2. Keep the following EXACTLY unchanged (never translate or alter): LaTeX "
        "($...$, \\frac{}{}, \\sqrt{}, ^, _), \\ce{...} chemistry, chemical formulae and "
        "element symbols, equations, numbers, units (m/s, kg, mol, N, J, ...), single "
        "variable letters, and proper nouns / names.\n"
        "3. Where a technical English term is universally used by Hindi-medium students "
        "(e.g. GDP, DNA, software), keep it in English inside the Hindi sentence instead "
        "of forcing a rare Hindi word.\n"
        "4. Translate structural headings to their standard Hindi equivalents: "
        "'Given Data:' -> 'दिया गया:', 'Step 1:' -> 'चरण 1:', 'Final Answer:' -> "
        "'अंतिम उत्तर:', 'Solution:' -> 'हल:', 'Formula:' -> 'सूत्र:'.\n"
        "5. Do NOT add, remove, explain, or solve anything. Translate only.\n"
        "6. Keep the SAME number of options in the SAME order.\n"
        "7. " + _STRUCT_NOTE_JSON + "If the source 'answer' text already has line breaks "
        "between its parts, KEEP the exact same line breaks in the same places in the "
        "Hindi translation - do not merge lines together.\n"
        "8. Return ONLY valid JSON with the SAME keys (no markdown, no commentary):\n"
        '{"question": "...", "answer": "...", "options": ["..."]}\n\n'
        "Translate this JSON:\n" + json.dumps(payload, ensure_ascii=False)
    )
    out = _gemini_generate([{"text": prompt}])
    if not out:
        return None
    try:
        data = json.loads(_strip_json(out))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "question": (data.get("question") or "").strip(),
        "answer": (data.get("answer") or "").strip(),
        "options": [str(o).strip() for o in (data.get("options") or [])],
    }
