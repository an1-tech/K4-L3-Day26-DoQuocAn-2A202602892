"""Bridge the kit Broker message protocol to its action-fence loop.

MockBroker reads role=tool observations and emits prose on final turns;
kit.loop.Agent stores observations as user messages and requires ANSWER JSON.
This adapter translates only the envelope, never supplies factual answers.
"""
import json
import re
from kit.loop.agent import canonicalise_action, extract_action_fence
from kit.world.anchor import Anchor, AnchorSyntaxError


def fence(body):
    tick = chr(96) * 3
    return tick + "action\n" + body + "\n" + tick


class BrokerAdapter:
    def __init__(self, broker):
        self.broker = broker

    def query(self, messages):
        translated, lease = [], None
        for message in messages:
            item = dict(message)
            if item.get("role") == "user":
                try:
                    value = json.loads(item.get("content", ""))
                except (ValueError, TypeError):
                    value = None
                if isinstance(value, dict) and type(value.get("ok")) is bool:
                    item["role"] = "tool"
                    if value.get("ok") and value.get("lease_id"):
                        lease = value["lease_id"]
            translated.append(item)
        response = self.broker.query(translated)
        content = response.get("content") or ""
        if response.get("tool_calls"):
            body = extract_action_fence(content)
            action = canonicalise_action(body,call_index=0)
            if action["tool"] == "get_frame" and not action["lease_id"] and lease:
                body += " lease=" + lease
            return {"content":fence(body)}
        citations = []
        for raw in re.findall(r"\[([^\[\]\s]+:[^\[\]\s]+)\]",content):
            try:
                Anchor.parse(raw)
            except AnchorSyntaxError:
                continue
            if raw not in citations:
                citations.append(raw)
        return {"content":fence("ANSWER " + json.dumps(
            {"text":content,"cited_anchors":citations},sort_keys=True,ensure_ascii=True
        ))}
