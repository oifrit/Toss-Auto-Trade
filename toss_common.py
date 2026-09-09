"""
toss_common.py — 여러 전략 스크립트(csuf, dSuf, 토슬법 등)에서 공통으로 쓰는 유틸 모음
이 파일은 C:\\@InvQuint\\@Toss\\ 바로 아래(공용 폴더, 각 전략 하위폴더의 상위)에 둡니다.

포함 기능:
  - load_credentials(): 토스 API 키 + 텔레그램 봇 정보 로딩 (환경변수 우선, 없으면 로컬 config 파일)
  - send_telegram(message): 텔레그램으로 메시지 전송
  - is_market_holiday(date): 오늘(또는 지정일)이 미국 증시 휴장일인지 확인

사전 준비:
  pip install requests
"""

import os
import json
import requests
import datetime as dt
from datetime import date as date_cls

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "toss_config.json")


# ---------------------------------------------------------------------------
# 1) API 키 로딩 (토스 + 텔레그램)
# ---------------------------------------------------------------------------
def load_credentials():
    """
    반환: dict {
        'toss_client_id', 'toss_client_secret',
        'telegram_bot_token', 'telegram_chat_id'
    }
    우선순위: 환경변수(TOSS_CLIENT_ID 등) > 로컬 toss_config.json 파일
    (GitHub Actions에서는 환경변수/Secrets로, 로컬에서는 config 파일로 동작)
    """
    creds = {
        "toss_client_id": os.environ.get("TOSS_CLIENT_ID"),
        "toss_client_secret": os.environ.get("TOSS_CLIENT_SECRET"),
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
    }
    if all(creds.values()):
        return creds

    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for key in creds:
            if not creds[key]:
                creds[key] = cfg.get(key)

    missing = [k for k, v in creds.items() if not v]
    if missing:
        print(f"[안내] {CONFIG_PATH} 에 다음 값이 없습니다: {missing}", flush=True)
        print("       toss_config.json 에 아래 키들을 채워주세요:", flush=True)
        print('       {"toss_client_id": "...", "toss_client_secret": "...",', flush=True)
        print('        "telegram_bot_token": "...", "telegram_chat_id": "..."}', flush=True)

    return creds


# ---------------------------------------------------------------------------
# 2) 텔레그램 전송
# ---------------------------------------------------------------------------
def send_telegram(message, creds=None):
    """텔레그램으로 메시지를 보냅니다. 실패해도 예외를 던지지 않고 False를 반환합니다."""
    if creds is None:
        creds = load_credentials()
    token = creds.get("telegram_bot_token")
    chat_id = creds.get("telegram_chat_id")
    if not token or not chat_id:
        print("[텔레그램] 봇 토큰/chat_id 가 없어 전송을 건너뜁니다.", flush=True)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
        if resp.status_code != 200:
            print(f"[텔레그램] 전송 실패: {resp.status_code} {resp.text}", flush=True)
            return False
        return True
    except requests.exceptions.RequestException as e:
        print(f"[텔레그램] 전송 중 예외: {e}", flush=True)
        return False


# ---------------------------------------------------------------------------
# 3) 미국 증시 휴장일 체크
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 3) 미국 증시 휴장일 체크 (토스 공식 API 사용) + 휴일 이름 계산 (로컬 규칙)
# ---------------------------------------------------------------------------
BASE_URL = "https://openapi.tossinvest.com"


def _nth_weekday(year, month, weekday, n):
    """해당 연/월의 n번째 요일(weekday: 월=0..일=6) 날짜를 반환"""
    d = date_cls(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    d += dt.timedelta(days=offset + 7 * (n - 1))
    return d


def _last_weekday(year, month, weekday):
    """해당 연/월의 마지막 해당 요일 날짜를 반환"""
    if month == 12:
        d = date_cls(year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        d = date_cls(year, month + 1, 1) - dt.timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - dt.timedelta(days=offset)


def _observed(d):
    """토요일이면 금요일로, 일요일이면 월요일로 대체휴일 적용"""
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def _easter_sunday(year):
    """Anonymous Gregorian algorithm으로 부활절(일요일) 계산"""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date_cls(year, month, day)


def _us_market_holidays(year):
    """해당 연도의 NYSE/Nasdaq 휴장일 {날짜: 이름} 딕셔너리"""
    good_friday = _easter_sunday(year) - dt.timedelta(days=2)
    return {
        _observed(date_cls(year, 1, 1)): "신정",
        _nth_weekday(year, 1, 0, 3): "마틴 루터 킹 데이",
        _nth_weekday(year, 2, 0, 3): "대통령의 날",
        good_friday: "성금요일",
        _last_weekday(year, 5, 0): "메모리얼 데이",
        _observed(date_cls(year, 6, 19)): "준틴스",
        _observed(date_cls(year, 7, 4)): "독립기념일",
        _nth_weekday(year, 9, 0, 1): "노동절",
        _nth_weekday(year, 11, 3, 4): "추수감사절",
        _observed(date_cls(year, 12, 25)): "크리스마스",
    }


def get_us_holiday_name(check_date=None):
    """check_date(기본값 오늘)가 알려진 미국 증시 휴일이면 이름을, 아니면 None을 반환"""
    if check_date is None:
        check_date = date_cls.today()
    elif isinstance(check_date, str):
        check_date = date_cls.fromisoformat(check_date)
    return _us_market_holidays(check_date.year).get(check_date)


def is_market_holiday(token, check_date=None):
    """
    check_date(기본값: 오늘, 'YYYY-MM-DD' 문자열 또는 date 객체)가
    미국 증시 휴장일이면 (True, 사유문구) 반환, 개장일이면 (False, None) 반환.

    휴장 여부 판정은 토스 공식 API GET /api/v1/market-calendar/US 사용.
    휴일 이름은 로컬 규칙(_us_market_holidays)으로 별도 계산.
    """
    params = {}
    if check_date is not None:
        if isinstance(check_date, date_cls):
            params["date"] = check_date.isoformat()
        else:
            params["date"] = check_date

    resp = requests.get(
        f"{BASE_URL}/api/v1/market-calendar/US",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    today_info = data.get("result", data).get("today", {})

    if today_info.get("regularMarket") is None:
        target = check_date if check_date else date_cls.today()
        if isinstance(target, str):
            target = date_cls.fromisoformat(target)

        holiday_name = get_us_holiday_name(target)
        if holiday_name:
            return True, holiday_name

        weekday = target.strftime("%A")
        if weekday in ("Saturday", "Sunday"):
            return True, f"주말({weekday})"
        return True, "공휴일(이름 미확인)"

    return False, None


# ---------------------------------------------------------------------------
# 4) 현재가 조회 + 주문가 ±20% 가격제한 클리핑
# ---------------------------------------------------------------------------
def get_current_price(token, symbol):
    """현재가(lastPrice) 조회"""
    resp = requests.get(
        f"{BASE_URL}/api/v1/prices",
        headers={"Authorization": f"Bearer {token}"},
        params={"symbols": symbol},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("result", data)
    if isinstance(items, dict):
        items = items.get("prices") or items.get("items") or [items]
    for item in items:
        if item.get("symbol") == symbol:
            return float(item["lastPrice"])
    raise ValueError(f"현재가 응답에서 {symbol}을 찾지 못했습니다: {data}")


def clip_order_price(order_price, current_price, side):
    """
    토스는 주문가가 주문시점 현재가 대비 ±20%를 넘을 수 없음.
    이 한도 안에서 최대한 유리하게 주문가를 조정한다.

    반환: (최종 주문가 또는 None, 스킵여부)
      - 범위(현재가*0.8 ~ 현재가*1.2) 안이면 원래 order_price 그대로, 스킵 False
      - BUY 이고 order_price가 상한 초과(너무 비쌈, 급등) → 매수 스킵 (None, True)
      - BUY 이고 order_price가 하한 미만(폭락) → 현재가*1.15 로 상향(체결 목표) (값, False)
      - SELL 이고 order_price가 하한 미만(폭락, 목표가 못미침) → 매도 스킵 (None, True)
      - SELL 이고 order_price가 상한 초과(급등, 이미 목표 초과달성) → 현재가*1.01 로 하향(체결 목표) (값, False)
    """
    lower = current_price * 0.8
    upper = current_price * 1.2

    if lower <= order_price <= upper:
        return order_price, False

    if side == "BUY":
        if order_price > upper:
            return None, True  # 너무 비싸짐 — 매수 스킵
        else:
            return current_price * 1.15, False  # 폭락 — 상향 조정해서 체결 목표
    else:  # SELL
        if order_price < lower:
            return None, True  # 폭락 — 매도 스킵
        else:
            return current_price * 1.01, False  # 급등 — 하향 조정해서 체결 목표


# ---------------------------------------------------------------------------
# 5) 계좌/시세/주문 공용 함수 (csuf, dSuf 등 여러 전략 스크립트에서 재사용)
# ---------------------------------------------------------------------------
def get_accounts(token):
    resp = requests.get(f"{BASE_URL}/api/v1/accounts", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    resp.raise_for_status()
    return resp.json()["result"]


def get_buying_power(token, account_seq, currency="USD"):
    resp = requests.get(
        f"{BASE_URL}/api/v1/buying-power",
        headers={"Authorization": f"Bearer {token}", "X-Tossinvest-Account": str(account_seq)},
        params={"currency": currency},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_holdings(token, account_seq):
    resp = requests.get(
        f"{BASE_URL}/api/v1/holdings",
        headers={"Authorization": f"Bearer {token}", "X-Tossinvest-Account": str(account_seq)},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_total_invest_from_tiers(token, account_seq, total_invest_tracked):
    """
    csuf v2는 total_invest를 '매도 발생시에만 실현손익만큼 갱신'하는 방식이라,
    매번 계좌를 다시 긁어와 계산하지 않고 상태파일에 저장된 값을 그대로 이어받는 게 원칙.
    이 함수는 그 갱신된 값을 그대로 반환하는 자리표시자 — 실제 갱신은 매도 처리 로직에서 수행.
    """
    return total_invest_tracked


def get_prev_close(token, symbol):
    """
    가장 최근 '완결된' 일봉의 종가 = '전일종가'.
    장이 이미 열린 뒤(저녁 실행 시점 등)엔 캔들 응답에 '오늘' 진행중인 캔들이
    이미 포함될 수 있어, 날짜가 오늘(KST)과 같은 캔들은 건너뛰고 그 다음을 사용한다.
    """
    resp = requests.get(
        f"{BASE_URL}/api/v1/candles",
        headers={"Authorization": f"Bearer {token}"},
        params={"symbol": symbol, "interval": "1d", "count": 5},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    result = data.get("result", data)
    candles = result.get("candles")
    if not candles:
        raise ValueError(f"캔들 데이터를 찾지 못했습니다: {data}")

    today_str = date_cls.today().isoformat()
    chosen = None
    for c in candles:
        candle_date = c.get("timestamp", "")[:10]
        if candle_date == today_str:
            continue
        chosen = c
        break
    if chosen is None:
        raise ValueError("오늘을 제외하고 완결된 캔들을 찾지 못했습니다.")

    close = chosen.get("closePrice") or chosen.get("close")
    if close is None:
        raise ValueError(f"캔들 항목에서 종가 필드를 찾지 못했습니다: {chosen}")
    return float(close), chosen


def get_completed_trading_dates(token, symbol, count=30):
    """최근 count개 캔들 중 '오늘'을 제외한, 완결된 거래일 날짜 목록(오름차순)"""
    resp = requests.get(
        f"{BASE_URL}/api/v1/candles",
        headers={"Authorization": f"Bearer {token}"},
        params={"symbol": symbol, "interval": "1d", "count": count},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    result = data.get("result", data)
    candles = result.get("candles", [])
    today_str = date_cls.today().isoformat()
    dates = []
    for c in candles:
        d = c.get("timestamp", "")[:10]
        if d and d != today_str:
            dates.append(d)
    return sorted(set(dates))


def count_held_trading_days(trading_dates, buy_date):
    """buy_date(매수 체결일자) 이후로 지난 완결 거래일 수 (매수당일=0일차)"""
    return sum(1 for d in trading_dates if d > buy_date)


def get_order_detail(token, account_seq, order_id, verbose=True):
    resp = requests.get(
        f"{BASE_URL}/api/v1/orders/{order_id}",
        headers={"Authorization": f"Bearer {token}", "X-Tossinvest-Account": str(account_seq)},
        timeout=10,
    )
    if verbose:
        print(f"   [주문상세 조회] orderId={order_id} 상태코드={resp.status_code}", flush=True)
        print(f"   [주문상세 응답 원본] {resp.text[:1500]}", flush=True)
    if resp.status_code != 200:
        return None
    data = resp.json()
    return data.get("result", data)


def place_order(token, account_seq, symbol, side, price, quantity, client_order_id):
    """side: 'BUY' 또는 'SELL'"""
    body = {
        "clientOrderId": client_order_id,
        "symbol": symbol,
        "side": side,
        "orderType": "LIMIT",
        "timeInForce": "CLS",
        "quantity": str(quantity),
        "price": str(price),
    }
    resp = requests.post(
        f"{BASE_URL}/api/v1/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tossinvest-Account": str(account_seq),
            "Content-Type": "application/json",
        },
        json=body,
        timeout=10,
    )
    return resp
