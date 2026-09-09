r"""
csuf 아침 10시 — 체결확인 + 티어정리 + 현황 리포팅

이 스크립트가 하는 일:
1. 로그인
2. csuf_state.json 로딩 (pending_order, tiers)
3. pending_order가 있으면 orderId로 체결여부 조회
   - 체결됨 → 새 티어 생성 (매수일=체결일자, sell_target은 전날 저녁에 이미 계산해둔 값 그대로 사용, 재계산 안 함)
   - 미체결 → 그대로 둠 (오늘 저녁에 재시도)
4. 기존 티어 중 pending_sell_order_id가 있는 것들도 같은 방식으로 체결확인 → 체결되면 티어 삭제
5. holdings API로 실제 보유수량과 티어 합계 대조 (불일치시 경고만, 자동수정 없음)
6. 각 티어의 보유일수 계산 (실제 거래일 캔들 날짜 카운트 기준, 주말/휴일 자동 제외)
7. 오늘 휴장일이든 아니든 항상 현재 티어 현황을 텔레그램으로 리포팅 (휴장이면 마지막에 안내 문구 추가)

사전 준비:
  - toss_common.py 가 이 파일의 상위 폴더(C:\@InvQuint\@Toss\)에 있어야 함
  - toss_config.json 도 같은 상위 폴더에 있어야 함 (비밀값 전용)
  - csuf_config.json 은 이 파일과 같은 폴더에 있어야 함 (전략 파라미터)
  - csuf_state.json 은 전날 저녁 스크립트가 만들어둔 상태파일
  - pip install requests
"""

import os
import sys
import json
from datetime import date as date_cls

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PARENT_DIR)
from toss_common import (  # noqa: E402
    load_credentials, send_telegram, is_market_holiday,
    get_accounts, get_holdings, get_order_detail,
    get_completed_trading_dates, count_held_trading_days,
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
    if not os.path.exists(STRATEGY_CONFIG_PATH):
        print(f"[안내] {STRATEGY_CONFIG_PATH} 가 없어 기본값으로 진행합니다.", flush=True)
        return dict(DEFAULT_STRATEGY_CONFIG)
    with open(STRATEGY_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_STRATEGY_CONFIG)
    merged.update(cfg)
    return merged


_strategy_cfg = load_strategy_config()
SYMBOL = _strategy_cfg["symbol"]
HOLD_LIMIT = _strategy_cfg["hold_limit"]


def load_state():
    if not os.path.exists(STATE_PATH):
        print(f"[안내] {STATE_PATH} 가 없습니다. 빈 상태로 시작합니다.", flush=True)
        return {"tiers": [], "pending_order": None, "total_invest": 0.0}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state, if_path=STATE_PATH):
    with open(if_path, "w", encoding="utf-8") as f:
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
    print(f"   휴장여부: {holiday} ({holiday_reason})\n", flush=True)

    print("3) 계좌 조회 중...", flush=True)
    accounts = get_accounts(token)
    account_seq = accounts[0]["accountSeq"]
    print(f"   accountSeq: {account_seq}\n", flush=True)

    state = load_state()
    tiers = state.get("tiers", [])
    pending_order = state.get("pending_order")

    # --- 4) 신규 매수 체결확인 ---
    if pending_order:
        print("4) 대기중인 신규 매수 주문 체결확인 중...", flush=True)
        detail = get_order_detail(token, account_seq, pending_order["order_id"])
        if detail is None:
            print("   [주의] 주문상세 조회 실패. 이번엔 건너뜁니다.", flush=True)
        else:
            # ⚠ 아래 필드명(status, filledDate 등)은 실제 응답을 보고 확정 필요.
            status = detail.get("status") or detail.get("orderStatus")
            filled_date = (detail.get("filledDate") or detail.get("executedDate")
                           or detail.get("filledAt") or detail.get("updatedAt"))
            print(f"   상태: {status}, 체결일자(추정필드): {filled_date}", flush=True)

            if status in ("FILLED", "FULLY_FILLED", "COMPLETE", "DONE"):
                buy_date = (filled_date or date_cls.today().isoformat())[:10]
                new_tier = {
                    "quantity": pending_order["quantity"],
                    "buy_price": pending_order["order_price_used"],
                    "buy_date": buy_date,
                    "sell_target": pending_order["sell_target"],  # 재계산 안 함, 전날 값 그대로
                    "order_id": pending_order["order_id"],
                    "pending_sell_order_id": None,
                }
                tiers.append(new_tier)
                pending_order = None
                print(f"   → 신규 티어 생성 완료: {new_tier}", flush=True)
            elif status in ("CANCELLED", "REJECTED", "EXPIRED"):
                print("   → 주문이 취소/거부/만료됨. 대기주문 제거.", flush=True)
                pending_order = None
            else:
                print("   → 아직 미체결. 오늘 저녁에 재시도 예정, 상태 유지.", flush=True)
    else:
        print("4) 대기중인 신규 매수 주문 없음.\n", flush=True)

    # --- 5) 기존 티어의 매도주문 체결확인 (저녁 스크립트가 pending_sell_order_id를 채워뒀다면) ---
    print("5) 기존 티어 매도주문 체결확인 중...", flush=True)

    # avg_price_before: 오늘 매도처리 시작 전(전일마감 시점) 전체가중평균 매입가.
    # 오늘 여러 티어가 동시에 팔려도, 이 값은 하루 동안 고정해서 씀(명세서 컨벤션).
    total_qty_before = sum(t["quantity"] for t in tiers)
    total_cost_before = sum(t["quantity"] * t["buy_price"] for t in tiers)
    avg_price_before = (total_cost_before / total_qty_before) if total_qty_before > 0 else 0.0
    print(f"   전체가중평균 매입가(avg_price_before): ${avg_price_before:.4f}", flush=True)

    realized_pnl_total = 0.0
    sold_summaries = []
    surviving_tiers = []
    for t in tiers:
        sell_oid = t.get("pending_sell_order_id")
        if not sell_oid:
            surviving_tiers.append(t)
            continue
        detail = get_order_detail(token, account_seq, sell_oid)
        status = detail.get("status") if detail else None
        if status in ("FILLED", "FULLY_FILLED", "COMPLETE", "DONE"):
            # ⚠ 체결가 필드명도 실제 응답 보고 확정 필요 — 여러 후보를 순차 시도
            fill_price = None
            if detail:
                for key in ("avgFillPrice", "filledPrice", "executedPrice", "price"):
                    if detail.get(key) is not None:
                        fill_price = float(detail[key])
                        break
            if fill_price is None:
                fill_price = t["sell_target"]  # 최후수단: 목표가로 대체 (근사치)
                print(f"   [주의] 체결가 필드를 못 찾아 sell_target(${fill_price})으로 근사합니다.", flush=True)

            realized_pnl = (fill_price - avg_price_before) * t["quantity"]
            realized_pnl_total += realized_pnl
            sold_summaries.append(
                f"매도체결: {t['buy_date']} {t['quantity']}주 @${fill_price:.2f} "
                f"(실현손익 ${realized_pnl:+.2f})"
            )
            print(f"   → 티어(매수일 {t['buy_date']}) 매도 체결 확인 — 실현손익 ${realized_pnl:+.2f}, 티어 제거", flush=True)
            continue  # 리스트에서 제외 = 삭제
        else:
            print(f"   → 티어(매수일 {t['buy_date']}) 매도 미체결 — 유지", flush=True)
            surviving_tiers.append(t)
    tiers = surviving_tiers

    total_invest = state.get("total_invest", 0.0) + realized_pnl_total
    if realized_pnl_total != 0.0:
        print(f"   total_invest 갱신: {state.get('total_invest', 0.0):.2f} + {realized_pnl_total:+.2f} = {total_invest:.2f}", flush=True)
    print()

    # --- 6) 보유일수 계산 ---
    trading_dates = get_completed_trading_dates(token, SYMBOL)
    for t in tiers:
        t["held_days"] = count_held_trading_days(trading_dates, t["buy_date"])

    # --- 7) holdings 대조 (경고만) ---
    print("6) holdings 대조 확인 중...", flush=True)
    holdings = get_holdings(token, account_seq)
    items = holdings.get("result", {}).get("items", [])
    actual_qty = 0.0
    for item in items:
        if item.get("symbol") == SYMBOL:
            actual_qty = float(item["quantity"])
            break
    tier_qty_sum = sum(t["quantity"] for t in tiers)
    print(f"   실제 보유수량: {actual_qty}  /  티어 합계: {tier_qty_sum}", flush=True)
    mismatch_note = ""
    if abs(actual_qty - tier_qty_sum) > 1e-6:
        mismatch_note = f"\n⚠ 불일치! 실제보유={actual_qty}, 티어합계={tier_qty_sum} (수동 확인 필요)"
        print(f"   {mismatch_note}", flush=True)
    else:
        print("   일치합니다.\n", flush=True)

    # --- 상태 저장 ---
    state["tiers"] = tiers
    state["pending_order"] = pending_order
    state["total_invest"] = total_invest
    save_state(state)
    print(f"상태 저장 완료: {STATE_PATH}\n", flush=True)

    # --- 8) 텔레그램 리포팅 ---
    total_bought = sum(t["quantity"] * t["buy_price"] for t in tiers)
    cash = total_invest - total_bought

    lines = [f"[csuf] 아침 티어정리({SYMBOL})"]
    if sold_summaries:
        lines.extend(sold_summaries)
    lines.append(f"총투자({total_invest:,.0f}) 총매입({total_bought:,.0f}) 잔금({cash:,.0f})")
    if not tiers:
        lines.append("보유 중인 티어가 없습니다.")
    else:
        for i, t in enumerate(tiers, start=1):
            held = t.get("held_days", "?")
            flag = " ⚠손절대상(오늘저녁)" if isinstance(held, int) and held >= HOLD_LIMIT else ""
            lines.append(
                f"{i}# {t['buy_date']}  {t['quantity']}주 "
                f"@${t['buy_price']:.2f}→${t['sell_target']:.2f}"
                f"({held:>2}일){flag}"
            )
    if pending_order:
        lines.append(f"[대기중] {pending_order['quantity']}주 @ ${pending_order['order_price_used']} 체결 대기")
    if mismatch_note:
        lines.append(mismatch_note)
    if holiday:
        lines.append(f"\n※ 오늘은 휴장({holiday_reason})입니다. 오늘 저녁 주문은 없습니다.")

    message = "\n".join(lines)
    print("=" * 50, flush=True)
    print(message, flush=True)
    print("=" * 50, flush=True)
    send_telegram(message, creds)


if __name__ == "__main__":
    main()
