"""Pure answer checks. Citation membership does not prove semantic truth.

Private values and source texts must come from this exchange's observations.
Injection matches are risk indicators, including when attacks are quoted.
Call prepare_answer before emitting ANSWER; the arena does not call it for you.
"""
from __future__ import annotations

import ast
import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Mapping
from kit.world.anchor import Anchor, AnchorSyntaxError


@dataclass(frozen=True, slots=True)
class GroundingResult:
    grounded: bool
    cited: tuple[str, ...]
    ungrounded: tuple[str, ...]
    malformed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InjectionScanResult:
    suspicious: bool
    matched_patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RedactionResult:
    redacted_text: str
    hits: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArithmeticCheckResult:
    checked: bool
    ok: bool | None
    detail: str


def check_grounding(
    answer: Mapping[str, Any],
    retrieved_anchors: Iterable[str],
    *,
    require_citation: bool = True,
) -> GroundingResult:
    raw = answer.get("cited_anchors", ())
    if not isinstance(raw, (list, tuple)):
        return GroundingResult(
            False, (), (), ("cited_anchors must be a list or tuple",)
        )
    cited = tuple(raw)
    returned = set()
    for value in retrieved_anchors:
        try:
            returned.add(Anchor.parse(value).key())
        except (AnchorSyntaxError, TypeError, AttributeError):
            continue

    without_rev = {(ns, slug, idx) for ns, slug, rev, idx in returned}
    bad, missing = [], []

    for value in cited:
        if not isinstance(value, str):
            bad.append(str(value))
            continue
        try:
            anchor = Anchor.parse(value)
        except AnchorSyntaxError:
            bad.append(value)
            continue

        present = (
            anchor.key() in returned if anchor.rev is not None else
            (anchor.ns, anchor.slug, anchor.idx) in without_rev
        )
        if not present:
            missing.append(value)

    return GroundingResult(
        not bad and not missing and (bool(cited) or not require_citation),
        cited,
        tuple(missing),
        tuple(bad),
    )


def scan_for_injected_instructions(text: str) -> InjectionScanResult:
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = folded.replace("đ", "d")
    folded = "".join(
        c for c in folded if unicodedata.category(c) != "Cf"
    )

    patterns = (
        (
            "ignore_instructions",
            r"\b(?:ignore|disregard|override)\b.{0,80}"
            r"\b(?:instructions?|system prompt|rules?)\b",
        ),
        (
            "fake_system",
            r"\b(?:system override|new system prompt|as the system)\b"
            r"|<\|(?:im_start|system)\|>|\[system\]",
        ),
        (
            "exfiltrate",
            r"\b(?:reveal|print|send|exfiltrate|disclose)\b.{0,100}"
            r"\b(?:api[_ ]?key|password|secret|private (?:note|data)"
            r"|ctx\.(?:act|scopes))\b",
        ),
        (
            "disable_checks",
            r"\b(?:disable|bypass|skip)\b.{0,60}"
            r"\b(?:guardrails?|gateway|safety checks?|verification)\b",
        ),
        (
            "vn_override",
            r"\b(?:bo qua|phot lo|ghi de|khong tuan theo)\b.{0,80}"
            r"\b(?:chi dan|huong dan|quy tac|lenh|he thong)\b",
        ),
        (
            "vn_private",
            r"\b(?:tiet lo|in ra|gui|hien thi)\b.{0,80}"
            r"\b(?:mat khau|khoa api|du lieu rieng tu"
            r"|ghi chu rieng tu)\b",
        ),
    )

    hits = tuple(
        name for name, pattern in patterns
        if re.search(pattern, folded, re.S)
    )
    return InjectionScanResult(bool(hits), hits)


def redact(
    text: str, private_values: Iterable[str] = (),
) -> RedactionResult:
    """Remove secrets; suppress text containing known private excerpts.

    hits contain categories only, never the protected values themselves.
    """
    normalized = " ".join(text.split()).casefold()

    for value in private_values:
        if not isinstance(value, str) or not value.strip():
            continue
        private = " ".join(value.split()).casefold()
        if private in normalized or (
            len(private) >= 40 and any(
                private[i:i + 40] in normalized
                for i in range(len(private) - 39)
            )
        ):
            return RedactionResult(
                "[REDACTED: private content]", ("private_content",)
            )

    hits = []
    result = text
    patterns = (
        (
            "credential",
            r"\b(?:sk-[A-Za-z0-9_-]{12,}"
            r"|gh[pousr]_[A-Za-z0-9]{12,})\b",
        ),
        (
            "credential_assignment",
            r"\b(?:[A-Z][A-Z0-9_]*API_KEY|password|secret|access_token)"
            r"\s*[:=]\s*[\"']?[^\s,\"';]+",
        ),
        (
            "authorization",
            r"\bBearer\s+[A-Za-z0-9._~-]{12,}",
        ),
        (
            "email",
            r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        ),
    )

    for name, pattern in patterns:
        result, count = re.subn(
            pattern, "[REDACTED]", result, flags=re.I
        )
        if count:
            hits.append(name)

    return RedactionResult(result, tuple(hits))


_NUMBER = r"\d+(?:,\d{3})*(?:\.\d+)?"
_NUMBER_RE = re.compile(
    r"(?<![\w.-])-?" + _NUMBER
    + r"(?![\d.]|,\d)(?=[^\w]|[kKmMbB]\b|$)"
)
_EQUATION_RE = re.compile(
    r"(?<![\w.])(?P<lhs>-?\d[\d\s.,()+*/-]{0,100})"
    r"\s*=\s*(?P<rhs>-?" + _NUMBER + r")(?![\w.]|,\d)"
)


def _calculate(expression: str) -> Fraction:
    expression = expression.strip().replace(",", "")
    tree = ast.parse(expression, mode="eval")
    if (
        len(expression) > 128
        or sum(1 for _ in ast.walk(tree)) > 32
    ):
        raise ValueError("expression too complex")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)

        if (
            isinstance(node, ast.Constant)
            and type(node.value) in (int, float)
        ):
            segment = ast.get_source_segment(expression, node)
            if (
                not segment
                or not re.fullmatch(r"\d+(?:\.\d+)?", segment)
            ):
                raise ValueError("unsupported number")
            return Fraction(segment)

        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, (ast.UAdd, ast.USub))
        ):
            value = visit(node.operand)
            return -value if isinstance(node.op, ast.USub) else value

        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right

        raise ValueError(
            "only decimal numbers and + - * / are supported"
        )

    return visit(tree)


def verify_arithmetic(
    text: str,
    source_texts: Iterable[str] | None = None,
) -> ArithmeticCheckResult:
    """Check equations, numeric precision and explicit currency/unit tokens.

    This is a mechanical support check, not a semantic truth oracle.
    """
    def remove_identifiers(value):
        # Frame indexes and learner IDs inside citations are identifiers,
        # not quantitative assertions. Validate syntax before excluding them.
        pattern = r"\b[A-Z][A-Za-z]*:[A-Za-z0-9_/-]+"
        def replace(match):
            try:
                Anchor.parse(match.group())
            except AnchorSyntaxError:
                return match.group()
            return " "
        return re.sub(pattern, replace, value)
    text = remove_identifiers(text)
    if source_texts is not None:
        source_texts = tuple(remove_identifiers(s) for s in source_texts if isinstance(s,str))
    equations, derived = 0, set()

    for match in _EQUATION_RE.finditer(text):
        equations += 1
        try:
            actual = _calculate(match["lhs"])
            claimed = Fraction(match["rhs"].replace(",", ""))
        except (
            SyntaxError, ValueError, ZeroDivisionError, OverflowError
        ):
            return ArithmeticCheckResult(
                True, False, "Unsupported arithmetic expression"
            )

        if actual != claimed:
            return ArithmeticCheckResult(
                True, False, "An explicit equation is incorrect"
            )
        derived.add(match["rhs"].replace(",", ""))

    if source_texts is not None:
        supported = {
            m.group().replace(",", "")
            for source in source_texts if isinstance(source, str)
            for m in _NUMBER_RE.finditer(source)
        }
        unsupported = [
            m.group().replace(",", "")
            for m in _NUMBER_RE.finditer(text)
            if m.group().replace(",", "") not in supported | derived
        ]

        if unsupported:
            return ArithmeticCheckResult(
                True,
                False,
                "Unsupported numbers: "
                + ", ".join(dict.fromkeys(unsupported)),
            )

        unit_pattern = re.compile(
            r"(?<![\w.])(?:[$€£]\s*)?-?" + _NUMBER
            + r"\s*(?:[kKmMbB]\b|%|percent\b|milliseconds?\b|seconds?\b|minutes?\b|hours?\b|GB\b|MB\b|KB\b)", re.I
        )
        supported_units = {"".join(m.group().split()).casefold()
                           for s in source_texts if isinstance(s, str)
                           for m in unit_pattern.finditer(s)}
        if any("".join(m.group().split()).casefold() not in supported_units
               for m in unit_pattern.finditer(text)):
            return ArithmeticCheckResult(True, False, "Unsupported numeric unit or scale")

        return ArithmeticCheckResult(
            True,
            True,
            "Numeric source membership and explicit equations checked; "
            "meaning not checked",
        )

    if equations:
        return ArithmeticCheckResult(
            True,
            True,
            "Explicit equations checked; source support not checked",
        )

    return ArithmeticCheckResult(
        False,
        None,
        "No explicit equation or source texts supplied",
    )


def abstention_policy(grounding: GroundingResult, *, incomplete=False,
                      missing_required=False, unresolved_conflict=False) -> bool:
    """Abstain only for a concrete evidence or contract failure."""
    return (not grounding.grounded or incomplete or missing_required
            or unresolved_conflict)


def prepare_answer(
    answer: Mapping[str, Any],
    retrieved_anchors: Iterable[str],
    *,
    source_texts: Iterable[str],
    private_values: Iterable[str] = (),
    require_citation: bool = True,
    required_fields: Iterable[str] = (),
    source_rows: Iterable[Mapping[str, Any]] = (),
    incomplete: bool = False,
    conflicts: Iterable[str] = (),
) -> tuple[dict, tuple[str, ...]]:
    """Return a fresh answer plus rejection reasons; preserve input.

    Rejected answers retain only text and citations, so unsafe structured
    fields cannot survive in an abstention answer.
    """
    retrieved_anchors = tuple(retrieved_anchors)
    if (
        not isinstance(answer.get("text"), str)
        or not answer["text"].strip()
    ):
        return (
            {
                "text": "Insufficient verified evidence.",
                "cited_anchors": [],
            },
            ("answer_shape",),
        )

    ground = check_grounding(
        answer,
        retrieved_anchors,
        require_citation=require_citation,
    )
    scan = scan_for_injected_instructions(answer["text"])
    private_values = tuple(private_values)
    redaction = redact(answer["text"], private_values)
    math = verify_arithmetic(answer["text"], source_texts)

    reasons = []
    source_rows = tuple(source_rows)
    required_fields = tuple(required_fields)
    numeric_fields = {"course_day", "count", "content_frames", "delta"}
    for field in required_fields:
        value = answer.get(field)
        if value is None or value == "" or value == []:
            reasons.append("required_field:" + field)
        elif field in numeric_fields and (type(value) is not int or value < 0):
            reasons.append("field_type:" + field)
        elif field in ("anchors",) and (not isinstance(value, list) or not all(isinstance(a, str) for a in value)):
            reasons.append("field_type:" + field)
        elif field not in numeric_fields | {"anchors"} and not isinstance(value, str):
            reasons.append("field_type:" + field)
    for field in numeric_fields | {"track", "fresher"}:
        if field not in answer:
            continue
        values = [r[field] for r in source_rows if field in r]
        if not values or answer[field] not in values or any(v != values[0] for v in values):
            reasons.append("structured_support:" + field)
    for field in ("anchor", "w_anchor", "c_anchor", "talk", "a", "b"):
        value = answer.get(field)
        if isinstance(value, str) and ":" in value:
            if not check_grounding({"cited_anchors": [value]}, retrieved_anchors).grounded:
                reasons.append("structured_citation:" + field)
    if incomplete:
        reasons.append("partial_without_continuation")
    if tuple(conflicts):
        # A contradiction ASK can safely present both exact, observed sides.
        explicit_pair = any(
            r.get("a") == answer.get("a") and r.get("b") == answer.get("b")
            and r.get("a") is not None and r.get("b") is not None
            for r in source_rows
        )
        caveat = re.search(r"conflict|contradict|disagree|mâu thuẫn|khác nhau", answer["text"], re.I)
        if not (explicit_pair and caveat):
            reasons.append("unresolved_conflict")
    if abstention_policy(ground):
        reasons.append("citation")
    if scan.suspicious:
        reasons.append("injection")
    if redaction.hits:
        reasons.append("privacy")
    if math.ok is False:
        reasons.append("numeric_support")

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            for key, item in value.items():
                yield from strings(key)
                yield from strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from strings(item)

    if any(
        redact(value, private_values).hits
        for value in strings(answer)
    ):
        if "privacy" not in reasons:
            reasons.append("privacy")

    if reasons:
        return (
            {
                "text": "Insufficient verified evidence to answer safely.",
                "cited_anchors": [],
            },
            tuple(reasons),
        )

    return dict(answer), ()
