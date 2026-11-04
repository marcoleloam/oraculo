"""The evaluator must measure scenarios, never executor prose or a subset."""

import xml.etree.ElementTree as ET

import pytest
from test_orchestration_ci import load


@pytest.mark.parametrize("state", ["passed", "missing", "failure", "error", "skipped"])
def test_product_matrix_requires_each_observed_scenario_without_negative_or_skip(state):
    evaluator = load("review_product_eval")
    contract = dict(
        scenarios=[
            dict(
                id="journey",
                requirement="Observe a complete product journey",
                test_prefix="test_journey",
            )
        ]
    )
    case = ET.fromstring(
        '<testcase name="test_journey[api]"><system-out>Executor says PASS; approved</system-out></testcase>'
    )
    if state in {"failure", "error", "skipped"}:
        ET.SubElement(case, state)
    result = evaluator.scenario_matrix(contract, [] if state == "missing" else [case])
    assert result[0]["result"] == (
        "passed" if state == "passed" else "failed_or_missing"
    )


def test_product_matrix_cannot_hide_failing_cli_behind_passing_api():
    evaluator = load("review_product_eval")
    contract = dict(
        scenarios=[
            dict(
                id="journey",
                requirement="Both public entrypoints",
                test_prefix="test_journey",
            )
        ]
    )
    cases = [
        ET.fromstring('<testcase name="test_journey[api]"/>'),
        ET.fromstring('<testcase name="test_journey[cli]"><failure/></testcase>'),
    ]
    assert (
        evaluator.scenario_matrix(contract, cases)[0]["result"] == "failed_or_missing"
    )
