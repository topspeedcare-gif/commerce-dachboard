"""
dashboard/web_app.py — TOPSPEED 웹 대시보드 (4단계: 배포 가능한 완성형)

실행: streamlit run dashboard/web_app.py

이 파일은 shared/db.py만 읽고 쓴다. assistant/ 폴더의 어떤 파일도 import하지 않는다.
비밀번호는 st.secrets["APP_PASSWORD"]에 설정 (로컬은 .streamlit/secrets.toml,
배포 시엔 Streamlit Cloud 대시보드에서 설정 — README 참고).
"""
from __future__ import annotations

import sys
from datetime import date, datetime
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
)
from dashboard.seed_demo import seed_demo_data
from dashboard.sync_job import run_sync

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


# ── 탭 구성 ────────────────────────────────────────────────
tab_dash, tab_input, tab_inv, tab_settings = st.tabs(
    ["📊 대시보드", "📝 일 마감 입력", "📦 재고 관리", "⚙️ 설정"]
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
        m1.metric("매출", f"{total_revenue:,}원")
        m2.metric("실손익", f"{total_profit:,}원")
        m3.metric("광고비", f"{total_ad:,}원")

        st.subheader("SKU별 상세")
        st.dataframe(rows, use_container_width=True)

        st.subheader("일별 매출 추이")
        st.bar_chart({r["date"]: r["revenue"] for r in rows})

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


# 4) 설정 — 원가·알람 임계치
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
        "재고 부족 기준 (이 수량 이하면 알람)",
        min_value=0, step=1,
        value=int(get_setting("low_stock_floor", "5")),
    )
    if st.button("임계치 저장"):
        set_setting("roas_floor", str(roas_floor))
        set_setting("low_stock_floor", str(low_stock_floor))
        st.success("임계치가 저장되었습니다. 다음 sync_job.py 실행부터 적용됩니다.")

    st.divider()
    st.subheader("⚠️ 데이터 초기화")
    st.caption("실제 쿠팡 연동을 시작하기 전, 샘플 데이터를 지우고 싶을 때 사용하세요.")
    if st.button("샘플/테스트 데이터 전체 삭제", type="secondary"):
        with get_conn() as _c:
            _c.execute("DELETE FROM daily_metrics")
            _c.execute("DELETE FROM inventory")
            _c.execute("DELETE FROM alerts")
        st.success("초기화 완료. 대시보드 탭에서 새로고침 해보세요.")
