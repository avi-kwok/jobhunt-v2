import json
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.adapters import epic
from jobhunt.adapters.epic import EpicAdapter
from jobhunt.models import CompanyConfig

URL = "https://careers.epic.com/jobs/"
CO = CompanyConfig(name="Epic Systems", ats="epic", host="careers.epic.com")
FIXTURE = (Path(__file__).parent / "fixtures" / "epic" / "jobs.html").read_text()


def page(inner: dict) -> str:
    """A minimal page whose flight payload holds `inner`, split over two chunks."""
    flight = '29:["$","$L2a",null,' + json.dumps(inner) + "]\n"
    cut = len(flight) // 2
    pushes = "".join(
        f"<script>self.__next_f.push([1,{json.dumps(s)}])</script>"
        for s in (flight[:cut], flight[cut:])
    )
    return f"<html><body>{pushes}</body></html>"


def pos(title, is_open=True, published=True):
    return {"externalName": title, "shortSummary": "S", "background": "B",
            "isOpen": is_open, "isPublished": published, "referenceNumber": 1}


def run():
    return EpicAdapter(client=httpx.Client()).fetch(CO)


@respx.mock
def test_parse_real_shape_single_call():
    route = respx.get(URL).mock(return_value=httpx.Response(200, text=FIXTURE))
    jobs = run()
    assert route.call_count == 1
    # unpublished 30299 is in avaturePositions but not allOpenJobs -> skipped
    assert [j.external_id for j in jobs] == ["740", "30318", "1437", "22472"]
    j = jobs[1]
    assert j.title == "Software Developer Intern - Summer 2027"
    assert j.url == "https://careers.epic.com/jobs/30318/"
    assert j.company == "Epic Systems" and j.source == "epic"
    assert j.description.startswith("An internship that makes an impact.")
    assert "Background: Working towards a bachelor's degree" in j.description
    assert j.location is None and j.posted_at is None
    assert j.uid == "epic:Epic Systems:30318"


@respx.mock
def test_dedupe_and_skips_missing_or_closed_positions():
    inner = {
        "allOpenJobs": [{"id": "1"}, {"id": "1"}, {"id": "2"}, {"id": "3"}, {"id": "4"}, {}],
        "avaturePositions": {"1": pos("Software Intern"), "3": pos("Closed", published=False),
                             "4": pos("")},
    }
    respx.get(URL).mock(return_value=httpx.Response(200, text=page(inner)))
    assert [j.external_id for j in run()] == ["1"]


@respx.mock
def test_hard_cap(monkeypatch):
    monkeypatch.setattr(epic, "MAX_JOBS", 3)
    inner = {"allOpenJobs": [{"id": str(i)} for i in range(10)],
             "avaturePositions": {str(i): pos(f"Job {i}") for i in range(10)}}
    respx.get(URL).mock(return_value=httpx.Response(200, text=page(inner)))
    assert [j.external_id for j in run()] == ["0", "1", "2"]


@respx.mock
def test_empty_board_with_markers():
    respx.get(URL).mock(return_value=httpx.Response(
        200, text=page({"allOpenJobs": [], "avaturePositions": {}})))
    assert run() == []


@respx.mock
def test_redesign_or_challenge_raises():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>challenge</html>"))
    with pytest.raises(ValueError):
        run()
    # flight present but positions dict missing
    respx.get(URL).mock(return_value=httpx.Response(
        200, text=page({"allOpenJobs": [{"id": "1"}]})))
    with pytest.raises(ValueError):
        run()


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()
