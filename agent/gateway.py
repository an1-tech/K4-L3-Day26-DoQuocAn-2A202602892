"""Pure Day26 gateway controls using arena-owned context.
Never executes tools. Result grounding and post-response token
validation belong to the answer layer.
"""

from __future__ import annotations
try:
    from kit.mcp.specs import TOOL_SPECS as _POLICY_SPECS, A2A_PEERS as _POLICY_PEERS, cost as _policy_cost
    from kit.mcp.a2a import AGENT_CARDS as _POLICY_CARDS, SKILL_ROUTES as _POLICY_SKILLS, admit_skill as _policy_admit, verify_card as _policy_verify_card
    from kit.world.anchor import Anchor as _PolicyAnchor, AnchorSyntaxError as _PolicyAnchorError
    from agent.strategy import successor_of as _policy_successor, cheap_mask as _policy_mask
    _POLICY_AVAILABLE = True
except ImportError:
    _POLICY_SPECS, _POLICY_CARDS, _POLICY_SKILLS = {}, {}, {}
    _POLICY_PEERS = frozenset()
    _POLICY_AVAILABLE = False

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

from agent.telemetry import RecordingGatewayContext, Telemetry

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """Pure admission, authorization, lease, write and budget controls."""

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)
        self._denied_cmd_ids = set()
        self._credits_authorised = 0
        self._starting_credits = ctx.credits
        self._write_keys = set()
        self._etags = {}
        self._history_seen = 0
        self._peer_cards = dict(_POLICY_CARDS)
        self._rate_calls = {}
        self._round_spend = {}
        self._blocked_requests = set()

    @staticmethod
    def _request_key(server, tool, args):
        # Pure, stable identity for retry discipline, excluding pagination.
        return (server, tool, tuple(sorted(
            (str(k), repr(v)) for k, v in args.items()
            if k not in ("continuation", "cursor")
        )))

    @staticmethod
    def _learner_id(value):
        if not isinstance(value, str):
            return None
        value = value.strip()
        if value.lower().startswith("learner:"):
            value = "Learner:" + value.split(":", 1)[1]
        elif value.startswith("sv-"):
            value = "Learner:" + value
        else:
            return None
        try:
            return _PolicyAnchor.parse(value).slug
        except (_PolicyAnchorError, TypeError, ValueError):
            return None

    def deny(self, cmd: Command, reason: str) -> Decision:
        self._denied_cmd_ids.add(cmd.cmd_id)
        decision = Decision(verdict="deny", reason=reason)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def note_provenance(self, anchor, etag):
        if isinstance(anchor, str) and isinstance(etag, str) and etag:
            self._etags[anchor] = etag

    def note_result(self, anchor, etag):
        self.note_provenance(anchor, etag)

    def note_card(self, server, card):
        if not _POLICY_AVAILABLE or server not in _POLICY_PEERS:
            return
        if isinstance(card, Mapping) and "name" not in card:
            if card.get("verified") is False:
                self._peer_cards.pop(server, None)
            return
        admission = _policy_verify_card(card)
        if admission.admitted and admission.peer == server:
            self._peer_cards[server] = card
        else:
            self._peer_cards.pop(server, None)

    def _sync_history(self):
        history = self.ctx.history or ()
        if len(history) < self._history_seen:
            self._history_seen = 0

        for item in history[self._history_seen:]:
            if not isinstance(item, Mapping):
                continue

            command = item.get("command", item.get("cmd"))
            result = item.get("outcome", item.get("result"))

            if isinstance(command, Command):
                command = command.to_dict()
            if not isinstance(command, Mapping):
                continue
            if not isinstance(result, Mapping):
                continue
            if result.get("ok") is not True:
                error = result.get("error") or {}
                code = error.get("code", result.get("error_code"))
                if code in ("precondition_failed", "conflict", "etag_mismatch"):
                    self._etags.pop((command.get("args") or {}).get("anchor"), None)
                if code in ("bad_request", "unauthorized", "not_found"):
                    self._blocked_requests.add(self._request_key(
                        command.get("server"), command.get("tool"), command.get("args") or {}
                    ))
                continue

            if (
                command.get("server") == "registry"
                and command.get("tool") == "provenance"
            ):
                anchor = (command.get("args") or {}).get("anchor")
                if anchor not in (result.get("anchors") or ()):
                    continue
                etag = result.get("etag")
                if not etag:
                    rows = result.get("rows") or ()
                    if rows and isinstance(rows[0], Mapping):
                        etag = rows[0].get("etag")
                self.note_provenance(anchor, etag)

            elif command.get("server") in _POLICY_PEERS:
                for row in result.get("rows") or ():
                    if isinstance(row, Mapping) and "agent_card" in row:
                        self.note_card(
                            command["server"], row["agent_card"]
                        )

        self._history_seen = len(history)

    def decide(self, cmd: Command) -> Decision:
        self._telemetry.decision_seen(cmd)

        if not _POLICY_AVAILABLE or not _TOOLCALL_AVAILABLE:
            return self.deny(cmd, "policy dependencies unavailable")

        self._sync_history()
        original_key = (cmd.server, cmd.tool)

        if original_key not in _POLICY_SPECS:
            return self.deny(cmd, "unknown or unpriced tool")

        args = dict(cmd.args)
        headers = {}

        for key, value in cmd.headers.items():
            if not isinstance(key, str):
                return self.deny(cmd, "header names must be strings")
            lower = key.lower()
            if lower in headers and headers[lower] != value:
                return self.deny(cmd, "conflicting header names")
            headers[lower] = value

        if any(args.get(key) for key in ("route", "_route", "replica")):
            return self.deny(
                cmd, "route must be declared in the header"
            )

        if headers.get("mcp-replica") not in (None, "w", "c"):
            return self.deny(cmd, "invalid replica header")

        server, tool = (
            _policy_successor(*original_key) or original_key
        )
        spec = _POLICY_SPECS[(server, tool)]

        # ADMIT: reject malformed requests before spending a tool attempt.
        required = {
            ("slides", "get_frame"): "anchor",
            ("slides", "whatlinkshere"): "anchor",
            ("registry", "provenance"): "anchor",
            ("content", "flag_stale_slide"): "anchor",
            ("labs", "get_exercise"): "anchor",
            ("glossary", "define"): "term",
            ("curriculum-analyst", "which_days_cover"): "concept",
            ("roster", "lookup_learner"): "learner",
        }.get((server, tool))
        if required and (not isinstance(args.get(required), str) or not args[required].strip()):
            return self.deny(cmd, f"missing non-empty {required}")
        for name in ("anchor", "concept"):
            if name in args:
                try:
                    parsed = _PolicyAnchor.parse(args[name])
                except (_PolicyAnchorError, TypeError, AttributeError):
                    return self.deny(cmd, f"invalid {name} anchor")
                if name == "anchor" and (server, tool) == ("slides", "get_frame") and parsed.ns != "Frame":
                    return self.deny(cmd, "get_frame requires a Frame anchor")
        if (server, tool) in (("research", "cite_source"), ("citation-checker", "verify_source")):
            if not any(isinstance(args.get(k), str) and args[k].strip() for k in ("anchor", "url")):
                return self.deny(cmd, "citation request requires anchor or URL")
        if self._request_key(server, tool, args) in self._blocked_requests:
            return self.deny(cmd, "unchanged failed request must be repaired before retry")

        # ROUTE: an explicit revision binds the request. Canonical is not
        # evidence of freshness; provenance/ASK comparison must establish that.
        route_anchor = args.get("anchor") or args.get("concept")
        if isinstance(route_anchor, str):
            route_rev = _PolicyAnchor.parse(route_anchor).rev
            if route_rev is not None:
                headers["mcp-replica"] = route_rev
        elif server == "slides":
            headers.setdefault("mcp-replica", "w")

        required_scope = (
            f"wiki.write:{server}" if spec.is_write else "wiki.read"
        )
        if required_scope not in self.ctx.scopes:
            return self.deny(
                cmd, f"missing scope {required_scope}"
            )

        owner = self._learner_id(self.ctx.act)

        for key in ("learner", "learner_id", "act"):
            if key in args:
                target = self._learner_id(args[key])
                if owner is None or target != owner:
                    return self.deny(
                        cmd, "target is not owned by ctx.act"
                    )

        for key in ("anchor", "target", "subject"):
            value = args.get(key)
            if (
                isinstance(value, str)
                and value.lower().startswith("learner:")
            ):
                if owner is None or self._learner_id(value) != owner:
                    return self.deny(
                        cmd, "target is not owned by ctx.act"
                    )

        if "learner" in args:
            args["learner"] = self.ctx.act

        if server in _POLICY_PEERS:
            if cmd.kind != "a2a":
                return self.deny(
                    cmd, "peer call must be canonicalized as a2a"
                )

            aud = headers.get("aud")
            if aud not in (None, server, f"a2a:{server}"):
                return self.deny(
                    cmd, "delegation aud does not match the peer"
                )
            headers["aud"] = f"a2a:{server}"
            if headers.get("x-card-signature") not in (None, "valid"):
                return self.deny(cmd, "unverified peer card signature")

            card = self._peer_cards.get(server)
            if card is None:
                return self.deny(
                    cmd, "peer has no verified registry card"
                )

            skill = args.get("skill")
            if skill is None:
                candidates = sorted(
                    name
                    for name, route in _POLICY_SKILLS.items()
                    if route == (server, tool)
                )
                skill = candidates[0] if candidates else ""

            admission = _policy_admit(card, skill)
            if (
                not admission.admitted
                or _POLICY_SKILLS.get(skill) != (server, tool)
            ):
                return self.deny(
                    cmd, "skill not admitted for this peer and tool"
                )

        elif cmd.kind == "a2a":
            return self.deny(
                cmd, "a2a kind used for a non-peer server"
            )

        if headers.get("x-server-fingerprint") == "unvouched":
            return self.deny(cmd, "server identity lacks registry verification")

        fields = tuple(cmd.fields)

        if original_key != (server, tool):
            old_spec = _POLICY_SPECS[original_key]
            old_fields = (
                old_spec.all_fields
                if fields == ("*",)
                else (fields or old_spec.default_fields)
            )
            if any(
                field not in old_spec.all_fields
                for field in old_fields
            ):
                return self.deny(
                    cmd, "unknown field in deprecated tool mask"
                )
            fields = tuple(
                "body" if field == "snippet" else field
                for field in old_fields
                if field != "anchor"
            )

        if (
            (server, tool) == ("registry", "list_servers")
            and fields in ((), ("*",))
        ):
            fields = ("name",)
        elif (
            (server, tool) == ("glossary", "list_terms")
            and fields in ((), ("*",))
        ):
            fields = ("term",)
        elif fields == ("*",):
            fields = spec.all_fields
        elif not fields:
            fields = spec.default_fields

        if any(field not in spec.all_fields for field in fields):
            return self.deny(cmd, "unknown field in mask")

        fields = _policy_mask(server, tool, fields)
        rows = 1

        if (server, tool) == ("slides", "query"):
            q = args.get("q")
            if not isinstance(q, str) or not q.strip():
                return self.deny(
                    cmd, "query requires a non-empty q"
                )

            limit = args.get("limit", 1)
            if isinstance(limit, bool):
                return self.deny(cmd, "invalid query limit")
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                return self.deny(cmd, "invalid query limit")
            if limit < 1:
                return self.deny(
                    cmd, "query limit must be positive"
                )

            rows = min(limit, 3)
            args["limit"] = rows

        if spec.needs_lease and (
            not cmd.lease_id
            or cmd.lease_id not in self.ctx.leases
        ):
            return self.deny(
                cmd, "frame read requires an arena-tracked live lease"
            )

        if spec.is_write:
            if (server, tool) == ("progress", "record_mastery"):
                target = args.get("anchor") or args.get("learner")
                if (
                    owner is None
                    or self._learner_id(target) != owner
                ):
                    return self.deny(
                        cmd,
                        "mastery write requires the learner owned by ctx.act",
                    )
                args["anchor"] = f"Learner:{owner}"

            anchor = args.get("anchor")
            if not isinstance(anchor, str) or not anchor:
                return self.deny(
                    cmd, "write requires a target anchor"
                )

            etag = headers.get("if-match")
            idem = headers.get("idempotency-key")

            if (
                not isinstance(etag, str) or not etag
                or not isinstance(idem, str) or not idem
            ):
                return self.deny(
                    cmd,
                    "write requires If-Match and Idempotency-Key",
                )

            if self._etags.get(anchor) != etag:
                return self.deny(
                    cmd,
                    "If-Match lacks matching observed registry provenance",
                )

            if idem in self._write_keys:
                return self.deny(
                    cmd,
                    "idempotency key already authorized this duel",
                )

        projected = _policy_cost(
            server, tool, fields=fields, n_rows=rows
        )
        if spec.rate_limit:
            allowed, window = spec.rate_limit
            recent = [r for r in self._rate_calls.get((server, tool), ())
                      if r >= self.ctx.round - window + 1]
            if len(recent) >= allowed:
                return self.deny(cmd, "tool rate window exhausted")

        # Leave nine credits for every future disciplined round. Earlier
        # savings may be used now; the last round may use the full remainder.
        future_reserve = 9 * max(0, 10 - self.ctx.round)
        available = min(
            self.ctx.credits,
            max(
                0,
                self._starting_credits - self._credits_authorised,
            ),
        )

        if projected > available or available - projected < future_reserve:
            return self.deny(
                cmd, "insufficient duel credits for this field mask"
            )

        call = ToolCall(
            server=server,
            tool=tool,
            args=args,
            fields=fields,
            headers=headers,
            lease_id=cmd.lease_id,
            call_index=cmd.call_index,
        )

        changed = (
            original_key != (server, tool)
            or args != cmd.args
            or fields != cmd.fields
            or headers != cmd.headers
        )

        decision = Decision(
            verdict="rewrite" if changed else "forward",
            call=call,
            note=(
                "validated route, identity, scope, mask, "
                "lease and projected cost"
            ),
        )

        self._credits_authorised += projected
        self._rate_calls.setdefault((server, tool), []).append(self.ctx.round)
        self._round_spend[self.ctx.round] = self._round_spend.get(self.ctx.round, 0) + projected
        if spec.is_write:
            self._write_keys.add(headers["idempotency-key"])

        self._telemetry.decision_made(cmd, decision)
        return decision

    def _to_tool_call(self, cmd: Command):
        return ToolCall(
            server=cmd.server,
            tool=cmd.tool,
            args=dict(cmd.args),
            fields=cmd.fields,
            headers=dict(cmd.headers),
            lease_id=cmd.lease_id,
            call_index=cmd.call_index,
        )
