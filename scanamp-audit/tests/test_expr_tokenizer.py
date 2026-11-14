import pytest
from scanamp_audit.errors import PredicateError
from scanamp_audit.expr.tokenizer import tokenize

def pairs(text):
    return [(token.kind, token.value) for token in tokenize(text) if token.kind != "EOF"]

def test_basic_operators_and_literals():
    assert pairs("a >= 10 AND b != 'x'") == [
        ("IDENT", "a"), ("OP", ">="), ("NUMBER", "10"), ("KEYWORD", "AND"),
        ("IDENT", "b"), ("OP", "!="), ("STRING", "x"),
    ]

def test_keywords_case_insensitive_and_dotted_identifiers():
    assert pairs("Not t.dt Is NuLl") == [
        ("KEYWORD", "NOT"), ("IDENT", "t.dt"), ("KEYWORD", "IS"), ("KEYWORD", "NULL")
    ]

@pytest.mark.parametrize("text,value", [("'a''b'", "a'b"), ('"a""b"', 'a"b'), ("'plain'", "plain")])
def test_string_escapes(text, value):
    assert pairs(text) == [("STRING", value)]

def test_numbers_exponent_and_minus():
    assert pairs("1 2.5 .5 1e3 -4") == [
        ("NUMBER", "1"), ("NUMBER", "2.5"), ("NUMBER", ".5"),
        ("NUMBER", "1e3"), ("MINUS", "-"), ("NUMBER", "4"),
    ]

@pytest.mark.parametrize("text,reason", [("'oops", "TOKENIZER_ERROR"), ("a @ 1", "TOKENIZER_ERROR")])
def test_tokenizer_errors(text, reason):
    with pytest.raises(PredicateError) as error:
        tokenize(text)
    assert error.value.reason == reason
    assert "position" in error.value.details

def test_eof_position():
    tokens = tokenize("a = 1")
    assert tokens[-1].kind == "EOF" and tokens[-1].pos == 5
