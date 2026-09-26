import pytest

from app import db, pipeline


def test_new_item_is_evaluated_and_scored(laya):
    oid, duplicate = pipeline.process(text="Python developer\nBuild Python and SQL tools. Fully remote.")
    assert duplicate is False
    ev = db.latest_evaluation(oid)
    assert ev["passed"] == 1 and ev["score"] > 50
    assert ev["result"]["answers"]["type"]["value"] == "job"
    assert db.get_opportunity(oid)["status"] == "new"


def test_rejected_item_is_stored_as_rejected_and_hidden(laya):
    oid, _ = pipeline.process(text="WIN NOW\nThis is spam, click here")
    ev = db.latest_evaluation(oid)
    assert ev["passed"] == 0 and ev["reject_reasons"] == ["Laya type is spam"]
    assert [o["id"] for o in db.list_opportunities()] == []
    assert [o["id"] for o in db.list_opportunities(show_rejected=True)] == [oid]


def test_duplicate_url_is_linked_and_not_evaluated(laya):
    record = {"source_url": "https://x.example/job/1?utm_source=a", "title": "Job", "description": "Python"}
    first, _ = pipeline.process(op=record)
    calls = laya.calls
    second, duplicate = pipeline.process(op={**record, "source_url": "https://x.example/job/1/#apply"})
    assert duplicate is True and laya.calls == calls
    assert db.get_opportunity(second)["duplicate_of"] == first
    assert [o["id"] for o in db.list_opportunities()] == [first]


def test_laya_down_marks_pending_then_retry_evaluates(laya):
    laya.down = True
    oid, _ = pipeline.process(text="Job\nPython work")
    assert db.get_opportunity(oid)["status"] == "pending_evaluation"
    assert db.latest_evaluation(oid)["result"]["error"] == "Laya not reachable"

    assert pipeline.retry_pending() == (0, 1)  # still down: nothing evaluated, item stays pending
    laya.down = False
    assert pipeline.retry_pending() == (1, 1)
    assert db.get_opportunity(oid)["status"] == "new"
    assert db.latest_evaluation(oid)["passed"] == 1


def test_retry_stops_at_first_failure(laya):
    laya.down = True
    for n in range(3):
        pipeline.process(text=f"Job {n}\nPython")
    calls = laya.calls
    pipeline.retry_pending()
    assert laya.calls == calls + 1


def test_reevaluating_keeps_user_status(laya):
    oid, _ = pipeline.process(text="Job\nPython")
    db.set_status(oid, "shortlisted")
    laya.down = True
    assert pipeline.evaluate_opportunity(oid) is False
    assert db.get_opportunity(oid)["status"] == "shortlisted"


def test_nothing_to_process():
    with pytest.raises(ValueError):
        pipeline.process(text="   ")


def test_profile_with_bom_is_read(laya):
    raw = pipeline.PROFILE_PATH.read_bytes()
    pipeline.PROFILE_PATH.write_bytes(b"\xef\xbb\xbf" + raw)
    assert pipeline.load_profile()["name"] == "Test User"
