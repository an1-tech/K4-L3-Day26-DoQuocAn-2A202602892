"""Checked observations and ANSWER actions around the unmodified kit loop."""
from __future__ import annotations

from pathlib import Path
from kit.loop.prompt import render_system_prompt

import json
from collections.abc import Mapping
from kit.loop.agent import Agent
from kit.mcp.a2a import verify_card, verify_delegation
from kit.mcp.specs import A2A_PEERS
from kit.world.anchor import Anchor, AnchorSyntaxError
from agent.guardrails import prepare_answer, redact, scan_for_injected_instructions


def _key(value):
    try:
        return Anchor.parse(value).key()
    except (AnchorSyntaxError, TypeError, AttributeError):
        return None


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


class GuardedAgent(Agent):
    """One instance per exchange; share seen_token_ids across the duel.

    expected_act is caller-owned context, never a value from a peer.
    The environment still owns execution, metering and the arena trace.
    """

    def __init__(
        self, model, env, *, expected_act, world=None,
        require_citation=True, seen_token_ids=None,
        policy_text=None, ask=None, **kwargs,
    ):
        if (
            not isinstance(expected_act, str)
            or not expected_act.startswith("learner:")
        ):
            raise ValueError(
                "expected_act must come from the learner context"
            )

        if policy_text is None:
            policy_text = (
                Path(__file__)
                .with_name("prompt.md")
                .read_text(encoding="utf-8")
            )

        if not isinstance(policy_text, str) or not policy_text.strip():
            raise ValueError(
                "A non-empty defensive policy is required"
            )

        caller_prompt = kwargs.pop("system_prompt", "")
        if not isinstance(caller_prompt, str):
            raise TypeError("system_prompt must be a string")

        system_prompt = (
            render_system_prompt(limits=kwargs.get("limits"))
            + "\n\n" + policy_text.strip()
            + "\n\nTrusted learner act: " + expected_act
        )
        if caller_prompt.strip():
            system_prompt += (
                "\n\nTrusted caller context:\n"
                + caller_prompt.strip()
            )

        super().__init__(
            model, env, system_prompt=system_prompt, **kwargs
        )

        self.policy_text = policy_text
        self.expected_act = expected_act
        self.world = world
        self.require_citation = require_citation
        self.seen_token_ids = (
            set() if seen_token_ids is None else seen_token_ids
        )
        self._anchors = set()
        self._sources = {}
        self._private = set()
        self.rejected_observations = []
        self.answer_reasons = ()
        if ask is None:
            try:
                task_data = json.loads(kwargs.get("task", ""))
                ask = task_data.get("ask", task_data) if isinstance(task_data, dict) else {}
            except (ValueError, TypeError):
                ask = {}
        self.ask = dict(ask or {})
        self._rows = {}
        self._pending_pages = {}
        self._incomplete_anchors = set()
        self._conflicts = set()

    def _remember_private(self, meta, body=None):
        if not isinstance(meta, Mapping):
            return

        if (
            meta.get("private") is True
            and isinstance(body, str)
            and body.strip()
        ):
            self._private.add(body)

        fields = meta.get("private_fields")
        if isinstance(fields, Mapping):
            values = fields.values()
        elif isinstance(fields, (list, tuple)):
            values = fields
        else:
            values = ()

        for value in values:
            if isinstance(value, str) and value.strip():
                self._private.add(value)

    def _reject(self, observation, action, reason):
        self.rejected_observations.append({
            "call_index": action["call_index"],
            "reason": reason,
        })
        result = {
            "ok": False,
            "error": {"code": "bad_request", "guardrail": reason},
            "cost": observation.get("cost", 0),
        }
        if "credits_left" in observation:
            result["credits_left"] = observation["credits_left"]
        return result

    def _execute(self, action):
        observation = super()._execute(action)
        if action["kind"] == "answer":
            return observation

        if observation.get("ok") is True:
            anchors = observation.get("anchors", [])
            rows = observation.get("rows", [])

            if (
                not isinstance(anchors, (list, tuple))
                or not isinstance(rows, (list, tuple))
            ):
                return self._reject(observation, action, "reply_shape")

            if any(not isinstance(row, Mapping) for row in rows):
                return self._reject(observation, action, "reply_shape")

            anchors = list(anchors)
            for row_index, row in enumerate(rows):
                if isinstance(row.get("anchor"), str):
                    anchors.append(row["anchor"])
                self._remember_private(row.get("meta"), row.get("body"))
                self._remember_private(row, row.get("body"))
                if self.world is not None:
                    row_anchor = row.get("anchor")
                    if row_anchor is None and len(rows) == len(observation.get("anchors", ())):
                        row_anchor = observation["anchors"][row_index]
                    if row_anchor is not None:
                        page = self.world.page(row_anchor)
                        if page is None:
                            return self._reject(observation, action, "unknown_world_anchor")
                        if action["server"] == "slides" and action["tool"] in ("query", "get_frame"):
                            for field in ("title", "body"):
                                if field in row and row[field] != getattr(page, field):
                                    return self._reject(observation, action, "corpus_mismatch")
                        if action["server"] in A2A_PEERS:
                            for field in ("course_day", "track"):
                                expected = page.meta.get(field)
                                if expected is not None and field in row and row[field] != expected:
                                    return self._reject(observation, action, "peer_fact_mismatch")

            if any(
                not isinstance(a, str) or _key(a) is None
                for a in anchors
            ):
                return self._reject(observation, action, "anchor_syntax")

            for anchor in anchors:
                key = _key(anchor)
                if (
                    key[0] == "Learner"
                    and key[1] != self.expected_act.split(":", 1)[1]
                ):
                    return self._reject(
                        observation, action, "learner_scope"
                    )

                if self.world is not None:
                    page = self.world.page(anchor)
                    if page is None:
                        return self._reject(
                            observation, action, "unknown_world_anchor"
                        )
                    self._remember_private(page.meta, page.body)

            if (
                action["server"] == "slides"
                and action["tool"] == "get_frame"
            ):
                requested = _key(action["args"].get("anchor"))
                if (
                    requested is None
                    or requested not in {_key(a) for a in anchors}
                ):
                    return self._reject(
                        observation, action, "requested_anchor"
                    )
            requested_rev = action["headers"].get("mcp-replica")
            replica = observation.get("replica")
            if requested_rev and replica and requested_rev != replica:
                return self._reject(observation, action, "replica_mismatch")

            peer = action["server"]
            pending_ids = set()

            for row in rows:
                scoped = row.get("scoped_to_learner")
                if scoped is not None and scoped != self.expected_act:
                    return self._reject(
                        observation, action, "learner_scope"
                    )

                if "agent_card" in row:
                    if (
                        peer not in A2A_PEERS
                        or not isinstance(row["agent_card"], Mapping)
                    ):
                        return self._reject(
                            observation, action, "agent_card"
                        )

                    admission = verify_card(row["agent_card"])
                    if (
                        not admission.admitted
                        or admission.peer != peer
                    ):
                        return self._reject(
                            observation, action, "agent_card"
                        )

                if "delegation_token" in row:
                    token = row["delegation_token"]
                    if (
                        peer not in A2A_PEERS
                        or not isinstance(token, Mapping)
                    ):
                        return self._reject(
                            observation, action, "delegation"
                        )

                    minted = token.get("minted_at_call_index")
                    if (
                        not isinstance(minted, int)
                        or isinstance(minted, bool)
                        or minted > action["call_index"]
                    ):
                        return self._reject(
                            observation, action, "delegation"
                        )

                    admission = verify_delegation(
                        token,
                        aud="a2a:" + peer,
                        call_index=action["call_index"],
                        expected_act=self.expected_act,
                        seen_token_ids=self.seen_token_ids | pending_ids,
                    )
                    if not admission.admitted:
                        return self._reject(
                            observation, action, "delegation"
                        )
                    pending_ids.add(token["token_id"])
        else:
            anchors, rows, pending_ids = [], [], set()

        rendered = json.dumps(
            observation,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        if scan_for_injected_instructions(rendered).suspicious:
            return self._reject(observation, action, "injection")

        if any(
            redact(value, self._private).hits
            for value in _strings(observation)
        ):
            return self._reject(observation, action, "privacy")

        if observation.get("ok") is not True:
            return observation

        # Evidence is accepted only after all observation checks pass.
        self.seen_token_ids.update(pending_ids)
        self._anchors.update(anchors)
        request_key = (action["server"], action["tool"], tuple(sorted(
            (k, repr(v)) for k, v in action["args"].items()
            if k not in ("cursor", "continuation")
        )))
        pending = self._pending_pages.get(request_key)
        continuation = action["args"].get("continuation", action["args"].get("cursor"))
        continued = (pending is not None and pending[0] is not None
                     and continuation is not None and str(continuation) == str(pending[0]))
        if pending and continued and observation.get("partial") is not True:
            self._incomplete_anchors.difference_update(pending[1])
            self._pending_pages.pop(request_key, None)
        if observation.get("partial") is True:
            affected = {_key(a) for a in anchors}
            if continued:
                affected.update(pending[1])
            self._incomplete_anchors.update(affected)
            self._pending_pages[request_key] = (observation.get("continuation"), affected)
        envelope_keys = {
            _key(a) for a in observation.get("anchors", ())
        }

        public_fields = (
            "title", "body", "text", "definition", "quote",
            "course_day", "track", "count", "content_frames",
            "role", "enrolled",
            "a", "b", "fresher", "delta", "url", "sense",
        )

        for row in rows:
            texts = [
                str(row[field])
                for field in public_fields
                if isinstance(row.get(field), (str, int, float))
                and not isinstance(row.get(field), bool)
            ]
            row_key = _key(row.get("anchor"))
            keys = {row_key} if row_key is not None else (
                envelope_keys if len(envelope_keys) == 1 else set()
            )
            for key in keys:
                self._sources.setdefault(key, []).extend(texts)
                existing = self._rows.setdefault(key, [])
                for previous in existing:
                    for field in ("course_day", "track", "count", "content_frames", "fresher", "body", "definition"):
                        if field in row and field in previous and row[field] != previous[field]:
                            self._conflicts.add(str(key))
                facts = dict(row)
                meta = row.get("meta")
                if isinstance(meta, Mapping):
                    for field in ("course_day", "track", "count", "content_frames"):
                        if field in meta:
                            facts.setdefault(field, meta[field])
                existing.append(facts)
                if key[0] == "Talk" and row.get("a") is not None and row.get("b") is not None and row["a"] != row["b"]:
                    self._conflicts.add(str(key))

        return observation

    def _next_action(self):
        action = super()._next_action()
        if action is None or action["kind"] != "answer":
            return action

        sources, source_rows, cited_keys = [], [], set()
        cited = action["args"].get("cited_anchors", [])

        if isinstance(cited, (list, tuple)):
            for citation in cited:
                key = _key(citation)
                if key is None:
                    continue

                for source_key, texts in self._sources.items():
                    if source_key == key or (
                        key[2] is None
                        and (key[0], key[1], key[3])
                        == (
                            source_key[0],
                            source_key[1],
                            source_key[3],
                        )
                    ):
                        sources.extend(texts)
                        source_rows.extend(self._rows.get(source_key, ()))
                        cited_keys.add(source_key)

        safe, reasons = prepare_answer(
            action["args"],
            tuple(sorted(self._anchors)),
            source_texts=sources,
            private_values=tuple(sorted(self._private)),
            require_citation=self.require_citation,
            required_fields=self.ask.get("require", ()),
            source_rows=source_rows,
            incomplete=bool(cited_keys & self._incomplete_anchors),
            conflicts=self._conflicts,
        )
        self.answer_reasons = reasons

        result = dict(action)
        result["args"] = safe
        result["raw"] = "ANSWER " + json.dumps(
            safe, sort_keys=True, ensure_ascii=True
        )
        return result
