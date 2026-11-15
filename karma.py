"""红名（Karma）判定 —— ``protocol.md`` §12 的落地实现。

规则来自 ``main/材料/文档/规则/rule.md`` 第三章（原文见协议 §12 的引用）。

触发（§12.1）
------------
**人类击杀同势力人类** → 击杀者进入红名（``red = true``）。
「击杀」的时点不是打空血那一瞬，而是中毒者濒死窗口结束、被服务端判定为
**真死亡**的那一刻（``game._resolve_timers`` 的 ``died`` 分支）。

本期只有 ``human`` 一个种族、没有势力系统，因此**人类杀人类必定触发**。

豁免（§12.2，判定函数留参，当前无对应实体）
------------------------------------------
下列情形都不触发红名。字段现在从玩家行里取不到时就落默认值
（``race='human'`` / ``faction='common'`` / ``is_boss=False``），
将来加了种族、势力、BOSS 身份只需把字段塞进 player 行，这里不用改结构：

* 不同势力人类互杀；
* 人类杀妖精；
* 妖精杀任何玩家；
* BOSS 身份击杀（击杀者是 BOSS）。

惩罚 / 解除（§12.3 / §12.4）不在本模块：惩罚只落成 ``peer.red`` /
``state.red`` 标记（本期没有任务系统，也没有额外攻击限制），
解除只有「确认死亡」与「注销重建角色」两条，见 ``game.py`` 的死亡结算。
"""

from __future__ import annotations

## 人类（本期唯一可玩种族）
RACE_HUMAN = "human"
## 默认势力：本期没有势力系统，所有玩家视为同一势力
DEFAULT_FACTION = "common"


def _text(value, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _race_of(row: dict) -> str:
    return _text(row.get("race"), RACE_HUMAN).lower() or RACE_HUMAN


def _faction_of(row: dict) -> str:
    return _text(row.get("faction"), DEFAULT_FACTION) or DEFAULT_FACTION


def should_become_red(killer: dict | None, victim: dict | None) -> bool:
    """击杀者是否因这次**确认死亡**进入红名（§12.1 / §12.2）。

    ``killer`` / ``victim`` 都是玩家行（``game.players`` 里的 dict）；
    击杀者可能已离线（行不在热缓存里），此时调用方会传 ``None`` → 不触发。
    """
    if not killer or not victim:
        return False

    killer_id = _text(killer.get("user_id"))
    victim_id = _text(victim.get("user_id"))
    # 自杀 / 自伤致死不触发（正常路径不会走到：attack 拒绝打自己）
    if killer_id and killer_id == victim_id:
        return False

    # BOSS 身份击杀豁免
    if bool(killer.get("is_boss")):
        return False

    killer_race = _race_of(killer)
    victim_race = _race_of(victim)
    # 妖精（非人类）杀任何玩家、人类杀妖精 —— 均豁免
    if killer_race != RACE_HUMAN or victim_race != RACE_HUMAN:
        return False

    # 不同势力人类互杀 —— 豁免；本期两边都取默认势力 'common'
    if _faction_of(killer) != _faction_of(victim):
        return False

    # 人类 × 同势力人类：必定触发
    return True
