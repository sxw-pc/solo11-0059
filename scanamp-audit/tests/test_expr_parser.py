import json
import pytest
from scanamp_audit.errors import PredicateError
from scanamp_audit.expr.parser import parse_predicate
from scanamp_audit.expr.validate import analyze_predicate, validate_predicate

TYPES = {"a": "integer", "b": "integer", "c": "integer", "dt": "date",
         "s": "string", "f": "float"}

def test_and_binds_tighter_than_or():
    node = parse_predicate("a = 1 OR b = 2 AND c = 3")
    assert node["node"] == "or"
    assert node["children"][0]["node"] == "cmp"
    assert node["children"][1]["node"] == "and"

def test_parens_override_precedence():
    node = parse_predicate("(a = 1 OR b = 2) AND c = 3")
    assert node["node"] == "and" and node["children"][0]["node"] == "or"

def test_not_in_and_between():
    assert parse_predicate("NOT (a = 1)")["node"] == "not"
    node = parse_predicate("a IN (1, 2, 3) AND b NOT BETWEEN 4 AND 7")
    assert node["children"][0]["negated"] is False
    assert node["children"][1]["negated"] is True

def test_is_null_and_function():
    node = parse_predicate("s IS NOT NULL OR substr(dt, 1, 7) = '2024-01'")
    assert node["children"][0]["negated"] is True
    function = node["children"][1]["left"]
    assert function["kind"] == "function" and function["name"] == "substr"

def test_literal_on_left_and_unary_minus():
    node = parse_predicate("'2024-01-01' <= dt AND a > -5")
    assert node["children"][0]["left"]["kind"] == "literal"
    assert node["children"][1]["right"]["value"] == -5

def test_ast_is_json_serializable():
    node = parse_predicate("a BETWEEN 1 AND 9 AND s = 'x'")
    assert json.loads(json.dumps(node)) == node
@pytest.mark.parametrize("text", ["", "a =", "a IN ()", "(a = 1", "a = 1 b = 2",
                                  "NOT", "a IS", "a NOT 1"])
def test_invalid_syntax(text):
    with pytest.raises(PredicateError):
        parse_predicate(text, "qbad")

def test_validate_unknown_column_and_type_mismatch():
    with pytest.raises(PredicateError) as error:
        validate_predicate(parse_predicate("missing = 1"), TYPES, "q1")
    assert error.value.reason == "UNKNOWN_COLUMN"
    with pytest.raises(PredicateError) as error:
        validate_predicate(parse_predicate("a = 'not-an-int'"), TYPES, "q1")
    assert error.value.reason == "TYPE_MISMATCH"

def test_validate_dates_and_function_analysis():
    validate_predicate(parse_predicate("dt >= '2024-01-01'"), TYPES, "q1")
    with pytest.raises(PredicateError) as error:
        validate_predicate(parse_predicate("dt = '01/01/2024'"), TYPES, "q1")
    assert error.value.reason == "TYPE_MISMATCH"
    analysis = analyze_predicate(parse_predicate("substr(dt, 1, 7) = '2024-01'"),
                                 TYPES, ["dt"], "q1")
    assert analysis["function_wrapped_partition_columns"] == [
        {"column": "dt", "function": "substr"}]
    assert analysis["undecidable_reasons"]
