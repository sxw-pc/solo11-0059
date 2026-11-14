"""Tokenizer for the supported WHERE subset."""
from __future__ import annotations
import re
from dataclasses import dataclass
from ..errors import PredicateError

KEYWORDS = {"AND", "OR", "NOT", "IN", "IS", "NULL", "BETWEEN", "TRUE", "FALSE"}
_NUMBER_RE = re.compile(r"(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_OPERATORS = ("!=", "<>", ">=", "<=", "=", ">", "<")

@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    pos: int
    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "value": self.value, "pos": self.pos}
def _error(text: str, pos: int, message: str) -> PredicateError:
    start = max(0, pos - 12)
    end = min(len(text), pos + 12)
    return PredicateError(
        message,
        field="where",
        reason="TOKENIZER_ERROR",
        details={"position": pos, "excerpt": text[start:end]},
    )
def tokenize(text: str) -> list[Token]:
    if not isinstance(text, str):
        raise PredicateError(
            "predicate must be a string",
            field="where",
            reason="TYPE_ERROR",
            details={"actual": type(text).__name__},
        )
    tokens: list[Token] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            start = index
            index += 1
            chunks: list[str] = []
            while index < length:
                current = text[index]
                if current == quote:
                    if index + 1 < length and text[index + 1] == quote:
                        chunks.append(quote)
                        index += 2
                        continue
                    index += 1
                    tokens.append(Token("STRING", "".join(chunks), start))
                    break
                chunks.append(current)
                index += 1
            else:
                raise _error(text, start, "unterminated string literal")
            continue
        match = _NUMBER_RE.match(text, index)
        if match:
            tokens.append(Token("NUMBER", match.group(0), index))
            index = match.end()
            continue
        match = _IDENT_RE.match(text, index)
        if match:
            raw = match.group(0)
            upper = raw.upper()
            if upper in KEYWORDS:
                tokens.append(Token("KEYWORD", upper, index))
            else:
                tokens.append(Token("IDENT", raw, index))
            index = match.end()
            continue
        if char == "(":
            tokens.append(Token("LPAREN", char, index))
            index += 1
            continue
        if char == ")":
            tokens.append(Token("RPAREN", char, index))
            index += 1
            continue
        if char == ",":
            tokens.append(Token("COMMA", char, index))
            index += 1
            continue
        if text.startswith(("!=", "<>", ">=", "<="), index):
            tokens.append(Token("OP", text[index : index + 2], index))
            index += 2
            continue
        if char in "=<>":
            tokens.append(Token("OP", char, index))
            index += 1
            continue
        if char == "-":
            tokens.append(Token("MINUS", char, index))
            index += 1
            continue
        raise _error(text, index, f"unexpected character {char!r}")
    tokens.append(Token("EOF", "", length))
    return tokens
