from __future__ import annotations
import json, os, stat, tempfile
from pathlib import Path
from unittest import mock
from bot import deploy_grace, heartbeat
from infra.server import watchdog

def _marker(path, value):
    with open(path,"w",encoding="utf-8") as fp: json.dump(value,fp)

def test_grace_blocks_restart_and_l1():
    state={"restarts":[],"alerted":False,"grace":False}
    with mock.patch.object(deploy_grace,"active",return_value=True), mock.patch.object(heartbeat,"age_s",return_value=300.), mock.patch.object(heartbeat,"sla_status",return_value=heartbeat.HARD_DISABLE), mock.patch.object(watchdog,"_restart_sentinel") as restart, mock.patch.object(watchdog.kill,"raise_level") as raised, mock.patch.object(watchdog.notify,"send",return_value=True): watchdog.check_cycle(state,now=1000)
    assert not restart.called and not raised.called
def test_expired_marker_triggers_existing_restart():
    state={"restarts":[],"alerted":False,"grace":False}
    with mock.patch.object(deploy_grace,"active",return_value=False), mock.patch.object(heartbeat,"age_s",return_value=301.), mock.patch.object(heartbeat,"sla_status",return_value=heartbeat.HARD_DISABLE), mock.patch.object(watchdog,"_restart_sentinel",return_value=True) as restart, mock.patch.object(watchdog.notify,"send",return_value=True): watchdog.check_cycle(state,now=1000)
    assert restart.call_count==1
def test_invalid_future_nonfinite_fail_closed():
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"DEPLOY_GRACE_PATH":f"{tmp}/g.json"}):
        p=f"{tmp}/g.json"; Path(p).write_text("{broken",encoding="utf-8"); assert not deploy_grace.active(now=1000)
        for ts in (1061,float("nan"),float("inf")): _marker(p,{"ts":ts,"sha":"abc"}); assert not deploy_grace.active(now=1000)
def test_ttl_clamped_to_600():
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"DEPLOY_GRACE_PATH":f"{tmp}/g.json","DEPLOY_GRACE_S":"9999"}):
        deploy_grace.write_marker("abc",now=399.); assert deploy_grace.ttl_s()==600 and not deploy_grace.active(now=1000); assert stat.S_IMODE(os.stat(f"{tmp}/g.json").st_mode)==0o644
def test_expiry_immediately_escalates_without_countdown_reset():
    state={"restarts":[500,600,700],"alerted":True,"grace":True}
    with mock.patch.object(deploy_grace,"active",return_value=False), mock.patch.object(heartbeat,"age_s",return_value=130.), mock.patch.object(heartbeat,"sla_status",return_value=heartbeat.HARD_DISABLE), mock.patch.object(watchdog.kill,"raise_level") as raised: watchdog.check_cycle(state,now=1000)
    raised.assert_called_once()
def test_autodeploy_marker_is_immediately_before_restart():
    text=Path("infra/server/autodeploy.sh").read_text(encoding="utf-8"); marker='python3 -m bot.deploy_grace "$REMOTE"'; restart="sudo systemctl restart $SERVICES"
    assert marker in text and text.index(marker)<text.index(restart) and "systemctl" not in text[text.index(marker)+len(marker):text.index(restart)]
def test_existing_recovery_alert_unchanged():
    state={"restarts":[],"alerted":True,"grace":True}; sent=[]
    with mock.patch.object(deploy_grace,"active",return_value=True), mock.patch.object(heartbeat,"age_s",return_value=5.), mock.patch.object(heartbeat,"sla_status",return_value=heartbeat.OK), mock.patch.object(watchdog.notify,"send",side_effect=lambda text,**kw: sent.append(text) or True), mock.patch.object(watchdog.kill_self_heal,"cycle",return_value={"action":"ineligible"}): watchdog.check_cycle(state,now=1000)
    assert any("회복" in x for x in sent) and state["alerted"] is False
def main():
    for fn in (test_grace_blocks_restart_and_l1,test_expired_marker_triggers_existing_restart,test_invalid_future_nonfinite_fail_closed,test_ttl_clamped_to_600,test_expiry_immediately_escalates_without_countdown_reset,test_autodeploy_marker_is_immediately_before_restart,test_existing_recovery_alert_unchanged): fn()
    print("deploy grace 7/7 PASS")
if __name__=="__main__": main()
