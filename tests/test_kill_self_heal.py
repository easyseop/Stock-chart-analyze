from __future__ import annotations
import json, os, stat, tempfile
from unittest import mock
from bot import kill, kill_self_heal
from bot.watchdog_policy import (BALANCE_FAILURE_REASON,
                                 HEARTBEAT_EXHAUSTED_REASON, WATCHDOG_WHO,
                                 self_heal_allowed)
def _env(tmp): return mock.patch.dict(os.environ,{"KILL_STATE_PATH":f"{tmp}/kill.json","KILL_LOG_PATH":f"{tmp}/kill.jsonl","SELF_HEAL_STATE_PATH":f"{tmp}/heal.json","SELF_HEAL_OBSERVE_S":"10","SELF_HEAL_RESET_AGE_S":"90","SELF_HEAL_MAX_SOFT_SAMPLES":"4","KILL_LEVEL":"0"})
def _raise(): assert kill.raise_level(1,WATCHDOG_WHO,HEARTBEAT_EXHAUSTED_REASON)==1; return float(kill.status()["ts"])
def test_normal_path_lowers_and_audits_self_heal():
    with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",return_value=True),mock.patch.object(kill_self_heal,"_readiness_go",return_value=(True,"scope=l0 blockers=0")):
        ts=_raise(); assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts)["action"]=="observing"; assert stat.S_IMODE(os.stat(f"{tmp}/heal.json").st_mode)==0o600; assert stat.S_IMODE(os.stat(f"{tmp}/heal.json.status").st_mode)==0o644; public=json.load(open(f"{tmp}/heal.json.status",encoding="utf-8")); assert set(public)=={"v","day_kst","action","why","observed_s","remaining_s","used_today"}; assert kill_self_heal.status(now=ts)["action"]=="observing"; assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+11)["action"]=="recovered"; assert kill.level()==0 and kill.status()["who"]=="self-heal" and '"who": "self-heal"' in open(f"{tmp}/kill.jsonl",encoding="utf-8").read()
def test_s1_operator_reason_and_l2_never_lower():
    with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",return_value=True):
        for lv,who,why in ((1,"operator",HEARTBEAT_EXHAUSTED_REASON),(1,WATCHDOG_WHO,"other"),(2,WATCHDOG_WHO,HEARTBEAT_EXHAUSTED_REASON)):
            kill._write_file(lv,who,why); assert kill_self_heal.cycle(heartbeat_age_s=1)["action"]=="ineligible" and kill.level()==lv
def test_s2_29_minutes_flap_and_restart_reset():
    with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",return_value=True),mock.patch.dict(os.environ,{"SELF_HEAL_OBSERVE_S":"1800"}):
        ts=_raise(); kill_self_heal.cycle(heartbeat_age_s=1,now=ts); assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+1740)["action"]=="observing"; assert kill_self_heal.cycle(heartbeat_age_s=61,now=ts+1750)["action"]=="degraded"; assert kill_self_heal.cycle(heartbeat_age_s=62,now=ts+1765)["why"]=="heartbeat_soft_consecutive"; assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+1810)["action"]=="observing"
        p=f"{tmp}/heal.json"; st=json.load(open(p,encoding="utf-8")); st["observer_pid"]=-1; json.dump(st,open(p,"w",encoding="utf-8")); assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+4000)["action"]=="observing" and kill.level()==1

def test_t1_single_soft_sample_preserves_window_and_recovers():
    with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",return_value=True),mock.patch.object(kill_self_heal,"_readiness_go",return_value=(True,"go")),mock.patch.dict(os.environ,{"SELF_HEAL_OBSERVE_S":"1800"}):
        ts=_raise()
        assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts)["action"]=="observing"
        for offset in range(45, 901, 45):
            assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+offset)["action"]=="observing"
        soft=kill_self_heal.cycle(heartbeat_age_s=71,now=ts+900)
        assert soft["action"]=="degraded" and soft["observed_s"]==900
        assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+915)["action"]=="observing"
        for offset in range(960, 1800, 45):
            assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+offset)["action"]=="observing"
        assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+1801)["action"]=="recovered"

def test_t1_hard_age_resets_immediately():
    with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",return_value=True):
        ts=_raise(); kill_self_heal.cycle(heartbeat_age_s=1,now=ts)
        out=kill_self_heal.cycle(heartbeat_age_s=90.01,now=ts+100)
        assert out["action"]=="reset" and out["why"]=="heartbeat_hard" and kill.level()==1

def test_t1_chronic_alternating_soft_samples_never_recovers():
    with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",return_value=True),mock.patch.object(kill_self_heal,"_readiness_go",return_value=(True,"go")),mock.patch.dict(os.environ,{"SELF_HEAL_OBSERVE_S":"100","SELF_HEAL_MAX_SOFT_SAMPLES":"2"}):
        ts=_raise(); kill_self_heal.cycle(heartbeat_age_s=1,now=ts)
        assert kill_self_heal.cycle(heartbeat_age_s=70,now=ts+20)["action"]=="degraded"
        assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+35)["action"]=="observing"
        assert kill_self_heal.cycle(heartbeat_age_s=71,now=ts+50)["action"]=="degraded"
        assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+65)["action"]=="observing"
        out=kill_self_heal.cycle(heartbeat_age_s=72,now=ts+80)
        assert out["action"]=="reset" and out["why"]=="heartbeat_soft_budget"
        assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+101)["action"]=="observing" and kill.level()==1
def test_s3_no_go_exception_and_l2_toctou_block():
    for verdict in ((False,"blockers=1"),RuntimeError("boom")):
        with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",return_value=True):
            ts=_raise(); kill_self_heal.cycle(heartbeat_age_s=1,now=ts)
            kwargs={"side_effect":verdict} if isinstance(verdict,Exception) else {"return_value":verdict}
            with mock.patch.object(kill_self_heal,"_readiness_go",**kwargs): out=kill_self_heal.cycle(heartbeat_age_s=1,now=ts+11)
            assert out["action"]=="blocked" and kill.level()==1
    with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",return_value=True):
        ts=_raise(); kill_self_heal.cycle(heartbeat_age_s=1,now=ts)
        def raise_l2(): kill.raise_level(2,"operator","주문 사고"); return True,"go"
        with mock.patch.object(kill_self_heal,"_readiness_go",side_effect=raise_l2): out=kill_self_heal.cycle(heartbeat_age_s=1,now=ts+11)
        assert out["why"]=="kill_changed_during_readiness" and kill.level()==2
def test_s4_once_per_kst_day_and_manual_alert():
    sent=[]
    with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",side_effect=lambda x,**kw: sent.append(x) or True),mock.patch.object(kill_self_heal,"_readiness_go",return_value=(True,"go")):
        ts=_raise(); kill_self_heal.cycle(heartbeat_age_s=1,now=ts); kill_self_heal.cycle(heartbeat_age_s=1,now=ts+11); kill._write_file(1,WATCHDOG_WHO,HEARTBEAT_EXHAUSTED_REASON); assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+20)["action"]=="manual_alert"; assert any("수동 확인" in x for x in sent); assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+86420)["action"]=="observing"
def test_corrupt_state_fail_closed():
    with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",return_value=True):
        _raise(); open(f"{tmp}/heal.json","w").write("{bad"); assert kill_self_heal.cycle(heartbeat_age_s=1)["why"]=="state_corrupt" and kill.level()==1

def test_state_write_failure_blocks_before_readiness_and_lower():
    with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",return_value=True):
        ts=_raise(); kill_self_heal.cycle(heartbeat_age_s=1,now=ts)
        with mock.patch.object(kill_self_heal,"_save",return_value=False),mock.patch.object(kill_self_heal,"_readiness_go",return_value=(True,"go")) as ready,mock.patch.object(kill,"lower_level") as lower:
            out=kill_self_heal.cycle(heartbeat_age_s=1,now=ts+11)
        assert out["action"]=="blocked" and out["why"]=="state_write"
        assert ready.call_count==0 and lower.call_count==0 and kill.level()==1

def test_relaxation_env_cannot_weaken_safety_ceiling():
    with mock.patch.dict(os.environ,{"SELF_HEAL_RESET_AGE_S":"9999","SELF_HEAL_MAX_SOFT_SAMPLES":"9999"}):
        assert kill_self_heal._reset_age_s()==90.0
        assert kill_self_heal._max_soft_samples()==4
def test_notification_failure_retries_after_lower():
    with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",return_value=True),mock.patch.object(kill_self_heal,"_readiness_go",return_value=(True,"go")):
        ts=_raise(); kill_self_heal.cycle(heartbeat_age_s=1,now=ts)
        with mock.patch.object(kill_self_heal,"_delivered",return_value=False):
            out=kill_self_heal.cycle(heartbeat_age_s=1,now=ts+11); assert out["action"]=="recovered" and out["notified"] is False
        with mock.patch.object(kill_self_heal,"_delivered",return_value=True) as sent: assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+12)["action"]=="notice_delivered" and sent.call_count==1
def test_pending_notice_is_discarded_after_manual_lower():
    with tempfile.TemporaryDirectory() as tmp,_env(tmp),mock.patch("bot.notify.send",return_value=True),mock.patch.object(kill_self_heal,"_readiness_go",return_value=(True,"go")):
        ts=_raise(); kill_self_heal.cycle(heartbeat_age_s=1,now=ts)
        with mock.patch.object(kill,"lower_level",side_effect=RuntimeError("crash")):
            assert kill_self_heal.cycle(heartbeat_age_s=1,now=ts+11)["action"]=="blocked"
        assert json.load(open(f"{tmp}/heal.json",encoding="utf-8"))["pending_notice"]
        assert kill.lower_level(0,ack="operator manual recovery")==0
        with mock.patch.object(kill_self_heal,"_delivered",return_value=True) as sent:
            out=kill_self_heal.cycle(heartbeat_age_s=1,now=ts+12)
        assert out["action"]=="notice_discarded" and out["why"]=="l0_owner_not_self_heal" and sent.call_count==0
        assert json.load(open(f"{tmp}/heal.json",encoding="utf-8"))["pending_notice"]==""
def test_self_heal_allowlist_requires_exact_match():
    assert self_heal_allowed(WATCHDOG_WHO,HEARTBEAT_EXHAUSTED_REASON)
    assert self_heal_allowed(WATCHDOG_WHO,BALANCE_FAILURE_REASON)
    variants=((WATCHDOG_WHO+"-extra",HEARTBEAT_EXHAUSTED_REASON),
              (WATCHDOG_WHO[:4],HEARTBEAT_EXHAUSTED_REASON),
              (" "+WATCHDOG_WHO,HEARTBEAT_EXHAUSTED_REASON),
              (WATCHDOG_WHO+" ",HEARTBEAT_EXHAUSTED_REASON),
              (WATCHDOG_WHO,HEARTBEAT_EXHAUSTED_REASON[:10]),
              (WATCHDOG_WHO,HEARTBEAT_EXHAUSTED_REASON+" 추가"),
              (WATCHDOG_WHO," "+HEARTBEAT_EXHAUSTED_REASON),
              (WATCHDOG_WHO,HEARTBEAT_EXHAUSTED_REASON+" "),
              (WATCHDOG_WHO,BALANCE_FAILURE_REASON.replace("KIS","kis")))
    assert all(not self_heal_allowed(who,why) for who,why in variants)
def main():
    for fn in (test_normal_path_lowers_and_audits_self_heal,test_s1_operator_reason_and_l2_never_lower,test_s2_29_minutes_flap_and_restart_reset,test_t1_single_soft_sample_preserves_window_and_recovers,test_t1_hard_age_resets_immediately,test_t1_chronic_alternating_soft_samples_never_recovers,test_s3_no_go_exception_and_l2_toctou_block,test_s4_once_per_kst_day_and_manual_alert,test_corrupt_state_fail_closed,test_state_write_failure_blocks_before_readiness_and_lower,test_relaxation_env_cannot_weaken_safety_ceiling,test_notification_failure_retries_after_lower,test_pending_notice_is_discarded_after_manual_lower,test_self_heal_allowlist_requires_exact_match): fn()
    print("kill self-heal 14/14 PASS")
if __name__=="__main__": main()
