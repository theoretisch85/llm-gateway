from __future__ import annotations

import re
from decimal import Decimal, DivisionByZero, InvalidOperation


_ALLOWED_EXPRESSION_RE = re.compile(r"^[0-9+\-*/().,%\s]+$")
_TOKEN_RE = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([()+\-*/%]))")


class MathExpressionError(ValueError):
    pass


def calculate_expression(expression: str) -> dict[str, int | float | str]:
    normalized = (expression or "").strip().replace(",", ".")
    if not normalized:
        raise MathExpressionError("Leerer Ausdruck.")
    if len(normalized) > 240:
        raise MathExpressionError("Ausdruck ist zu lang.")
    if not _ALLOWED_EXPRESSION_RE.fullmatch(normalized):
        raise MathExpressionError("Ausdruck enthaelt ungueltige Zeichen.")

    parser = _MathParser(normalized)
    result = parser.parse()
    return {
        "expression": normalized,
        "result": _normalize_number(result),
    }


def _normalize_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


class _MathParser:
    def __init__(self, text: str) -> None:
        self._text = text
        self._tokens = self._tokenize(text)
        self._index = 0

    def parse(self) -> Decimal:
        try:
            value = self._parse_expression()
            if not self._is_end():
                raise MathExpressionError("Ungueltiger Ausdruck.")
            return value
        except DivisionByZero as exc:
            raise MathExpressionError("Division durch 0 ist nicht erlaubt.") from exc
        except InvalidOperation as exc:
            raise MathExpressionError("Ungueltiger numerischer Ausdruck.") from exc

    def _tokenize(self, text: str) -> list[tuple[str, Decimal | str]]:
        tokens: list[tuple[str, Decimal | str]] = []
        index = 0
        while index < len(text):
            match = _TOKEN_RE.match(text, index)
            if not match:
                raise MathExpressionError("Ungueltiges Zeichen im Ausdruck.")
            number_token = match.group(1)
            symbol_token = match.group(2)
            if number_token is not None:
                tokens.append(("number", Decimal(number_token)))
            elif symbol_token is not None:
                tokens.append((symbol_token, symbol_token))
            else:
                raise MathExpressionError("Ungueltiger Ausdruck.")
            index = match.end()
        return tokens

    def _is_end(self) -> bool:
        return self._index >= len(self._tokens)

    def _peek(self) -> tuple[str, Decimal | str] | None:
        if self._is_end():
            return None
        return self._tokens[self._index]

    def _accept(self, token_type: str) -> bool:
        current = self._peek()
        if current is None or current[0] != token_type:
            return False
        self._index += 1
        return True

    def _expect(self, token_type: str) -> None:
        if not self._accept(token_type):
            raise MathExpressionError("Ungueltiger Ausdruck.")

    def _parse_expression(self) -> Decimal:
        value = self._parse_term()
        while True:
            if self._accept("+"):
                value += self._parse_term()
                continue
            if self._accept("-"):
                value -= self._parse_term()
                continue
            return value

    def _parse_term(self) -> Decimal:
        value = self._parse_unary()
        while True:
            if self._accept("*"):
                value *= self._parse_unary()
                continue
            if self._accept("/"):
                divisor = self._parse_unary()
                if divisor == 0:
                    raise MathExpressionError("Division durch 0 ist nicht erlaubt.")
                value /= divisor
                continue
            return value

    def _parse_unary(self) -> Decimal:
        if self._accept("+"):
            return self._parse_unary()
        if self._accept("-"):
            return -self._parse_unary()
        return self._parse_percent_value()

    def _parse_percent_value(self) -> Decimal:
        value = self._parse_primary()
        while self._accept("%"):
            value /= Decimal("100")
        return value

    def _parse_primary(self) -> Decimal:
        if self._accept("("):
            value = self._parse_expression()
            self._expect(")")
            return value

        current = self._peek()
        if current is None or current[0] != "number":
            raise MathExpressionError("Ungueltiger Ausdruck.")
        self._index += 1
        number = current[1]
        if not isinstance(number, Decimal):
            raise MathExpressionError("Ungueltiger Ausdruck.")
        return number
