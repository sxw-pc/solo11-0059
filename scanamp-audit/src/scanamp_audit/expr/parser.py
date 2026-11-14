"""Recursive-descent parser and schema validator for WHERE predicates."""
from __future__ import annotations
from typing import Any
from ..errors import PredicateError
from .tokenizer import Token, tokenize

_CMP_OPS = {"=", "!=", ">", ">=", "<", "<="}
def _error(message: str, token: Token, query_id: str | None, reason: str) -> PredicateError:
    location = f"query:{query_id}" if query_id else "where"
    return PredicateError(
        message,
        location=location,
        field="where",
        reason=reason,
        details={"position": token.pos, "token": token.value},
    )
def _literal(value: Any, literal_type: str) -> dict[str, Any]:
    return {"kind": "literal", "value": value, "literal_type": literal_type}

class Parser:
    def __init__(self, text: str, query_id: str | None = None) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.index = 0
        self.query_id = query_id
    @property
    def current(self) -> Token:
        return self.tokens[self.index]
    def advance(self) -> Token:
        token = self.current
        self.index += 1
        return token
    def expect(self, kind: str, message: str) -> Token:
        if self.current.kind != kind:
            raise _error(message, self.current, self.query_id, "UNEXPECTED_TOKEN")
        return self.advance()
    def expect_keyword(self, keyword: str) -> Token:
        if self.current.kind != "KEYWORD" or self.current.value != keyword:
            raise _error(
                f"expected {keyword}", self.current, self.query_id, "UNEXPECTED_TOKEN"
            )
        return self.advance()
    def parse(self) -> dict[str, Any]:
        if self.current.kind == "EOF":
            raise _error(
                "predicate is empty", self.current, self.query_id, "EMPTY_PREDICATE"
            )
        node = self.parse_or()
        if self.current.kind != "EOF":
            raise _error(
                f"unexpected token {self.current.value!r}",
                self.current,
                self.query_id,
                "TRAILING_TOKEN",
            )
        return node
    def parse_or(self) -> dict[str, Any]:
        parts = [self.parse_and()]
        while self.current.kind == "KEYWORD" and self.current.value == "OR":
            self.advance()
            parts.append(self.parse_and())
        return _combine("or", parts)
    def parse_and(self) -> dict[str, Any]:
        parts = [self.parse_not()]
        while self.current.kind == "KEYWORD" and self.current.value == "AND":
            self.advance()
            parts.append(self.parse_not())
        return _combine("and", parts)
    def parse_not(self) -> dict[str, Any]:
        if self.current.kind == "KEYWORD" and self.current.value == "NOT":
            self.advance()
            return {"node": "not", "child": self.parse_not()}
        return self.parse_primary()
    def parse_primary(self) -> dict[str, Any]:
        if self.current.kind == "LPAREN":
            self.advance()
            node = self.parse_or()
            self.expect("RPAREN", "expected ')'")
            return node
        return self.parse_predicate()
    def parse_predicate(self) -> dict[str, Any]:
        left = self.parse_operand()
        token = self.current
        if token.kind == "KEYWORD" and token.value == "IS":
            self.advance()
            negated = False
            if self.current.kind == "KEYWORD" and self.current.value == "NOT":
                self.advance()
                negated = True
            self.expect_keyword("NULL")
            return {"node": "is_null", "operand": left, "negated": negated}
        if token.kind == "KEYWORD" and token.value == "NOT":
            self.advance()
            if self.current.kind == "KEYWORD" and self.current.value == "IN":
                self.advance()
                return self._parse_in(left, negated=True)
            if self.current.kind == "KEYWORD" and self.current.value == "BETWEEN":
                self.advance()
                return self._parse_between(left, negated=True)
            raise _error(
                "expected IN or BETWEEN after NOT",
                self.current,
                self.query_id,
                "UNEXPECTED_TOKEN",
            )
        if token.kind == "KEYWORD" and token.value == "IN":
            self.advance()
            return self._parse_in(left, negated=False)
        if token.kind == "KEYWORD" and token.value == "BETWEEN":
            self.advance()
            return self._parse_between(left, negated=False)
        if token.kind == "OP":
            op = self.advance().value
            if op == "<>":
                op = "!="
            right = self.parse_operand()
            return {"node": "cmp", "op": op, "left": left, "right": right}
        raise _error(
            "expected a comparison operator",
            self.current,
            self.query_id,
            "MISSING_OPERATOR",
        )
    def _literal_operand(self, role: str) -> dict[str, Any]:
        operand = self.parse_operand(allow_column=False)
        if operand.get("kind") != "literal" or operand.get("literal_type") == "null":
            raise _error(
                f"{role} must be a non-NULL literal",
                self.current,
                self.query_id,
                "EXPECTED_LITERAL",
            )
        return operand
    def _parse_in(self, left: dict[str, Any], negated: bool) -> dict[str, Any]:
        self.expect("LPAREN", "expected '(' after IN")
        if self.current.kind == "RPAREN":
            raise _error(
                "IN list must not be empty",
                self.current,
                self.query_id,
                "EMPTY_IN_LIST",
            )
        values = [self._literal_operand("IN value")]
        while self.current.kind == "COMMA":
            self.advance()
            values.append(self._literal_operand("IN value"))
        self.expect("RPAREN", "expected ')' after IN list")
        return {"node": "in", "operand": left, "values": values, "negated": negated}
    def _parse_between(self, left: dict[str, Any], negated: bool) -> dict[str, Any]:
        low = self._literal_operand("BETWEEN lower bound")
        self.expect_keyword("AND")
        high = self._literal_operand("BETWEEN upper bound")
        return {
            "node": "between",
            "operand": left,
            "low": low,
            "high": high,
            "negated": negated,
        }
    def parse_operand(self, *, allow_column: bool = True) -> dict[str, Any]:
        token = self.current
        if token.kind == "MINUS":
            self.advance()
            number = self.expect("NUMBER", "expected a number after '-'")
            value = _number_value(number.value)
            return _literal(-value, "float" if isinstance(value, float) else "integer")
        if token.kind == "NUMBER":
            self.advance()
            value = _number_value(token.value)
            return _literal(value, "float" if isinstance(value, float) else "integer")
        if token.kind == "STRING":
            self.advance()
            return _literal(token.value, "string")
        if token.kind == "KEYWORD" and token.value in ("TRUE", "FALSE"):
            self.advance()
            return _literal(token.value == "TRUE", "boolean")
        if token.kind == "KEYWORD" and token.value == "NULL":
            self.advance()
            return _literal(None, "null")
        if token.kind == "IDENT":
            if not allow_column:
                raise _error(
                    "expected a literal", token, self.query_id, "EXPECTED_LITERAL"
                )
            name = self.advance().value
            if self.current.kind == "LPAREN":
                return self._parse_function(name)
            return {"kind": "column", "name": name}
        raise _error(
            f"unexpected token {token.value!r}", token, self.query_id, "UNEXPECTED_TOKEN"
        )
    def _parse_function(self, name: str) -> dict[str, Any]:
        self.expect("LPAREN", "expected '(' after function name")
        args: list[dict[str, Any]] = []
        if self.current.kind != "RPAREN":
            args.append(self.parse_operand())
            while self.current.kind == "COMMA":
                self.advance()
                args.append(self.parse_operand())
        self.expect("RPAREN", "expected ')' after function arguments")
        return {"kind": "function", "name": name.lower(), "args": args}
def _number_value(raw: str) -> int | float:
    if any(char in raw for char in ".eE"):
        return float(raw)
    return int(raw, 10)
def _combine(kind: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
    if len(parts) == 1:
        return parts[0]
    children: list[dict[str, Any]] = []
    for part in parts:
        if part.get("node") == kind:
            children.extend(part["children"])
        else:
            children.append(part)
    return {"node": kind, "children": children}
def parse_predicate(text: str, query_id: str | None = None) -> dict[str, Any]:
    return Parser(text, query_id).parse()
