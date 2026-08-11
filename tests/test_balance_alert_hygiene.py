from __future__ import annotations
import inspect, os, stat, subprocess, time
from unittest import mock
from bot import balance_health, kis, kis_telegram, ops_status

def test_ten_failures_are_bundled_and_escalated():
    balance_health.reset_for_tests(); sent=[]
    with mock.patch.object(balance_health,"_send",side_effect=lambda x: sent.append(x) or True):
        for t in [0,60,120,180,240,300,360,1860,1920,3660]: balance_health.record_failure({"http_status":500},now=t)
    assert len(sent)==3 and "60분째" in sent[-1] and "10회" in sent[-1]
def test_recovery_once_and_new_incident():
    balance_health.reset_for_tests(); sent=[]
    with mock.patch.object(balance_health,"_send",side_effect=lambda x: sent.append(x) or True):
        balance_health.record_failure(TimeoutError(),now=100); balance_health.record_failure(TimeoutError(),now=160); assert balance_health.record_success(now=700); assert not balance_health.record_success(now=701); balance_health.record_failure(TimeoutError(),now=800)
    assert len(sent)==3 and "2회" in sent[1] and "10분" in sent[1]
def test_delivery_failure_does_not_latch():
    balance_health.reset_for_tests(); send=mock.Mock(side_effect=[False,True,False,True])
    with mock.patch.object(balance_health,"_send",send):
        assert not balance_health.record_failure("x",now=1); assert balance_health.record_failure("x",now=2); assert not balance_health.record_success(now=3); assert balance_health.record_success(now=4)
    assert send.call_count==4
def test_cause_counter_and_diag():
    balance_health.reset_for_tests()
    with mock.patch.object(balance_health,"_send",return_value=True):
        balance_health.record_failure({"http_status":500},now=time.time()); balance_health.record_failure(TimeoutError(),now=time.time()); balance_health.record_failure({"http_status":500,"msg_cd":"EGW00201"},now=time.time())
    assert balance_health.summary()["rate_limit_count"]==1 and stat.S_IMODE(os.stat(os.environ["BALANCE_HEALTH_PATH"]).st_mode)==0o600
    with mock.patch.object(kis,"positions_detail",return_value=[]), mock.patch.object(subprocess,"run",side_effect=OSError("no")): text=kis_telegram._diag_text()
    assert "잔고 실패(24h): 3회" in text and "프로세스 기동 후" in text
def test_snapshot_total_time_budget_and_publishable_shape():
    from bot import sentinel
    def slow(*a,**kw): time.sleep(.2); return []
    start=time.monotonic()
    with mock.patch.object(ops_status,"_kis_budget_s",return_value=.05), mock.patch.object(kis,"positions_detail",side_effect=slow), mock.patch.object(sentinel,"_fetch_positions",return_value=([],1.)), mock.patch.object(subprocess,"run",side_effect=OSError("no")): snap=ops_status.snapshot()
    assert time.monotonic()-start<.15 and all(v is None for v in snap["kis_positions_query"].values()) and snap["kis_query_ok"] is False
def test_sentinel_fail_closed_contract_is_unchanged():
    import bot.sentinel as s
    src=inspect.getsource(s.check_once); assert "bh = {}" in src and "held = feed" in src and "balance_health.record_failure" in src and "place_sell" not in inspect.getsource(balance_health)
def main():
    with __import__('tempfile').TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"BALANCE_HEALTH_PATH":f"{tmp}/health.json"}):
        for fn in (test_ten_failures_are_bundled_and_escalated,test_recovery_once_and_new_incident,test_delivery_failure_does_not_latch,test_cause_counter_and_diag,test_snapshot_total_time_budget_and_publishable_shape,test_sentinel_fail_closed_contract_is_unchanged): fn()
    print("balance alert hygiene 6/6 PASS")
if __name__=="__main__": main()
