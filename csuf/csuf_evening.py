r"""
csuf 저녁 7시 — 매도(청산) 처리 + 매수 실행 (v2 명세 기준)

일별 처리 순서(v2):
① 매도 처리: 각 보유 티어에 대해 "보유일(hold_limit) 초과" 또는 "sell_target 도달 예상"이면 매도주문
   - 보유일 초과(hold_limit+1일째) → 무조건 매도(강제청산). 진짜 MOC는 미지원이라
     현재가×0.8(허용 하한선)로 지정가+CLS 주문을 걸어 사실상 반드시 체결되게 함
   - 그 외 → sell_target으로 지정가+CLS 매도주문 (단, ±20% 가격제한 클리핑 적용:
     현재가 대비 목표가 너무 낮으면 매도 스킵, 너무 높으면 현재가×1.01로 하향)
② 매수 사이징: per_trade = min(total_invest/3, cash)  ← v2는 동시보유 티어수 상한 없음
③ 매수 실행: per_trade >= 전일종가×1.3 이면 매수 (±20% 클리핑 적용, day1과 동일 규칙)

주의: 실현손익에 따른 total_invest 갱신은 '체결 확인 후'(다음날 아침)에 반영됩니다.
      이 저녁 스크립트는 '주문만' 내고, 실제 체결/손익 반영은 아침 스크립트가 담당합니다.

사전 준비:
  - toss_common.py 가 이 파일의 상위 폴더(C:\@InvQuint\@Toss\)에 있어야 함
  - toss_config.json 도 같은 상위 폴더에 있어야 함 (비밀값 전용)
  - csuf_config.json 은 이 파일과 같은 폴더에 있어야 함 (전략 파라미터)
  - csuf_state.json 은 아침 스크립트가 최신화해둔 상태파일
  - pip install requests
"""

import os
import sys
import time
import json
import math

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, PARENT_DIR)
from toss_common import (  # noqa: E402
    load_credentials, send_telegram, is_market_holiday,
    get_current_price, clip_order_price,
    get_accounts, get_buying_power, get_holdings,
    get_prev_close, get_completed_trading_dates, count_held_trading_days,
    place_order,
)

STATE_PATH = os.path.join(BASE_DIR, "csuf_state.json")
STRATEGY_CONFIG_PATH = os.path.join(BASE_DIR, "csuf_config.json")

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
    if not os.path.exists(STRATEGY_CONFIG_PATH):
        print(f"[안내] {STRATEGY_CONFIG_PATH} 가 없어 기본값으로 진행합니다.", flush=True)
        return dict(DEFAULT_STRATEGY_CONFIG)
    with open(STRATEGY_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_STRATEGY_CONFIG)
    merged.update(cfg)
    return merged


_cfg = load_strategy_config()
SYMBOL = _cfg["symbol"]
N_DIV = _cfg["n_div"]
BUY_MIN_MULTIPLIER = _cfg["buy_min_multiplier"]
SELL_RATE = _cfg["sell_rate"]
HOLD_LIMIT = _cfg["hold_limit"]
BUFFER_USD = _cfg["buffer_usd"]
DRY_RUN = _cfg["dry_run"]
print(f"[전략설정] symbol={SYMBOL} n_div={N_DIV} buy_min_multiplier={BUY_MIN_MULTIPLIER} "
      f"sell_rate={SELL_RATE} hold_limit={HOLD_LIMIT} buffer_usd={BUFFER_USD} dry_run={DRY_RUN}", flush=True)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"tiers": [], "pending_order": None, "total_invest": 0.0}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_access_token(client_id, client_secret):
    import requests
    resp = requests.post(
        "https://openapi.tossinvest.com/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        timeout=10,
    )
    if resp.status_code != 200:
        print("토큰 발급 실패:", resp.status_code, resp.text, flush=True)
        sys.exit(1)
    return resp.json()["access_token"]


def main():
    creds = load_credentials()

    print("1) 토큰 발급 중...", flush=True)
    token = get_access_token(creds["toss_client_id"], creds["toss_client_secret"])
    print("   완료\n", flush=True)

    print("2) 오늘 미국 증시 휴장일 여부 확인 중...", flush=True)
    holiday, holiday_reason = is_market_holiday(token)
    if holiday:
        msg = f"[csuf] 오늘은 휴장({holiday_reason})입니다. 매수/매도 주문 없이 종료합니다."
        print(f"   {msg}", flush=True)
        send_telegram(msg, creds)
        return
    print("   오늘은 개장일입니다. 진행합니다.\n", flush=True)

    print("3) 계좌 조회 중...", flush=True)
    accounts = get_accounts(token)
    account_seq = accounts[0]["accountSeq"]
    print(f"   accountSeq: {account_seq}\n", flush=True)

    state = load_state()
    tiers = state.get("tiers", [])
    total_invest = state.get("total_invest", 0.0)

    print(f"4) {SYMBOL} 현재가 조회 중...", flush=True)
    current_price = get_current_price(token, SYMBOL)
    print(f"   현재가: ${current_price}\n", flush=True)

    trading_dates = get_completed_trading_dates(token, SYMBOL)
    telegram_lines = [f"[csuf] 저녁 주문 실행({SYMBOL})"]

    # ------------------------------------------------------------------
    # ① 매도(청산) 처리 — 기존 티어별로 매도주문 실행
    # ------------------------------------------------------------------
    print("5) 기존 티어 매도주문 처리 중...", flush=True)
    for t in tiers:
        held_tonight = count_held_trading_days(trading_dates, t["buy_date"]) + 1  # 오늘 마감까지 포함
        force_sell = held_tonight > HOLD_LIMIT

        if force_sell:
            sell_price = round(current_price * 0.8, 4)  # 허용 하한선 — 사실상 반드시 체결 목표(강제청산)
            reason = f"보유일초과(held={held_tonight})"
        else:
            sell_price, skip = clip_order_price(t["sell_target"], current_price, "SELL")
            if skip:
                print(f"   티어(매수일 {t['buy_date']}) — 목표가 미달로 매도 스킵 (다음날 재시도)", flush=True)
                telegram_lines.append(f"매도스킵: {t['buy_date']} 티어 (목표${t['sell_target']:.2f} 미달)")
                continue
            reason = f"목표가(${t['sell_target']:.2f})"

        client_order_id = f"csuf-sell-{t['buy_date']}-{int(time.time())}"
        print(f"   티어(매수일 {t['buy_date']}, {t['quantity']}주) 매도주문: ${sell_price} ({reason})", flush=True)

        if DRY_RUN:
            print("   [DRY-RUN] 실제 주문 없이 시뮬레이션만.", flush=True)
            telegram_lines.append(f"[DRY-RUN]매도: {t['buy_date']} {t['quantity']}주 @${sell_price:.2f} ({reason})")
        else:
            resp = place_order(token, account_seq, SYMBOL, "SELL", sell_price, t["quantity"], client_order_id)
            if resp.status_code == 200:
                order_id = resp.json().get("result", {}).get("orderId")
                t["pending_sell_order_id"] = order_id
                print(f"   → 매도주문 접수. orderId={order_id}", flush=True)
                telegram_lines.append(f"매도주문: {t['buy_date']} {t['quantity']}주 @${sell_price:.2f} ({reason})")
            else:
                print(f"   [에러] 매도주문 실패: {resp.status_code} {resp.text}", flush=True)
                telegram_lines.append(f"[에러]매도실패: {t['buy_date']} 티어 — {resp.text[:200]}")

    print()

    # ------------------------------------------------------------------
    # ② 매수 사이징 확정 (v2: 티어수 상한 없음)
    # ------------------------------------------------------------------
    print("6) 매수 사이징 계산 중...", flush=True)
    bp = get_buying_power(token, account_seq)
    cash = float(bp["result"]["cashBuyingPower"]) - BUFFER_USD
    cash = max(cash, 0)
    per_trade = min(total_invest / N_DIV, cash) if total_invest > 0 else cash
    print(f"   total_invest=${total_invest:,.2f}  cash(버퍼차감후)=${cash:,.2f}  per_trade=${per_trade:,.2f}\n", flush=True)

    # ------------------------------------------------------------------
    # ③ 매수 실행 (v2: 티어수 조건 없이 금액조건만 확인)
    # ------------------------------------------------------------------
    print(f"7) {SYMBOL} 전일종가 조회 및 매수 조건 확인 중...", flush=True)
    prev_close, _ = get_prev_close(token, SYMBOL)
    print(f"   전일종가: ${prev_close}", flush=True)

    min_required = prev_close * BUY_MIN_MULTIPLIER
    print(f"   매수조건: per_trade(${per_trade:.2f}) >= 전일종가×{BUY_MIN_MULTIPLIER}(${min_required:.2f}) ?", flush=True)

    new_pending_order = None
    if per_trade < min_required:
        print("   조건 미충족 — 오늘은 매수하지 않습니다.\n", flush=True)
        telegram_lines.append(f"매수스킵: per_trade(${per_trade:.2f}) < 최소조건(${min_required:.2f})")
    else:
        buy_price, skip_buy = clip_order_price(prev_close, current_price, "BUY")
        if skip_buy:
            print("   전일종가가 현재가 대비 급등 — 매수 스킵.\n", flush=True)
            telegram_lines.append(f"매수스킵: 전일종가(${prev_close})가 현재가(${current_price}) 대비 급등")
        else:
            quantity = math.floor(per_trade / buy_price)
            print(f"   매수 수량 = floor({per_trade:.2f} / {buy_price:.4f}) = {quantity}주\n", flush=True)
            if quantity > 0:
                client_order_id = f"csuf-buy-{int(time.time())}"
                sell_target = round(buy_price * (1 + SELL_RATE), 4)
                if DRY_RUN:
                    print("   [DRY-RUN] 실제 주문 없이 시뮬레이션만.", flush=True)
                    telegram_lines.append(
                        f"[DRY-RUN]매수: {quantity}주 @${buy_price:.2f} (목표${sell_target:.2f})"
                    )
                else:
                    resp = place_order(token, account_seq, SYMBOL, "BUY", buy_price, quantity, client_order_id)
                    if resp.status_code == 200:
                        order_id = resp.json().get("result", {}).get("orderId")
                        new_pending_order = {
                            "client_order_id": client_order_id,
                            "order_id": order_id,
                            "symbol": SYMBOL,
                            "quantity": quantity,
                            "order_price_used": buy_price,
                            "sell_target": sell_target,
                        }
                        print(f"   → 매수주문 접수. orderId={order_id}", flush=True)
                        telegram_lines.append(
                            f"매수주문: {quantity}주 @${buy_price:.2f} (목표${sell_target:.2f}, orderId={order_id})"
                        )
                    else:
                        print(f"   [에러] 매수주문 실패: {resp.status_code} {resp.text}", flush=True)
                        telegram_lines.append(f"[에러]매수실패: {resp.text[:200]}")

    # ------------------------------------------------------------------
    # 상태 저장 + 텔레그램 리포팅
    # ------------------------------------------------------------------
    state["tiers"] = tiers
    state["pending_order"] = new_pending_order
    state["total_invest"] = total_invest
    save_state(state)
    print(f"상태 저장 완료: {STATE_PATH}", flush=True)

    message = "\n".join(telegram_lines)
    print("=" * 50, flush=True)
    print(message, flush=True)
    print("=" * 50, flush=True)
    send_telegram(message, creds)


if __name__ == "__main__":
    main()
