"""
APEX Resume Intelligence Engine
Single-file app: Python (Flask) + HTML + CSS + JS + SQLite
Run: python apex_resume.py
Then open: http://localhost:5000
"""

import os, re, json, sqlite3, threading, time, hashlib
from pathlib import Path
from flask import Flask, request, jsonify, g

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# ─── Config ───────────────────────────────────────────────────────────────────
DB_PATH   = "apex_resumes.db"
API_KEY   = os.environ.get("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", "")   # set env var or paste below
API_KEY = "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"                           # ← paste your key here
MODEL     = "claude-sonnet-4-20250514"
app       = Flask(__name__)

# ─── Database ─────────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS resumes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash   TEXT UNIQUE,
                file_name   TEXT,
                raw_text    TEXT,
                analysis    TEXT,
                verdict     TEXT,
                overall_score INTEGER,
                hire_readiness TEXT,
                ats_score   INTEGER,
                auth_score  INTEGER,
                candidate_name TEXT,
                candidate_email TEXT,
                experience_years REAL,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        db.commit()

# ─── Heuristic Analysis ───────────────────────────────────────────────────────
TECH_SKILLS = [
    "python","java","javascript","typescript","react","angular","vue","node.js","sql",
    "aws","azure","gcp","docker","kubernetes","tensorflow","pytorch","machine learning",
    "data analysis","excel","tableau","power bi","agile","scrum","ci/cd","git","linux",
    "rest api","mongodb","postgresql","redis","c++","c#",".net","html","css","fastapi",
    "flask","django","spring","graphql","kafka","spark","hadoop","airflow","dbt",
]
SOFT_SKILLS = {"communication","leadership","problem solving","teamwork","agile","scrum"}
EMAIL_RE    = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
PHONE_RE    = re.compile(r"\+?[\d\s\-().]{10,}")
LINKEDIN_RE = re.compile(r"linkedin\.com/in/[\w-]+", re.I)
GITHUB_RE   = re.compile(r"github\.com/[\w-]+", re.I)

FAKE_PATTERNS = [
    (r"\b(expert|guru|ninja|rockstar)\b.*\b(everything|all)\b", "Inflated generic expert claims"),
    (r"responsible for everything",                              "Unrealistically broad responsibility"),
    (r"\b\d{3,}%\b",                                            "Very high percentage claims — verify"),
]

def _extract_name(text):
    for line in text.strip().splitlines()[:6]:
        line = line.strip()
        if 2 < len(line) < 50 and not EMAIL_RE.search(line) and not PHONE_RE.search(line):
            if re.match(r"^[A-Za-z][A-Za-z\s.\'-]+$", line):
                return line.title()
    return "Unknown Candidate"

def _estimate_years(text):
    years = re.findall(r"(20\d{2})", text)
    if len(years) >= 2:
        try:
            return max(0, max(int(y) for y in years) - min(int(y) for y in years))
        except ValueError:
            pass
    m = re.search(r"(\d+)\+?\s*years?", text, re.I)
    return float(m.group(1)) if m else None

def heuristic_analyze(text, job_desc=""):
    lower = text.lower()

    # Skills
    tech  = sorted(set(s.title() for s in TECH_SKILLS if s in lower and s not in SOFT_SKILLS))
    soft  = sorted(set(s.title() for s in TECH_SKILLS if s in lower and s in SOFT_SKILLS))

    # Sections
    sections = ["experience","education","skills","summary","projects","certifications"]
    sec_hits  = [s for s in sections if s in lower]
    sec_score = int(len(sec_hits) / len(sections) * 100)

    # ATS
    kw_density = min(100, len(tech) * 7)
    readability = min(100, max(40, 120 - len(text) // 80))
    ats_score   = int(kw_density * 0.45 + sec_score * 0.35 + readability * 0.2)
    ats_grade   = "A+" if ats_score>=90 else "A" if ats_score>=80 else "B+" if ats_score>=70 else "B" if ats_score>=60 else "C" if ats_score>=50 else "D"

    # Fake detection
    red_flags, green_flags = [], []
    for pat, msg in FAKE_PATTERNS:
        if re.search(pat, text, re.I):
            red_flags.append(msg)
    vague = len(re.findall(r"\b(responsible for|worked on|helped with)\b", text, re.I))
    if vague > 8:  red_flags.append("Many vague duty phrases without outcomes")
    if len(text) < 400: red_flags.append("Resume unusually short")
    if EMAIL_RE.search(text):   green_flags.append("Valid email contact present")
    if re.search(r"\b(increased|reduced|improved|saved|delivered|achieved)\b", text, re.I):
        green_flags.append("Uses outcome-oriented action verbs")
    if re.search(r"\d+%|\$\d|#\d", text): green_flags.append("Contains quantified achievements")
    auth_score = max(15, min(95, 72 - len(red_flags)*12 + len(green_flags)*8))
    risk = "low" if auth_score>=75 else "medium" if auth_score>=55 else "high" if auth_score>=35 else "very_high"
    verdict = "ORIGINAL" if auth_score >= 60 else "SUSPICIOUS" if auth_score >= 40 else "FAKE"

    # Weaknesses
    critical, moderate = [], []
    if ats_score < 50:  critical.append("Low ATS compatibility — likely filtered automatically")
    if sec_score < 50:  moderate.append("Missing standard resume sections")
    if len(text) < 600: moderate.append("Resume content too thin — expand impact bullets")
    if not EMAIL_RE.search(text): critical.append("No email address found on resume")

    # Skill gap
    job_kw = []
    if job_desc.strip():
        words = re.findall(r"[a-zA-Z+#.]{2,}", job_desc.lower())
        stop  = {"the","and","for","with","you","will","our","are","this","that","from","have"}
        freq  = {}
        for w in words:
            if w not in stop and len(w) >= 3:
                freq[w] = freq.get(w, 0) + 1
        job_kw = sorted(freq, key=freq.get, reverse=True)[:40]

    resume_set = {s.lower() for s in tech + soft}
    if job_kw:
        matched  = [k for k in job_kw if k in lower or k in resume_set]
        missing  = [k.title() for k in job_kw[:12] if k not in lower and k not in resume_set][:8]
        match_pct = int(len(matched) / max(len(job_kw[:15]), 1) * 100)
    else:
        matched  = list(resume_set)[:12]
        missing  = []
        match_pct = min(90, 50 + len(tech)*4)

    severity = "critical" if match_pct<40 else "moderate" if match_pct<65 else "minor" if match_pct<85 else "none"

    # Ranking
    years = _estimate_years(text)
    overall = int((ats_score + auth_score + match_pct + sec_score) / 4)
    readiness = "strong" if overall>=80 else "ready" if overall>=65 else "developing" if overall>=45 else "not_ready"

    return {
        "parser": "apex-heuristic-v3",
        "personal_info": {
            "name":     _extract_name(text),
            "email":    (EMAIL_RE.search(text) or type("",(), {"group":lambda s,x:None})()).group(0),
            "phone":    (PHONE_RE.search(text) or type("",(), {"group":lambda s,x:None})()).group(0),
            "location": None,
            "linkedin": (LINKEDIN_RE.search(text) or type("",(), {"group":lambda s,x:None})()).group(0),
            "github":   (GITHUB_RE.search(text)   or type("",(), {"group":lambda s,x:None})()).group(0),
        },
        "summary": (text.strip().split("\n\n")[0][:400] if text.strip() else None),
        "total_experience_years": years,
        "skills": {"technical": tech, "soft": soft, "tools": [], "domains": []},
        "experience": [], "education": [], "certifications": [], "projects": [],
        "ats_analysis": {
            "ats_score": ats_score, "grade": ats_grade,
            "keyword_density": kw_density, "format_score": sec_score,
            "readability_score": readability,
            "ats_issues":        [f"Missing clear {s} section" for s in sections if s not in sec_hits][:5],
            "ats_passed_checks": [f"Contains {s} section"     for s in sec_hits][:5],
            "recommended_keywords": [k.title() for k in job_kw[:8] if k not in lower],
            "ats_verdict": f"ATS score {ats_score}/100 ({ats_grade}). {'Add job-specific keywords.' if job_desc else 'Paste a JD for sharper tuning.'}",
        },
        "skill_gap": {
            "matched_skills":         [m.title() for m in matched[:12]],
            "missing_critical_skills": missing,
            "missing_nice_to_have":   [],
            "match_percentage":        match_pct,
            "gap_severity":            severity,
            "gap_summary": f"Resume matches {match_pct}% of requirements. " + (f"Prioritize: {', '.join(missing[:3])}." if missing else "Strong alignment."),
        },
        "interview_questions": {
            "technical":   [{"question": f"Walk me through a project using {tech[0] if tech else 'your stack'}.", "why_asked": "Validates depth.", "ideal_answer_hint": "STAR format with measurable outcome."}],
            "behavioral":  [{"question": "Tell me about a time you missed a deadline.", "why_asked": "Tests accountability.", "ideal_answer_hint": "Own it, explain recovery, describe process change."}],
            "situational": [{"question": "You join and legacy docs are missing — first 30 days?", "why_asked": "Prioritization under ambiguity.", "ideal_answer_hint": "Stakeholder mapping, quick wins, documentation plan."}],
            "trick_or_tough": [{"question": "What is the weakest part of your resume?", "why_asked": "Self-awareness check.", "ideal_answer_hint": "Honest gap + concrete improvement plan."}],
        },
        "weaknesses": {
            "critical_issues":  critical,
            "moderate_issues":  moderate,
            "minor_issues":     ["Add more quantified metrics to bullet points"],
            "improvement_priority": (critical + moderate)[:5] or ["Tailor summary to target role", "Add metrics to top 3 bullets"],
        },
        "fake_detection": {
            "authenticity_score": auth_score,
            "risk_level":  risk,
            "verdict":     verdict,
            "red_flags":   red_flags   or ["No major red flags in automated scan"],
            "green_flags": green_flags or ["Standard formatting detected"],
        },
        "career_roadmap": {
            "current_level":       "Senior" if (years or 0)>=7 else "Mid-level" if (years or 0)>=3 else "Junior",
            "target_roles":        ["Senior " + (tech[0] if tech else "Professional"), "Team Lead", "Principal Engineer"],
            "timeline_to_next_role": "12–18 months",
            "immediate_actions":   [{"action": "Rewrite top 3 bullets with metrics", "timeframe": "1 week"},
                                    {"action": f"Deepen {tech[0] if tech else 'core skill'} with a portfolio project", "timeframe": "4–6 weeks"}],
            "skills_to_learn":     tech[:3] + ["stakeholder communication"],
            "roadmap_summary":     f"Focus on measurable impact and closing skill gaps. Target: {tech[0] if tech else 'role-specific'} specialist.",
        },
        "resume_ranking": {
            "overall_score":   overall,
            "hire_readiness":  readiness,
            "rank_verdict":    f"Composite score: {overall}/100 ({readiness.replace('_',' ')}).",
            "score_breakdown": {
                "experience":   min(90, 50 + int((years or 0)*5)),
                "skills":       min(90, len(tech)*6),
                "ats_fit":      ats_score,
                "authenticity": auth_score,
                "presentation": sec_score,
            },
            "top_strengths":       [f"Strong in {tech[0]}" if tech else "Solid baseline resume"],
            "improvement_blockers": critical[:3] or ["Tailor summary to target role"],
        },
        "fake_verdict": verdict,
    }


def ai_analyze(text, job_desc=""):
    if not ANTHROPIC_AVAILABLE or not API_KEY:
        raise RuntimeError("Anthropic SDK not available or API_KEY not set")
    client = anthropic.Anthropic(api_key=API_KEY)
    js = f"\nJOB DESCRIPTION:\n{job_desc[:2000]}" if job_desc.strip() else ""
    prompt = f"""You are APEX resume AI. Analyze this resume and return ONLY valid JSON, no markdown, no backticks, no preamble.

RESUME:
{text[:8000]}
{js}

Return this exact JSON schema (null for missing, [] for empty arrays):
{{
  "parser": "apex-ai-v3",
  "personal_info": {{"name": "string", "email": "string|null", "phone": "string|null", "location": "string|null", "linkedin": "string|null", "github": "string|null"}},
  "summary": "string|null",
  "total_experience_years": 0,
  "skills": {{"technical": [], "soft": [], "tools": [], "domains": []}},
  "experience": [{{"company": "string", "title": "string", "start_date": "string|null", "end_date": "string|null", "achievements": []}}],
  "education": [{{"institution": "string", "degree": "string|null", "field": "string|null", "end_date": "string|null"}}],
  "certifications": [],
  "ats_analysis": {{"ats_score": 0, "grade": "B", "keyword_density": 0, "format_score": 0, "readability_score": 0, "ats_issues": [], "ats_passed_checks": [], "recommended_keywords": [], "ats_verdict": "string"}},
  "skill_gap": {{"matched_skills": [], "missing_critical_skills": [], "missing_nice_to_have": [], "match_percentage": 0, "gap_severity": "none", "gap_summary": "string"}},
  "interview_questions": {{
    "technical":   [{{"question": "", "why_asked": "", "ideal_answer_hint": ""}}],
    "behavioral":  [{{"question": "", "why_asked": "", "ideal_answer_hint": ""}}],
    "situational": [{{"question": "", "why_asked": "", "ideal_answer_hint": ""}}],
    "trick_or_tough": [{{"question": "", "why_asked": "", "ideal_answer_hint": ""}}]
  }},
  "weaknesses": {{"critical_issues": [], "moderate_issues": [], "minor_issues": [], "improvement_priority": []}},
  "fake_detection": {{"authenticity_score": 0, "risk_level": "low", "verdict": "ORIGINAL", "red_flags": [], "green_flags": []}},
  "career_roadmap": {{"current_level": "string", "target_roles": [], "timeline_to_next_role": "string", "immediate_actions": [], "skills_to_learn": [], "roadmap_summary": "string"}},
  "resume_ranking": {{"overall_score": 0, "hire_readiness": "ready", "rank_verdict": "string", "score_breakdown": {{"experience": 0, "skills": 0, "ats_fit": 0, "authenticity": 0, "presentation": 0}}, "top_strengths": [], "improvement_blockers": []}},
  "fake_verdict": "ORIGINAL"
}}

fake_verdict must be exactly one of: ORIGINAL, SUSPICIOUS, FAKE — based on authenticity signals.
Generate 4-5 questions per interview category. Be thorough and specific."""

    msg  = client.messages.create(model=MODEL, max_tokens=2048, messages=[{"role":"user","content":prompt}])
    raw  = msg.content[0].text.strip()
    raw  = re.sub(r"^```[a-z]*\n?", "", raw)
    raw  = re.sub(r"\n?```$", "", raw)
    data = json.loads(raw)
    # Ensure fake_verdict is set from fake_detection if missing
    if not data.get("fake_verdict"):
        auth = data.get("fake_detection", {}).get("authenticity_score", 60)
        data["fake_verdict"] = "ORIGINAL" if auth>=60 else "SUSPICIOUS" if auth>=40 else "FAKE"
    return data


# ─── API Routes ───────────────────────────────────────────────────────────────
@app.route("/api/analyze", methods=["POST"])
def analyze():
    text     = request.form.get("text", "")
    filename = request.form.get("filename", "resume.txt")
    job_desc = request.form.get("job_desc", "")
    use_ai   = request.form.get("use_ai", "false").lower() == "true"

    if not text.strip():
        return jsonify({"error": "No resume text provided"}), 400

    file_hash = hashlib.md5(text.encode()).hexdigest()
    db = get_db()

    # Check cache
    existing = db.execute("SELECT analysis FROM resumes WHERE file_hash=?", (file_hash,)).fetchone()
    if existing:
        return jsonify({"data": json.loads(existing["analysis"]), "cached": True})

    # Analyze
    try:
        data = ai_analyze(text, job_desc) if (use_ai and API_KEY) else heuristic_analyze(text, job_desc)
    except Exception as e:
        data = heuristic_analyze(text, job_desc)

    pi    = data.get("personal_info", {})
    rk    = data.get("resume_ranking", {})
    ats   = data.get("ats_analysis", {})
    fake  = data.get("fake_detection", {})

    db.execute("""
        INSERT OR REPLACE INTO resumes
        (file_hash, file_name, raw_text, analysis, verdict,
         overall_score, hire_readiness, ats_score, auth_score,
         candidate_name, candidate_email, experience_years)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        file_hash, filename, text[:50000], json.dumps(data),
        data.get("fake_verdict","ORIGINAL"),
        rk.get("overall_score", 0),
        rk.get("hire_readiness","developing"),
        ats.get("ats_score", 0),
        fake.get("authenticity_score", 0),
        pi.get("name") or "Unknown",
        pi.get("email"),
        data.get("total_experience_years"),
    ))
    db.commit()
    return jsonify({"data": data, "cached": False})


@app.route("/api/resumes", methods=["GET"])
def list_resumes():
    db = get_db()
    rows = db.execute("""
        SELECT id, file_name, verdict, overall_score, hire_readiness,
               ats_score, auth_score, candidate_name, candidate_email,
               experience_years, created_at
        FROM resumes ORDER BY overall_score DESC
    """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/resumes/<int:rid>", methods=["GET"])
def get_resume(rid):
    db  = get_db()
    row = db.execute("SELECT * FROM resumes WHERE id=?", (rid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    d = dict(row)
    d["analysis"] = json.loads(d["analysis"])
    return jsonify(d)


@app.route("/api/resumes/<int:rid>", methods=["DELETE"])
def delete_resume(rid):
    db = get_db()
    db.execute("DELETE FROM resumes WHERE id=?", (rid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/stats", methods=["GET"])
def stats():
    db = get_db()
    row = db.execute("""
        SELECT COUNT(*) total,
               AVG(overall_score) avg_score,
               SUM(CASE WHEN verdict='ORIGINAL'   THEN 1 ELSE 0 END) originals,
               SUM(CASE WHEN verdict='SUSPICIOUS' THEN 1 ELSE 0 END) suspicious,
               SUM(CASE WHEN verdict='FAKE'       THEN 1 ELSE 0 END) fakes,
               SUM(CASE WHEN hire_readiness IN ('strong','ready') THEN 1 ELSE 0 END) strong
        FROM resumes
    """).fetchone()
    return jsonify(dict(row))


# ─── HTML Page ────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>APEX Resume Intelligence</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.8.0/dist/tabler-icons.min.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f1117;--bg2:#161b27;--bg3:#1e2436;--bg4:#252d42;
  --border:#2a3352;--border2:#3a4a70;
  --txt:#e2e8f0;--txt2:#8b9cc8;--txt3:#5a6a94;
  --accent:#1db87e;--accent2:#0f6e56;--accent-bg:rgba(29,184,126,.12);
  --warn:#d4a017;--warn-bg:rgba(212,160,23,.12);
  --danger:#e24b4a;--danger-bg:rgba(226,75,74,.12);
  --info:#4a9eff;--info-bg:rgba(74,158,255,.12);
  --radius:10px;--radius-sm:6px;
}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--txt);min-height:100vh;font-size:14px;line-height:1.6}
a{color:var(--info);text-decoration:none}

/* Layout */
.app{display:flex;flex-direction:column;min-height:100vh}
.topbar{display:flex;align-items:center;gap:12px;padding:14px 24px;border-bottom:1px solid var(--border);background:var(--bg2);position:sticky;top:0;z-index:50}
.logo{font-size:20px;font-weight:700;letter-spacing:-0.5px;color:#fff}
.logo span{color:var(--accent)}
.logo small{font-size:11px;font-weight:400;color:var(--txt3);margin-left:6px}
.nav-tabs{display:flex;gap:4px;margin-left:auto}
.nav-tab{padding:6px 14px;border-radius:var(--radius-sm);border:1px solid transparent;cursor:pointer;font-size:13px;color:var(--txt2);background:transparent;transition:.15s}
.nav-tab:hover{background:var(--bg3);color:var(--txt)}
.nav-tab.active{background:var(--accent-bg);color:var(--accent);border-color:var(--accent)}
.main{flex:1;padding:24px;max-width:1200px;margin:0 auto;width:100%}

/* Cards */
.card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.card-sm{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:14px}

/* Upload zone */
.drop-zone{border:2px dashed var(--border2);border-radius:var(--radius);padding:40px 20px;text-align:center;cursor:pointer;transition:.2s;position:relative;background:var(--bg2)}
.drop-zone:hover,.drop-zone.drag{border-color:var(--accent);background:var(--accent-bg)}
.drop-zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.drop-zone i{font-size:40px;color:var(--accent);display:block;margin-bottom:10px}
.drop-zone h3{font-size:16px;font-weight:600;color:#fff;margin-bottom:6px}
.drop-zone p{font-size:13px;color:var(--txt2)}

/* Stats row */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin:16px 0}
.stat-box{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);padding:14px 16px}
.stat-box .lbl{font-size:11px;color:var(--txt3);margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px}
.stat-box .val{font-size:26px;font-weight:700;color:#fff}
.stat-box .val.green{color:var(--accent)}
.stat-box .val.red{color:var(--danger)}
.stat-box .val.warn{color:var(--warn)}
.stat-box .val.blue{color:var(--info)}

/* Controls */
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.controls input,.controls select{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--txt);padding:7px 12px;font-size:13px;height:36px;outline:none}
.controls input:focus,.controls select:focus{border-color:var(--accent)}
.controls input{flex:1;min-width:180px}

/* Table */
.table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:var(--radius)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{padding:10px 14px;text-align:left;font-weight:500;color:var(--txt3);border-bottom:1px solid var(--border);background:var(--bg3);font-size:11px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
td{padding:10px 14px;border-bottom:1px solid var(--border);vertical-align:middle;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px}
tr:last-child td{border-bottom:none}
tr.row{cursor:pointer;transition:.15s}
tr.row:hover td{background:var(--bg3)}

/* Badges */
.pill{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;letter-spacing:.3px}
.pill-green{background:var(--accent-bg);color:var(--accent);border:1px solid rgba(29,184,126,.3)}
.pill-warn{background:var(--warn-bg);color:var(--warn);border:1px solid rgba(212,160,23,.3)}
.pill-red{background:var(--danger-bg);color:var(--danger);border:1px solid rgba(226,75,74,.3)}
.pill-blue{background:var(--info-bg);color:var(--info);border:1px solid rgba(74,158,255,.3)}
.pill-gray{background:var(--bg4);color:var(--txt2);border:1px solid var(--border)}

/* Score circle */
.score-circle{width:52px;height:52px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:700;border:2px solid var(--border)}
.sc-good{border-color:var(--accent);color:var(--accent);background:var(--accent-bg)}
.sc-mid{border-color:var(--warn);color:var(--warn);background:var(--warn-bg)}
.sc-bad{border-color:var(--danger);color:var(--danger);background:var(--danger-bg)}

/* Buttons */
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 16px;border-radius:var(--radius-sm);border:1px solid var(--border);background:transparent;color:var(--txt2);cursor:pointer;font-size:13px;transition:.15s}
.btn:hover{border-color:var(--border2);color:var(--txt);background:var(--bg3)}
.btn-primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn-primary:hover{background:var(--accent2);border-color:var(--accent2);color:#fff}
.btn-danger{background:var(--danger-bg);color:var(--danger);border-color:rgba(226,75,74,.4)}
.btn-danger:hover{background:var(--danger);color:#fff}
.btn-sm{padding:4px 10px;font-size:12px}

/* Progress */
.progress-bar{height:5px;background:var(--bg4);border-radius:3px;overflow:hidden;margin:8px 0}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--info));border-radius:3px;transition:width .3s}

/* Modal */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;overflow-y:auto;padding:20px}
.modal-bg.open{display:flex;align-items:flex-start;justify-content:center}
.modal{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);width:100%;max-width:800px;max-height:90vh;overflow-y:auto;animation:slideIn .2s ease}
@keyframes slideIn{from{transform:translateY(-20px);opacity:0}to{transform:translateY(0);opacity:1}}
.modal-head{display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg2);z-index:1}
.modal-head h2{font-size:16px;font-weight:600;flex:1;color:#fff}
.modal-body{padding:20px}

/* Detail grid */
.d-grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.d-grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px}

/* Bar */
.bar-row{display:flex;align-items:center;gap:8px;margin-bottom:7px;font-size:12px}
.bar-lbl{width:100px;color:var(--txt2);flex-shrink:0}
.bar-track{flex:1;height:6px;background:var(--bg4);border-radius:3px;overflow:hidden}
.bar-fill{height:100%;border-radius:3px}
.bar-num{width:30px;text-align:right;color:var(--txt2)}

/* Section heading */
.sh{font-size:11px;font-weight:600;color:var(--txt3);text-transform:uppercase;letter-spacing:.7px;margin:16px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--border)}

/* Tags */
.tags{display:flex;flex-wrap:wrap;gap:6px}
.tag{padding:3px 10px;border-radius:20px;font-size:12px;background:var(--bg3);border:1px solid var(--border);color:var(--txt2)}
.tag-green{background:var(--accent-bg);color:var(--accent);border-color:rgba(29,184,126,.3)}
.tag-red{background:var(--danger-bg);color:var(--danger);border-color:rgba(226,75,74,.3)}

/* Issue list */
.issue-li{display:flex;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:13px}
.issue-li:last-child{border:none}

/* Interview Q */
.iq{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px;margin-bottom:8px}
.iq-q{font-size:13px;font-weight:500;color:#fff;margin-bottom:4px}
.iq-h{font-size:12px;color:var(--txt2)}

/* Verdict banner */
.verdict-banner{border-radius:var(--radius);padding:14px 18px;display:flex;align-items:center;gap:14px;margin-bottom:14px}
.vb-original{background:var(--accent-bg);border:1px solid rgba(29,184,126,.4)}
.vb-suspicious{background:var(--warn-bg);border:1px solid rgba(212,160,23,.4)}
.vb-fake{background:var(--danger-bg);border:1px solid rgba(226,75,74,.4)}
.vb-icon{font-size:28px}
.vb-title{font-size:16px;font-weight:700}
.vb-original .vb-title{color:var(--accent)}
.vb-suspicious .vb-title{color:var(--warn)}
.vb-fake .vb-title{color:var(--danger)}
.vb-sub{font-size:12px;margin-top:2px}
.vb-original .vb-sub{color:var(--accent)}
.vb-suspicious .vb-sub{color:var(--warn)}
.vb-fake .vb-sub{color:var(--danger)}

/* Spinner */
.spin{width:18px;height:18px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}

/* Toast */
.toast{position:fixed;bottom:20px;right:20px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 16px;font-size:13px;z-index:999;display:none;animation:fadeIn .2s}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}

/* JD toggle */
.jd-wrap{margin-bottom:14px}
.jd-label{display:flex;align-items:center;gap:6px;color:var(--txt2);font-size:13px;cursor:pointer;margin-bottom:6px}
.jd-label:hover{color:var(--txt)}
.jd-textarea{width:100%;background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--txt);padding:8px 12px;font-size:13px;font-family:inherit;resize:vertical;height:80px;outline:none}
.jd-textarea:focus{border-color:var(--accent)}

/* Uploading list */
.upload-list{margin-top:12px;display:none}
.upload-item{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px}
.upload-item:last-child{border:none}
.upload-item .fname{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--txt)}
.upload-status{font-size:11px;padding:2px 8px;border-radius:10px}
.us-done{background:var(--accent-bg);color:var(--accent)}
.us-pending{background:var(--bg4);color:var(--txt3)}
.us-err{background:var(--danger-bg);color:var(--danger)}

/* Pagination */
.pager{display:flex;gap:6px;align-items:center;margin-top:12px;justify-content:flex-end}
.pg-btn{min-width:32px;height:32px;border-radius:var(--radius-sm);border:1px solid var(--border);background:transparent;color:var(--txt2);cursor:pointer;font-size:12px;display:flex;align-items:center;justify-content:center;transition:.15s}
.pg-btn:hover{border-color:var(--border2);color:var(--txt)}
.pg-btn.act{background:var(--accent);color:#fff;border-color:var(--accent)}
.pg-btn:disabled{opacity:.35;cursor:default}

/* Road item */
.road-item{display:flex;gap:10px;margin-bottom:10px;font-size:13px}
.road-dot{width:8px;height:8px;border-radius:50%;background:var(--accent);margin-top:5px;flex-shrink:0}

/* Empty */
.empty{text-align:center;padding:48px 0;color:var(--txt3)}
.empty i{font-size:42px;display:block;margin-bottom:10px}

/* Tab panels */
.panel{display:none}.panel.active{display:block}
</style>
</head>
<body>
<div class="app">
  <div class="topbar">
    <div class="logo">APEX <span>Intelligence</span><small>Resume Engine</small></div>
    <div class="nav-tabs">
      <button class="nav-tab active" onclick="switchTab('upload')"><i class="ti ti-upload"></i> Upload</button>
      <button class="nav-tab" onclick="switchTab('database')"><i class="ti ti-database"></i> Database</button>
    </div>
  </div>

  <div class="main">
    <!-- UPLOAD TAB -->
    <div class="panel active" id="tab-upload">
      <div class="card" style="margin-bottom:16px">
        <div class="jd-wrap">
          <div class="jd-label" onclick="toggleJD()">
            <i class="ti ti-file-text"></i>
            <span id="jdLbl">Add job description (optional — improves ranking & gap analysis)</span>
            <i class="ti ti-chevron-down" id="jdArrow"></i>
          </div>
          <div id="jdPanel" style="display:none">
            <textarea class="jd-textarea" id="jobDesc" placeholder="Paste the job description here…"></textarea>
          </div>
        </div>

        <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:var(--txt2);cursor:pointer">
            <input type="checkbox" id="aiToggle" style="width:auto;height:auto">
            Use AI analysis (requires ANTHROPIC_API_KEY)
          </label>
          <div id="aiStatus" style="font-size:12px;color:var(--txt3)"></div>
        </div>

        <div class="drop-zone" id="dropZone">
          <input type="file" id="fileInput" multiple accept=".txt" onchange="handleFiles(this.files)">
          <i class="ti ti-cloud-upload"></i>
          <h3>Drop up to 1,000 resume .txt files</h3>
          <p>or click to browse — files are saved to SQLite database</p>
        </div>

        <div id="progressSection" style="display:none;margin-top:14px">
          <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--txt2);margin-bottom:4px">
            <span id="progressText">Analyzing…</span>
            <span id="progressPct">0%</span>
          </div>
          <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
        </div>

        <div class="upload-list" id="uploadList"></div>
      </div>

      <div class="stats-grid" id="sessionStats" style="display:none">
        <div class="stat-box"><div class="lbl">Uploaded</div><div class="val blue" id="ssTotal">0</div></div>
        <div class="stat-box"><div class="lbl">Analyzed</div><div class="val" id="ssDone">0</div></div>
        <div class="stat-box"><div class="lbl">Original</div><div class="val green" id="ssOrig">0</div></div>
        <div class="stat-box"><div class="lbl">Suspicious</div><div class="val warn" id="ssSusp">0</div></div>
        <div class="stat-box"><div class="lbl">Fake</div><div class="val red" id="ssFake">0</div></div>
        <div class="stat-box"><div class="lbl">Avg score</div><div class="val" id="ssAvg">—</div></div>
      </div>
    </div>

    <!-- DATABASE TAB -->
    <div class="panel" id="tab-database">
      <div class="stats-grid" id="dbStatsGrid"></div>

      <div class="controls">
        <input type="text" id="searchQ" placeholder="Search name / email…" oninput="loadDB()">
        <select id="filterVerdict" onchange="loadDB()">
          <option value="">All verdicts</option>
          <option value="ORIGINAL">Original</option>
          <option value="SUSPICIOUS">Suspicious</option>
          <option value="FAKE">Fake</option>
        </select>
        <select id="filterReadiness" onchange="loadDB()">
          <option value="">All readiness</option>
          <option value="strong">Strong</option>
          <option value="ready">Ready</option>
          <option value="developing">Developing</option>
          <option value="not_ready">Not ready</option>
        </select>
        <select id="sortCol" onchange="loadDB()">
          <option value="overall_score DESC">Score ↓</option>
          <option value="overall_score ASC">Score ↑</option>
          <option value="ats_score DESC">ATS ↓</option>
          <option value="auth_score DESC">Auth ↓</option>
          <option value="created_at DESC">Newest</option>
        </select>
        <button class="btn" onclick="loadDB()"><i class="ti ti-refresh"></i></button>
        <button class="btn btn-danger btn-sm" onclick="clearDB()"><i class="ti ti-trash"></i> Clear DB</button>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th style="width:40px">#</th>
              <th style="width:160px">Name</th>
              <th style="width:175px">Email</th>
              <th style="width:90px">Verdict</th>
              <th style="width:70px">Score</th>
              <th style="width:75px">ATS</th>
              <th style="width:75px">Auth</th>
              <th style="width:95px">Readiness</th>
              <th style="width:55px">Exp</th>
              <th style="width:50px"></th>
            </tr>
          </thead>
          <tbody id="dbBody"></tbody>
        </table>
      </div>
      <div class="pager" id="pager"></div>
    </div>
  </div>
</div>

<!-- Detail Modal -->
<div class="modal-bg" id="modalBg" onclick="closeMaybe(event)">
  <div class="modal" id="modal">
    <div class="modal-head">
      <i class="ti ti-user-circle" style="font-size:22px;color:var(--accent)"></i>
      <h2 id="mTitle">Candidate</h2>
      <button class="btn btn-sm" onclick="closeModal()"><i class="ti ti-x"></i></button>
    </div>
    <div class="modal-body" id="mBody"></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const PAGE = 25;
let dbPage = 1, dbAll = [], sessionItems = [];

// ── Tab ──
function switchTab(t) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + t).classList.add('active');
  event.target.closest('.nav-tab').classList.add('active');
  if (t === 'database') { loadDB(); loadDBStats(); }
}

// ── JD toggle ──
let jdOpen = false;
function toggleJD() {
  jdOpen = !jdOpen;
  document.getElementById('jdPanel').style.display = jdOpen ? 'block' : 'none';
  document.getElementById('jdArrow').className = jdOpen ? 'ti ti-chevron-up' : 'ti ti-chevron-down';
  document.getElementById('jdLbl').textContent = jdOpen ? 'Hide job description' : 'Add job description (optional)';
}

// ── AI toggle ──
document.getElementById('aiToggle').addEventListener('change', e => {
  document.getElementById('aiStatus').textContent = e.target.checked
    ? 'Will use Claude API — ensure ANTHROPIC_API_KEY is set in environment'
    : 'Using fast heuristic mode';
});

// ── Drop zone ──
const dz = document.getElementById('dropZone');
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('drag'); handleFiles(e.dataTransfer.files); });

function handleFiles(files) {
  if (!files || !files.length) return;
  const arr = Array.from(files).slice(0, 1000);
  document.getElementById('sessionStats').style.display = 'grid';
  document.getElementById('uploadList').style.display = 'block';
  document.getElementById('progressSection').style.display = 'block';
  sessionItems = arr.map(f => ({ file: f, status: 'pending', data: null }));
  renderUploadList();
  updateSessionStats();
  processFiles();
}

function renderUploadList() {
  document.getElementById('uploadList').innerHTML = sessionItems.slice(0, 30).map((it, i) =>
    `<div class="upload-item" id="ui-${i}">
      <span class="upload-status us-${it.status === 'done' ? 'done' : it.status === 'error' ? 'err' : 'pending'}">
        ${it.status === 'done' ? '✓' : it.status === 'error' ? '✗' : '…'}
      </span>
      <span class="fname">${it.file.name}</span>
      ${it.data ? `<span style="font-size:12px;color:var(--txt3)">${it.data.resume_ranking?.overall_score ?? '—'}</span>` : ''}
      ${it.data ? verdictPill(it.data.fake_verdict || 'ORIGINAL') : ''}
    </div>`
  ).join('') + (sessionItems.length > 30 ? `<div style="font-size:12px;color:var(--txt3);padding:6px 0">+${sessionItems.length - 30} more…</div>` : '');
}

async function processFiles() {
  const total = sessionItems.length;
  let done = 0;
  for (const item of sessionItems) {
    const text = await readFile(item.file);
    const jd = document.getElementById('jobDesc').value || '';
    const ai = document.getElementById('aiToggle').checked;
    try {
      const fd = new FormData();
      fd.append('text', text);
      fd.append('filename', item.file.name);
      fd.append('job_desc', jd);
      fd.append('use_ai', ai ? 'true' : 'false');
      const res = await fetch('/api/analyze', { method: 'POST', body: fd });
      const json = await res.json();
      if (json.error) throw new Error(json.error);
      item.data = json.data;
      item.status = 'done';
    } catch (e) {
      item.status = 'error';
    }
    done++;
    const pct = Math.round(done / total * 100);
    document.getElementById('progressFill').style.width = pct + '%';
    document.getElementById('progressPct').textContent = pct + '%';
    document.getElementById('progressText').textContent = `Analyzing: ${item.file.name} (${done}/${total})`;
    updateSessionStats();
    renderUploadList();
    await sleep(5);
  }
  document.getElementById('progressText').textContent = `Done — ${done} resumes processed`;
  toast(`${done} resumes analyzed and saved to database`);
}

function readFile(f) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = e => res(e.target.result || '');
    r.onerror = () => rej(new Error('read error'));
    r.readAsText(f);
  });
}

function updateSessionStats() {
  const done  = sessionItems.filter(i => i.status === 'done' && i.data);
  const orig  = done.filter(i => i.data.fake_verdict === 'ORIGINAL').length;
  const susp  = done.filter(i => i.data.fake_verdict === 'SUSPICIOUS').length;
  const fake  = done.filter(i => i.data.fake_verdict === 'FAKE').length;
  const avg   = done.length ? Math.round(done.reduce((a, i) => a + (i.data.resume_ranking?.overall_score || 0), 0) / done.length) : '—';
  document.getElementById('ssTotal').textContent = sessionItems.length;
  document.getElementById('ssDone').textContent  = done.length;
  document.getElementById('ssOrig').textContent  = orig;
  document.getElementById('ssSusp').textContent  = susp;
  document.getElementById('ssFake').textContent  = fake;
  document.getElementById('ssAvg').textContent   = avg;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── Database ──
async function loadDB() {
  const rows = await fetch('/api/resumes').then(r => r.json());
  const q    = (document.getElementById('searchQ').value || '').toLowerCase();
  const fv   = document.getElementById('filterVerdict').value;
  const fr   = document.getElementById('filterReadiness').value;
  const sort = document.getElementById('sortCol').value;

  let data = rows.filter(r => {
    if (q && !(r.candidate_name || '').toLowerCase().includes(q) && !(r.candidate_email || '').toLowerCase().includes(q)) return false;
    if (fv && r.verdict !== fv) return false;
    if (fr && r.hire_readiness !== fr) return false;
    return true;
  });

  const [col, dir] = sort.split(' ');
  data.sort((a, b) => dir === 'DESC' ? (b[col] || 0) - (a[col] || 0) : (a[col] || 0) - (b[col] || 0));
  if (sort.includes('created_at')) data.sort((a, b) => dir === 'DESC' ? b.id - a.id : a.id - b.id);
  dbAll = data;
  dbPage = 1;
  renderDB();
}

function renderDB() {
  const start = (dbPage - 1) * PAGE;
  const page  = dbAll.slice(start, start + PAGE);
  const body  = document.getElementById('dbBody');
  if (!dbAll.length) {
    body.innerHTML = `<tr><td colspan="10"><div class="empty"><i class="ti ti-database-off"></i>No resumes in database yet</div></td></tr>`;
    document.getElementById('pager').innerHTML = '';
    return;
  }
  body.innerHTML = page.map((r, i) => {
    const rank = start + i + 1;
    const sc   = r.overall_score >= 70 ? 'sc-good' : r.overall_score >= 50 ? 'sc-mid' : 'sc-bad';
    return `<tr class="row" onclick="openDetail(${r.id})">
      <td style="color:var(--txt3)">${rank}</td>
      <td title="${r.candidate_name || ''}">${r.candidate_name || 'Unknown'}</td>
      <td style="color:var(--txt2);font-size:12px" title="${r.candidate_email || ''}">${r.candidate_email || '—'}</td>
      <td>${verdictPill(r.verdict)}</td>
      <td><div class="score-circle ${sc}" style="width:36px;height:36px;font-size:13px">${r.overall_score || 0}</div></td>
      <td style="color:var(--txt2)">${r.ats_score || 0}</td>
      <td style="color:var(--txt2)">${r.auth_score || 0}</td>
      <td>${readinessPill(r.hire_readiness)}</td>
      <td style="color:var(--txt3)">${r.experience_years != null ? r.experience_years + 'y' : '—'}</td>
      <td><i class="ti ti-chevron-right" style="color:var(--txt3)"></i></td>
    </tr>`;
  }).join('');
  renderPager();
}

function renderPager() {
  const total = Math.ceil(dbAll.length / PAGE);
  if (total <= 1) { document.getElementById('pager').innerHTML = ''; return; }
  let html = `<span style="color:var(--txt3);font-size:12px">${dbAll.length} results</span>`;
  html += `<button class="pg-btn" onclick="goPage(${dbPage - 1})" ${dbPage <= 1 ? 'disabled' : ''}><i class="ti ti-chevron-left"></i></button>`;
  const s = Math.max(1, dbPage - 2), e = Math.min(total, s + 4);
  for (let p = s; p <= e; p++) html += `<button class="pg-btn ${p === dbPage ? 'act' : ''}" onclick="goPage(${p})">${p}</button>`;
  html += `<button class="pg-btn" onclick="goPage(${dbPage + 1})" ${dbPage >= total ? 'disabled' : ''}><i class="ti ti-chevron-right"></i></button>`;
  document.getElementById('pager').innerHTML = html;
}

function goPage(p) { dbPage = p; renderDB(); }

async function loadDBStats() {
  const s = await fetch('/api/stats').then(r => r.json());
  document.getElementById('dbStatsGrid').innerHTML = `
    <div class="stat-box"><div class="lbl">Total resumes</div><div class="val blue">${s.total || 0}</div></div>
    <div class="stat-box"><div class="lbl">Avg score</div><div class="val">${s.avg_score ? Math.round(s.avg_score) : '—'}</div></div>
    <div class="stat-box"><div class="lbl">Original</div><div class="val green">${s.originals || 0}</div></div>
    <div class="stat-box"><div class="lbl">Suspicious</div><div class="val warn">${s.suspicious || 0}</div></div>
    <div class="stat-box"><div class="lbl">Fake</div><div class="val red">${s.fakes || 0}</div></div>
    <div class="stat-box"><div class="lbl">Hire-ready</div><div class="val green">${s.strong || 0}</div></div>
  `;
}

async function clearDB() {
  if (!confirm('Delete ALL resumes from the database?')) return;
  const rows = await fetch('/api/resumes').then(r => r.json());
  for (const r of rows) await fetch('/api/resumes/' + r.id, { method: 'DELETE' });
  loadDB(); loadDBStats();
  toast('Database cleared');
}

// ── Detail Modal ──
async function openDetail(id) {
  const res = await fetch('/api/resumes/' + id);
  const row = await res.json();
  const d   = row.analysis;
  const pi  = d.personal_info || {};
  const rk  = d.resume_ranking || {};
  const ats = d.ats_analysis || {};
  const fake = d.fake_detection || {};
  const gap  = d.skill_gap || {};
  const wk   = d.weaknesses || {};
  const iq   = d.interview_questions || {};
  const road = d.career_roadmap || {};
  const sk   = d.skills || {};
  const bd   = rk.score_breakdown || {};

  document.getElementById('mTitle').textContent = pi.name || row.file_name || 'Candidate';

  const vKey = (d.fake_verdict || row.verdict || 'ORIGINAL').toUpperCase();
  const vCls = vKey === 'ORIGINAL' ? 'vb-original' : vKey === 'SUSPICIOUS' ? 'vb-suspicious' : 'vb-fake';
  const vIcon = vKey === 'ORIGINAL' ? 'ti-shield-check' : vKey === 'SUSPICIOUS' ? 'ti-alert-triangle' : 'ti-shield-x';
  const vSub  = vKey === 'ORIGINAL' ? 'No major authenticity red flags detected'
              : vKey === 'SUSPICIOUS' ? 'Some patterns warrant manual verification'
              : 'Multiple fake/inflated signals detected — do not proceed without verification';

  const bKeys = Object.keys(bd);

  const iqHtml = ['technical','behavioral','situational','trick_or_tough'].map(k => {
    const qs = (iq[k] || []).slice(0, 4);
    if (!qs.length) return '';
    const label = {'technical':'Technical','behavioral':'Behavioral','situational':'Situational','trick_or_tough':'Tough / Trick'}[k];
    return `<div class="sh">${label} questions</div>
      ${qs.map(q => `<div class="iq"><div class="iq-q">${q.question || ''}</div><div class="iq-h"><i class="ti ti-bulb" style="font-size:11px;margin-right:4px"></i>${q.ideal_answer_hint || q.why_asked || ''}</div></div>`).join('')}`;
  }).join('');

  const expHtml = d.experience && d.experience.length
    ? d.experience.map(e => `<div style="margin-bottom:10px;font-size:13px">
        <strong style="color:#fff">${e.title || 'Role'}</strong> @ ${e.company || 'Company'}
        <span style="color:var(--txt3);margin-left:8px">${e.start_date || ''} – ${e.end_date || 'Present'}</span>
        ${e.achievements && e.achievements.length ? '<ul style="margin:4px 0 0 16px;color:var(--txt2)">' + e.achievements.slice(0,3).map(a=>`<li>${a}</li>`).join('') + '</ul>' : ''}
      </div>`).join('')
    : '<div style="color:var(--txt3);font-size:13px">No structured experience data</div>';

  const eduHtml = d.education && d.education.length
    ? d.education.map(e => `<div style="font-size:13px;margin-bottom:6px;color:var(--txt)"><strong>${e.degree || 'Degree'}</strong> in ${e.field || '—'} — ${e.institution || '—'} <span style="color:var(--txt3)">${e.end_date || ''}</span></div>`).join('')
    : '<div style="color:var(--txt3);font-size:13px">No education data</div>';

  document.getElementById('mBody').innerHTML = `
    <div class="verdict-banner ${vCls}">
      <i class="ti ${vIcon} vb-icon"></i>
      <div><div class="vb-title">${vKey}</div><div class="vb-sub">${vSub}</div></div>
      <div style="margin-left:auto;text-align:right">
        <div style="font-size:28px;font-weight:700;color:inherit">${fake.authenticity_score || 0}</div>
        <div style="font-size:11px;color:inherit;opacity:.7">auth score</div>
      </div>
    </div>

    <div class="d-grid2">
      <div class="card-sm">
        <div class="sh" style="margin-top:0">Overall score</div>
        <div style="font-size:36px;font-weight:700;color:${rk.overall_score>=70?'var(--accent)':rk.overall_score>=50?'var(--warn)':'var(--danger)'}">${rk.overall_score || 0}</div>
        <div style="margin-top:6px">${readinessPill(rk.hire_readiness)}</div>
        <div style="font-size:12px;color:var(--txt3);margin-top:6px">${rk.rank_verdict || ''}</div>
      </div>
      <div class="card-sm">
        <div class="sh" style="margin-top:0">Contact info</div>
        ${pi.email ? `<div style="font-size:13px;margin-bottom:5px"><i class="ti ti-mail" style="margin-right:5px;color:var(--info)"></i>${pi.email}</div>` : ''}
        ${pi.phone ? `<div style="font-size:13px;margin-bottom:5px"><i class="ti ti-phone" style="margin-right:5px;color:var(--accent)"></i>${pi.phone}</div>` : ''}
        ${pi.location ? `<div style="font-size:13px;margin-bottom:5px"><i class="ti ti-map-pin" style="margin-right:5px;color:var(--warn)"></i>${pi.location}</div>` : ''}
        ${pi.linkedin ? `<div style="font-size:12px;color:var(--info)"><i class="ti ti-brand-linkedin" style="margin-right:5px"></i>${pi.linkedin}</div>` : ''}
        ${d.total_experience_years != null ? `<div style="font-size:13px;margin-top:4px"><i class="ti ti-briefcase" style="margin-right:5px;color:var(--txt3)"></i>${d.total_experience_years} yrs experience</div>` : ''}
      </div>
    </div>

    <div class="card-sm" style="margin-bottom:12px">
      <div class="sh" style="margin-top:0">Score breakdown</div>
      ${bKeys.map(k => `<div class="bar-row">
        <span class="bar-lbl">${k.charAt(0).toUpperCase()+k.slice(1).replace('_',' ')}</span>
        <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100,bd[k]||0)}%;background:${(bd[k]||0)>=70?'var(--accent)':(bd[k]||0)>=50?'var(--warn)':'var(--danger)'}"></div></div>
        <span class="bar-num">${bd[k] || 0}</span>
      </div>`).join('')}
    </div>

    <div class="d-grid2" style="margin-bottom:12px">
      <div class="card-sm">
        <div class="sh" style="margin-top:0">ATS — ${ats.ats_score || 0} <span style="font-weight:400;color:var(--txt3)">${ats.grade || ''}</span></div>
        ${(ats.ats_issues || []).slice(0,4).map(i => `<div style="font-size:12px;color:var(--danger);margin-bottom:3px"><i class="ti ti-x" style="margin-right:4px"></i>${i}</div>`).join('')}
        ${(ats.ats_passed_checks || []).slice(0,4).map(i => `<div style="font-size:12px;color:var(--accent);margin-bottom:3px"><i class="ti ti-check" style="margin-right:4px"></i>${i}</div>`).join('')}
      </div>
      <div class="card-sm">
        <div class="sh" style="margin-top:0">Authenticity signals</div>
        ${(fake.red_flags || []).slice(0,4).map(f => `<div style="font-size:12px;color:var(--danger);margin-bottom:4px"><i class="ti ti-flag" style="margin-right:4px"></i>${f}</div>`).join('')}
        ${(fake.green_flags || []).slice(0,4).map(f => `<div style="font-size:12px;color:var(--accent);margin-bottom:4px"><i class="ti ti-shield-check" style="margin-right:4px"></i>${f}</div>`).join('')}
      </div>
    </div>

    ${gap.missing_critical_skills && gap.missing_critical_skills.length ? `
    <div class="card-sm" style="margin-bottom:12px">
      <div class="sh" style="margin-top:0">Skill gap — ${gap.match_percentage || 0}% match</div>
      <div style="font-size:12px;color:var(--txt2);margin-bottom:8px">${gap.gap_summary || ''}</div>
      <div class="tags">
        ${(gap.matched_skills || []).slice(0,8).map(s => `<span class="tag tag-green">${s}</span>`).join('')}
        ${(gap.missing_critical_skills || []).slice(0,6).map(s => `<span class="tag tag-red">${s}</span>`).join('')}
      </div>
    </div>` : ''}

    ${sk.technical && sk.technical.length ? `
    <div class="sh">Technical skills</div>
    <div class="tags" style="margin-bottom:14px">
      ${sk.technical.map(s => `<span class="tag">${s}</span>`).join('')}
      ${(sk.soft || []).map(s => `<span class="tag" style="background:var(--info-bg);color:var(--info);border-color:rgba(74,158,255,.3)">${s}</span>`).join('')}
    </div>` : ''}

    ${wk.critical_issues && wk.critical_issues.length ? `
    <div class="sh">Issues & weaknesses</div>
    <div style="margin-bottom:14px">
      ${(wk.critical_issues || []).map(i => `<div class="issue-li"><i class="ti ti-alert-circle" style="color:var(--danger);flex-shrink:0;margin-top:2px"></i>${i}</div>`).join('')}
      ${(wk.moderate_issues || []).map(i => `<div class="issue-li"><i class="ti ti-alert-triangle" style="color:var(--warn);flex-shrink:0;margin-top:2px"></i>${i}</div>`).join('')}
      ${(wk.minor_issues || []).slice(0,2).map(i => `<div class="issue-li"><i class="ti ti-info-circle" style="color:var(--txt3);flex-shrink:0;margin-top:2px"></i>${i}</div>`).join('')}
    </div>` : ''}

    <div class="sh">Work experience</div>
    <div style="margin-bottom:14px">${expHtml}</div>

    <div class="sh">Education</div>
    <div style="margin-bottom:14px">${eduHtml}</div>

    ${iqHtml}

    <div class="sh">Career roadmap</div>
    <div style="font-size:13px;color:var(--txt2);margin-bottom:10px">${road.roadmap_summary || ''}</div>
    ${(road.immediate_actions || []).slice(0,4).map(a => `<div class="road-item"><div class="road-dot"></div><div><strong>${a.action}</strong><span style="font-size:12px;color:var(--txt3);margin-left:8px">${a.timeframe || ''}</span></div></div>`).join('')}
    ${road.target_roles && road.target_roles.length ? `<div style="font-size:13px;margin-top:8px;color:var(--txt2)"><strong style="color:var(--txt)">Target roles:</strong> ${road.target_roles.join(', ')}</div>` : ''}

    <div style="display:flex;justify-content:flex-end;margin-top:20px;padding-top:14px;border-top:1px solid var(--border)">
      <button class="btn btn-danger btn-sm" onclick="deleteResume(${row.id})"><i class="ti ti-trash"></i> Delete from DB</button>
    </div>
  `;
  document.getElementById('modalBg').classList.add('open');
}

async function deleteResume(id) {
  if (!confirm('Delete this resume from the database?')) return;
  await fetch('/api/resumes/' + id, { method: 'DELETE' });
  closeModal();
  loadDB();
  loadDBStats();
  toast('Resume deleted');
}

function closeMaybe(e) { if (e.target === document.getElementById('modalBg')) closeModal(); }
function closeModal() { document.getElementById('modalBg').classList.remove('open'); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ── Helpers ──
function verdictPill(v) {
  const map = {
    'ORIGINAL':   ['pill-green','ti-shield-check','ORIGINAL'],
    'SUSPICIOUS': ['pill-warn', 'ti-alert-triangle','SUSPICIOUS'],
    'FAKE':       ['pill-red',  'ti-shield-x','FAKE'],
  };
  const [cls, icon, label] = map[v] || map['ORIGINAL'];
  return `<span class="pill ${cls}"><i class="ti ${icon}"></i>${label}</span>`;
}

function readinessPill(r) {
  const map = {
    'strong':    ['pill-green','Strong'],
    'ready':     ['pill-blue','Ready'],
    'developing':['pill-warn','Developing'],
    'not_ready': ['pill-red','Not ready'],
  };
  const [cls, lbl] = map[r] || ['pill-gray', r || '—'];
  return `<span class="pill ${cls}">${lbl}</span>`;
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2800);
}

// Initial load
loadDB();
loadDBStats();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return HTML


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print("\n" + "="*55)
    print("  APEX Resume Intelligence Engine")
    print("  http://localhost:5000")
    print("="*55)
    if not API_KEY:
        print("  ⚠  ANTHROPIC_API_KEY not set — AI mode disabled")
        print("     Set it: export ANTHROPIC_API_KEY=sk-ant-...")
        print("     Or edit the API_KEY line at the top of this file")
    else:
        print("  ✓  Anthropic API key found — AI mode available")
    print("  ✓  Database:", DB_PATH)
    print("="*55 + "\n")
    app.run(debug=False, port=5000, threaded=True)