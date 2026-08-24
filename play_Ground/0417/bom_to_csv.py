#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
findProductBOM API 로 호기별 BOM 을 조회해서 CSV 로 저장하는 스크립트.

- 먼저 gethogilistByBlockopt API 로 '금일 최초설계완료된 호기' 목록을 받아온다.
  응답의 'posid' 값이 호기 정보이며, 이 값들을 productNo 로 사용한다.
- productNo 목록을 순회하며 API 를 호출하고, 호출 사이에 SLEEP_SEC(기본 3초) 간격을 둔다.
- productNo 하나당 CSV 파일 하나를 만든다. 파일명은 '<productNo>_<YYYYMMDD>.csv'.
- 파일은 스크립트를 실행한 폴더(현재 작업 디렉터리)에 생성된다.
- CSV 헤더는 응답(ArrayList<BomPartDTO>)의 필드명을 그대로 사용한다.

requirements: pip install requests
"""

import csv
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------
BASE_URL = "https://vault-in.hdel.co.kr:8070/api/findProductBOM"
API_KEY = "subae"

# 금일 최초설계완료된 호기 목록을 뽑는 API. 반환값의 'posid' 가 호기(=productNo).
HOGI_LIST_URL = "https://plmpro.hdel.co.kr/jsp/help/gethogilistByBlockopt.jsp"

# productNo 목록을 어디서 받을지 결정한다.
#   True  : HOGI_LIST_URL 에서 오늘 날짜로 호기 목록을 받아 productNo 로 사용(기본).
#   False : 아래 PRODUCT_NOS / PRODUCT_NO_FILE 를 사용.
USE_HOGI_LIST_API = False

# USE_HOGI_LIST_API=False 일 때 사용할 productNo 목록.
# 파일에서 읽으려면 PRODUCT_NO_FILE 을 지정한다(한 줄에 하나).
PRODUCT_NOS = [
    "N27748L02",
]
PRODUCT_NO_FILE = None  # 예: "product_no.txt"

SLEEP_SEC = 3.0      # 호출 간 대기 시간 (서버 부하 방지)
TIMEOUT = 60         # 요청 타임아웃(초)
MAX_RETRY = 3        # 실패 시 재시도 횟수
RETRY_SLEEP = 5.0    # 재시도 전 대기 시간(초)

# 사내 인증서 문제로 SSL 검증에 실패하면 False 로 바꾼다.
# (사내망 전용 서버에 한해서만 사용할 것)
VERIFY_SSL = True

# 결과 파일을 만들 위치. 실행한 폴더에 생성한다.
OUTPUT_DIR = Path.cwd()
# 스크립트 파일이 있는 폴더에 만들고 싶으면 위 줄 대신 아래를 사용:
# OUTPUT_DIR = Path(__file__).resolve().parent


# ----------------------------------------------------------------------------
# productNo(호기) 목록 확보
# ----------------------------------------------------------------------------
def load_product_nos():
    """PRODUCT_NO_FILE 이 있으면 파일에서, 없으면 상수에서 목록을 읽는다.

    (USE_HOGI_LIST_API=False 로 두고 수동 목록을 쓸 때만 호출된다.)
    """
    # 파일 경로가 지정돼 있으면 파일에서 한 줄에 하나씩 읽는다.
    if PRODUCT_NO_FILE:
        path = Path(PRODUCT_NO_FILE)
        if not path.exists():
            sys.exit(f"[ERROR] productNo 파일을 찾을 수 없습니다: {path}")
        lines = path.read_text(encoding="utf-8").splitlines()
        # 빈 줄과 '#' 로 시작하는 주석 줄은 걸러낸다.
        return [x.strip() for x in lines if x.strip() and not x.startswith("#")]
    # 파일이 없으면 상수 목록을 복사해서 반환한다.
    return list(PRODUCT_NOS)


def extract_posids(payload):
    """호기 목록 응답에서 posid 값들을 순서대로(중복 제거) 뽑는다.

    응답이 [{"posid": ...}, ...] 형태이거나 {"data": [...]} 로 감싼 경우,
    혹은 필드명이 대문자(POSID)로 오는 경우까지 대비한다.
    """
    # 응답을 레코드 리스트로 정규화한다(래핑 형태 대비).
    records = extract_records(payload)
    posids = []
    seen = set()  # 중복 호기 판별용
    for record in records:
        # 레코드가 dict 이면 posid 계열 키를 순서대로 찾고, 아니면 값 자체를 사용한다.
        if isinstance(record, dict):
            value = None
            for key in ("posid", "POSID", "Posid", "posId"):
                if key in record:
                    value = record[key]
                    break
        else:
            value = record
        if value is None:
            continue
        # 공백 제거 후, 비어있지 않고 처음 보는 값만 담는다(등장 순서 유지).
        value = str(value).strip()
        if value and value not in seen:
            seen.add(value)
            posids.append(value)
    return posids


def fetch_hogi_list(session, searchdate):
    """searchdate(YYYYMMDD)로 호기 목록 API 를 호출해 posid 리스트를 반환한다."""
    last_error = None
    # 네트워크/파싱 오류에 대비해 MAX_RETRY 만큼 재시도한다.
    for attempt in range(1, MAX_RETRY + 1):
        try:
            response = session.get(
                HOGI_LIST_URL,
                params={"searchdate": searchdate},  # 예: searchdate=20260820
                timeout=TIMEOUT,
                verify=VERIFY_SSL,
            )
            response.raise_for_status()  # HTTP 4xx/5xx 면 예외 발생
            return extract_posids(response.json())  # 성공 시 posid 목록 반환하고 종료
        except (requests.RequestException, ValueError) as exc:
            # RequestException: 통신 오류 / ValueError: JSON 파싱 실패
            last_error = exc
            if attempt < MAX_RETRY:
                print(f"  [WARN] 호기 목록 조회 실패({attempt}/{MAX_RETRY}): {exc} "
                      f"-> {RETRY_SLEEP}초 후 재시도")
                time.sleep(RETRY_SLEEP)
    # 모든 재시도가 실패하면 마지막 오류를 담아 예외를 던진다.
    raise RuntimeError(f"호기 목록 조회 실패: {last_error}")


# ----------------------------------------------------------------------------
# 응답 파싱 / CSV 저장 유틸
# ----------------------------------------------------------------------------
def safe_filename(value):
    """파일명으로 쓸 수 없는 문자를 '_' 로 바꾼다."""
    # 윈도우에서 금지된 문자(\ / : * ? " < > |)를 밑줄로 치환한다.
    return re.sub(r'[\\/:*?"<>|]', "_", value).strip() or "unknown"


def extract_records(payload):
    """응답 본문에서 BomPartDTO 리스트를 꺼낸다.

    ArrayList 를 그대로 내려주면 최상위가 list 이고,
    {"data": [...]} 처럼 감싸는 경우도 대비한다.
    """
    # 1) 최상위가 이미 배열이면 그대로 사용한다.
    if isinstance(payload, list):
        return payload
    # 2) dict 로 감싸져 있으면 흔히 쓰는 키 이름에서 배열을 찾는다.
    if isinstance(payload, dict):
        for field in ("data", "result", "list", "items", "bomList"):
            value = payload.get(field)
            if isinstance(value, list):
                return value
        # 배열이 없으면 dict 하나를 단일 레코드로 취급한다.
        return [payload]
    # 3) 그 외(문자열/숫자 등)는 빈 목록으로 처리한다.
    return []


def flatten_value(value):
    """CSV 한 칸에 들어갈 수 있게 값을 문자열로 변환한다."""
    if value is None:
        return ""  # None 은 빈 문자열로
    # dict/list 는 CSV 한 칸에 못 넣으므로 JSON 문자열로 직렬화한다(한글 보존).
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def fetch_bom(session, product_no):
    """productNo 하나에 대해 API 를 호출하고 레코드 리스트를 반환한다."""
    last_error = None
    # fetch_hogi_list 와 동일한 재시도 패턴.
    for attempt in range(1, MAX_RETRY + 1):
        try:
            response = session.get(
                BASE_URL,
                params={"key": API_KEY, "productNo": product_no},  # 인증키 + 호기번호
                timeout=TIMEOUT,
                verify=VERIFY_SSL,
            )
            response.raise_for_status()
            return extract_records(response.json())  # 성공 시 BOM 레코드 목록 반환
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < MAX_RETRY:
                print(f"  [WARN] {product_no} 조회 실패({attempt}/{MAX_RETRY}): {exc} "
                      f"-> {RETRY_SLEEP}초 후 재시도")
                time.sleep(RETRY_SLEEP)
    # 재시도를 모두 소진하면 호출자(main)에서 잡을 수 있게 예외를 던진다.
    raise RuntimeError(f"{product_no} 조회 실패: {last_error}")


def write_csv(product_no, records, today):
    """레코드를 '<productNo>_<YYYYMMDD>.csv' 로 저장하고 경로를 반환한다."""
    # 등장 순서를 유지하면서 필드명(= CSV 헤더) 수집
    columns = []  # 헤더로 쓸 필드명 목록(처음 등장한 순서)
    rows = []     # 각 레코드를 {필드: 값} 형태로 정리한 목록
    for record in records:
        # dict 가 아닌 값은 'VALUE' 라는 단일 컬럼으로 감싼다.
        if not isinstance(record, dict):
            record = {"VALUE": record}
        row = {}
        for field, value in record.items():
            row[field] = flatten_value(value)
            # 새로 등장한 필드는 헤더 목록에 추가한다(레코드마다 필드가 달라도 대응).
            if field not in columns:
                columns.append(field)
        rows.append(row)

    # 파일명: '<호기>_<오늘날짜>.csv'
    out_path = OUTPUT_DIR / f"{safe_filename(product_no)}_{today}.csv"
    # utf-8-sig: 엑셀에서 한글이 깨지지 않도록 BOM 을 붙인다.
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=columns, restval="")
        writer.writeheader()
        writer.writerows(rows)
    return out_path


# ----------------------------------------------------------------------------
# 메인 실행 흐름
# ----------------------------------------------------------------------------
def main():
    today = date.today().strftime("%Y%m%d")  # 오늘 날짜(YYYYMMDD) = 조회 기준일
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 저장 폴더 준비

    # 커넥션 재사용 + 공통 헤더를 위한 세션 하나로 모든 요청을 처리한다.
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    # 1) productNo 목록 확보: 호기 목록 API(오늘 날짜) 또는 상수/파일
    if USE_HOGI_LIST_API:
        print(f"호기 목록 조회 중 (searchdate={today}) ...")
        try:
            product_nos = fetch_hogi_list(session, today)
        except RuntimeError as exc:
            sys.exit(f"[ERROR] {exc}")
        print(f"  -> 금일 최초설계완료 호기 {len(product_nos)}건: "
              f"{', '.join(product_nos) if product_nos else '(없음)'}")
    else:
        product_nos = load_product_nos()

    if not product_nos:
        sys.exit("[ERROR] 조회할 productNo(호기) 가 없습니다.")

    print(f"총 {len(product_nos)}건 BOM 조회 시작 (호출 간격 {SLEEP_SEC}초)")
    print(f"저장 위치: {OUTPUT_DIR}")

    failed = []  # 조회에 실패한 호기 목록(마지막에 요약 출력)

    # 2) 호기(productNo)를 하나씩 돌면서 BOM 을 조회하고 CSV 로 저장한다.
    for index, product_no in enumerate(product_nos, start=1):
        print(f"[{index}/{len(product_nos)}] productNo={product_no} 조회 중 ...")
        try:
            records = fetch_bom(session, product_no)
        except RuntimeError as exc:
            # 재시도까지 모두 실패한 경우: 기록만 하고 다음 호기로 넘어간다.
            print(f"  [ERROR] {exc}")
            failed.append(product_no)
            records = None

        if records is None:
            pass  # 실패 → 파일 없음(위에서 이미 로그 출력)
        elif not records:
            print("  -> 조회 결과 0건, 파일을 만들지 않습니다.")
        else:
            # 정상 조회 → CSV 저장
            out_path = write_csv(product_no, records, today)
            print(f"  -> {len(records)}건 저장: {out_path.name}")

        # 서버 부하 방지를 위한 대기. 단, 마지막 호출 뒤에는 기다리지 않는다.
        if index < len(product_nos):
            time.sleep(SLEEP_SEC)

    # 3) 전체 결과 요약
    print("\n완료")
    if failed:
        print(f"실패한 productNo {len(failed)}건: {', '.join(failed)}")


# 스크립트로 직접 실행할 때만 main() 을 호출한다(import 시에는 실행 안 함).


if __name__ == "__main__":
    main()