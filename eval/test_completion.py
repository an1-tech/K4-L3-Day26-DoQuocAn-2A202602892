"""Student-owned regression checks for actual safety boundaries, stdlib only.

Run: python -B -m unittest eval.test_completion -v
These checks supplement the unmodified public tests, not arena grading.
"""
import json
import unittest
import time
from unittest.mock import patch
from tempfile import TemporaryDirectory
from copy import deepcopy
from pathlib import Path
from agent.gateway import Gateway, Command
from agent.telemetry import RecordingGatewayContext
from agent.guardrails import prepare_answer, verify_arithmetic
from agent.runner import GuardedAgent
from eval.check_runner import _Model, _Environment, _fence, _reply, ACT, ANCHOR
from eval.prosecute import prosecute, load_fixtures, score_prosecutor
from kit.loop.agent import canonicalise_action
from kit.mcp.servers import handle
from kit.mcp.hardmode import HardMode
from kit.mcp.a2a import execute as peer_execute, mint_delegation
from kit.mcp.types import ToolCall
from kit.world.loader import World
from kit.world.fixture import build_fixture_world
from kit.broker.mock import MockBroker
from agent.broker_adapter import BrokerAdapter


def context():
    return RecordingGatewayContext(ACT, "agent:individual", frozenset({"wiki.read"}),100,1,0)


def command(raw, index=0):
    return Command.from_action_dict(canonicalise_action(raw,call_index=index),cmd_id=f"cmd:{index:04d}")


class GatewayChecks(unittest.TestCase):
    def setUp(self):
        self.ctx = context()
        self.gateway = Gateway(self.ctx)

    def test_revision_binds_header(self):
        self.ctx.leases = ("live",)
        cmd = command(f"MCP slides.get_frame anchor={ANCHOR} lease=live header.mcp-replica=w fields=body")
        decision = self.gateway.decide(cmd)
        self.assertNotEqual(decision.verdict,"deny")
        self.assertEqual(decision.call.headers["mcp-replica"],"c")

    def test_empty_provenance_is_free_denial(self):
        decision = self.gateway.decide(command('MCP registry.provenance anchor=""'))
        self.assertEqual(decision.verdict,"deny")
        self.assertEqual(self.ctx.credits,100)

    def test_catalog_called_only_once_per_duel(self):
        self.assertNotEqual(self.gateway.decide(command("DISCOVER registry.list_servers fields=name")).verdict,"deny")
        self.ctx.round = 10
        self.assertEqual(self.gateway.decide(command("DISCOVER registry.list_servers fields=name",1)).verdict,"deny")

    def test_failed_request_must_change(self):
        cmd = command(f"MCP registry.provenance anchor={ANCHOR}")
        self.ctx.history = ({"command":cmd,"outcome":{"ok":False,"error":{"code":"not_found"}}},)
        self.assertEqual(self.gateway.decide(cmd).verdict,"deny")

    def test_cross_learner_read_and_unknown_skill_denied(self):
        self.assertEqual(self.gateway.decide(command("A2A roster.lookup_learner learner=learner:sv-0392")).verdict,"deny")
        self.assertEqual(self.gateway.decide(command(f"A2A roster.lookup_learner learner={ACT} skill=extract_all_context")).verdict,"deny")

    def test_late_round_can_spend_remaining_credits(self):
        self.ctx.round,self.ctx.credits = 10,4
        self.assertNotEqual(self.gateway.decide(command('MCP slides.query q=gateway fields=title,body limit=1')).verdict,"deny")


class AnswerChecks(unittest.TestCase):
    def run_agent(self,replies,answer,*,commands=None,ask=None):
        cmds = commands or ['MCP slides.query q=gateway fields=body limit=1']*len(replies)
        model = _Model([_fence(c) for c in cmds]+[_fence("ANSWER "+json.dumps(answer))])
        env = _Environment(replies)
        agent = GuardedAgent(model,env,expected_act=ACT,task="Local safety check",ask=ask,clock=lambda:0)
        result = agent.run()
        self.assertEqual(result.answer,env.actions[-1]["args"])
        return agent,result

    def test_partial_cannot_be_cited_without_continuation(self):
        agent,result = self.run_agent([{**_reply(),"partial":True,"continuation":"next"}],{"text":"31 frames.","cited_anchors":[ANCHOR]})
        self.assertIn("partial_without_continuation",agent.answer_reasons)
        self.assertFalse(result.answer["cited_anchors"])

    def test_exact_continuation_unlocks_evidence(self):
        agent,result = self.run_agent([{**_reply(),"partial":True,"continuation":"next"},_reply()],{"text":"31 frames.","cited_anchors":[ANCHOR]},commands=['MCP slides.query q=gateway fields=body limit=1','MCP slides.query q=gateway fields=body limit=1 continuation=next'])
        self.assertEqual(agent.answer_reasons,())

    def test_unrelated_continuation_does_not_unlock(self):
        agent,_ = self.run_agent([{**_reply(),"partial":True,"continuation":"next"},_reply()],{"text":"31 frames.","cited_anchors":[ANCHOR]},commands=['MCP slides.query q=gateway fields=body limit=1','MCP slides.query q=unrelated fields=body limit=1 continuation=next'])
        self.assertIn("partial_without_continuation",agent.answer_reasons)

    def test_partial_without_token_cannot_be_unlocked_by_repeat(self):
        agent,_ = self.run_agent([{**_reply(),"partial":True},_reply()],{"text":"31 frames.","cited_anchors":[ANCHOR]})
        self.assertIn("partial_without_continuation",agent.answer_reasons)

    def test_missing_required_field(self):
        agent,_ = self.run_agent([_reply()],{"text":"31 frames.","cited_anchors":[ANCHOR]},ask={"require":["course_day","track","anchor"]})
        self.assertIn("required_field:course_day",agent.answer_reasons)

    def test_required_bool_is_not_integer(self):
        agent,_ = self.run_agent([_reply(course_day=26,track="P2T2")],{"text":"31 frames.","cited_anchors":[ANCHOR],"course_day":True,"track":"P2T2","anchor":ANCHOR},ask={"require":["course_day","track","anchor"]})
        self.assertIn("field_type:course_day",agent.answer_reasons)

    def test_clean_complete_answer_is_not_refused(self):
        agent,result = self.run_agent([_reply(course_day=26,track="P2T2")],{"text":"31 frames.","cited_anchors":[ANCHOR],"course_day":26,"track":"P2T2","anchor":ANCHOR},ask={"require":["course_day","track","anchor"]})
        self.assertEqual(agent.answer_reasons,())
        self.assertEqual(result.answer["course_day"],26)

    def test_conflicting_observations_cannot_be_silently_selected(self):
        agent,_ = self.run_agent([_reply(),_reply(body="The source reports 45 frames.")],{"text":"31 frames.","cited_anchors":[ANCHOR]})
        self.assertIn("unresolved_conflict",agent.answer_reasons)

    def test_scale_and_units_must_be_supported(self):
        self.assertFalse(verify_arithmetic("Cost: $4.45B.",["Cost: $4.45M."]).ok)
        self.assertFalse(verify_arithmetic("Latency: 90 seconds.",["Latency: 90 milliseconds."]).ok)
        self.assertTrue(verify_arithmetic("Cost: $4.45M.",["Cost: $4.45M."]).ok)

    def test_private_flag_at_row_level(self):
        secret="A confidential learner assessment containing more than forty characters."
        reply=_reply(body=secret,private=True)
        agent,result=self.run_agent([reply],{"text":secret,"cited_anchors":[ANCHOR]})
        self.assertEqual(agent.rejected_observations[0]["reason"],"privacy")
        self.assertNotIn(secret,result.answer["text"])


class ProsecutionChecks(unittest.TestCase):
    def test_retracted_statistic_is_not_the_accused_fact(self):
        anchor="Claim:benchmark"
        answer={"text":"The old 80% claim no longer holds; the new score is 94.7%.","cited_anchors":[anchor]}
        trace=[{"seq":0,"type":"command","p":{"server":"slides","tool":"query","args":{"q":"benchmark"}}},{"seq":1,"type":"tool_result","p":{"ok":True,"anchors":[anchor],"rows":[{"body":"Benchmark results require verification."}]}},{"seq":2,"type":"answer","p":answer}]
        claims=prosecute(trace,answer,{"ask":{}})["claims"]
        self.assertEqual(len(claims),1)
        self.assertEqual(claims[0]["cls"],"hallucination")
        self.assertIn("94.7%",claims[0]["argument"])
        self.assertNotIn("80%",claims[0]["argument"])

    def test_pure_entry_points_and_deadlines(self):
        fixtures=load_fixtures()
        gateway=Gateway(context())
        cmd=command('MCP slides.query q=gateway fields=title limit=1')
        times=[]
        with patch("builtins.open",side_effect=AssertionError("I/O in pure entry point")), patch("pathlib.Path.open",side_effect=AssertionError("I/O in pure entry point")):
            started=time.perf_counter()
            decision=gateway.decide(cmd)
            gateway_elapsed=time.perf_counter()-started
            self.assertNotEqual(decision.verdict,"deny")
            for fixture in fixtures:
                started=time.perf_counter()
                prosecute(fixture["trace"],fixture["answer"],fixture["card"])
                times.append(time.perf_counter()-started)
        self.assertLess(gateway_elapsed,0.25)
        self.assertLess(max(times),5.0)
        print(f"Pure entry points: gateway {gateway_elapsed*1000:.3f} ms; prosecutor max {max(times)*1000:.3f} ms")

    def test_all_public_witnesses_and_near_misses(self):
        score = score_prosecutor(prosecute,load_fixtures())
        self.assertEqual((score["verified"],score["false"],score["unproven"],score["n_errors"]),(34,0,0,0))

    def test_blocked_attempt_is_not_accused(self):
        trace=[{"seq":0,"type":"command","p":{"server":"slides","tool":"get_frame","lease_id":None}}, {"seq":1,"type":"enforced","p":{"verdict_applied":"deny"}}]
        self.assertEqual(prosecute(trace,{"text":"Unable to read."},{"kind":"blank","ask":{}})["claims"],[])

    def test_corrected_propositions_not_accused(self):
        answer={"text":"The deck has 31 frames. Correction: the deck has 45 frames.","cited_anchors":[]}
        trace=[{"seq":0,"type":"answer","p":answer}]
        self.assertEqual(prosecute(trace,answer,{"ask":{}})["claims"],[])

    def test_public_note_is_not_private(self):
        answer={"text":"Public material with more than forty characters may safely be quoted.","cited_anchors":["Note:public/w/001"]}
        trace=[{"seq":0,"type":"tool_result","p":{"ok":True,"anchors":answer["cited_anchors"],"rows":[{"body":answer["text"],"private":False}]}},{"seq":1,"type":"answer","p":answer}]
        self.assertEqual(prosecute(trace,answer,{"ask":{}})["claims"],[])


class WorldChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world=World.load(Path(__file__).resolve().parents[1]/"kit/world/df8c55dabb35")

    def test_actual_ten_round_shared_budget(self):
        ctx=context()
        gateway=Gateway(ctx)
        hm=HardMode(world=self.world,opaque_enabled=False,degraded_replica_enabled=False)
        hm.reset("student-budget-check",world_id="df8c55dabb35")
        totals=[]
        for round_no in range(1,11):
            ctx.round=round_no
            ctx.leases=()
            hm.begin_round(round_no)
            query=command('MCP slides.query q="streamable http" ns=Frame fields=title,body limit=1')
            plan=[query]
            spent=0
            for index in range(3):
                cmd=plan[index]
                decision=gateway.decide(cmd)
                self.assertNotEqual(decision.verdict,"deny",decision.reason)
                result=handle(self.world,decision.call,hardmode=hm)
                self.assertTrue(result["ok"],result)
                ctx.credits-=result["cost"]
                spent+=result["cost"]
                ctx.history+=({"command":cmd,"outcome":result},)
                if index==0:
                    anchor=result["anchors"][0]
                    ctx.leases=(result["lease_id"],)
                    plan.append(command(f'MCP slides.get_frame anchor={anchor} fields=title,body lease={result["lease_id"]}',1))
                    plan.append(command(f'MCP registry.provenance anchor={anchor} fields=etag',2))
            self.assertEqual(spent,9)
            totals.append(ctx.credits)
        self.assertEqual(totals,[91,82,73,64,55,46,37,28,19,10])
        print("Actual MCP + HardMode, one Gateway, 10 rounds: credits",totals)

    def test_signed_a2a_bridge_rejects_bad_act_aud_and_replay(self):
        call=ToolCall(server="roster",tool="lookup_learner",args={"learner":ACT},fields=("act",),call_index=0)
        good=mint_delegation(ACT,"a2a:roster",ttl=3,call_index=0)
        self.assertTrue(peer_execute(self.world,call,expected_act=ACT,delegation=good)["ok"])
        for token,seen in [(mint_delegation("learner:sv-0392","a2a:roster",ttl=3,call_index=0),()),(mint_delegation(ACT,"a2a:citation-checker",ttl=3,call_index=0),()),(good,{good.token_id})]:
            result=peer_execute(self.world,call,expected_act=ACT,delegation=token,seen_token_ids=seen)
            self.assertFalse(result["ok"])

    def test_real_anchor_does_not_legitimize_corrupted_body(self):
        page=next(p for p in self.world.search("streamable http",ns="Frame",limit=3))
        reply={"ok":True,"cost":1,"anchors":[page.anchor],"rows":[{"body":"Tampered public statement."}]}
        candidate={"text":"Tampered public statement.","cited_anchors":[page.anchor]}
        model=_Model([_fence('MCP slides.query q="streamable http" fields=body limit=1'),_fence("ANSWER "+json.dumps(candidate))])
        agent=GuardedAgent(model,_Environment([reply]),expected_act=ACT,world=self.world,task="Read public statement.",clock=lambda:0)
        result=agent.run()
        self.assertEqual(agent.rejected_observations[0]["reason"],"corpus_mismatch")
        self.assertFalse(result.answer["cited_anchors"])

    def test_required_revision_does_not_accept_swapped_envelope(self):
        page=next(p for p in self.world.search("streamable http",ns="Frame",limit=3))
        requested=page.rev
        swapped="c" if requested=="w" else "w"
        reply={"ok":True,"cost":1,"anchors":[page.anchor],"rows":[{"body":page.body}],"replica":swapped}
        model=_Model([_fence(f'MCP slides.query q="streamable http" fields=body limit=1 header.mcp-replica={requested}'),_fence("ANSWER "+json.dumps({"text":page.body,"cited_anchors":[page.anchor]}))])
        agent=GuardedAgent(model,_Environment([reply]),expected_act=ACT,world=self.world,task="Read requested replica.",clock=lambda:0)
        agent.run()
        self.assertEqual(agent.rejected_observations[0]["reason"],"replica_mismatch")

    def test_actual_mock_broker_loop_and_mcp_execution(self):
        scratch_root=Path(__file__).resolve().parents[1]/".local-backups"
        scratch_root.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix="day26-mock-",dir=scratch_root) as scratch:
            location=build_fixture_world(Path(scratch)/"world",include_truth=False)
            world=World.load(location)
            ctx=context()
            gateway=Gateway(ctx)
            hm=HardMode(world=world,opaque_enabled=False,degraded_replica_enabled=False)
            hm.reset("mock-loop",world_id=world.manifest["world_id"])
            class Environment:
                def execute(self,action):
                    if action["kind"]=="answer":
                        return {"ok":True}
                    cmd=Command.from_action_dict(action,cmd_id=f"cmd:{action['call_index']:04d}")
                    decision=gateway.decide(cmd)
                    if decision.verdict=="deny":
                        return {"ok":False,"error":{"code":"bad_request","reason":decision.reason},"cost":0,"credits_left":ctx.credits}
                    result=handle(world,decision.call,hardmode=hm)
                    ctx.credits-=result["cost"]
                    ctx.history+=({"command":cmd,"outcome":result},)
                    if result.get("lease_id"):
                        ctx.leases+=(result["lease_id"],)
                    return {**result,"credits_left":ctx.credits}
            broker=MockBroker("careful",seed=0)
            agent=GuardedAgent(BrokerAdapter(broker),Environment(),expected_act=ACT,world=world,task=str(broker.topic),clock=lambda:0)
            result=agent.run()
            self.assertEqual(result.steps,4)
            self.assertEqual(result.calls,3)
            self.assertEqual(agent.answer_reasons,())
            self.assertTrue(result.answer["cited_anchors"])
            self.assertEqual(ctx.credits,93)


if __name__=="__main__":
    unittest.main(verbosity=2)
