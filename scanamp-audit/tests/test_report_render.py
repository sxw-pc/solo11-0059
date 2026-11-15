import copy
import json
from scanamp_audit.report.format import md_escape
from scanamp_audit.report.html_report import render_html
from scanamp_audit.report.json_report import build_report, write_json
from scanamp_audit.report.markdown_report import render_markdown
from scanamp_audit.report.renderer import write_reports

def test_json_report_shape_and_summary_stability(demo_result, tmp_path):
    report = build_report(demo_result)
    assert {"schema_version", "run_id", "generated_at", "inputs", "config", "table",
            "queries", "diagnostics", "summary"} <= set(report)
    assert "generated_at" not in report["summary"]
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    write_json(first, report["summary"])
    write_json(second, report["summary"])
    assert first.read_bytes() == second.read_bytes()
    assert json.loads(first.read_text(encoding="utf-8")) == demo_result.summary

def test_markdown_escapes_dynamic_text(demo_result):
    result = copy.deepcopy(demo_result)
    result.queries[0]["description"] = "<script>|`x`"
    text = render_markdown(result)
    assert "&lt;script&gt;" in text
    assert "\\|" in text and "\\`" in text
    assert "<script>" not in text
    assert "Query details" in text and "Diagnostics" in text

def test_html_is_self_contained_and_escaped(demo_result):
    result = copy.deepcopy(demo_result)
    evil = "<script>alert(1)</script>"
    result.queries[0]["where"] = evil
    result.summary["queries"][0]["where"] = evil
    text = render_html(result)
    assert "&lt;script&gt;" in text and "<script>alert(1)</script>" not in text
    for marker in ("http://", "https://", "<link", "src=", "@import", "cdn"):
        assert marker not in text.lower()
    assert "Content-Security-Policy" in text

def test_write_reports_artifacts(demo_result, tmp_path):
    artifacts = write_reports(demo_result, tmp_path / "run")
    assert [item["name"] for item in artifacts] == [
        "report.json", "report.md", "report.html", "summary.json"]
    for item in artifacts:
        assert len(item["sha256"]) == 64 and item["byte_size"] > 0
    summary = json.loads((tmp_path / "run" / "summary.json").read_text(encoding="utf-8"))
    assert summary == demo_result.summary

def test_layout_evaluation_rendered_in_all_reports(demo_result, tmp_path):
    report = build_report(demo_result)
    assert report["layout_evaluation"] == demo_result.layout_evaluation
    markdown = render_markdown(demo_result)
    assert "## Partition layout evaluation" in markdown
    assert "### Layout `without:dt,region`" in markdown
    html = render_html(demo_result)
    assert "Partition layout evaluation" in html
    assert "without:dt,region" in html
    write_reports(demo_result, tmp_path / "run")
    summary = json.loads((tmp_path / "run" / "summary.json").read_text(encoding="utf-8"))
    assert summary["layout_evaluation"] == demo_result.layout_evaluation

def test_markdown_escape_helper():
    assert md_escape("a|b`c<d>&e") == "a\\|b\\`c&lt;d&gt;&amp;e"
