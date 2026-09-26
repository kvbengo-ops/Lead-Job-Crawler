import json
from pathlib import Path
import pytest

from app import db
from app.extract import from_text
from app.normalize import canonical_url, clean_title

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    db.init()


def test_clean_title_drops_site_suffix_and_entities():
    url = "https://www.onlinejobs.ph/jobseekers/job/1733026"
    assert clean_title("AI &amp; IT Specialist 1733026 - OnlineJobs.ph", url) == "AI & IT Specialist"
    assert clean_title("Backend - Python", url) == "Backend - Python"  # suffix is not the site: kept


def test_canonical_url_removes_tracking():
    assert canonical_url("HTTPS://Example.COM/jobs/1/?utm_source=x&ref=abc&x=1#top") == "https://example.com/jobs/1?x=1"

def test_jsonld_extracts_job_fields():
    html = '''<html><title>Fallback</title><script type="application/ld+json">{"@type":"JobPosting","title":"Python Developer","description":"Build tools","hiringOrganization":{"name":"Acme"},"datePosted":"2026-01-01"}</script><p>Build tools</p></html>'''
    op = from_text(html, "https://example.com/job")
    assert op["title"] == "Python Developer"
    assert op["company"] == "Acme"
    assert op["extracted_by"]["title"] == "jsonld"

def test_duplicate_insert_is_linked():
    op = from_text("same", "https://example.com/job?utm_campaign=x")
    first, was_duplicate = db.insert_opportunity(op)
    second, was_duplicate = db.insert_opportunity(op)
    assert was_duplicate is True
    assert db.get_opportunity(second)["duplicate_of"] == first
