# backend/main.py
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# .env 로부터 환경변수 로드
load_dotenv()

# --- 고정값: 연세대학교 도서관 좌표 ---
LIB_LAT = 37.563729
LIB_LNG = 126.936898

ODSAY_KEY = os.getenv("ODSAY_API_KEY")
ODSAY_BASE = "https://api.odsay.com/v1/api"

# [수정됨] 키가 없어도 일단 서버는 켜지게 변경 (대신 로그에 경고 출력)
if not ODSAY_KEY:
    print("⚠️ 경고: ODSAY_API_KEY가 설정되지 않았습니다. API 요청 시 에러가 발생할 수 있습니다.")
    # raise RuntimeError(...)  <-- 이 줄을 지워서 서버가 꺼지는 것을 방지

app = FastAPI()

# [수정됨] CORS 설정 확실하게 적용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 모든 주소 허용
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----- ODsay 공통 호출 -----

def odsay_get(endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    # 키가 없을 때 여기서 에러 발생시키기
    if not ODSAY_KEY:
         raise HTTPException(status_code=500, detail="서버 내부 오류: ODSAY_API_KEY가 설정되지 않았습니다.")

    """ODSAY GET 요청 + 기본 에러 처리"""
    url = f"{ODSAY_BASE}/{endpoint.lstrip('/')}"
    merged = {
        "apiKey": ODSAY_KEY,
        "lang": 0,
        "output": "json",
    }
    merged.update(params or {})

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params=merged)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"ODSAY HTTP error: {e}")

    data = resp.json()
    if isinstance(data, dict) and "error" in data:
        msg = data["error"].get("msg", "ODSAY error")
        code = data["error"].get("code", "")
        raise HTTPException(status_code=502, detail=f"ODSAY error {code}: {msg}")

    return data

# ----- 문자열 정규화 유틸 -----

def norm(s: Optional[str]) -> str:
    """노선명/정류장명 비교를 위한 간단한 정규화"""
    if not s:
        return ""
    s = s.strip()
    # 괄호 제거, '번' 제거 등 간단한 처리
    for ch in ["(", ")", " ", "번"]:
        s = s.replace(ch, "")
    return s

# ----- path 선택 로직 -----

def score_path_for_ride(
    path: Dict[str, Any],
    ride: str,
    board: str,
    drop: str,
) -> Tuple[int, int]:
    """
    해당 path가 ride / board / drop과 얼마나 잘 맞는지 점수 계산.
    반환값: (score, totalTime)
    """
    ride_n = norm(ride)
    board_n = norm(board)
    drop_n = norm(drop)

    score = 0
    info = path.get("info", {})
    total_time = int(info.get("totalTime") or 10**9)

    sub_paths = path.get("subPath", []) or []

    # 1) ride 매칭 (버스/지하철 노선)
    if ride_n:
        for sp in sub_paths:
            ttype = sp.get("trafficType")
            if ttype not in (1, 2):  # 1: 지하철, 2: 버스
                continue
            lanes = sp.get("lane", []) or []
            for lane in lanes:
                candidates = []
                # 버스
                if ttype == 2:
                    bus_no = lane.get("busNo")
                    name = lane.get("name")
                    if bus_no:
                        candidates.append(bus_no)
                    if name:
                        candidates.append(name)
                # 지하철
                if ttype == 1:
                    name = lane.get("name")
                    if name:
                        candidates.append(name)

                for cand in candidates:
                    if ride_n and ride_n in norm(str(cand)):
                        score += 10
                        # 한 번이라도 매칭되면 충분
                        break

    # 2) board / drop 정류장 이름 매칭
    station_names_norm = []
    for sp in sub_paths:
        pass_list = sp.get("passStopList") or {}
        stations = pass_list.get("stations", []) or pass_list.get("station", []) or []
        for st in stations:
            name = st.get("stationName") or ""
            station_names_norm.append(norm(name))

    if board_n and any(board_n in s for s in station_names_norm):
        score += 5
    if drop_n and any(drop_n in s for s in station_names_norm):
        score += 5

    return score, total_time

def select_path_for_ride(
    data: Dict[str, Any],
    ride: str,
    board: str,
    drop: str,
) -> Dict[str, Any]:
    """
    searchPubTransPathT 전체 응답에서
    ride/board/drop에 가장 잘 맞는 path 선택.
    아무것도 안 맞으면 path[0] fallback.
    """
    try:
        paths: List[Dict[str, Any]] = data["result"]["path"]
    except (KeyError, TypeError):
        raise HTTPException(status_code=500, detail="ODSAY 응답 포맷이 예상과 다릅니다 (path 누락).")

    if not paths:
        raise HTTPException(status_code=404, detail="ODSAY: 경로를 찾지 못했습니다.")

    # ride/board/drop이 전혀 없으면 바로 추천 1번 경로
    if not (ride or board or drop):
        return paths[0]

    best = None  # (score, totalTime, path)
    for p in paths:
        sc, tt = score_path_for_ride(p, ride, board, drop)
        if best is None:
            best = (sc, tt, p)
            continue
        b_sc, b_tt, _ = best
        # 1) score 높은 게 우선
        # 2) 점수 같으면 totalTime이 더 짧은 게 우선
        if sc > b_sc or (sc == b_sc and tt < b_tt):
            best = (sc, tt, p)

    if best is None:
        return paths[0]

    best_score, _, best_path = best

    # 모든 path가 0점이면 → path[0]로 fallback
    if best_score == 0:
        return paths[0]

    return best_path

# ----- mapObj 및 loadLane -----

def get_map_obj(
    from_lat: float,
    from_lng: float,
    ride: str,
    board: str,
    drop: str,
) -> str:
    """
    searchPubTransPathT 로 길찾기 호출 후
    ride/board/drop에 맞는 path를 골라 mapObj 반환.
    """
    data = odsay_get(
        "searchPubTransPathT",
        {
            "SX": from_lng,     # 경도
            "SY": from_lat,     # 위도
            "EX": LIB_LNG,      # 도착지 경도
            "EY": LIB_LAT,      # 도착지 위도
            "OPT": 0,           # 추천 경로
            "SearchPathType": 0 # 버스+지하철 모두
        },
    )

    path = select_path_for_ride(data, ride, board, drop)
    info = path.get("info", {})
    map_obj = info.get("mapObj")
    if not map_obj:
        raise HTTPException(status_code=500, detail="선택한 경로에 mapObj 가 없습니다.")
    return map_obj

def get_lane_graph(map_obj: str) -> Dict[str, Any]:
    """
    노선 그래픽 데이터 검색(loadLane) 호출.
    공식 예제처럼 mapObject=0:0@{mapObj} 형태로 호출해야 함.
    """
    data = odsay_get(
        "loadLane",
        {
            "mapObject": f"0:0@{map_obj}",
        },
    )
    return data

# ----- API 엔드포인트 -----

@app.get("/api/route")
def get_route(
    from_lat: float = Query(...),
    from_lng: float = Query(...),
    ride: str = Query("", description="CSV ride (버스/지하철 노선명)"),
    board: str = Query("", description="CSV board (탑승 정류장 이름)"),
    drop: str = Query("", description="CSV drop (하차 정류장 이름)"),
):
    """
    클릭한 점(from_lat, from_lng) -> 연세대 도서관까지의
    대중교통 경로를 ODsay에 요청하고,
    loadLane 결과(정밀한 노선 그래픽 데이터)를 그대로 반환.
    ride/board/drop 정보를 이용해 가능한 한 CSV와 같은 노선을 선택함.
    """
    map_obj = get_map_obj(from_lat, from_lng, ride, board, drop)
    lane_data = get_lane_graph(map_obj)
    return lane_data
