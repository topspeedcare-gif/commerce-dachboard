"""
dashboard/web_app.py — TOPSPEED 웹 대시보드 (4단계: 배포 가능한 완성형)

실행: streamlit run dashboard/web_app.py

이 파일은 shared/db.py만 읽고 쓴다. assistant/ 폴더의 어떤 파일도 import하지 않는다.
비밀번호는 st.secrets["APP_PASSWORD"]에 설정 (로컬은 .streamlit/secrets.toml,
배포 시엔 Streamlit Cloud 대시보드에서 설정 — README 참고).
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.db import (
    init_db,
    get_conn,
    upsert_daily_metric,
    upsert_inventory,
    add_alert,
    get_setting,
    set_setting,
    add_experiment,
    update_experiment_status,
    add_inventory_move,
    get_inventory_moves,
    InsufficientStockError,
    get_stock_predictions,
    get_reorder_suggestions,
    REORDER_LEAD_TIME_DAYS_DEFAULT,
    get_rocket_growth_revenue_estimate,
    ROCKET_STOCK_ALERT_DAYS_DEFAULT,
    get_settlement_vs_expected,
    get_channel_inventory,
    get_wing_sales_summary,
)
from dashboard.seed_demo import seed_demo_data
from dashboard.sync_job import run_sync, sync_date_range
from dashboard.settlement_sync import run_settlement_sync
from dashboard.channel_inventory_sync import run_channel_inventory_sync
from dashboard.coupang_client import CoupangClient, CoupangClientUnavailable
from dashboard.ad_report_importer import import_ad_report
from dashboard.sync_health import check_and_alert, read_last_sync_status

st.set_page_config(page_title="TOPSPEED 대시보드", layout="wide")
init_db()


# ── 로그인 게이트 ──────────────────────────────────────────
def check_password() -> bool:
    if st.session_state.get("authed"):
        return True

    st.title("🔒 TOPSPEED 대시보드")
    pw = st.text_input("비밀번호", type="password")
    if st.button("입장"):
        correct = st.secrets.get("APP_PASSWORD", "")
        if not correct:
            st.error("APP_PASSWORD가 설정되지 않았습니다. .streamlit/secrets.toml 확인하세요.")
        elif pw == correct:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("비밀번호가 틀렸습니다.")
    return False


if not check_password():
    st.stop()


# ── 동기화 안전장치: 열 때마다 어제 데이터가 들어왔는지 조용히 확인 ──
# 문제가 없으면 아무것도 안 하고 넘어간다. 문제가 있으면 카톡으로도 알리고
# (하루 한 번만, 중복 발송 방지) 화면에도 배너로 보여준다.
_health = check_and_alert()
if not _health["ok"]:
    st.warning(f"🩺 {_health['message'] or '동기화 상태를 확인해주세요.'}")


# ── 데이터 조회 헬퍼 ───────────────────────────────────────
def fetch_metrics(start: str, end: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_metrics WHERE date BETWEEN ? AND ? ORDER BY date, sku",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]


def fetch_inventory() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM inventory ORDER BY coupang_qty ASC").fetchall()]


def fetch_alerts(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        ]


def fetch_running_experiments() -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM experiments WHERE status = 'running' ORDER BY review_date"
            ).fetchall()
        ]


# 비율 지표는 기간 평균, 누적 지표는 기간 합산이 의미가 맞다.
_EXPERIMENT_RATE_METRICS = {"roas", "ctr", "cvr"}
_EXPERIMENT_METRIC_COLUMNS = {
    "roas", "ctr", "cvr", "revenue", "sales_qty", "net_profit", "ad_spend",
}


def fetch_experiment_actual(sku: str, start_date: str, metric_type: str) -> float | None:
    """실험 시작일부터 오늘까지 daily_metrics에서 실제 지표 실적을 계산한다."""
    if metric_type not in _EXPERIMENT_METRIC_COLUMNS:
        return None
    agg = "AVG" if metric_type in _EXPERIMENT_RATE_METRICS else "SUM"
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {agg}({metric_type}) AS v FROM daily_metrics "
            "WHERE sku = ? AND date >= ? AND date <= date('now', 'localtime')",
            (sku, start_date),
        ).fetchone()
    return row["v"] if row and row["v"] is not None else None


# ── 탭 구성 ────────────────────────────────────────────────
tab_dash, tab_input, tab_inv, tab_exp, tab_settings = st.tabs(
    ["📊 대시보드", "📝 일 마감 입력", "📦 재고 관리", "🧪 실험 관리", "⚙️ 설정"]
)

# 1) 대시보드 — 실데이터 조회
with tab_dash:
    with get_conn() as _c:
        _has_data = _c.execute("SELECT 1 FROM daily_metrics LIMIT 1").fetchone()

    if not _has_data:
        st.warning("아직 데이터가 없습니다. 실제 쿠팡 연동 전, 샘플 데이터로 먼저 둘러보시겠어요?")
        if st.button("🧪 샘플 데이터로 체험하기"):
            msg = seed_demo_data()
            st.success(msg)
            st.rerun()

    col1, col2 = st.columns(2)
    start = col1.date_input("시작일", value=date.today() - timedelta(days=6))
    end = col2.date_input("종료일", value=date.today())

    rows = fetch_metrics(str(start), str(end))
    wing_summary_rows = get_wing_sales_summary(str(start), str(end))
    wing_summary_by_date = {r["date"]: r for r in wing_summary_rows}

    if not rows and not wing_summary_rows:
        st.info("해당 기간에 집계된 데이터가 없습니다. 설정 탭에서 먼저 동기화하세요.")
    else:
        total_revenue = sum(r["gmv"] for r in wing_summary_rows)
        total_qty = sum(r["units_sold"] for r in wing_summary_rows)
        total_profit = sum(r["net_profit"] for r in rows)
        total_ad = sum(r["ad_spend"] for r in rows)

        m1, m2, m3 = st.columns(3)
        m1.metric("매출 (취소·반품 반영, 정확)", f"{total_revenue:,.0f}원")
        m2.metric("판매량", f"{total_qty:,.0f}개")
        m3.metric("광고비", f"{total_ad:,}원")
        st.caption(
            "윙 판매자센터의 '비즈니스 인사이트 > 판매분석'에서 가져온 확정 수치입니다 "
            "(취소·반품이 이미 반영되어 있음) — automation/wing_sync.py가 로그인 세션으로 "
            "가져옵니다. 세션이 오래돼 만료되면 여기 숫자가 갱신을 멈추니, 그럴 땐 PC에서 "
            "'python automation\\wing_login.py'로 다시 로그인해주세요."
        )

        missing_dates = [
            str(start + timedelta(days=i))
            for i in range((end - start).days + 1)
            if str(start + timedelta(days=i)) not in wing_summary_by_date
        ]
        if missing_dates:
            st.warning(f"⚠️ 아래 날짜는 정확한 매출 데이터가 아직 없습니다: {', '.join(missing_dates)}")

        st.caption(
            f"참고: 실손익(약 {total_profit:,.0f}원)은 아직 SKU별 원가·수수료 추정치(로켓그로스 "
            "주문 API 기준, 취소·반품 미반영)로 계산됩니다 — 위 매출과 정확히 맞물리진 않습니다."
        )

        if rows:
            st.subheader("SKU별 상세 (참고용 — 취소·반품 미반영 추정치)")
            st.dataframe(rows, use_container_width=True)

        st.subheader("일별 매출 추이 (정확)")
        st.bar_chart({r["date"]: r["gmv"] for r in wing_summary_rows})

        if rows:
            by_date: dict[str, dict] = {}
            for r in rows:
                d = by_date.setdefault(r["date"], {"ad_spend": 0, "net_profit": 0})
                d["ad_spend"] += r["ad_spend"]
                d["net_profit"] += r["net_profit"]

            st.subheader("일별 광고비 추이")
            st.bar_chart({d: v["ad_spend"] for d, v in by_date.items()})

            st.subheader("일별 순이익 추이 (추정)")
            st.bar_chart({d: v["net_profit"] for d, v in by_date.items()})

    st.subheader("🚀 로켓그로스 추정 매출")
    st.caption(
        "쿠팡 공식 API는 로켓그로스 매출을 실시간으로 안 줘서(정산 확정까지 9일+ 지연), "
        "최근 30일 판매속도 x 판매가로 추정한 값입니다. 정확한 당일 매출이 아닙니다."
    )
    rg_estimate = get_rocket_growth_revenue_estimate()
    if not rg_estimate["items"]:
        st.write("추정할 데이터가 없습니다 (쿠팡 동기화를 먼저 실행하세요).")
    else:
        rc1, rc2 = st.columns(2)
        rc1.metric("추정 일평균 매출", f"약 {rg_estimate['total_estimated_daily_revenue']:,}원")
        rc2.metric("추정 일평균 판매량", f"약 {rg_estimate['total_estimated_daily_qty']}개")
        with st.expander("SKU별 추정 매출 보기"):
            st.dataframe(rg_estimate["items"], use_container_width=True)

    st.subheader("💰 정산 현황")
    st.caption(
        "쿠팡이 실제로 확정한 매출인식 내역입니다 (판매일보다 약 9일 늦게 확정되어 보입니다 — "
        "최근 며칠은 아직 안 보이는 게 정상입니다). expected_revenue는 시스템이 계산한 예상치로, "
        "판매자배송 위주라 로켓그로스가 섞인 실제 확정액보다 작게 나오는 게 정상입니다."
    )
    if st.button("🔄 정산 동기화 실행 (최근 25일)"):
        with st.spinner("쿠팡 매출인식 내역 가져오는 중..."):
            st.code(run_settlement_sync())
        st.rerun()

    settlement_rows = get_settlement_vs_expected(
        str(date.today() - timedelta(days=40)), str(date.today())
    )
    if not settlement_rows:
        st.write("정산 데이터가 없습니다. 위 버튼으로 먼저 동기화하세요.")
    else:
        st.dataframe(settlement_rows, use_container_width=True)

    st.subheader("🔔 최근 알람")
    alerts = fetch_alerts()
    if not alerts:
        st.write("알람 없음")
    for a in alerts:
        st.write(f"`{a['created_at'][:16]}` {a['message']}")


# 2) 일 마감 입력 — 손두껍 앱의 "일 마감 입력" 대응
with tab_input:
    st.write("API로 자동 수집되지 않는 값(원가 변경, 특이사항)을 수동으로 기록합니다.")

    with st.form("daily_note_form"):
        note_date = st.date_input("날짜", value=date.today())
        note_sku = st.text_input("SKU (선택)")
        note_text = st.text_area("특이사항 메모")
        submitted = st.form_submit_button("저장")

    if submitted and note_text:
        add_alert(
            kind="note",
            message=f"[{note_date}] {note_text}",
            created_at=datetime.now().isoformat(),
            sku=note_sku or None,
        )
        st.success("저장되었습니다.")
        st.rerun()


# 3) 재고 관리 — 수동 조정
with tab_inv:
    inv = fetch_inventory()
    st.dataframe(inv, use_container_width=True)

    st.subheader("재고 수동 조정")
    with st.form("inventory_form"):
        sku = st.text_input("SKU")
        product_name = st.text_input("상품명")
        coupang_qty = st.number_input("쿠팡 재고", min_value=0, step=1)
        office_qty = st.number_input("사무실 재고", min_value=0, step=1)
        submitted = st.form_submit_button("업데이트")

    if submitted and sku:
        upsert_inventory(sku, product_name, int(coupang_qty), int(office_qty), datetime.now().isoformat())
        st.success(f"{sku} 재고 업데이트 완료")
        st.rerun()

    st.divider()
    st.subheader("📥 입고 등록")
    st.caption("1차 물류가 사무실 창고로 들어올 때 등록합니다.")
    with st.form("inbound_form"):
        in_sku = st.text_input("SKU", key="in_sku")
        in_qty = st.number_input("수량", min_value=1, step=1, key="in_qty")
        in_note = st.text_input("메모 (선택)", key="in_note")
        in_submitted = st.form_submit_button("입고 등록")
    if in_submitted:
        if not in_sku:
            st.error("SKU를 입력하세요.")
        else:
            add_inventory_move(in_sku, "inbound", int(in_qty), datetime.now().isoformat(), note=in_note or None)
            st.success(f"{in_sku} {int(in_qty)}개 입고 등록 완료")
            st.rerun()

    st.subheader("🚚 쿠팡 이관")
    st.caption("사무실 창고 재고를 쿠팡(로켓그로스) 창고로 이관합니다.")
    with st.form("transfer_form"):
        tr_sku = st.text_input("SKU", key="tr_sku")
        tr_qty = st.number_input("수량", min_value=1, step=1, key="tr_qty")
        tr_submitted = st.form_submit_button("쿠팡 이관 등록")
    if tr_submitted:
        if not tr_sku:
            st.error("SKU를 입력하세요.")
        else:
            try:
                add_inventory_move(tr_sku, "transfer_to_coupang", int(tr_qty), datetime.now().isoformat())
                st.success(f"{tr_sku} {int(tr_qty)}개 쿠팡 이관 완료")
                st.rerun()
            except InsufficientStockError as exc:
                st.error(f"❌ {exc}")

    st.subheader("📦 기타채널 출고")
    st.caption("네이버·토스 등 다른 채널 주문을 사무실 창고에서 일반택배로 출고할 때 등록합니다.")
    with st.form("outbound_form"):
        ob_sku = st.text_input("SKU", key="ob_sku")
        ob_qty = st.number_input("수량", min_value=1, step=1, key="ob_qty")
        ob_channel = st.selectbox("채널", ["네이버", "토스", "기타"], key="ob_channel")
        ob_submitted = st.form_submit_button("출고 등록")
    if ob_submitted:
        if not ob_sku:
            st.error("SKU를 입력하세요.")
        else:
            try:
                add_inventory_move(ob_sku, "outbound_other", int(ob_qty), datetime.now().isoformat(), channel=ob_channel)
                st.success(f"{ob_sku} {int(ob_qty)}개 {ob_channel} 출고 완료")
                st.rerun()
            except InsufficientStockError as exc:
                st.error(f"❌ {exc}")

    st.divider()
    st.subheader("📦 통합 재고 현황 (윙 x 로켓그로스)")
    st.caption(
        "상품명 기준으로 윙(판매자배송) 재고와 로켓그로스 재고를 나란히 보여줍니다. "
        "옵션마다 API 조회가 따로 필요해서(상품 약 60개 기준 40초 안팎) 자동 동기화엔 안 들어있고, "
        "아래 버튼으로 필요할 때 갱신합니다."
    )
    if st.button("🔄 통합 재고 동기화 (약 40초 소요)"):
        with st.spinner("윙 + 로켓그로스 재고를 상품별로 조회하는 중... (40초 안팎)"):
            st.code(run_channel_inventory_sync())
        st.rerun()

    channel_rows = get_channel_inventory()
    if not channel_rows:
        st.write("데이터가 없습니다. 위 버튼으로 먼저 동기화하세요.")
    else:
        st.caption(f"최근 동기화: {channel_rows[0]['synced_at'][:16]}")
        st.dataframe(channel_rows, use_container_width=True)

    st.divider()
    st.subheader("최근 재고 이동 이력 (최근 20건)")
    moves = get_inventory_moves(limit=20)
    if not moves:
        st.write("이동 이력이 없습니다.")
    else:
        st.dataframe([dict(m) for m in moves], use_container_width=True)

    st.divider()
    lead_time = int(get_setting("reorder_lead_time_days", str(REORDER_LEAD_TIME_DAYS_DEFAULT)))
    st.subheader("📉 품절 예측")
    st.caption("최근 14일 평균 판매속도 기준 예상 품절일수 (판매 이력 없는 SKU는 예측 불가로 표시)")
    predictions = [p for p in get_stock_predictions() if p["daily_velocity"] > 0 or p["total_qty"] > 0]
    if not predictions:
        st.write("재고 데이터가 없습니다.")
    else:
        st.dataframe(
            [
                # stock_days를 숫자/문자 섞어서 넣으면 Arrow 직렬화가 깨진다 —
                # 컬럼 하나는 타입을 통일해야 해서, None만 있던 곳도 전부 문자열로 맞춘다.
                {**p, "stock_days": f"{p['stock_days']}일" if p["stock_days"] is not None else "예측 불가"}
                for p in predictions
            ],
            use_container_width=True,
        )

    st.subheader("🚨 발주 제안")
    st.caption(f"예상 품절일수가 (발주 리드타임 {lead_time}일 + 안전재고 7일) 이내인 SKU만 표시됩니다. 리드타임은 ⚙️ 설정 탭에서 바꿀 수 있어요.")
    suggestions = get_reorder_suggestions()
    if not suggestions:
        st.write("지금 발주가 필요한 SKU가 없습니다.")
    else:
        st.dataframe(suggestions, use_container_width=True)


# 4) 실험 관리 — 신상품/광고 테스트 가설 등록·판정
with tab_exp:
    st.write("신상품·광고 테스트를 시작할 때 가설(기준치)을 기록해두고, 판정 예정일이 지나면 실제 성과와 비교합니다.")

    with st.form("experiment_form"):
        exp_sku = st.text_input("SKU")
        exp_hypothesis = st.text_area("가설", placeholder="예: 신규 패드 적용 시 ROAS 2.0 이상 나올 것")
        c1, c2, c3 = st.columns(3)
        exp_metric = c1.selectbox("기준 지표", sorted(_EXPERIMENT_METRIC_COLUMNS))
        exp_target = c2.number_input("목표치", min_value=0.0, step=0.1)
        exp_review = c3.date_input("판정 예정일", value=date.today() + timedelta(days=14))
        exp_submitted = st.form_submit_button("실험 등록")

    if exp_submitted:
        if not exp_sku or not exp_hypothesis:
            st.error("SKU와 가설은 필수입니다.")
        else:
            add_experiment(
                sku=exp_sku,
                hypothesis=exp_hypothesis,
                start_date=str(date.today()),
                review_date=str(exp_review),
                metric_type=exp_metric,
                target_value=float(exp_target),
                created_at=datetime.now().isoformat(),
            )
            st.success(f"{exp_sku} 실험 등록 완료 (판정 예정일: {exp_review})")
            st.rerun()

    st.divider()
    st.subheader("진행 중인 실험")
    running = fetch_running_experiments()
    if not running:
        st.write("진행 중인 실험이 없습니다.")
    else:
        today_str = str(date.today())
        for exp in running:
            actual = fetch_experiment_actual(exp["sku"], exp["start_date"], exp["metric_type"])
            overdue = exp["review_date"] <= today_str
            actual_text = f"{actual:.2f}" if actual is not None else "데이터 없음"
            verdict = ""
            if actual is not None:
                verdict = " · ✅ 목표 달성" if actual >= exp["target_value"] else " · ⚠️ 목표 미달"
            title = (
                f"{'🔴 [판정 필요] ' if overdue else ''}"
                f"{exp['sku']} — {exp['metric_type'].upper()} 목표 {exp['target_value']} / 실제 {actual_text}{verdict}"
            )
            with st.expander(title, expanded=overdue):
                st.write(f"**가설:** {exp['hypothesis']}")
                st.write(f"시작일: `{exp['start_date']}` · 판정 예정일: `{exp['review_date']}`")
                bcol1, bcol2 = st.columns(2)
                if bcol1.button("✅ 계속 진행", key=f"exp_continue_{exp['id']}"):
                    update_experiment_status(exp["id"], "decided_continue")
                    st.rerun()
                if bcol2.button("🛑 중단", key=f"exp_stop_{exp['id']}"):
                    update_experiment_status(exp["id"], "decided_stop")
                    st.rerun()


# 5) 설정 — 원가·알람 임계치
with tab_settings:
    st.subheader("🩺 자동 동기화 상태")
    _last_status = read_last_sync_status()
    if not _last_status:
        st.caption("automation/Daily_sync.py가 아직 한 번도 실행되지 않았거나, 상태 파일이 이 배포본엔 없습니다 (로컬 PC에만 남는 파일이라 Streamlit Cloud에는 안 보일 수 있어요).")
    else:
        _stage_icon = lambda ok: "✅" if ok else "❌"
        st.caption(
            f"마지막 자동 동기화 대상일: {_last_status.get('target_date', '?')} "
            f"(기록 시각: {_last_status.get('finished_at', '?')})\n\n"
            f"동기화 {_stage_icon(_last_status.get('sync_ok'))} · "
            f"git commit {_stage_icon(_last_status.get('git_committed'))} · "
            f"git push {_stage_icon(_last_status.get('git_pushed'))} · "
            f"카톡 발송 {_stage_icon(_last_status.get('kakao_sent'))}"
        )
    if not _health["ok"]:
        st.caption(f"⚠️ 방금 확인: {_health['message']}")

    st.subheader("🔗 쿠팡 실시간 연동")
    st.warning(
        "⚠️ **이 아래 동기화 버튼은 지금 보고 계신 배포된(Streamlit Cloud) 화면에서는 항상 실패합니다.** "
        "쿠팡 오픈API는 미리 등록해둔 IP에서 온 요청만 받는데, 이 버튼을 누르면 브라우저가 아니라 "
        "Streamlit 서버 컴퓨터가 쿠팡에 요청을 보내고, 그 서버의 IP는 등록되어 있지 않아서 "
        "'HTTP 403 Forbidden'으로 막힙니다. 실제 데이터는 PC의 자동 동기화(매일 자동 실행)로만 들어오고, "
        "지금 당장 최신 데이터를 넣고 싶으면 대시보드가 아니라 **PC에서 직접** "
        "`python automation\\Daily_sync.py`를 실행해주세요. (PC에서 직접 `streamlit run`으로 이 화면을 켰을 때는 "
        "PC의 등록된 IP로 나가므로 이 버튼이 정상 작동합니다.)"
    )
    _secret_access_key = st.secrets.get("COUPANG_ACCESS_KEY", "")
    _secret_secret_key = st.secrets.get("COUPANG_SECRET_KEY", "")
    _secret_vendor_id = st.secrets.get("COUPANG_VENDOR_ID", "")
    _keys_from_secrets = bool(_secret_access_key and _secret_secret_key and _secret_vendor_id)
    if _keys_from_secrets:
        st.caption(
            "✅ 쿠팡 API 키가 Streamlit Secrets에 등록되어 있어 자동으로 채워집니다 — 매번 입력하지 않아도 됩니다. "
            "다른 계정으로 임시로 조회하고 싶을 때만 아래 값을 직접 바꾸세요."
        )
    else:
        st.caption(
            "매번 키를 입력하지 않으려면: 이 앱의 우측 하단 'Manage app' → Settings → Secrets에 "
            "COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, COUPANG_VENDOR_ID를 등록해두세요 "
            "(APP_PASSWORD 등록했던 곳과 같은 화면입니다). 등록해두면 이 페이지가 자동으로 채워줍니다."
        )
    st.caption(
        "쿠팡 주문서(ordersheets) API 기준이라 실시간입니다 — 오늘 날짜도 바로 동기화됩니다. "
        "판매자배송(윙) 매출만 잡히고, 로켓그로스는 별도 추정치로 아래에 표시됩니다. "
        "과거 날짜도 자유롭게 선택할 수 있어요 (최대 6개월 전까지). 하루하루 순서대로 "
        "조회하는 방식이라 기간이 길수록 오래 걸립니다(실측 약 9초/일)."
    )
    with st.form("live_sync_form"):
        c1, c2, c3 = st.columns(3)
        access_key = c1.text_input("COUPANG_ACCESS_KEY", type="password", value=_secret_access_key)
        secret_key = c2.text_input("COUPANG_SECRET_KEY", type="password", value=_secret_secret_key)
        vendor_id = c3.text_input("COUPANG_VENDOR_ID", value=_secret_vendor_id)

        d1, d2 = st.columns(2)
        sync_start = d1.date_input(
            "동기화 시작일", value=date.today() - timedelta(days=6),
            min_value=date.today() - timedelta(days=183),
            help="최대 6개월 전까지 선택할 수 있습니다.",
        )
        sync_end = d2.date_input("동기화 종료일", value=date.today())

        sync_submitted = st.form_submit_button("동기화 실행")

    diag_col, _ = st.columns([1, 3])
    if diag_col.button("🔍 연결만 먼저 진단하기"):
        if not (access_key and secret_key and vendor_id):
            st.error("진단하려면 위 3개 키를 먼저 입력하세요.")
        else:
            with st.spinner("진단 중..."):
                try:
                    diag_client = CoupangClient(access_key, secret_key, vendor_id)
                    basic = diag_client.diagnose()
                    st.write("**기본 상품 조회 (인증 검증용)**", basic)

                    try:
                        rg = diag_client.list_rocket_growth_inventory()
                        st.write(f"✅ 로켓그로스 재고 API: 정상 ({len(rg)}건)")
                    except CoupangClientUnavailable as exc:
                        st.write(f"❌ 로켓그로스 재고 API 실패: `{exc}`")

                    try:
                        sl = diag_client.list_seller_inventory()
                        st.write(f"✅ 판매자배송 재고 API: 정상 ({len(sl)}건)")
                    except CoupangClientUnavailable as exc:
                        st.write(f"❌ 판매자배송 재고 API 실패: `{exc}`")

                except CoupangClientUnavailable as exc:
                    st.error(f"기본 연결부터 실패: {exc}")

    if sync_submitted:
        if not (access_key and secret_key and vendor_id):
            st.error("쿠팡 오픈API 키 3개(ACCESS/SECRET/VENDOR)는 필수입니다.")
        else:
            days = (sync_end - sync_start).days + 1
            est_minutes = max(1, round(days * 9 / 60))  # 실측 약 9초/일 기준
            with st.spinner(f"쿠팡 데이터 가져오는 중... ({days}일치, 대략 {est_minutes}분 예상)"):
                result = sync_date_range(
                    start_date=str(sync_start),
                    end_date=str(sync_end),
                    unit_costs={},
                    access_key=access_key,
                    secret_key=secret_key,
                    vendor_id=vendor_id,
                )
            st.code(result)
            if "✅" in result:
                st.success("동기화 완료! 📊 대시보드 탭에서 확인하세요.")

    st.divider()
    st.subheader("📢 광고 리포트 등록")
    st.caption(
        "쿠팡은 셀러용 광고 성과 공개 API를 제공하지 않습니다 (확인 완료). "
        "쿠팡 윙 광고관리센터 > '기간별 키워드' 리포트를 엑셀로 받아 여기에 올리면, "
        "이미 동기화된 매출·판매량 위에 광고비만 반영해서 순이익·ROAS를 다시 계산합니다."
    )
    ad_file = st.file_uploader("광고 리포트 엑셀 (.xlsx)", type=["xlsx"], key="ad_report_upload")
    if ad_file is not None and st.button("이 파일 등록하기"):
        temp_path = Path("runtime_workspace") / "_uploads" / ad_file.name
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(ad_file.getvalue())
        with st.spinner("광고 리포트 반영 중..."):
            result = import_ad_report(temp_path)
        st.code(result)
        if result.startswith("✅"):
            st.success("등록 완료! 📊 대시보드 탭에서 순이익/ROAS 확인하세요.")

    st.divider()
    st.subheader("SKU별 원가 설정")
    with st.form("cost_form"):
        cost_sku = st.text_input("SKU")
        cost_value = st.number_input("원가 (원)", min_value=0, step=1000)
        submitted = st.form_submit_button("저장")
    if submitted and cost_sku:
        set_setting(f"unit_cost:{cost_sku}", str(int(cost_value)))
        st.success(f"{cost_sku} 원가 {int(cost_value):,}원으로 저장됨")

    st.subheader("알람 임계치")
    roas_floor = st.number_input(
        "ROAS 하한선 (이 값 미만이면 알람)",
        min_value=0.0, step=0.1,
        value=float(get_setting("roas_floor", "1.5")),
    )
    low_stock_floor = st.number_input(
        "재고 부족 기준 — 판매 이력 없는 신상품용 (이 수량 이하면 알람)",
        min_value=0, step=1,
        value=int(get_setting("low_stock_floor", "5")),
    )
    rocket_stock_alert_days = st.number_input(
        "🚀 로켓그로스 재고 부족 기준 (일) — 판매속도 대비 이 일수 이하로 남으면 알람",
        min_value=1, step=1,
        value=int(get_setting("rocket_stock_alert_days", str(ROCKET_STOCK_ALERT_DAYS_DEFAULT))),
    )
    office_stock_floor = st.number_input(
        "사무실 재고 부족 기준 (이 수량 이하면 입고 필요 알람)",
        min_value=0, step=1,
        value=int(get_setting("low_office_stock_floor", "10")),
    )
    reorder_lead_time = st.number_input(
        "발주 리드타임 (일) — 발주해서 재고가 도착하기까지 걸리는 기간",
        min_value=1, step=1,
        value=int(get_setting("reorder_lead_time_days", str(REORDER_LEAD_TIME_DAYS_DEFAULT))),
    )
    if st.button("임계치 저장"):
        set_setting("roas_floor", str(roas_floor))
        set_setting("low_stock_floor", str(low_stock_floor))
        set_setting("rocket_stock_alert_days", str(rocket_stock_alert_days))
        set_setting("low_office_stock_floor", str(office_stock_floor))
        set_setting("reorder_lead_time_days", str(reorder_lead_time))
        st.success("임계치가 저장되었습니다. 다음 sync_job.py 실행부터 적용됩니다.")

    st.divider()
    st.subheader("⚠️ 데이터 초기화")
    st.caption("실제 쿠팡 연동을 시작하기 전, 샘플 데이터를 지우고 싶을 때 사용하세요.")
    if st.button("샘플/테스트 데이터 전체 삭제", type="secondary"):
        with get_conn() as _c:
            _c.execute("DELETE FROM daily_metrics")
            _c.execute("DELETE FROM inventory")
            _c.execute("DELETE FROM alerts")
            _c.execute("DELETE FROM inventory_moves")
            _c.execute("DELETE FROM experiments")
        st.success("초기화 완료. 대시보드 탭에서 새로고침 해보세요.")
