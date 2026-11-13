"""bash ``printf``'s numeric conversions, as C prints them, for the lifecycle guard's printf model.

bash parses a number with ``strtoimax``/``strtoumax`` (``%d``/``%u`` …) or ``strtold`` (``%f`` …)
and hands it to the C library's ``printf``, so the digits it prints follow C, not Python:
``%#x`` of 0 is ``0`` (Python: ``0x0``), the ``0`` flag is ignored when an integer has a
precision, and the floating conversions use the platform's ``long double``. Those digits can name
a script (``printf 'bash D/%#x.sh' 0 | bash`` runs ``D/0.sh``), so the guard's walk needs them
exact: integers are written out by hand, and floats are rounded to a :class:`FloatModel` and
printed from the exact value with :class:`fractions.Fraction`.

``long double`` differs by platform: x86's 80-bit one (64-bit significand, so
``printf %.0f 18446744073709551615`` prints that number exactly and ``1e400`` is finite, and
glibc's ``%a`` prints ``0x8p-3`` for 1), the IEEE double of Apple Silicon and the IEEE quad of
Linux on arm64. Callers walk the output under every model (FLOAT_MODELS), so the host's is always
among them; only the x86 one is checked against real bash here.
"""

import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import NamedTuple, Optional

_INT64 = 1 << 63
_UINT64 = 1 << 64
_INTEGER_PREFIX_RE = re.compile(r"\s*([-+]?)(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)?")
_FLOAT_PREFIX_RE = re.compile(
    r"\s*([-+]?)(inf(?:inity)?|nan|0[xX](?:[0-9a-fA-F]+\.?[0-9a-fA-F]*|\.[0-9a-fA-F]+)"
    r"(?:[pP][-+]?[0-9]+)?|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)",
    re.IGNORECASE,
)
_HEX_FLOAT_RE = re.compile(r"0x([0-9a-f]*)\.?([0-9a-f]*)(?:p([-+]?[0-9]+))?")
# Past these binary/decimal magnitudes every model is inf or 0; checked before building a
# Fraction, so `1e999999999` cannot allocate a huge integer.
_BINARY_EXP_LIMIT = 16500
_DECIMAL_EXP_LIMIT = 5000
# Past 20 decimal digits a number is beyond 2**64, where strtoimax/strtoumax saturate and every
# width, precision and exponent caller caps; Python's int() refuses a decimal string past 4300
# digits (ValueError), which would abort the guard's whole walk, so longer ones saturate here.
_SATURATED_DIGITS = 20
_DECIMAL_EXPONENT_DIGITS = 12
_DECIMAL_EXPONENT_RE = re.compile(r"(.*?)[eE]([-+]?)0*([0-9]+)")


class FloatModel(NamedTuple):
    """A C ``long double``: significand bits (integer bit included), the normal exponent range
    and whether ``%a`` prints the significand's top nibble first (glibc x86: ``0x8p-3``) or a
    leading 1 (``0x1p+0``)."""

    bits: int
    min_exp: int
    max_exp: int
    nibble_hex: bool


X87 = FloatModel(64, -16382, 16383, True)  # x86 and x86-64
DOUBLE = FloatModel(53, -1022, 1023, False)  # Apple Silicon (long double is double)
QUAD = FloatModel(113, -16382, 16383, False)  # Linux arm64
FLOAT_MODELS = (X87, DOUBLE, QUAD)

Special = str  # "inf" or "nan"
Real = tuple[bool, Fraction | Special]  # (negative, exact value or special)


def decimal_digits(digits: str) -> int:
    """``int(digits)`` of a decimal digit string, saturated to ``10**20`` past 20 significant
    digits (see _SATURATED_DIGITS): never the ValueError of Python's int-string limit."""
    digits = digits.lstrip("0")
    return int(digits or "0") if len(digits) <= _SATURATED_DIGITS else 10**_SATURATED_DIGITS


def _signed_digits(sign: Optional[str], digits: str) -> int:
    value = decimal_digits(digits)
    return -value if sign == "-" else value


def integer(text: str) -> int:
    """``strtoimax`` of *text* as bash's printf reads a number: ``'c`` is the code of c, the
    longest numeric prefix counts (``12abc`` is 12, ``08`` is 0) and anything else is 0."""
    if text[:1] in ("'", '"'):
        return ord(text[1]) if len(text) > 1 else 0
    match = _INTEGER_PREFIX_RE.match(text)
    if not match or not match[2]:
        return 0
    digits = match[2]
    if digits[:2] in ("0x", "0X"):
        value = int(digits, 0)
    elif digits.startswith("0"):
        value = int(digits, 8)
    else:  # base 8 and 16 have no int-string limit; base 10 does
        value = decimal_digits(digits)
    return -value if match[1] == "-" else value


def _format_integer(conversion: str, flags: str, width: int, precision: Optional[int],
                    argument: str) -> str:
    """C's ``%d``/``%i``/``%o``/``%u``/``%x``/``%X``, written out rather than through Python's
    ``%``, which differs in the corners (``%#x`` of 0 is ``0`` in C, ``0x0`` in Python; C ignores
    the ``0`` flag when a precision is given; ``%+.0d`` of 0 is ``+``; ``%#.0o`` of 0 is ``0``)."""
    value = integer(argument)
    if conversion in "di":
        value = max(-_INT64, min(_INT64 - 1, value))
        sign = "-" if value < 0 else "+" if "+" in flags else " " if " " in flags else ""
        magnitude = abs(value)
    else:  # strtoumax: a negative number wraps, an out-of-range one clamps
        sign = ""
        magnitude = _UINT64 - 1 if abs(value) >= _UINT64 else value % _UINT64
    digits = "" if precision == 0 and magnitude == 0 else format(
        magnitude, {"o": "o", "x": "x", "X": "X"}.get(conversion, "d"))
    if precision is not None:
        digits = digits.rjust(precision, "0")
    prefix = ""
    if "#" in flags:
        if conversion == "o" and not digits.startswith("0"):
            digits = "0" + digits
        elif conversion in "xX" and magnitude:
            prefix = "0" + conversion
    return _justify(sign + prefix, digits, flags, width, zero_pad=precision is None)


def _justify(head: str, body: str, flags: str, width: int, *, zero_pad: bool) -> str:
    """C field padding: ``-`` pads right, ``0`` pads with zeros after the sign/prefix *head*."""
    if "-" in flags:
        return (head + body).ljust(width)
    if "0" in flags and zero_pad:
        return head + body.rjust(width - len(head), "0")
    return (head + body).rjust(width)


def _parse_real(text: str) -> Real:
    """``strtold`` of *text*, before rounding: (negative, exact value, ``inf`` or ``nan``)."""
    if text[:1] in ("'", '"'):
        return False, Fraction(ord(text[1]) if len(text) > 1 else 0)
    match = _FLOAT_PREFIX_RE.match(text)
    if not match:
        return False, Fraction(0)
    negative, word = match[1] == "-", match[2].lower()
    if word.startswith(("inf", "nan")):
        return negative, word[:3]
    hex_match = _HEX_FLOAT_RE.fullmatch(word)
    if hex_match:
        whole, fraction, exponent = hex_match.groups()
        mantissa = int(whole + fraction or "0", 16)
        if not mantissa:  # 0 at any exponent: `0x0p9999999999` must not build 2**9999999999
            return negative, Fraction(0)
        sign, exponent_digits = (exponent[:1], exponent[1:]) if exponent and exponent[0] in "+-" \
            else ("", exponent or "0")
        power = _signed_digits(sign, exponent_digits) - 4 * len(fraction)
        if abs(mantissa.bit_length() + power) > _BINARY_EXP_LIMIT:
            return negative, "inf" if power > 0 else Fraction(0)
        return negative, mantissa * Fraction(2) ** power
    exponent_match = _DECIMAL_EXPONENT_RE.fullmatch(word)
    if exponent_match and len(exponent_match[3]) > _DECIMAL_EXPONENT_DIGITS:
        # Decimal() refuses an exponent past ~10**18 (InvalidOperation, which would read as 0);
        # 10**12 is already far past _DECIMAL_EXP_LIMIT, so the value is inf or 0 either way.
        word = f"{exponent_match[1]}e{exponent_match[2]}{10**_DECIMAL_EXPONENT_DIGITS}"
    try:
        decimal = Decimal(word)
    except InvalidOperation:
        return negative, Fraction(0)
    if decimal and abs(decimal.adjusted()) > _DECIMAL_EXP_LIMIT:
        return negative, "inf" if decimal.adjusted() > 0 else Fraction(0)
    return negative, Fraction(decimal)


def _round_to(value: Fraction, model: FloatModel) -> tuple[int, int]:
    """*value* (> 0) rounded half-even to *model*: (significand, exponent of its top bit), the
    result being ``significand * 2 ** (exponent - bits + 1)``. Subnormals keep the minimum
    exponent and a smaller significand."""
    exponent = value.numerator.bit_length() - value.denominator.bit_length()
    if Fraction(2) ** exponent > value:
        exponent -= 1
    exponent = max(exponent, model.min_exp)
    significand = round(value * Fraction(2) ** (model.bits - 1 - exponent))
    if significand >> model.bits:  # rounded up to the next power of two
        significand >>= 1
        exponent += 1
    return significand, exponent


def real(text: str, model: FloatModel) -> Real:
    """``strtold`` of *text* under *model*: the exact value of the nearest representable one."""
    negative, value = _parse_real(text)
    if isinstance(value, str) or not value:
        return negative, value
    significand, exponent = _round_to(value, model)
    if exponent > model.max_exp:
        return negative, "inf"
    return negative, significand * Fraction(2) ** (exponent - model.bits + 1)


def _int_text(number: int) -> str:
    """``str(number)`` past Python's 4300-digit int-to-str limit (a long double reaches ~4950
    digits); Decimal's own conversion has no such limit."""
    return str(Decimal(number))


def _fixed(value: Fraction, precision: int, alternate: bool) -> str:
    """``%f``: *value* rounded half-even to *precision* decimals."""
    digits = _int_text(round(value * 10**precision)).rjust(precision + 1, "0")
    whole, fraction = digits[: len(digits) - precision], digits[len(digits) - precision :]
    return whole + ("." + fraction if precision or alternate else "")


def _decimal_exponent(value: Fraction, precision: int) -> tuple[int, int]:
    """(digits, exponent) with ``digits`` the *precision* + 1 significant digits of *value*
    rounded half-even, ``value ≈ digits * 10 ** (exponent - precision)``."""
    if not value:
        return 0, 0
    exponent = (value.numerator.bit_length() - value.denominator.bit_length()) * 30103 // 100000
    while Fraction(10) ** exponent > value:
        exponent -= 1
    while Fraction(10) ** (exponent + 1) <= value:
        exponent += 1
    digits = round(value * Fraction(10) ** (precision - exponent))
    if digits == 10 ** (precision + 1):
        digits, exponent = 10**precision, exponent + 1
    return digits, exponent


def _scientific(value: Fraction, precision: int, alternate: bool) -> str:
    """``%e``: one digit, *precision* decimals, then ``e±XX``."""
    digits, exponent = _decimal_exponent(value, precision)
    text = _int_text(digits).rjust(precision + 1, "0")
    mantissa = text[0] + ("." + text[1:] if precision or alternate else "")
    return f"{mantissa}e{'-' if exponent < 0 else '+'}{abs(exponent):02d}"


def _general(value: Fraction, precision: int, alternate: bool) -> str:
    """``%g``: ``%e`` or ``%f`` by the exponent, trailing zeros dropped unless ``#``."""
    significant = precision or 1
    _digits, exponent = _decimal_exponent(value, significant - 1)
    if significant > exponent >= -4:
        text = _fixed(value, significant - 1 - exponent, alternate)
        suffix = ""
    else:
        text, _e, suffix = _scientific(value, significant - 1, alternate).partition("e")
        suffix = "e" + suffix
    if not alternate and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text + suffix


def _hex_parts(value: Fraction, model: FloatModel) -> tuple[int, str, int]:
    """``%a``'s (leading digit, fraction hex digits, binary exponent) of *value* under *model*:
    glibc x86 leads with the significand's top nibble and 15 more digits; the IEEE formats lead
    with the integer bit (0 for a subnormal) and the fraction bits padded to whole digits."""
    if not value:
        return 0, "", 0
    significand, top = _round_to(value, model)
    if model.nibble_hex:
        lead_bits, exponent = model.bits - 4, top - 3
    else:
        lead_bits, exponent = model.bits - 1, top
    count = -(-lead_bits // 4)
    fraction = (significand & ((1 << lead_bits) - 1)) << (4 * count - lead_bits)
    return significand >> lead_bits, f"{fraction:0{count}x}", exponent


def _hexadecimal(value: Fraction, precision: Optional[int], alternate: bool,
                 model: FloatModel) -> str:
    """``%a`` after the ``0x``: the digits rounded half-even to *precision*, else trailing zeros
    dropped, then ``p±exponent``."""
    leading, digits, exponent = _hex_parts(value, model)
    if precision is None:
        digits = digits.rstrip("0")
    elif precision < len(digits):
        kept = leading * 16**precision + int(digits[:precision] or "0", 16)
        rest = Fraction(int(digits[precision:], 16), 16 ** (len(digits) - precision))
        kept += rest > Fraction(1, 2) or (rest == Fraction(1, 2) and kept % 2 == 1)
        if kept >> 4 * (precision + 1):  # the leading digit overflowed past f: `0x1p` and +4
            kept, exponent = 16**precision, exponent + 4
        leading = kept >> 4 * precision
        digits = f"{kept & (16**precision - 1):0{precision}x}" if precision else ""
    else:
        digits = digits.ljust(precision, "0")
    body = f"{leading:x}" + ("." + digits if digits or alternate else "")
    return f"{body}p{'-' if exponent < 0 else '+'}{abs(exponent)}"


_FLOAT_FORMATTERS = {"f": _fixed, "e": _scientific, "g": _general}


def _format_float(conversion: str, flags: str, width: int, precision: Optional[int],
                  argument: str, model: FloatModel) -> str:
    negative, value = real(argument, model)
    sign = "-" if negative else "+" if "+" in flags else " " if " " in flags else ""
    lower = conversion.lower()
    if isinstance(value, str):  # C pads inf/nan with blanks, never zeros
        text = _justify(sign, value, flags, width, zero_pad=False)
    elif lower == "a":
        text = _justify(sign + "0x", _hexadecimal(value, precision, "#" in flags, model), flags,
                        width, zero_pad=True)
    else:
        body = _FLOAT_FORMATTERS[lower](value, 6 if precision is None else precision, "#" in flags)
        text = _justify(sign, body, flags, width, zero_pad=True)
    return text.upper() if conversion.isupper() else text


def format_number(conversion: str, flags: str, width: int, precision: Optional[int],
                  argument: str, *, max_field: int, model: FloatModel = X87) -> str:
    """What bash prints for one numeric conversion of *argument* (C locale; the ``'``
    thousands flag groups nothing there), floats under *model*."""
    if precision is not None:
        precision = min(precision, max_field)  # past the cap only more leading zeros or digits
    if conversion in "diouxX":
        return _format_integer(conversion, flags, width, precision, argument)
    return _format_float(conversion, flags, width, precision, argument, model)
