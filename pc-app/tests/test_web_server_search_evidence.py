"""The web API must carry the real excerpts that informed an AI estimate (2026-11-12), so the page
can show the operator exactly what the estimate was grounded in."""
import json

import profile_search
import web_server
from modbus_client import CurveSection, Profile


def _candidate(**kwargs):
    return profile_search.ProfileSearchCandidate(
        source_title="AI-generated estimate", source_url="",
        profile=Profile(name="X (AI estimate)", sections=[CurveSection(upper_temp=200)] * 8),
        machine_match=False, quotes_verified=False, ai_generated=True, **kwargs,
    )


def test_search_result_json_includes_the_excerpts_that_informed_an_estimate():
    evidence = [{"text": "Reflow at 230 degrees Celsius for 90 seconds.", "source_title": "iFixit", "source_url": "https://x/"}]
    result = profile_search.ProfileSearchResult(candidates=[_candidate(evidence=evidence)], errors=[])
    out = web_server._search_result_to_json(result)
    assert out["candidates"][0]["evidence"] == evidence
    json.dumps(out)  # must be serializable as-is


def test_search_result_json_has_an_empty_evidence_list_when_there_is_none():
    result = profile_search.ProfileSearchResult(candidates=[_candidate()], errors=[])
    assert web_server._search_result_to_json(result)["candidates"][0]["evidence"] == []
