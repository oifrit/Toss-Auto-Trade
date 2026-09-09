"""
csuf 티어 현황 조회
csuf_state.json 을 읽어서 현재 살아있는 티어들의 상태를 화면 + 텔레그램으로 보여줍니다.
언제든 실행해서 확인용으로 쓰면 됩니다 (주문/체결 등 어떤 것도 건드리지 않는 조회 전용 스크립트).

참고: held_days(보유일)는 아침 스크립트(csuf_morning.py)가 마지막으로 계산해서
저장해둔 값을 그대로 보여줍니다. 저녁 스크립트 실행 이후~다음날 아침 사이에는
그 사이 지난 하루가 아직 반영 안 된 값일 수 있습니다.

사전 준비:
  - toss_common.py 가 이 파일의 상위 폴더(C:\\@InvQuint\\@Toss\\)에 있어야 함
  - toss_config.json 도 같은 상위 폴더에 있어야 함 (텔레그램 키 포함)
"""

import os
import sys
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PARENT_DIR)
from toss_common import load_credentials, send_telegram  # noqa: E402

STATE_PATH = os.path.join(SCRIPT_DIR, "csuf_state.json")

HOLD_LIMIT = 3  # 참고 표시용 (실제 손절 판정은 매매 스크립트에서 수행)


def load_state():
    if not os.path.exists(STATE_PATH):
        return None
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_report():
    state = load_state()
    if state is None:
        return None, f"[안내] {STATE_PATH} 가 아직 없습니다. 아직 체결된 티어가 없는 상태입니다."

    tiers = state.get("tiers", [])
    pending = state.get("pending_order")
    total_invest = state.get("total_invest")

    console_lines = []
    console_lines.append("=" * 78)
    console_lines.append(" csuf 티어 현황")
    console_lines.append("=" * 78)
    total_bought = sum(t.get("quantity", 0) * t.get("buy_price", 0) for t in tiers)
    cash = (total_invest - total_bought) if total_invest is not None else None
    if total_invest is not None:
        console_lines.append(f" 총투자({total_invest:,.0f}) 총매입({total_bought:,.0f}) 잔금({cash:,.0f})")
    console_lines.append(f" 현재 살아있는 티어 수: {len(tiers)}\n")

    telegram_lines = [f"[csuf] 티어 현황 조회"]
    if total_invest is not None:
        telegram_lines.append(f"총투자({total_invest:,.0f}) 총매입({total_bought:,.0f}) 잔금({cash:,.0f})")

    if not tiers:
        console_lines.append(" 보유 중인 티어가 없습니다.\n")
        telegram_lines.append("보유 중인 티어가 없습니다.")
    else:
        header = f"{'티어':^4} | {'매수일':^12} | {'매수가':>10} | {'매도목표가':>10} | {'수량':>8} | {'보유일':>6} | {'상태':^10}"
        console_lines.append(header)
        console_lines.append("-" * len(header))
        for i, t in enumerate(tiers, start=1):
            buy_date = t.get("buy_date", "?")
            held = t.get("held_days")
            if held is None:
                status = "?"
                held_display = "?"
            else:
                held_display = held
                if held >= HOLD_LIMIT:
                    status = "손절대상"
                elif held == HOLD_LIMIT - 1:
                    status = "마지막날"
                else:
                    status = "정상보유"
            sell_pending = " (매도주문중)" if t.get("pending_sell_order_id") else ""
            console_lines.append(
                f"{i:^4} | {buy_date:^12} | {t.get('buy_price', 0):>10.4f} | {t.get('sell_target', 0):>10.4f} | "
                f"{t.get('quantity', 0):>8.4f} | {held_display:>6} | {status:^10}{sell_pending}"
            )
            flag = " ⚠손절대상" if isinstance(held, int) and held >= HOLD_LIMIT else ""
            telegram_lines.append(
                f"{i}# {buy_date}  {t.get('quantity', 0)}주 "
                f"@${t.get('buy_price', 0):.2f}→${t.get('sell_target', 0):.2f}"
                f"({held_display}일){flag}{sell_pending}"
            )

    if pending:
        console_lines.append("-" * 78)
        console_lines.append(" [대기중인 신규 매수 주문] 아직 체결 확인 전:")
        console_lines.append(f"   수량 {pending.get('quantity')}주 @ ${pending.get('order_price_used')} "
                              f"(체결되면 매도목표가 ${pending.get('sell_target')} 티어로 등록 예정)")
        console_lines.append("-" * 78)
        telegram_lines.append(
            f"[대기중] {pending.get('quantity')}주 @${pending.get('order_price_used')} 체결 대기"
        )

    return "\n".join(console_lines), "\n".join(telegram_lines)


def main():
    console_msg, telegram_msg = build_report()
    if console_msg is None:
        print(telegram_msg)  # 안내 메시지만 있는 경우
        return
    print(console_msg)

    creds = load_credentials()
    send_telegram(telegram_msg, creds)


if __name__ == "__main__":
    main()
