"""KIS 잔고·주문 연속페이지 완전성 계약."""
from __future__ import annotations

import io
import json
import os
import threading
from unittest import mock

from bot import balance_health, kis, kis_reconcile


def _page(rows, *, suffix="200", fk="", nk="", cont="", key="output1",
          rt="0", output2=None):
    return {
        "rt_cd": rt, key: rows, "output2": [] if output2 is None else output2,
        f"ctx_area_fk{suffix}": fk, f"ctx_area_nk{suffix}": nk,
        "_tr_cont": cont,
    }


def test_two_pages_merge_and_next_header_contract():
    kis._set_get_failure(None)
    calls = []
    pages = [
        _page([{"id": 1}], fk=" FK-1 ", nk=" NK-1 ", cont="F",
              output2=[{"summary": 1}]),
        _page([{"id": 2}], output2=[{"summary": 2}]),
    ]

    def get(path, tr, params, **kwargs):
        calls.append((dict(params), dict(kwargs)))
        return pages[len(calls) - 1]

    params = {"CTX_AREA_FK200": "", "CTX_AREA_NK200": "", "X": "fixed"}
    with mock.patch.object(kis, "_get", side_effect=get):
        out = kis._get_all_pages("/read", "TR", params, suffix="200",
                                 row_keys=("output1",))
    assert [row["id"] for row in out["output1"]] == [1, 2]
    assert out["output2"] == [{"summary": 2}]
    assert out["_pagination_complete"] is True and out["_pagination_pages"] == 2
    assert not any(key.lower().startswith("ctx_area_") for key in out)
    assert "_tr_cont" not in out and "tr_cont" not in out
    assert calls[0][1] == {}
    assert calls[1] == ({"CTX_AREA_FK200": "FK-1", "CTX_AREA_NK200": "NK-1",
                         "X": "fixed"}, {"tr_cont": "N"})

    class Resp(io.BytesIO):
        headers = {"tr_cont": ""}
        def __enter__(self): return self
        def __exit__(self, *args): return False

    captured = {}
    def urlopen(req, timeout=None):
        captured.update({key.lower(): value for key, value in req.header_items()})
        return Resp(json.dumps({"rt_cd": "0", "output": []}).encode())
    with mock.patch.object(kis, "_token", return_value="token"), \
            mock.patch.object(kis, "_cred", return_value=("key", "secret")), \
            mock.patch.object(kis._LIMITER, "acquire", return_value=True), \
            mock.patch.object(kis.urllib.request, "urlopen", side_effect=urlopen):
        assert kis._get("/read", "TR", {}, tr_cont="N")["rt_cd"] == "0"
    assert captured["tr_cont"] == "N"


def test_external_response_cannot_forge_pagination_completeness():
    class Resp(io.BytesIO):
        headers = {"tr_cont": ""}
        def __enter__(self): return self
        def __exit__(self, *args): return False

    forged = {
        "rt_cd": "0",
        "output": [{"odno": str(idx)} for idx in range(15)],
        "_pagination_complete": True,
        "_pagination_pages": 1,
        "_pagination_vendor_claim": "trusted",
    }
    with mock.patch.object(kis, "_token", return_value="token"), \
            mock.patch.object(kis, "_cred", return_value=("key", "secret")), \
            mock.patch.object(kis._LIMITER, "acquire", return_value=True), \
            mock.patch.object(kis.urllib.request, "urlopen", return_value=Resp(
                json.dumps(forged).encode())):
        response = kis._get("/read", "TR", {})
    assert response is not None
    assert not any(str(key).startswith("_pagination") for key in response)
    assert kis_reconcile.trusted_response_rows(response) is None


def test_middle_page_none_or_rt_failure_discards_everything():
    first = _page([{"id": 1}], fk="F1", nk="N1", cont="F")
    for failed in (None, _page([], rt="1")):
        kis._set_get_failure(None)
        with mock.patch.object(kis, "_get", side_effect=[first, failed]) as get:
            out = kis._get_all_pages("/read", "TR", {}, suffix="200",
                                     row_keys=("output1",))
        assert out is None and get.call_count == 2
        assert kis.last_get_failure()["exception"].startswith("Pagination")


def test_repeated_context_is_finite_and_untrusted():
    repeated = _page([{"id": 1}], fk="F1", nk="N1", cont="F")
    with mock.patch.object(kis, "_get", side_effect=[repeated, repeated]) as get:
        assert kis._get_all_pages("/read", "TR", {}, suffix="200",
                                  row_keys=("output1",)) is None
    assert get.call_count == 2
    assert kis.last_get_failure()["exception"] == "PaginationContextRepeated"
    header_only = _page([{"id": 1}], cont="F")
    with mock.patch.object(kis, "_get", return_value=header_only) as get:
        assert kis._get_all_pages("/read", "TR", {}, suffix="200",
                                  row_keys=("output1",)) is None
    assert get.call_count == 1
    assert kis.last_get_failure()["exception"] == "PaginationContextMissing"


def test_page_limit_discards_partial_rows():
    pages = [_page([{"id": 1}], fk="F1", nk="N1", cont="F"),
             _page([{"id": 2}], fk="F2", nk="N2", cont="M")]
    with mock.patch.dict(os.environ, {"KIS_MAX_PAGES": "2"}), \
            mock.patch.object(kis, "_get", side_effect=pages) as get:
        assert kis._get_all_pages("/read", "TR", {}, suffix="200",
                                  row_keys=("output1",)) is None
    assert get.call_count == 2
    assert kis.last_get_failure()["exception"] == "PaginationPageLimit"
    exhausted = [pages[0], _page([{"id": 2}])]
    with mock.patch.dict(os.environ, {"KIS_MAX_PAGES": "2"}), \
            mock.patch.object(kis, "_get", side_effect=exhausted):
        out = kis._get_all_pages("/read", "TR", {}, suffix="200",
                                 row_keys=("output1",))
    assert [row["id"] for row in out["output1"]] == [1, 2]


def test_single_page_keeps_data_and_calls_once():
    page = _page([{"id": 1}], output2={"cash": "100"})
    with mock.patch.object(kis, "_get", return_value=page) as get:
        out = kis._get_all_pages("/read", "TR", {}, suffix="200",
                                 row_keys=("output1",))
    assert get.call_count == 1 and out["output1"] == page["output1"]
    assert out["output2"] == page["output2"] and out["_pagination_pages"] == 1
    with mock.patch.object(kis, "_get") as get:
        assert kis._get_all_pages(
            "/read", "TR", {"CTX_AREA_NK200": "stale"}, suffix="200",
            row_keys=("output1",)) is None
    assert get.call_count == 0
    assert kis.last_get_failure()["exception"] == "PaginationInitialContextUntrusted"


def test_holdings_and_positions_accept_exhausted_merge_and_label_drops():
    rows = [{"ovrs_pdno": f"S{idx}", "ovrs_cblc_qty": "1",
             "pchs_avg_pric": "10", "ovrs_now_pric1": "11"}
            for idx in range(30)]
    complete = {"rt_cd": "0", "output1": rows,
                "_pagination_complete": True, "_pagination_pages": 2}
    with mock.patch.object(kis, "overseas_balance", return_value=complete):
        assert len(kis.holdings("US")) == 30
        assert len(kis.positions_detail("US")) == 30

    incomplete = {"rt_cd": "0", "output1": rows,
                  "ctx_area_nk200": "NEXT"}
    with mock.patch.object(kis, "overseas_balance", return_value=incomplete):
        assert kis.holdings("US") is None
    detail = kis.last_get_failure()
    assert balance_health.cause_label(detail) != "unknown"
    assert detail["exception"] == "BalancePaginationIncomplete"

    invalid_qty = {"rt_cd": "0", "output1": [
        {"ovrs_pdno": "BAD", "ovrs_cblc_qty": "not-a-number"}]}
    with mock.patch.object(kis, "overseas_balance", return_value=invalid_qty):
        assert kis.positions_detail("US") is None
    assert kis.last_get_failure()["exception"] == "PositionsQuantityUntrusted"

    barrier = threading.Barrier(2)
    labels = []
    def isolated(reason):
        kis._consumer_untrusted(reason)
        barrier.wait()
        labels.append(kis.last_get_failure()["exception"])
    threads = [threading.Thread(target=isolated, args=(reason,))
               for reason in ("ThreadA", "ThreadB")]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(labels) == ["ThreadA", "ThreadB"]


def test_trusted_rows_accept_only_proven_complete_merge():
    rows = [{"odno": str(idx)} for idx in range(30)]
    complete = {"rt_cd": "0", "output": rows,
                "_pagination_complete": True, "_pagination_pages": 2}
    assert len(kis_reconcile.trusted_response_rows(complete)) == 30
    assert kis_reconcile.trusted_response_rows(
        {**complete, "_pagination_complete": False}) is None
    assert kis_reconcile.trusted_response_rows(
        {**complete, "_pagination_pages": 0}) is None
    assert kis_reconcile.trusted_response_rows(
        {**complete, "ctx_area_nk200": "NEXT"}) is None
    assert kis_reconcile.trusted_response_rows(
        {"rt_cd": "0", "output": rows}) is None


def test_all_balance_and_order_queries_use_page_helper():
    sentinel = {"rt_cd": "0", "output": [], "_pagination_complete": True,
                "_pagination_pages": 1}
    calls = []
    def pages(path, tr, params, *, suffix, row_keys):
        calls.append((path, dict(params), suffix, row_keys))
        return sentinel
    with mock.patch.object(kis, "account", return_value={"CANO": "1",
                                                          "ACNT_PRDT_CD": "01"}), \
            mock.patch.object(kis, "enabled", return_value=True), \
            mock.patch.object(kis, "_get_all_pages", side_effect=pages):
        results = [kis.overseas_balance(), kis.open_orders(), kis.fills(),
                   kis.domestic_balance(), kis.domestic_open_orders(),
                   kis.domestic_fills(), kis.domestic_unfilled_orders()]
    assert all(result is sentinel for result in results) and len(calls) == 7
    assert [suffix for _path, _params, suffix, _keys in calls].count("200") == 3
    assert [suffix for _path, _params, suffix, _keys in calls].count("100") == 4
    assert all(any(key.startswith("CTX_AREA_FK") for key in params)
               and any(key.startswith("CTX_AREA_NK") for key in params)
               for _path, params, _suffix, _keys in calls)


def main():
    tests = (
        test_two_pages_merge_and_next_header_contract,
        test_external_response_cannot_forge_pagination_completeness,
        test_middle_page_none_or_rt_failure_discards_everything,
        test_repeated_context_is_finite_and_untrusted,
        test_page_limit_discards_partial_rows,
        test_single_page_keeps_data_and_calls_once,
        test_holdings_and_positions_accept_exhausted_merge_and_label_drops,
        test_trusted_rows_accept_only_proven_complete_merge,
        test_all_balance_and_order_queries_use_page_helper,
    )
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("\nKIS balance pagination 9/9 PASS")


if __name__ == "__main__":
    main()
