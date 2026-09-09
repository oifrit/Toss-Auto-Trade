r"""
csuf 1일차 — 초기 매수 실행 (대상 종목은 csuf_config.json 의 symbol 값)
(화요일 저녁 7시경 실행: 아직 미국장이 열리기 전이므로, 이때 조회되는
 '가장 최근 완결된 일봉 종가'가 곧 오늘 밤 체결 기준이 되는 '전일종가'입니다)

이 스크립트가 하는 일:
1. 로그인 (토큰 발급)
2. 오늘 미국 증시 휴장일인지 확인 → 휴장이면 텔레그램 알림만 보내고 종료
3. 계좌 조회
4. 총투자금 = SOXL 매수총금액 + 잔금 (참고용 자동계산) → 직접 확인 후 입력/확정
5. SOXL 일봉 캔들 조회 → 전일종가 확인
6. 매수 실행조건(per_trade >= 전일종가*1.3) 확인
7. LOC 매수 주문 실행 (LIMIT + CLS, price=전일종가, quantity=floor(per_trade/전일종가))
8. 다음날 아침(2일차) 스크립트가 체결 확인할 수 있도록 csuf_state.json 에 상태 저장
   (각 단계 주요 결과는 텔레그램으로도 전송됩니다)

사전 준비:
  - toss_common.py 가 이 파일의 상위 폴더(C:\@InvQuint\@Toss\)에 있어야 함
  - toss_config.json 도 같은 상위 폴더에 있어야 함 (비밀값 전용)
    {"toss_client_id": "...", "toss_client_secret": "...",
     "telegram_bot_token": "...", "telegram_chat_id": "..."}
  - csuf_config.json 은 이 파일과 같은 폴더에 있어야 함 (전략 파라미터, 비밀 아님)
    {"symbol": "SOXL", "n_div": 3, "buy_min_multiplier": 1.3,
     "sell_rate": 0.03, "hold_limit": 3, "dry_run": true}
    → 다른 계정(예: 배우자용 별도 저장소)에 재사용할 땐 이 파일 숫자만 바꾸면 되고
      .py 코드는 그대로 복사해서 쓰면 됩니다.
  - pip install requests
"""

import os
import sys
import time
import json
import math
import requests
from datetime import date as date_cls

BASE_URL = "https://openapi.tossinvest.com"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PARENT_DIR)
from toss_common import (  # noqa: E402
    load_credentials, send_telegram, is_market_holiday,
    get_current_price, clip_order_price,
)

STATE_PATH = os.path.join(SCRIPT_DIR, "csuf_state.json")
STRATEGY_CONFIG_PATH = os.path.join(SCRIPT_DIR, "csuf_config.json")

DEFAULT_STRATEGY_CONFIG = {
    "symbol": "SOXL",
    "n_div": 3,
    "buy_min_multiplier": 1.3,
    "sell_rate": 0.03,
    "hold_limit": 3,
    "buffer_usd": 0,
    "dry_run": True,
}


def load_strategy_config():
    """
    csuf_config.json 에서 전략 파라미터를 읽어옵니다.
    이 파일은 계정마다(예: 배우자용 별도 저장소) 값만 다르게 채우면 되고,
    코드(.py)는 전혀 손대지 않아도 되도록 하기 위한 분리입니다.
    (API 키/텔레그램 같은 비밀값은 toss_config.json 또는 GitHub Secrets에 별도 보관)
    """
    if not os.path.exists(STRATEGY_CONFIG_PATH):
        print(f"[안내] {STRATEGY_CONFIG_PATH} 가 없어 기본값으로 진행합니다: {DEFAULT_STRATEGY_CONFIG}", flush=True)
        return dict(DEFAULT_STRATEGY_CONFIG)
    with open(STRATEGY_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_STRATEGY_CONFIG)
    merged.update(cfg)
    return merged


_strategy_cfg = load_strategy_config()
SYMBOL = _strategy_cfg["symbol"]
N_DIV = _strategy_cfg["n_div"]
BUY_MIN_MULTIPLIER = _strategy_cfg["buy_min_multiplier"]   # 매수 실행조건: per_trade >= 전일종가 * 이 배수
SELL_RATE = _strategy_cfg["sell_rate"]                     # 매도목표가 = 전일종가 * (1+sell_rate)
HOLD_LIMIT = _strategy_cfg["hold_limit"]                   # 보유일(거래일 기준) 이 값 초과시 강제손절
BUFFER_USD = _strategy_cfg["buffer_usd"]                   # 총투자금 자동계산시 항상 남겨둘 버퍼(양도세 등 용도)
DRY_RUN = _strategy_cfg["dry_run"]                         # True면 실제 주문 없이 시뮬레이션만
print(f"[전략설정] symbol={SYMBOL} n_div={N_DIV} buy_min_multiplier={BUY_MIN_MULTIPLIER} "
      f"sell_rate={SELL_RATE} hold_limit={HOLD_LIMIT} buffer_usd={BUFFER_USD} dry_run={DRY_RUN}", flush=True)


def get_access_token(client_id, client_secret):
    resp = requests.post(
        f"{BASE_URL}/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        timeout=10,
    )
    if resp.status_code != 200:
        print("토큰 발급 실패:", resp.status_code, resp.text, flush=True)
        sys.exit(1)
    return resp.json()["access_token"]


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


def get_total_invest(token, account_seq, symbol):
    """
    총투자금 = (symbol의 현재 매수총금액=보유중인 원가) + (잔금=매수가능금액)
    접속(스크립트 실행)할 때마다 실시간으로 다시 계산합니다.
    """
    bp = get_buying_power(token, account_seq)
    cash = float(bp["result"]["cashBuyingPower"])

    holdings = get_holdings(token, account_seq)
    items = holdings.get("result", {}).get("items", [])
    purchase_amount = 0.0
    for item in items:
        if item.get("symbol") == symbol:
            purchase_amount = float(item["marketValue"]["purchaseAmount"])
            break

    total_invest = purchase_amount + cash
    print(f"   [총투자금 계산] {symbol} 매수총금액=${purchase_amount:,.2f} + 잔금=${cash:,.2f} = ${total_invest:,.2f}", flush=True)
    return total_invest, purchase_amount, cash


def get_accounts(token):
    resp = requests.get(f"{BASE_URL}/api/v1/accounts", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    resp.raise_for_status()
    return resp.json()["result"]


def get_prev_close(token, symbol):
    """
    가장 최근 '완결된' 일봉의 종가 = '전일종가'.
    장이 이미 열린 뒤(예: 저녁7시 실행 시점)엔 캔들 응답에 '오늘' 진행중인
    캔들이 이미 포함될 수 있고, 그 closePrice는 최종 마감가가 아니라
    그 순간까지의 실시간 값이라 그대로 쓰면 안 됨. 날짜가 오늘(KST 기준)과
    같은 캔들은 건너뛰고, 그 다음(=실제 완결된 이전 세션)을 사용한다.
    """
    resp = requests.get(
        f"{BASE_URL}/api/v1/candles",
        headers={"Authorization": f"Bearer {token}"},
        params={"symbol": symbol, "interval": "1d", "count": 5},
        timeout=10,
    )
    print(f"   [캔들 조회] 상태코드: {resp.status_code}", flush=True)
    print(f"   [캔들 응답 원본] {resp.text[:1200]}", flush=True)
    resp.raise_for_status()
    data = resp.json()

    result = data.get("result", data)
    candles = result.get("candles")
    if not candles:
        print("[에러] 캔들 데이터를 찾지 못했습니다. 위 원본 응답을 보고 파싱 로직을 수정해야 합니다.", flush=True)
        sys.exit(1)

    today_str = date_cls.today().isoformat()  # KST 기준 오늘 날짜 (YYYY-MM-DD)

    # 응답은 최신순(내림차순) 정렬. 날짜가 오늘인 캔들(진행중/실시간)은 건너뛴다.
    chosen = None
    for c in candles:
        ts = c.get("timestamp", "")
        candle_date = ts[:10]  # "2026-09-08T13:00:00.000+09:00" → "2026-09-08"
        if candle_date == today_str:
            print(f"   (오늘 날짜 캔들 발견 — 진행중일 수 있어 건너뜀: {ts})", flush=True)
            continue
        chosen = c
        break

    if chosen is None:
        print("[에러] 오늘을 제외하고 완결된 캔들을 찾지 못했습니다.", flush=True)
        sys.exit(1)

    close = chosen.get("closePrice") or chosen.get("close")
    if close is None:
        print("[에러] 캔들 항목에서 종가 필드를 찾지 못했습니다:", chosen, flush=True)
        sys.exit(1)
    print(f"   (기준 캔들 날짜: {chosen.get('timestamp')})", flush=True)
    return float(close), chosen


def place_buy_order(token, account_seq, symbol, price, quantity, client_order_id):
    body = {
        "clientOrderId": client_order_id,
        "symbol": symbol,
        "side": "BUY",
        "orderType": "LIMIT",
        "timeInForce": "CLS",
        "quantity": str(quantity),
        "price": str(price),
    }
    if DRY_RUN:
        print("   [DRY-RUN] 실제 주문을 넣지 않습니다. 아래 내용으로 주문이 나갈 예정입니다:", flush=True)
        print(f"   {json.dumps(body, ensure_ascii=False, indent=2)}", flush=True)
        return None

    print(f"   [주문 요청] {json.dumps(body, ensure_ascii=False)}", flush=True)
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
    print(f"   [주문 응답] 상태코드: {resp.status_code}", flush=True)
    print(f"   [주문 응답] {resp.text}", flush=True)
    return resp


def main():
    creds = load_credentials()

    print("1) 토큰 발급 중...", flush=True)
    token = get_access_token(creds["toss_client_id"], creds["toss_client_secret"])
    print("   완료\n", flush=True)

    print("2) 오늘 미국 증시 휴장일 여부 확인 중...", flush=True)
    holiday, reason = is_market_holiday(token)
    if holiday:
        msg = f"[csuf] 오늘은 휴장({reason})입니다. 매수/매도 주문 없이 종료합니다."
        print(f"   {msg}", flush=True)
        send_telegram(msg, creds)
        return
    print("   오늘은 개장일입니다. 진행합니다.\n", flush=True)

    print("3) 계좌 조회 중...", flush=True)
    accounts = get_accounts(token)
    account_seq = accounts[0]["accountSeq"]
    print(f"   accountSeq: {account_seq}\n", flush=True)

    print(f"4) 총투자금 계산 중 ({SYMBOL} 매수총금액 + 전체 잔금)...", flush=True)
    auto_total_invest_raw, purchase_amount, cash = get_total_invest(token, account_seq, SYMBOL)
    total_invest = max(auto_total_invest_raw - BUFFER_USD, 0)
    print(f"   자동계산값(원본): ${auto_total_invest_raw:,.2f}", flush=True)
    print(f"   버퍼(${BUFFER_USD:,.2f}) 차감 후 총투자금: ${total_invest:,.2f}\n", flush=True)

    print(f"5) {SYMBOL} 전일종가 조회 중...", flush=True)
    prev_close, raw_candle = get_prev_close(token, SYMBOL)
    print(f"   전일종가: ${prev_close}\n", flush=True)

    print(f"5-1) {SYMBOL} 현재가 조회 및 ±20% 가격제한 확인 중...", flush=True)
    current_price = get_current_price(token, SYMBOL)
    print(f"   현재가: ${current_price}", flush=True)
    buy_order_price, skip_buy = clip_order_price(prev_close, current_price, "BUY")
    if skip_buy:
        msg = (f"[csuf] 매수 스킵 — 전일종가(${prev_close})가 현재가(${current_price}) 대비 "
               f"허용범위(±20%)를 초과(급등)해서 오늘은 매수하지 않습니다.")
        print(f"   {msg}", flush=True)
        send_telegram(msg, creds)
        state = {"trading_day_index": 1, "total_invest": total_invest, "tiers": [], "pending_order": None}
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        return
    if buy_order_price != prev_close:
        print(f"   ⚠ 전일종가가 허용범위를 벗어나 매수주문가를 ${buy_order_price:.4f} 로 조정했습니다.", flush=True)
    else:
        print("   허용범위 이내 — 전일종가 그대로 사용합니다.", flush=True)
    print()

    per_trade = total_invest / N_DIV
    print(f"6) 회당매수금(per_trade) = ${total_invest:,.2f} / {N_DIV} = ${per_trade:.2f}", flush=True)

    min_required = prev_close * BUY_MIN_MULTIPLIER
    print(f"   매수 실행조건: per_trade(${per_trade:.2f}) >= 전일종가×{BUY_MIN_MULTIPLIER}(${min_required:.2f}) ?", flush=True)

    if per_trade < min_required:
        print("   조건 미충족 — 오늘은 매수하지 않습니다 (손절 아님, 단순 대기).", flush=True)
        send_telegram(
            f"[csuf] 매수 실행조건 미충족 (per_trade=${per_trade:.2f} < ${min_required:.2f}) — 오늘은 매수하지 않습니다.",
            creds,
        )
        state = {
            "trading_day_index": 1,
            "total_invest": total_invest,
            "tiers": [],
            "pending_order": None,
        }
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        return

    quantity = math.floor(per_trade / buy_order_price)
    print(f"   조건 충족 — 매수 수량 = floor({per_trade:.2f} / {buy_order_price:.4f}) = {quantity}주\n", flush=True)

    if quantity <= 0:
        print("[에러] 계산된 수량이 0입니다. 가격/금액을 다시 확인해주세요.", flush=True)
        sys.exit(1)

    client_order_id = f"csuf-day1-{int(time.time())}"
    print("7) 매수 주문 실행 중...", flush=True)
    resp = place_buy_order(token, account_seq, SYMBOL, buy_order_price, quantity, client_order_id)

    order_id = None
    if DRY_RUN:
        print("\n   [DRY-RUN] 실제 주문은 나가지 않았습니다. 계산 로직만 확인된 상태입니다.", flush=True)
        send_telegram(
            f"[csuf][DRY-RUN] 매수 시뮬레이션: {SYMBOL} {quantity}주 @ ${buy_order_price:.4f} "
            f"(sell_target=${round(buy_order_price*(1+SELL_RATE),2)}) — 실제 주문은 나가지 않았습니다.",
            creds,
        )
    elif resp is not None and resp.status_code == 200:
        order_id = resp.json().get("result", {}).get("orderId")
        print(f"\n   주문 접수 완료. orderId={order_id}", flush=True)
        send_telegram(
            f"[csuf] 1일차 매수주문 접수: {SYMBOL} {quantity}주 @ ${buy_order_price:.4f} "
            f"(sell_target=${round(buy_order_price*(1+SELL_RATE),2)}, orderId={order_id})",
            creds,
        )
    else:
        print("\n   [주의] 주문 접수 실패. 위 응답 내용을 확인해주세요.", flush=True)
        send_telegram(f"[csuf][에러] 매수 주문 접수 실패. 응답: {resp.text if resp is not None else 'None'}", creds)

    # 2일차 아침 스크립트가 체결 확인할 수 있도록 상태 저장
    state = {
        "trading_day_index": 1,
        "total_invest": total_invest,
        "tiers": [],  # 아직 체결 전이므로 빈 상태. 체결 확인 후 2일차 스크립트가 채움.
        "pending_order": {
            "client_order_id": client_order_id,
            "order_id": order_id,
            "symbol": SYMBOL,
            "quantity": quantity,
            "order_price_used": buy_order_price,   # 실제 주문에 쓰인 가격(가격제한 클리핑 반영됨)
            "sell_target": round(buy_order_price * (1 + SELL_RATE), 4),
            "buy_day_index": 1,
        },
    }
    save_path = STATE_PATH
    if DRY_RUN:
        save_path = os.path.join(SCRIPT_DIR, "csuf_state_dryrun.json")
        print(f"\n[DRY-RUN] 실제 상태파일({STATE_PATH})은 건드리지 않고, 확인용으로만 저장합니다.", flush=True)
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    print(f"상태 저장 완료: {save_path}", flush=True)


if __name__ == "__main__":
    main()
