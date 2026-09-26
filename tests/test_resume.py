import io
import json
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, ollama_client, pipeline
from app.main import app
from app.normalize import employment_types, normalize
from app.resume import ResumeError, extract_text

RESUME_TEXT = ("Maria Santos\nQuezon City, Philippines\n\nJunior Backend Developer, BrightPay (2023 - present)\n"
               "Built REST APIs with Django and PostgreSQL.\n\nSkills: Python, Django, PostgreSQL, Git")


# --- reading files ---------------------------------------------------------------------

def make_docx(paragraphs):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("word/document.xml", xml)
    return buffer.getvalue()


def make_pdf(lines):
    """A minimal one-page PDF with real text, built by hand so the test needs no PDF writer."""
    content = "BT /F1 12 Tf 50 750 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               "/Resources << /Font << /F1 5 0 R >> >> >>",
               f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_reads_txt_docx_and_pdf():
    assert extract_text("cv.txt", RESUME_TEXT.encode("utf-8")) == RESUME_TEXT
    assert extract_text("CV.TXT", b"\xef\xbb\xbf" + RESUME_TEXT.encode()) == RESUME_TEXT  # BOM, upper-case name
    docx = extract_text("cv.docx", make_docx(["Maria Santos", "Backend Developer at BrightPay", "Skills: Python"]))
    assert docx == "Maria Santos\nBackend Developer at BrightPay\nSkills: Python"
    pdf = extract_text("cv.pdf", make_pdf(["Maria Santos", "Backend Developer at BrightPay", "Skills: Python, Django"]))
    assert "Maria Santos" in pdf and "Skills: Python, Django" in pdf


@pytest.mark.parametrize("name, data, message", [
    ("cv.exe", b"x" * 100, "Upload a PDF"),
    ("cv.doc", b"x" * 100, "Upload a PDF"),
    ("cv.txt", b"", "empty"),
    ("cv.txt", b"x" * (5 * 1024 * 1024 + 1), "larger than 5 MB"),
    ("cv.txt", b"hi", "No readable text"),
    ("cv.docx", b"not a zip", "Save it as .docx"),
    ("cv.pdf", b"%PDF-1.4 garbage", "could not be read|No readable text"),
], ids=["exe", "doc", "empty", "too-big", "too-short", "bad-docx", "bad-pdf"])
def test_bad_files_are_explained(name, data, message):
    with pytest.raises(ResumeError, match=message):
        extract_text(name, data)


def test_scanned_pdf_hint():
    with pytest.raises(ResumeError, match="scanned image"):
        extract_text("cv.pdf", make_pdf([]))


# --- reading the resume with the model ------------------------------------------------------

def test_analyze_resume_request_and_cleanup(ollama):
    ollama.reply = {"message": {"content": json.dumps({
        "name": " Maria  Santos ", "headline": "Junior Backend Developer",
        "skills": ["Python", "python", "Django", "", "PostgreSQL."], "services": ["backend development"],
        "experience": "Junior Backend Developer at BrightPay since 2023.", "location": "Quezon City, Philippines"})},
        "done": True}
    found = ollama_client.analyze_resume(RESUME_TEXT)
    assert found == {"name": "Maria Santos", "headline": "Junior Backend Developer",
                     "skills": ["Python", "Django", "PostgreSQL"], "services": ["backend development"],
                     "experience": "Junior Backend Developer at BrightPay since 2023.",
                     "location": "Quezon City, Philippines"}
    body = json.loads(ollama.requests[0].content)
    assert body["format"] == "json"  # without it qwen3 wrote reasoning until it hit the token cap
    assert "Copy words from the resume" in body["messages"][0]["content"]
    assert body["messages"][1]["content"].startswith("RESUME\nMaria Santos")


def test_analyze_resume_tolerates_text_around_json(ollama):
    ollama.reply = {"message": {"content": 'Here it is:\n{"name": "Maria", "skills": ["Go"]}\nDone.'}, "done": True}
    found = ollama_client.analyze_resume(RESUME_TEXT)
    assert found["name"] == "Maria" and found["skills"] == ["Go"] and found["experience"] == ""


def test_analyze_resume_bad_answer(ollama):
    ollama.reply = {"message": {"content": "I can't help with that."}, "done": True}
    with pytest.raises(ollama_client.OllamaError, match="wasn't valid JSON"):
        ollama_client.analyze_resume(RESUME_TEXT)


# --- the profile page -------------------------------------------------------------------------

@pytest.fixture
def client(laya):
    return TestClient(app)


def upload(client, name="cv.txt", data=RESUME_TEXT.encode()):
    return client.post("/profile/resume", files={"file": (name, data, "text/plain")})


def test_upload_fills_form_without_saving(client, ollama):
    ollama.reply = {"message": {"content": json.dumps({
        "name": "Maria Santos", "headline": "Junior Backend Developer", "skills": ["Django", "Python"],
        "services": ["backend development"], "experience": "Built REST APIs at BrightPay.",
        "location": "Quezon City"})}, "done": True}
    before = pipeline.load_profile()
    page = upload(client)
    assert page.status_code == 200
    assert 'value="Maria Santos"' in page.text and "Built REST APIs at BrightPay." in page.text
    assert 'value="python, sql, Django"' in page.text  # existing skills kept, new ones added, no duplicates
    assert "Nothing is saved until you do" in page.text and 'action="/profile"' in page.text
    assert pipeline.load_profile() == before  # not saved yet
    assert pipeline.load_resume() == RESUME_TEXT  # resume text kept for drafts
    assert "Resume on file" in page.text


def test_upload_with_ollama_offline_keeps_resume_and_explains(client):
    page = upload(client)
    assert page.status_code == 503
    assert "resume text was saved" in page.text and "isn&#39;t running" in page.text
    assert pipeline.load_resume() == RESUME_TEXT


def test_upload_bad_file(client, ollama):
    page = upload(client, "cv.exe", b"MZ" * 100)
    assert page.status_code == 422 and "Upload a PDF" in page.text
    assert pipeline.load_resume() == "" and ollama.requests == []


def test_delete_resume(client, ollama):
    upload(client)
    r = client.post("/profile/resume/delete", follow_redirects=False)
    assert r.status_code == 303 and pipeline.load_resume() == ""
    assert "deleted from this PC" in client.get(r.headers["location"]).text


def test_ai_drafts_use_the_resume(client, ollama):
    upload(client)
    oid, _ = pipeline.process(text="Backend Developer\nDjango work")
    client.post(f"/opportunities/{oid}/ai-draft/job_application")
    prompt = json.loads(ollama.requests[-1].content)["messages"][1]["content"]
    assert "RESUME\nMaria Santos" in prompt


def test_preferences_are_saved(client):
    client.post("/profile", data={"work_modes": ["remote", "hybrid"], "employment_types": ["full_time", "flexible_hours", "bogus"],
                                  "preferred_locations": "Manila, Remote", "confidence_threshold": "0.6"})
    saved = pipeline.load_profile()
    assert saved["employment_types"] == ["full_time", "flexible_hours"]
    assert saved["work_modes"] == ["remote", "hybrid"] and saved["preferred_locations"] == ["Manila", "Remote"]
    page = client.get("/profile").text
    assert 'value="flexible_hours" checked' in page and 'value="part_time" >' in page


def test_old_files_are_imported_once(tmp_path):
    pipeline.RESUME_PATH.write_text(RESUME_TEXT, encoding="utf-8")
    assert pipeline.load_profile()["name"] == "Test User" and pipeline.load_resume() == RESUME_TEXT
    assert not pipeline.PROFILE_PATH.exists() and not pipeline.RESUME_PATH.exists()
    assert (tmp_path / "profile.json.imported").exists() and (tmp_path / "resume.txt.imported").exists()
    db.delete_resume()
    assert pipeline.load_resume() == "" and db.get_profile()["name"] == "Test User"


def test_example_profile_is_used_until_a_profile_is_saved(monkeypatch, tmp_path):
    example = tmp_path / "profile.example.json"
    example.write_text(json.dumps({"name": "Your Name"}), encoding="utf-8")
    monkeypatch.setattr(pipeline, "PROFILE_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(pipeline, "EXAMPLE_PROFILE_PATH", example)
    assert pipeline.load_profile() == {"name": "Your Name"}


# --- employment types on opportunities ------------------------------------------------------------

@pytest.mark.parametrize("given, text, expected", [
    ("FULL_TIME", "", ["full_time"]),                         # schema.org
    (["PART_TIME", "CONTRACTOR"], "", ["part_time", "contract"]),
    ("Full-time", "Flexible hours available.", ["full_time", "flexible_hours"]),  # Lever + flexible from text
    ("Full-time", "We also have part-time roles.", ["full_time"]),  # stated type wins over text
    (None, "This is a full-time role. Part-time also possible.", ["full_time", "part_time"]),
    (None, "Freelance project, fixed price.", ["freelance"]),
    (None, "Summer internship for students", ["internship"]),
    (None, "6-month contract, flexible schedule", ["contract", "flexible_hours"]),
    (None, "We signed a contract with a big client.", []),  # plain "contract" isn't enough
    (None, "Great team, Python work.", []),
])
def test_employment_type_detection(given, text, expected):
    assert employment_types(given, text) == expected


def test_normalize_sets_employment_types():
    assert normalize({"title": "Part-time Python tutor", "description": "Evenings"})["employment_types"] == ["part_time"]


def test_list_filters_by_employment_type(client):
    full, _ = pipeline.process(text="Python dev\nA full-time Python role")
    part, _ = pipeline.process(text="Python tutor\nA part-time Python role")
    page = client.get("/opportunities?employment=part_time").text
    assert f"/opportunities/{part}" in page and f"/opportunities/{full}" not in page
    assert "Part-time" in client.get(f"/opportunities/{part}").text


def test_analyze_resume_joins_list_answers(ollama):
    ollama.reply = {"message": {"content": json.dumps({"experience": ["Developer at A", " ", "Intern at B"]})}, "done": True}
    assert ollama_client.analyze_resume(RESUME_TEXT)["experience"] == "Developer at A\nIntern at B"
