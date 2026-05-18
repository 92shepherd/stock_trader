"""EOD bot simulation infrastructure.

Subpackages:
    factors    - factor catalog + panel loader (read factor_signals)
    strategy   - BaseStrategy ABC, DeclarativeStrategy, plugin registry
    simulator  - paper broker (KIS price-only, no order API)
    bot        - daily tick runner, lifecycle, PnL accounting
    repositories - DB CRUD for the 6 EOD-bot tables

Design rules (non-negotiable, mirror project conventions):
  1. KIS 매매 API 는 절대 호출하지 않는다. 가격 조회만 허용.
  2. 잔고/매수/매도/포지션은 전부 DB 안에서만 존재.
  3. STOPPED 봇은 어떤 daily tick 도 거래를 만들지 않는다.
  4. 같은 봇이 같은 decision_date 에 두 번 tick 하지 못한다.
"""
from __future__ import annotations
