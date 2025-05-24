# -*- coding: utf-8 -*-
"""
plannerui_front.py (rev-2025-05-24-b)
===============================================================
*FIX* : ‘지도(폴리움)가 잠깐 보였다가 사라지는’ 문제 해결
----------------------------------------------------------------
Streamlit 앱은 **실행이 한 번 끝날 때마다 전체 스크립트를 재실행**합니다.
`st.button()` 안에서만 지도를 그리면, 버튼을 누른 직후 1 회차 렌더링에서만
출력되고 다음 자동 재실행(2 회차)부터는 조건문이 건너뛰어 지도가 사라집니다.

👉 **버튼을 누른 결과(경로·지도)를 `st.session_state`에 저장**하고,
스크립트가 재실행될 때마다 세션 값이 있으면 지도를 다시 그리도록 구조를
분리했습니다. 따라서 한 번 탐색한 경로는 사이드바 값을 바꿔도 유지됩니다.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import requests
import streamlit as st
from streamlit.components.v1 import html as st_html

# ──────────────────────────────────────────────────────────────
# 외부 유틸 · 시각화 모듈 (backend 일부 함수 재사용)
# ──────────────────────────────────────────────────────────────
from planner import (
    draw_map,
    load_prefs,
    save_prefs,
    choose_best_route,
    append_history,
    haversine,
)

try:
    from streamlit_folium import st_folium  # type: ignore
except ImportError:
    st_folium = None

# ──────────────────────────────────────────────────────────────
# API 키, 상수 -------------------------------------------------------------
# ──────────────────────────────────────────────────────────────
_ODSAY_KEY_FILE = Path("odsay_api.txt")
_KAKAO_KEY_FILE = Path("kakao_api.txt")
ODSAY_KEY = os.getenv("ODSAY_KEY") or (
    _ODSAY_KEY_FILE.read_text().strip() if _ODSAY_KEY_FILE.exists() else ""
)
KAKAO_REST_KEY = os.getenv("KAKAO_REST_KEY") or (
    _KAKAO_KEY_FILE.read_text().strip() if _KAKAO_KEY_FILE.exists() else ""
)
HEADERS = {"Authorization": f"KakaoAK {KAKAO_REST_KEY}"}
AVG_WALK_SPEED = 1.3  # m/s

# ──────────────────────────────────────────────────────────────
# Kakao Geocoding ----------------------------------------------------------
# ──────────────────────────────────────────────────────────────


def geocode(addr: str) -> Tuple[float, float]:
    for ep in ("address", "keyword"):
        url = f"https://dapi.kakao.com/v2/local/search/{ep}.json"
        r = requests.get(
            url, headers=HEADERS, params={"query": addr}, timeout=5, verify=False
        )
        r.raise_for_status()
        docs = r.json().get("documents", [])
        if docs:
            return float(docs[0]["y"]), float(docs[0]["x"])
    raise ValueError(f"주소/역 '{addr}' 검색 실패")


def parse_location(s: str) -> Tuple[float, float]:
    try:
        lat, lng = map(float, s.split(","))
        return lat, lng
    except ValueError:
        return geocode(s)


# ──────────────────────────────────────────────────────────────
# ODsay API (프론트 직접 호출) ---------------------------------------------
# ──────────────────────────────────────────────────────────────
_MODE = {1: "SUBWAY", 2: "BUS", 3: "WALK"}


def _segment_from_subpath(sp: dict) -> Dict:
    tp = sp.get("trafficType")
    mode = _MODE.get(tp, "WALK")
    dur = float(sp.get("sectionTime", 0))  # 분
    dist = float(sp.get("distance", 0))  # m

    if tp == 1:
        lane0 = sp.get("lane", [{}])[0]
        name = (
            lane0.get("laneName")
            or lane0.get("name")
            or lane0.get("subwayName")
            or "지하철"
        )
    elif tp == 2:
        name = sp.get("lane", [{}])[0].get("busNo", "버스")
    else:
        name = "도보"
        dur = dist / (AVG_WALK_SPEED * 60)

    coords = [
        (float(x["y"]), float(x["x"]))
        for x in sp.get("passStopList", {}).get("stations", [])
    ]
    return {
        "mode": mode,
        "name": name,
        "distance_m": dist,
        "duration_min": round(dur, 2),
        "crowd": 1,
        "best_car": None,
        "poly": coords,
    }


def odsay_all_routes_front(
    origin: Tuple[float, float], dest: Tuple[float, float]
) -> List[List[Dict]]:
    if not ODSAY_KEY:
        st.error("❌  ODSAY API Key를 찾을 수 없습니다.")
        return []

    common = {
        "apiKey": ODSAY_KEY,
        "lang": 0,
        "output": "json",
        "SX": origin[1],
        "SY": origin[0],
        "EX": dest[1],
        "EY": dest[0],
        "OPT": 0,
        "SearchPathType": 0,
        "reqCoordType": "WGS84GEO",
        "resCoordType": "WGS84GEO",
    }
    endpoints = [
        ("https://api.odsay.com/v1/api/searchPubTransPath", {"SearchType": 0}),
        ("https://api.odsay.com/v1/api/searchPubTransPathT", {"SearchType": 0}),
    ]
    candidates: List[List[Dict]] = []
    for endpoint, extra in endpoints:
        try:
            r = requests.get(
                endpoint, params={**common, **extra}, timeout=8, verify=False
            )
            r.raise_for_status()
            for path in r.json().get("result", {}).get("path", []):
                segs = [_segment_from_subpath(sp) for sp in path.get("subPath", [])]
                if segs:
                    candidates.append(segs)
        except requests.RequestException:
            continue
    return candidates


# ──────────────────────────────────────────────────────────────
# Streamlit UI -------------------------------------------------------------
# ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="멀티모달 경로 플래너 (프론트 API)", layout="wide")

if "prefs" not in st.session_state:
    st.session_state["prefs"] = load_prefs()

p: Dict = st.session_state["prefs"]

# ── 사이드바 : 선호도 ------------------------------------------------------
with st.sidebar:
    st.header("⚙️ 선호도 & 가중치 설정")
    crowd_weight = st.slider(
        "혼잡도 가중치", 0.0, 5.0, float(p.get("crowd_weight", 2.0)), 0.1
    )
    max_crowd = st.slider("허용 최대 혼잡 레벨", 1, 4, int(p.get("max_crowd", 4)), 1)
    walk_limit_min = st.number_input(
        "허용 최대 도보 (분)", 0, 60, int(p.get("walk_limit_min", 15)), 1
    )

    st.subheader("모드별 페널티")
    mp_subway = st.number_input(
        "지하철", 0.0, 10.0, float(p.get("mode_penalty", {}).get("SUBWAY", 0.0)), 0.5
    )
    mp_bus = st.number_input(
        "버스", 0.0, 10.0, float(p.get("mode_penalty", {}).get("BUS", 0.0)), 0.5
    )
    mp_walk = st.number_input(
        "도보", 0.0, 10.0, float(p.get("mode_penalty", {}).get("WALK", 0.0)), 0.5
    )

    if st.button("💾 선호도 저장"):
        save_prefs(
            {
                "crowd_weight": crowd_weight,
                "max_crowd": max_crowd,
                "walk_limit_min": walk_limit_min,
                "mode_penalty": {"SUBWAY": mp_subway, "BUS": mp_bus, "WALK": mp_walk},
                "runs": p.get("runs", 0) + 1,
            }
        )
        st.session_state["prefs"].update(p)
        st.success("✅ 선호도가 저장되었습니다!")

# ── 메인 영역 -------------------------------------------------------------

st.title("🚍 ODsay 멀티모달 경로 플래너 · 프론트 API")

col1, col2 = st.columns(2)
with col1:
    origin_input = st.text_input("출발지 (역명/주소/위도,경도)")
with col2:
    dest_input = st.text_input("도착지 (역명/주소/위도,경도)")

search_clicked = st.button("🚀 경로 탐색")

if search_clicked:
    if not origin_input or not dest_input:
        st.warning("출발지와 도착지를 모두 입력하세요.")
    else:
        try:
            origin = parse_location(origin_input)
            dest = parse_location(dest_input)
        except ValueError as e:
            st.error(str(e))
        else:
            with st.spinner("경로 계산 중…"):
                routes = odsay_all_routes_front(origin, dest)
                best_idx, segs = choose_best_route(routes)
                if not segs:
                    dist = haversine(origin, dest)
                    segs = [
                        {
                            "mode": "WALK",
                            "name": "직선도보",
                            "distance_m": dist,
                            "duration_min": round(dist / (AVG_WALK_SPEED * 60), 2),
                            "crowd": 1,
                            "best_car": None,
                            "poly": [origin, dest],
                        }
                    ]
                total_min = sum(s["duration_min"] for s in segs)

            # 결과 세션에 저장 → 다음 rerun에서도 유지
            st.session_state.update(
                {
                    "segs": segs,
                    "origin_coord": origin,
                    "dest_coord": dest,
                    "total_min": total_min,
                    "origin_input": origin_input,
                    "dest_input": dest_input,
                }
            )

# ── 세션에 경로가 있으면 항상 지도·요약 표시 -----------------------------
if "segs" in st.session_state:
    segs = st.session_state["segs"]
    origin = st.session_state["origin_coord"]
    dest = st.session_state["dest_coord"]
    total_min = st.session_state["total_min"]

    st.subheader("📝 경로 요약")
    for i, s in enumerate(segs, 1):
        st.write(f"{i}. {s['mode']:<6} | {s['name']:<10} | {s['duration_min']:5.1f}분")
    st.success(f"예상 총 소요 시간: {total_min:.1f}분")

    map_obj, _ = draw_map(segs, origin, dest)
    if st_folium:
        st_folium(map_obj, width=900, height=600)
    else:
        st_html(map_obj.get_root().render(), height=600, width=900, scrolling=False)

    if st.checkbox("🧠 이 경로를 학습 기록에 저장"):
        append_history(
            {
                "datetime": datetime.now().isoformat(),
                "origin": st.session_state.get("origin_input", ""),
                "dest": st.session_state.get("dest_input", ""),
                "total_min": total_min,
                "modes": "/".join({s["mode"] for s in segs}),
            }
        )
        st.info("📚 경로 이용 기록이 저장되었습니다.")

# ── 푸터 ---------------------------------------------------------------
st.markdown(
    "---\n"
    "<div style='text-align:center;'>ⓒ 2025 Multimodal Route Planner UI · 개발: JunWooPark</div>",
    unsafe_allow_html=True,
)
