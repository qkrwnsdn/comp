# -*- coding: utf-8 -*-
"""
Streamlit UI (front‑call edition)
=================================
변경 핵심
---------
* **ODsay API 호출을 브라우저(프론트엔드)에서 수행**합니다.
  Web 플랫폼용 API‑KEY를 노출해도 정상 동작하도록 ODsay 정책을 준수합니다.
  (ODsay LAB 게시물에서 안내한 ‘프론트엔드 = Web 키’ 규칙)
* 서버‑사이드 Python 은 **경로 스코어링 및 지도 그리기**만 담당합니다.
* 새로운 의존성 **`streamlit‑javascript`**(MIT ‑ https://github.com/victoriadrake/streamlit‑javascript) 를 추가합니다.
  브라우저에서 실행한 JS 의 반환값을 파이썬으로 가져오기 위해 사용합니다.
* UI/레이아웃은 기존 `plannerui.py` 와 동일합니다.

배포 전 준비
-------------
1. `requirements.txt` 에 다음을 추가
   ```
   streamlit
   streamlit‑folium
   streamlit‑javascript
   orjson
   folium
   ```
2. **Streamlit Cloud Secrets**(`.streamlit/secrets.toml`) 에 Web 키 저장
   ```toml
   [odsay]
   web_key = "YOUR_WEBSITE_API_KEY"
   ```
3. ODsay 콘솔 ➜ Web 플랫폼에
   `https://*.streamlit.app` (또는 실제 배포 도메인) 등록.

코드
----
```python
"""
import os
import inspect
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import orjson
import streamlit as st
from streamlit.components.v1 import html as st_html
from streamlit_javascript import st_javascript

# 내부 로직 ---------------------------------------------------------------
from planner import (
    paths_to_segs,  # NEW: raw JSON → seg 리스트 변환에 사용
    choose_best_route,
    draw_map,
    load_prefs,
    save_prefs,
    haversine,
    AVG_WALK_SPEED,
    append_history,
)

try:
    from streamlit_folium import st_folium  # noqa: F401  (미사용 경고 억제)
except ImportError:
    pass

# ------------------------------------------------------------------------
# 환경 변수 / 시크릿 -------------------------------------------------------
ODsay_WEB_KEY = os.getenv(
    "ODSAY_KEY"
)  # : str | None = st.secrets.get("odsay", {}).get("web_key")  # type: ignore[arg-type]
if not ODsay_WEB_KEY:
    st.error("❗️ ODsay Web 키가 설정되지 않았습니다. secrets.toml 확인!")
    st.stop()

# ------------------------------------------------------------------------
# Streamlit 설정 및 세션 초기화
# ------------------------------------------------------------------------
st.set_page_config(
    page_title="멀티모달 경로 플래너",
    layout="wide",
)

if "prefs" not in st.session_state:
    st.session_state["prefs"] = load_prefs()
if "route" not in st.session_state:
    st.session_state["route"] = None

# ------------------------------------------------------------------------
# Sidebar – 사용자 선호 입력
# ------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️  선호도 & 가중치 설정")

    p: Dict = st.session_state["prefs"]

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

    st.subheader("모드별 선호도")
    pref_subway = st.number_input(
        "지하철 선호도",
        -10.0,
        10.0,
        float(p.get("mode_preference", {}).get("SUBWAY", 0.0)),
        0.5,
    )
    pref_bus = st.number_input(
        "버스 선호도",
        -10.0,
        10.0,
        float(p.get("mode_preference", {}).get("BUS", 0.0)),
        0.5,
    )
    pref_walk = st.number_input(
        "도보 선호도",
        -10.0,
        10.0,
        float(p.get("mode_preference", {}).get("WALK", 0.0)),
        0.5,
    )

    if st.button("💾  선호도 저장"):
        to_save: Dict = {
            "crowd_weight": crowd_weight,
            "max_crowd": max_crowd,
            "walk_limit_min": walk_limit_min,
            "mode_penalty": {"SUBWAY": mp_subway, "BUS": mp_bus, "WALK": mp_walk},
            "mode_preference": {
                "SUBWAY": pref_subway,
                "BUS": pref_bus,
                "WALK": pref_walk,
            },
            "runs": p.get("runs", 0),
        }
        save_prefs(to_save)
        st.session_state["prefs"] = to_save
        st.success("✅  선호도가 영구 저장되었습니다!")

    learn_mode = st.checkbox("🧠  학습 모드로 경로 기록", value=False)

# ------------------------------------------------------------------------
# Main – 입력 폼
# ------------------------------------------------------------------------
st.title("🚍  ODsay 멀티모달 경로 플래너 · Web API")
col1, col2 = st.columns(2)
with col1:
    origin_input = st.text_input("출발지 (위도,경도)")
with col2:
    dest_input = st.text_input("도착지 (위도,경도)")

# ---------------------------------------------------
# 공통 prefs dict (이번 세션용 – 저장 X)
# ---------------------------------------------------
current_prefs: Dict = {
    "crowd_weight": crowd_weight,
    "max_crowd": max_crowd,
    "walk_limit_min": walk_limit_min,
    "mode_penalty": {"SUBWAY": mp_subway, "BUS": mp_bus, "WALK": mp_walk},
    "mode_preference": {"SUBWAY": pref_subway, "BUS": pref_bus, "WALK": pref_walk},
}

# ------------------------------------------------------------------------
# Helper – 브라우저에서 ODsay Web API 호출(JS) 후 JSON 문자열 반환
# ------------------------------------------------------------------------

JS_TEMPLATE = """
async () => {
  try {
    const key = "%s";
    const [sy, sx] = "%s".split(",").map(parseFloat);
    const [ey, ex] = "%s".split(",").map(parseFloat);
    if (isNaN(sx) || isNaN(sy) || isNaN(ex) || isNaN(ey)) {
      return JSON.stringify({ error: "Invalid coordinate format" });
    }
    const base = `https://api.odsay.com/v1/api/searchPubTransPath`;
    const params = new URLSearchParams({
      SX: sx, SY: sy,
      EX: ex, EY: ey,
      SearchType: 0,
      OPT: 0,
      lang: 0,
      output: "json",
      reqCoordType: "WGS84GEO",
      resCoordType: "WGS84GEO",
      apiKey: key
    });
    const url = `${base}?${params.toString()}`;

    const resp = await fetch(url);
    if (!resp.ok) {
      return JSON.stringify({ error: `HTTP ${resp.status}` });
    }
    const data = await resp.json();
    return JSON.stringify(data);
  } catch (e) {
    return JSON.stringify({ error: e.toString() });
  }
}
"""

# ------------------------------------------------------------------------
# 버튼 – 경로 탐색
# ------------------------------------------------------------------------

if st.button("🚀  경로 탐색"):
    if not origin_input or not dest_input:
        st.warning("출발지와 도착지를 모두 입력하세요.")
        st.stop()

    with st.spinner("브라우저에서 ODsay API 호출 중…"):
        raw_json: str | None = st_javascript(
            JS_TEMPLATE % (ODsay_WEB_KEY, origin_input, dest_input)
        )

    if not raw_json:
        st.error("⚠️  JS 실행 실패 or 응답 없음")
        st.stop()

    resp = orjson.loads(raw_json)
    if "error" in resp:
        st.error(f"ODsay API 오류: {resp['error']}")
        st.stop()

    # --------------------------------------------------------------------
    # 웹 API → 세그먼트 리스트 변환 (서버‑사이드 로직 그대로 재사용)
    # --------------------------------------------------------------------
    paths = resp.get("result", {}).get("path", [])
    routes: List[List[Dict]] = [
        paths_to_segs(p.get("subPath", []), prefs=current_prefs) for p in paths
    ]

    best_idx, segs = choose_best_route(routes, prefs=current_prefs)

    if not segs:
        # fallback: pure walk straight line
        try:
            sy, sx = map(float, origin_input.split(","))
            ey, ex = map(float, dest_input.split(","))
            dist = haversine((sy, sx), (ey, ex))
        except ValueError:
            st.error("좌표 형식 오류")
            st.stop()
        segs = [
            {
                "mode": "WALK",
                "name": "직선도보",
                "distance_m": dist,
                "duration_min": round(dist / (AVG_WALK_SPEED * 60), 2),
                "crowd": 1,
                "best_car": None,
                "poly": [(sy, sx), (ey, ex)],
            }
        ]

    # --------------------------------------------------------------------
    # 요약 & 지도 출력
    # --------------------------------------------------------------------
    total_min = sum(s.get("duration_min", 0) for s in segs)
    st.subheader("📝  경로 요약")
    for i, s in enumerate(segs, 1):
        car = f" | 추천칸 {s.get('best_car')}" if s.get("best_car") else ""
        st.write(
            f"{i}. {s.get('mode'):<6} | {s.get('name'):<10} | {s.get('duration_min',0):5.1f}분{car}"
        )
    st.success(f"예상 총 소요 시간: {total_min:.1f}분")

    # 지도 (Folium) --------------------------------------------------------
    # Folium 객체와 HTML 파일 경로 반환
    map_obj, html_path = draw_map(
        segs,
        (float(origin_input.split(",")[0]), float(origin_input.split(",")[1])),
        (float(dest_input.split(",")[0]), float(dest_input.split(",")[1])),
    )

    map_html = map_obj.get_root().render()
    st.session_state["route"] = {"map": map_html, "segs": segs, "total_min": total_min}

    if learn_mode:
        append_history(
            {
                "datetime": datetime.now().isoformat(),
                "origin": origin_input,
                "dest": dest_input,
                "total_min": total_min,
                "modes": "/".join({s.get("mode") for s in segs}),
            }
        )
        st.info("📚  경로 이용 기록이 저장되었습니다.")

# ------------------------------------------------------------------------
# 항상 지도 표시 (세션 상태에 저장해 둔 HTML 사용)
# ------------------------------------------------------------------------
if st.session_state.get("route"):
    st.subheader("🗺️  경로 지도")
    st_html(st.session_state["route"]["map"], height=600, width=900, scrolling=False)

# ------------------------------------------------------------------------
# Footer
# ------------------------------------------------------------------------

st.markdown(
    "---\n"
    "<div style='text-align:center;'>ⓒ 2025 Multimodal Route Planner UI · 개발: JunWooPark</div>",
    unsafe_allow_html=True,
)
