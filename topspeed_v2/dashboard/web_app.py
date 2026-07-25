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
)
from dashboard.seed_demo import seed_demo_data
from dashboard.sync_job import run_sync
from dashboard.coupang_client import CoupangClient, CoupangClientUnavailable

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
    start = col1.date_input("시작일", value=date.today())
    end = col2.date_input("종료일", value=date.today())

    rows = fetch_metrics(str(start), str(end))

    if not rows:
        st.info("해당 기간에 집계된 데이터가 없습니다. sync_job.py를 먼저 실행하세요.")
    else:
        total_revenue = sum(r["revenue"] for r in rows)
        total_profit = sum(r["net_profit"] for r in rows)
        total_ad = sum(r["ad_spend"] for r in rows)

        m1, m2, m3 = st.columns(3)
        m1.metric("매출 (판매자배송)", f"{total_revenue:,}원")
        m2.metric("실손익", f"{total_profit:,}원")
        m3.metric("광고비", f"{total_ad:,}원")

        st.subheader("SKU별 상세")
        st.dataframe(rows, use_container_width=True)

        st.subheader("일별 매출 추이")
        st.bar_chart({r["date"]: r["revenue"] for r in rows})

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
                {**p, "stock_days": p["stock_days"] if p["stock_days"] is not None else "예측 불가"}
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
    st.subheader("🔗 쿠팡 실시간 연동")
    st.caption(
        "API 키를 입력하고 동기화하면 오늘 날짜 기준으로 매출·재고를 바로 가져옵니다. "
        "키는 저장되지 않고 이번 세션에서만 사용됩니다."
    )
    with st.form("live_sync_form"):
        c1, c2, c3 = st.columns(3)
        access_key = c1.text_input("COUPANG_ACCESS_KEY", type="password")
        secret_key = c2.text_input("COUPANG_SECRET_KEY", type="password")
        vendor_id = c3.text_input("COUPANG_VENDOR_ID")

        with st.expander("광고 API 키 (선택 — 발급받으셨다면)"):
            ac1, ac2, ac3 = st.columns(3)
            ads_access = ac1.text_input("ADS_ACCESS_KEY", type="password")
            ads_secret = ac2.text_input("ADS_SECRET_KEY", type="password")
            ads_account = ac3.text_input("ADS_ACCOUNT_ID")

        sync_submitted = st.form_submit_button("지금 동기화 실행")

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
            with st.spinner("쿠팡 데이터 가져오는 중..."):
                today_str = str(date.today())
                result = run_sync(
                    target_date=today_str,
                    unit_costs={},
                    access_key=access_key,
                    secret_key=secret_key,
                    vendor_id=vendor_id,
                    ads_access=ads_access or None,
                    ads_secret=ads_secret or None,
                    ads_account=ads_account or None,
                )
            st.code(result)
            if result.startswith("✅") or "완료" in result:
                st.success("동기화 완료! 📊 대시보드 탭에서 확인하세요.")

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
