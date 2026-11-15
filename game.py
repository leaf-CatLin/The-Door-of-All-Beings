"""运行时热缓存：世界实体 + 玩家状态。

**权威在数据库，不在内存。** 这是与旧版最大的区别：

| 旧版 | 现在 |
|---|---|
| 内存权威，每 60s 把内存写进库 | 库权威，内存只是缓存；启动从库读回 |
| 写库直接覆盖 | 写库带 ``rev`` 门禁，落后数据直接被库拒掉 |
| 落库失败只写本地 JSON | 同样写本地 JSON 兜底，但下次启动以库为准 |

因此“重启会丢数据”不再是真的：重启只会丢掉**未落库的最后几秒**，
而那些数据本来也不能覆盖库里更新的快照。

===========================================================================
world 元素结构（对应表 world_item）
---------------------------------------------------------------------------
{
  "id": "door_01",      # 全局唯一标识（主键）
  "world_id": "default", # 所属世界
  "kind": "door",        # door / crate / lever / generic …
  "pos_x": 12.5,
  "pos_y": 0.0,
  "pos_z": -8.0,
  "yaw": 90.0,            # 度，0 = 正北 / -Z
  "state": "idle",
  "rev": 3,               # 修订号：只有比库里大才能覆盖
  "updated_at": 1761379200.0,
}

players 元素结构（对应表 player）
---------------------------------------------------------------------------
{
  "user_id": "ZWME-2WKQ", # 主键（= users.user_id）
  "user_name": "勇者",     # 账号名（来自 users 表，只读）
  "player_name": "勇者",   # 角色名（游戏内显示）
  "race": "human",         # 种族
  "device_id": "ab cd…",  # 绑定设备（只在内存里，不落库）
  "pos_x": 12.5, "pos_y": 1.65, "pos_z": -8.0, "yaw": 180.0,
                          # **头部中心的世界坐标**（protocol.md §11.1）：
                          # 脚底 = pos_y - HEAD_OFFSET_Y(1.65)
  "pitch": 0.0,           # 瞄准俯仰角（度，0 = 水平，正 = 抬头；protocol.md §4 第 9 段）。
                          # **只在内存里**：不落库、不进 peers，只给攻击方向用（§11.3）
  "coord_base": "head",    # 这一行的坐标基准（内存里恒为 head；
                          # 库里旧行是 'feet'，加载时一次性 +1.65 迁移）
  "state": "moving",       # idle / moving / dead
  "online": True,
  "hp_max": 100.0, "hp": 88.0,
  "stamina_max": 100.0, "stamina": 100.0,
  "mana_max": 100.0, "mana": 100.0,
  "spirit": 4.0,          # 灵力（每小时 +1，升级消耗）
  "level": 2,             # 等级
  "spirit_at": 1761379200.0,  # 上次保存时间（每小时累计的基准点）
  "death_at": None,       # 濒死开始时间（5 秒内可被复活）
  "spectator_until": None,# 旁观结束时间（死亡 30 分钟）
  "last_attacker": "",    # 最后一次伤害来源（死亡结算赏金）
  "inventory": {},        # 背包
  "kills": 0, "deaths": 0,
  "red_name": False,      # 红名（protocol.md §12）
  "red_at": None,         # 变红时间（epoch 秒）
  "rev": 7,
  "updated_at": 1761379200.0,
}
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import hitbox
import karma

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 运行时热缓存
# ---------------------------------------------------------------------------
world: dict[str, dict] = {}        # item_id -> world_item 行
players: dict[str, dict] = {}      # user_id -> player 行
world_meta: dict[str, dict] = {}   # world_id -> world 行

_lock = threading.RLock()
## 推送用的全局修订号（单调递增，不等于行级 rev）
_world_rev = 0
_players_rev = 0

## 坐标合法性上限（米）：超出直接丢弃，避免 NaN / inf 污染内存与数据库
COORD_LIMIT = 100000.0
## 允许的玩家状态
PLAYER_STATES = ("idle", "moving", "dead")

DEFAULT_WORLD = "default"
DEFAULT_RACE = "human"
## 三围默认值（上限与当前值都从 100 起）
DEFAULT_STATS = {
    "hp_max": 100.0,
    "hp": 100.0,
    "stamina_max": 100.0,
    "stamina": 100.0,
    "mana_max": 100.0,
    "mana": 100.0,
}

# ---------------------------------------------------------------------------
# 灵力 / 等级
# ---------------------------------------------------------------------------
## 每小时固定增长的灵力
SPIRIT_PER_HOUR = 1.0
SPIRIT_HOUR_SECONDS = 3600.0
## 等级上限（防御性：避免异常数据把升级循环卡死）
MAX_LEVEL = 9999

# ---------------------------------------------------------------------------
# 战斗 / 死亡
# ---------------------------------------------------------------------------
## 近战：射程 / 单次伤害 / 攻击冷却（秒）
ATTACK_RANGE = 3.0
ATTACK_DAMAGE = 25.0
ATTACK_COOLDOWN = 0.8
## 濒死后被复活类治疗的最大距离
REVIVE_RANGE = 5.0
## 复活后恢复到生命上限的百分比
REVIVE_HP_RATIO = 0.3
## 倒地后没有治疗则判定死亡的窗口（秒）
DYING_SECONDS = 5.0
## 死亡后旁观时长（秒）；到期清空灵力与背包并原地复活
SPECTATOR_SECONDS = 1800.0
## 击杀灵力转移比例：受害者等级 > 击杀者 75%；同级 50%；更低 25%
KILL_SPIRIT_RATIO_HIGHER = 0.75
KILL_SPIRIT_RATIO_EQUAL = 0.5
KILL_SPIRIT_RATIO_LOWER = 0.25

# ---------------------------------------------------------------------------
# 坐标基准（protocol.md §11.1）
# ---------------------------------------------------------------------------
## 头部 → 脚底的偏移（米），数值本身在 hitbox 里冻结
HEAD_OFFSET_Y = hitbox.HEAD_OFFSET_Y
## 内存 / 库里的 pos_* 一律是头部中心
COORD_BASE_HEAD = "head"
## 旧客户端（v0.1.x）按脚底上报；库里的旧行也标记成 feet，加载时一次性迁移
COORD_BASE_FEET = "feet"
COORD_BASES = (COORD_BASE_HEAD, COORD_BASE_FEET)

# ---------------------------------------------------------------------------
# 红名（protocol.md §12）
# ---------------------------------------------------------------------------
## 红名的触发条件与豁免规则都在 karma 模块里（human × 同势力 human 必定触发）


# ---------------------------------------------------------------------------
# 归一工具
# ---------------------------------------------------------------------------

def _norm_yaw(yaw) -> float:
    """把任意角度归一到 [0, 360)；非法值回 0。"""
    try:
        value = float(yaw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return value % 360.0


def _coord(value) -> float | None:
    """归一坐标；非法/越界返回 None。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or abs(out) > COORD_LIMIT:
        return None
    return out


def _num(value, default: float = 0.0) -> float:
    """宽容取数：非数字 / NaN / inf 都回默认值。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def _apply_stat(row: dict, key: str, value) -> None:
    """写一项当前值（hp / stamina / mana），自动夹到 [0, 上限]。

    ``None`` / 空串 = “这次没上报”，保持原值不动（客户端不必每次都带三围）。
    """
    if value is None or value == "":
        return
    raw = _coord(value)
    if raw is None:
        return
    cap = _num(row.get(f"{key}_max"), 100.0)
    row[key] = max(0.0, min(cap, raw))


def _bool(value, default: bool = False) -> bool:
    """宽容取布尔：None / 空串回默认值，字符串按 1/true/yes/on/t 认。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "t")


def coord_base_of(row: dict) -> str:
    """取一行数据记录的坐标基准；缺省按 head 处理（服务端自己造的行一定是 head）。"""
    base = str(row.get("coord_base") or COORD_BASE_HEAD).strip().lower()
    return base if base in COORD_BASES else COORD_BASE_HEAD


def normalize_coord_base(coord_base) -> str:
    """把「会话/客户端声明的基准」归一成 head / feet（非法值按 head）。"""
    base = str(coord_base or COORD_BASE_HEAD).strip().lower()
    return base if base in COORD_BASES else COORD_BASE_HEAD


def to_head_y(value, coord_base=COORD_BASE_HEAD) -> float | None:
    """客户端上报的 Y → 头部基准。非法坐标返回 None（调用方回 BAD_PAYLOAD）。"""
    raw = _coord(value)
    if raw is None:
        return None
    if normalize_coord_base(coord_base) == COORD_BASE_FEET:
        return raw + HEAD_OFFSET_Y
    return raw


def from_head_y(value, coord_base=COORD_BASE_HEAD) -> float:
    """头部基准的 Y → 要下发给该连接的基准（旧客户端减 1.65）。"""
    out = _num(value)
    if normalize_coord_base(coord_base) == COORD_BASE_FEET:
        return out - HEAD_OFFSET_Y
    return out


# ---------------------------------------------------------------------------
# 灵力 / 等级 / 生死流转
# ---------------------------------------------------------------------------

def level_cost(level: int) -> int:
    """从 ``level`` 升到 ``level + 1`` 需要消耗的灵力。

    需求：1 级需要 1 灵力，2 级在一级基础上 +2，3 级再 +3（累加 1/3/6/10…）。
    这里返回“当前等级 -> 下一级”的增量，即 level + 1。
    """
    return int(level) + 1


def total_spirit_for_level(level: int) -> int:
    """达到 ``level`` 级累计需要获得的灵力（1 / 3 / 6 / 10 …）。"""
    level = max(0, int(level))
    return level * (level + 1) // 2


def _apply_levels(row: dict) -> bool:
    """按“升级消耗灵力”自动升级；返回是否发生了升级。"""
    changed = False
    while True:
        level = int(row.get("level", 1) or 1)
        if level >= MAX_LEVEL:
            break
        cost = level_cost(level)
        if _num(row.get("spirit")) < cost:
            break
        row["spirit"] = _num(row.get("spirit")) - cost
        row["level"] = level + 1
        changed = True
    return changed


def _tick_spirit(row: dict, now: float) -> bool:
    """懒结算灵力：每小时 +1（离线时间同样计入）。调用方必须持锁。"""
    at = _num(row.get("spirit_at"))
    if at <= 0 or at > now:
        # 未结算过 / 时间被倒拨：以当前时刻作为起点，不发放异常收益
        row["spirit_at"] = now
        return False
    hours = int((now - at) // SPIRIT_HOUR_SECONDS)
    if hours <= 0:
        return False
    row["spirit_at"] = at + hours * SPIRIT_HOUR_SECONDS
    row["spirit"] = _num(row.get("spirit")) + hours * SPIRIT_PER_HOUR
    _apply_levels(row)
    return True


def _is_spectating(row: dict, now: float) -> bool:
    until = row.get("spectator_until")
    return until is not None and _num(until) > now


def _is_dying(row: dict) -> bool:
    return row.get("death_at") is not None


def _kill_ratio(killer_level: int, victim_level: int) -> float:
    """击杀灵力转移比例：击杀高等级者 75%，同级 50%，低等级者 25%。"""
    if victim_level > killer_level:
        return KILL_SPIRIT_RATIO_HIGHER
    if victim_level == killer_level:
        return KILL_SPIRIT_RATIO_EQUAL
    return KILL_SPIRIT_RATIO_LOWER


def _resolve_timers(row: dict, now: float) -> list[str]:
    """推进一名玩家的濒死 / 旁观计时器；返回发生的事件名列表。

    调用方必须持锁。可能的返回值：
      ``died``      —— 5 秒窗口到期，确认死亡并进入 30 分钟旁观
      ``respawned`` —— 旁观结束，灵力 / 背包清空、满血原地复活
    """
    events: list[str] = []

    death_at = row.get("death_at")
    if death_at is not None and now - _num(death_at) >= DYING_SECONDS:
        row["death_at"] = None
        row["deaths"] = int(row.get("deaths") or 0) + 1
        row["spectator_until"] = now + SPECTATOR_SECONDS
        row["hp"] = 0.0
        row["state"] = "dead"
        # §12.1 的时点就在这里：濒死窗口结束、判定**真死亡**的那一刻，
        # 击杀者变红、受害者清红名（复活成功的路径不会走到这里，两边都不动）。
        killer_id = str(row.get("last_attacker") or "").strip()
        _transfer_kill_spirit(row, now)
        _settle_karma_on_death(row, killer_id, now)
        events.append("died")

    until = row.get("spectator_until")
    if until is not None and now >= _num(until):
        row["spectator_until"] = None
        row["death_at"] = None
        row["spirit"] = 0.0
        row["inventory"] = {}
        row["hp"] = _num(row.get("hp_max"), 100.0)
        row["state"] = "idle"
        row["last_attacker"] = ""
        row["rev"] = int(row.get("rev", 0)) + 1
        row["updated_at"] = now
        events.append("respawned")

    return events


def _settle_karma_on_death(victim: dict, killer_id: str, now: float) -> None:
    """真死亡时的红名结算（protocol.md §12.1 / §12.4）。调用方必须持锁。

    * 受害者：**清红名**（死亡是唯一的解除途径，见 §12.4）；
    * 击杀者：按 :func:`karma.should_become_red` 判定，命中则变红并记 ``red_at``。

    击杀者此刻可能已经下线（行不在热缓存里）——那就只记一条 WARNING：
    没有热缓存行可以承载标记，等它下次上线时由库里的旧值决定。
    """
    victim_id = str(victim.get("user_id") or "")
    if victim.get("red_name"):
        log.info("红名解除：%s 确认死亡（红名玩家被杀）", victim_id)
    victim["red_name"] = False
    victim["red_at"] = None
    victim["rev"] = int(victim.get("rev", 0)) + 1
    victim["updated_at"] = now

    killer_id = str(killer_id or "").strip()
    if not killer_id or killer_id == victim_id:
        return
    killer = players.get(killer_id)
    if killer is None:
        log.warning("红名判定跳过：击杀者 %s 不在热缓存（已下线）", killer_id)
        return
    if not karma.should_become_red(killer, victim):
        return
    if not killer.get("red_name"):
        log.info("红名：%s 击杀同势力人类 %s → 进入红名", killer_id, victim_id)
    killer["red_name"] = True
    killer["red_at"] = now
    killer["rev"] = int(killer.get("rev", 0)) + 1
    killer["updated_at"] = now


def _transfer_kill_spirit(victim: dict, now: float) -> None:
    """死亡结算：把受害者当前灵力的一部分转给最后攻击者。调用方必须持锁。"""
    killer_id = str(victim.get("last_attacker") or "").strip()
    victim["last_attacker"] = ""
    if not killer_id or killer_id == victim.get("user_id"):
        return
    killer = players.get(killer_id)
    if killer is None:
        return

    victim_level = int(victim.get("level", 1) or 1)
    killer_level = int(killer.get("level", 1) or 1)
    ratio = _kill_ratio(killer_level, victim_level)
    killer["kills"] = int(killer.get("kills") or 0) + 1
    killer["rev"] = int(killer.get("rev", 0)) + 1
    killer["updated_at"] = now

    amount = _num(victim.get("spirit")) * ratio
    if amount <= 0:
        return

    victim["spirit"] = max(0.0, _num(victim.get("spirit")) - amount)
    killer["spirit"] = _num(killer.get("spirit")) + amount
    _apply_levels(killer)
    victim["rev"] = int(victim.get("rev", 0)) + 1
    victim["updated_at"] = now


def _distance(a: dict, b: dict) -> float:
    dx = _num(a.get("pos_x")) - _num(b.get("pos_x"))
    dy = _num(a.get("pos_y")) - _num(b.get("pos_y"))
    dz = _num(a.get("pos_z")) - _num(b.get("pos_z"))
    return math.sqrt(dx * dx + dy * dy + dz * dz)


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

@contextmanager
def snapshot() -> Iterator[tuple[list[dict], list[dict]]]:
    """只读快照：持锁期间拷贝，出来就释放锁。"""
    yield _snapshot_lists()


def _snapshot_lists() -> tuple[list[dict], list[dict]]:
    """拷贝一份当前内存数据（持锁极短）。"""
    with _lock:
        items = [dict(it) for it in world.values()]
        ps = [dict(p) for p in players.values()]
    return items, ps


@contextmanager
def mutate():
    """写操作上下文：增删改 ``world`` / ``players`` 必须持有。"""
    with _lock:
        yield


def revisions() -> tuple[int, int]:
    """(world_rev, players_rev)——推送用的全局序号。"""
    with _lock:
        return _world_rev, _players_rev


def _default_world_row() -> dict:
    return {
        "id": DEFAULT_WORLD,
        "name": "众生之门",
        "seed": 0,
        "rev": 0,
    }


def _touch_world_rev() -> int:
    """推世界全局修订号，同时把世界行的 rev 抬到同一高度。"""
    global _world_rev
    with _lock:
        _world_rev += 1
        meta = world_meta.setdefault(DEFAULT_WORLD, _default_world_row())
        meta["rev"] = _world_rev
        meta["updated_at"] = time.time()
        return _world_rev


def bump_world_rev() -> int:
    return _touch_world_rev()


def bump_players_rev() -> int:
    global _players_rev
    with _lock:
        _players_rev += 1
        return _players_rev


# ---------------------------------------------------------------------------
# 世界物品
# ---------------------------------------------------------------------------

def upsert_item(item: dict) -> dict | None:
    """新增/更新一个世界物品；字段非法返回 None。每次修改 rev + 1。"""
    item_id = str(item.get("id", "")).strip()
    if not item_id or "|" in item_id:
        return None
    pos = [_coord(item.get(k, 0.0)) for k in ("pos_x", "pos_y", "pos_z")]
    if any(v is None for v in pos):
        return None

    with mutate():
        prev = world.get(item_id) or {}
        row = {
            "id": item_id,
            "world_id": str(item.get("world_id") or prev.get("world_id") or DEFAULT_WORLD),
            "kind": str(item.get("kind", "generic")).strip() or "generic",
            "pos_x": pos[0],
            "pos_y": pos[1],
            "pos_z": pos[2],
            "yaw": _norm_yaw(item.get("yaw", 0.0)),
            "state": str(item.get("state", "idle")).strip() or "idle",
            "rev": int(prev.get("rev", 0)) + 1,
            "updated_at": time.time(),
        }
        world[item_id] = row
        result = dict(row)
    _touch_world_rev()
    return result


def world_payload() -> list[dict]:
    with _lock:
        return [
            {
                "id": it["id"],
                "kind": it.get("kind", "generic"),
                "pos_x": it.get("pos_x", 0.0),
                "pos_y": it.get("pos_y", 0.0),
                "pos_z": it.get("pos_z", 0.0),
                "yaw": it.get("yaw", 0.0),
                "state": it.get("state", "idle"),
            }
            for it in world.values()
        ]


def world_meta_payload() -> dict:
    """世界元信息（给 ``world`` 动作下发名称 / 种子）。"""
    with _lock:
        row = dict(world_meta.get(DEFAULT_WORLD) or _default_world_row())
    return {
        "id": row.get("id", DEFAULT_WORLD),
        "name": row.get("name", ""),
        "seed": int(row.get("seed", 0) or 0),
        "rev": int(row.get("rev", 0) or 0),
    }


# ---------------------------------------------------------------------------
# 玩家
# ---------------------------------------------------------------------------

def _new_player_row(
    user_id: str,
    user_name: str = "",
    device_id_hex: str = "",
    race: str = "",
    player_name: str = "",
    player_id: str = "",
    file_hash: str = "",
) -> dict:
    row = {
        "user_id": user_id,
        "user_name": user_name or "",
        "player_id": player_id or "",        # 客户端生成的 UUID
        "file_hash": file_hash or "",       # 登录校验文件哈希
        "player_name": player_name or user_name or "",
        "race": race or DEFAULT_RACE,
        "device_id": device_id_hex or "",
        "pos_x": 0.0,
        "pos_y": 0.0,
        "pos_z": 0.0,
        "yaw": 0.0,
        # 瞄准俯仰角（§4 第 9 段）：0 = 水平，正 = 抬头。只在内存里，不落库。
        "pitch": 0.0,
        "state": "idle",
        "online": True,
        # 从当前全局修订号起算：下线后销毁了缓存行、重连重建时，
        # rev 从 0 起会被库里上一轮的旧 rev 拒掉（反而丢数据）。
        "rev": _players_rev,
        "updated_at": time.time(),
    }
    row.update(DEFAULT_STATS)
    row.update({
        "spirit": 0.0,
        "level": 1,
        "spirit_at": time.time(),
        "death_at": None,
        "spectator_until": None,
        "last_attacker": "",
        "inventory": {},
        "kills": 0,
        "deaths": 0,
        "red_name": False,        # 红名（§12）
        "red_at": None,
        "coord_base": COORD_BASE_HEAD,   # 内存行一律头部基准
    })
    return row


def set_player_state(
    user_id: str,
    user_name: str,
    device_id_hex: str,
    pos_x,
    pos_y,
    pos_z,
    yaw,
    hp=None,
    state: str = "idle",
    stamina=None,
    mana=None,
    pitch=0.0,
) -> dict | None:
    """写入一名玩家的最新状态（坐标非法返回 None）。每次写入 rev + 1。

    ``pitch`` 是瞄准俯仰角（§4 第 9 段，度，0 = 水平、正 = 抬头）。它**只在内存里**，
    不落库、不进 ``peers``，只用于攻击方向与朝向锥（§11.3）。
    非法值 / 缺省由 :func:`hitbox.norm_pitch` 归成 0。
    """
    if not user_id:
        return None
    pos = [_coord(pos_x), _coord(pos_y), _coord(pos_z)]
    if any(v is None for v in pos):
        return None

    with mutate():
        row = players.get(user_id)
        if row is None:
            row = _new_player_row(user_id, user_name, device_id_hex)
            players[user_id] = row
        now = time.time()
        _tick_spirit(row, now)
        _resolve_timers(row, now)
        if user_name:
            row["user_name"] = user_name
        if device_id_hex:
            row["device_id"] = device_id_hex
        # 濒死 / 旁观期间坐标与状态由服务端冻结：客户端上报的飞行坐标不落库，
        # 死亡点就是复活点（旁观结束后原地满血复活）。
        if not _is_dying(row) and not _is_spectating(row, now):
            row["pos_x"], row["pos_y"], row["pos_z"] = pos[0], pos[1], pos[2]
            row["yaw"] = _norm_yaw(yaw)
            row["pitch"] = hitbox.norm_pitch(pitch)
            row["state"] = state if state in PLAYER_STATES else "idle"
            _apply_stat(row, "stamina", stamina)
            _apply_stat(row, "mana", mana)
        row["online"] = True
        row["rev"] = int(row.get("rev", 0)) + 1
        row["updated_at"] = now
        result = dict(row)
    bump_players_rev()
    return result


def remove_player(user_id: str) -> bool:
    """下线一个玩家（直接从缓存移除；归档数据仍在库里）。"""
    with mutate():
        existed = players.pop(user_id, None) is not None
    if existed:
        bump_players_rev()
    return existed


def _normalize_player_row(row: dict) -> dict | None:
    """把库里读回的 player 行归一成内存形状。

    启动预载与「销毁后重连重载」共用这一份规则，两边行为必须一致：
    ``online`` 一律 False（拿到这条数据的人还没上线，由 enter_world 置 True），
    ``rev`` 原样继承（否则写库会被 rev 门禁拒掉）。

    **坐标基准迁移**（protocol.md §11.1）：库里 ``coord_base='feet'`` 的旧行
    在这里一次性 +1.65m 改写为 ``'head'``；缺省 / 非法值按 head 处理
    （服务端自己造的行一定是 head，只有 ALTER 补出来的旧行才是 feet）。
    """
    user_id = str(row.get("user_id") or "").strip()
    if not user_id:
        return None
    base = coord_base_of(row)
    pos_y = _num(row.get("pos_y"))
    if base == COORD_BASE_FEET:
        pos_y += HEAD_OFFSET_Y
    return {
        "user_id": user_id,
        "user_name": str(row.get("user_name") or ""),
        "player_id": str(row.get("player_id") or ""),
        "file_hash": str(row.get("file_hash") or ""),
        "player_name": str(row.get("player_name") or row.get("user_name") or ""),
        "race": str(row.get("race") or DEFAULT_RACE),
        "device_id": "",          # 设备不在 player 表里
        "pos_x": _num(row.get("pos_x")),
        "pos_y": pos_y,
        "pos_z": _num(row.get("pos_z")),
        "yaw": _norm_yaw(row.get("yaw")),
        # 库里的 player 表没有 pitch 列：重载时一律回水平（内存态，不落库）。
        "pitch": 0.0,
        "coord_base": COORD_BASE_HEAD,
        "state": str(row.get("state") or "idle"),
        "online": False,           # 重启后/重载时没人在线
        "hp_max": _num(row.get("hp_max"), 100.0),
        "hp": _num(row.get("hp"), 100.0),
        "stamina_max": _num(row.get("stamina_max"), 100.0),
        "stamina": _num(row.get("stamina"), 100.0),
        "mana_max": _num(row.get("mana_max"), 100.0),
        "mana": _num(row.get("mana"), 100.0),
        "spirit": _num(row.get("spirit")),
        "level": int(row.get("level") or 1),
        "spirit_at": _num(row.get("spirit_at")),
        "death_at": _num(row.get("death_at")) if row.get("death_at") is not None else None,
        "spectator_until": (
            _num(row.get("spectator_until")) if row.get("spectator_until") is not None else None
        ),
        "last_attacker": str(row.get("last_attacker") or ""),
        "inventory": row.get("inventory") if isinstance(row.get("inventory"), dict) else {},
        "kills": int(row.get("kills") or 0),
        "deaths": int(row.get("deaths") or 0),
        "red_name": _bool(row.get("red_name")),
        "red_at": _num(row.get("red_at")) if row.get("red_at") is not None else None,
        "rev": int(row.get("rev") or 0),
        "updated_at": _num(row.get("updated_at"), time.time()),
    }


def enter_world(
    user_id: str,
    user_name: str = "",
    device_id_hex: str = "",
    race: str = "",
    player_name: str = "",
    player_id: str = "",
    file_hash: str = "",
    store=None,
) -> dict | None:
    """玩家进入世界（注册/登录成功后调）。

    缓存里没有该玩家时，先从库读一次（下线时被 ``destroy_player`` 销毁过），
    读到就原位重进，读不到才新建行 —— 这就是 flows.md 流程 4 里的「player 重载」。
    坐标与三围都从库里那条最新记录恢复，不会回到出生点。

    ``player_id`` / ``file_hash`` 只在当前为空时填，不能把库里已有的值刷掉。
    """
    if not user_id:
        return None

    # DB 读取必须在锁外：否则一个慢查询会卡住 10Hz 推送线程
    backend = store or _store
    stored_row: dict | None = None
    if get_player(user_id) is None and backend is not None:
        try:
            stored_row = backend.load_player(user_id)
        except Exception as exc:  # noqa: BLE001 - 读不到就当作新玩家
            log.warning("重载玩家 %s 失败：%s", user_id, exc)
            stored_row = None

    with mutate():
        row = players.get(user_id)
        if row is None:
            if stored_row is not None:
                row = _normalize_player_row(stored_row) or _new_player_row(user_id)
            else:
                row = _new_player_row(
                    user_id, user_name, device_id_hex, race, player_name,
                    player_id, file_hash,
                )
            players[user_id] = row
        if user_name:
            row["user_name"] = user_name
        if player_name:
            row["player_name"] = player_name or row.get("player_name", "")
        if race:
            row["race"] = race
        if device_id_hex:
            row["device_id"] = device_id_hex
        if player_id and not row.get("player_id"):
            row["player_id"] = player_id
        if file_hash and not row.get("file_hash"):
            row["file_hash"] = file_hash
        # 离线期间攒的灵力在这里补结算；濒死 / 旁观计时器也一并推进
        now = time.time()
        _tick_spirit(row, now)
        _resolve_timers(row, now)
        row["online"] = True
        row["rev"] = int(row.get("rev", 0)) + 1
        row["updated_at"] = time.time()
        result = dict(row)
    bump_players_rev()
    return result


def mark_offline(user_id: str) -> None:
    """标为离线：**只改 online，不删 key**（彻底移除见 ``destroy_player``）。

    保留缓存行是有意的：宽限期内重连、或同账号另一条连接上线时，
    坐标与三围不应该丢。
    """
    with mutate():
        row = players.get(user_id)
        if row is None:
            return
        row["online"] = False
        row["state"] = "idle"
        row["rev"] = int(row.get("rev", 0)) + 1
        row["updated_at"] = time.time()
    bump_players_rev()


def destroy_player(user_id: str) -> bool:
    """销毁一名玩家的热缓存行（正常下线 / 宽限期超时都走这里）。

    与 ``mark_offline`` 的区别：这里是**彻底移除**，同时清掉
    “已落库 rev” 记录，下次该玩家重连时从库中重新加载。
    调用前必须已经把数据 flush 进库（``save_player``）。
    """
    global _players_rev
    with _lock:
        existed = players.pop(user_id, None) is not None
        _persisted_player_revs.pop(str(user_id), None)
        if existed:
            _players_rev += 1
    return existed


def players_payload(
    include_offline: bool = False,
    coord_base: str = COORD_BASE_HEAD,
) -> list[dict]:
    """在线玩家快照。旁观者（无实体）不入列表，濒死者仍在列表里（state=dead）。

    ``coord_base`` 是**本次下发的坐标基准**（§11.4）：给旧客户端（v0.1.x）发时
    统一减 1.65m 转成脚底坐标，并且每行的 ``coord_base`` 字段如实写成 ``'feet'``；
    给新客户端发时原样（头部基准）。
    """
    now = time.time()
    base = normalize_coord_base(coord_base)
    with _lock:
        return [
            {
                "user_id": p["user_id"],
                "user_name": p.get("user_name", ""),
                "player_name": p.get("player_name", "") or p.get("user_name", ""),
                "race": p.get("race", DEFAULT_RACE),
                "level": int(p.get("level", 1) or 1),
                "pos_x": p.get("pos_x", 0.0),
                "pos_y": from_head_y(p.get("pos_y"), base),
                "pos_z": p.get("pos_z", 0.0),
                "yaw": p.get("yaw", 0.0),
                "coord_base": base,
                "state": p.get("state", "idle"),
                "hp_max": p.get("hp_max", 100.0),
                "hp": p.get("hp", 100.0),
                "stamina_max": p.get("stamina_max", 100.0),
                "stamina": p.get("stamina", 100.0),
                "mana_max": p.get("mana_max", 100.0),
                "mana": p.get("mana", 100.0),
                "dying": _is_dying(p),
                "red": bool(p.get("red_name")),
            }
            for p in players.values()
            if (include_offline or p.get("online", False)) and not _is_spectating(p, now)
        ]


def self_payload(
    user_id: str,
    coord_base: str = COORD_BASE_HEAD,
) -> dict | None:
    """自己的权威状态（state 动作的返回值，客户端据此刷新 HP / 灵力 / 等级 / 生死 / 红名）。

    ``coord_base`` 语义同 :func:`players_payload`：``pos_*`` 按该基准下发。
    """
    now = time.time()
    base = normalize_coord_base(coord_base)
    with _lock:
        row = players.get(user_id)
        if row is None:
            return None
        _tick_spirit(row, now)
        _resolve_timers(row, now)
        dying_remaining = 0.0
        if _is_dying(row):
            dying_remaining = max(0.0, DYING_SECONDS - (now - _num(row.get("death_at"))))
        spectator_remaining = 0.0
        if _is_spectating(row, now):
            spectator_remaining = max(0.0, _num(row.get("spectator_until")) - now)
        return {
            "user_id": row["user_id"],
            "player_name": row.get("player_name", "") or row.get("user_name", ""),
            "hp": _num(row.get("hp")),
            "hp_max": _num(row.get("hp_max"), 100.0),
            "spirit": _num(row.get("spirit")),
            "level": int(row.get("level", 1) or 1),
            "kills": int(row.get("kills") or 0),
            "deaths": int(row.get("deaths") or 0),
            "dying": _is_dying(row),
            "dying_remaining": dying_remaining,
            "spectating": _is_spectating(row, now),
            "spectator_remaining": spectator_remaining,
            "pos_x": _num(row.get("pos_x")),
            "pos_y": from_head_y(row.get("pos_y"), base),
            "pos_z": _num(row.get("pos_z")),
            "yaw": _norm_yaw(row.get("yaw")),
            "coord_base": base,
            "red": bool(row.get("red_name")),
        }


def attack(attacker_id: str, target_id: str, now: float | None = None) -> dict:
    """近战攻击：射程 / 朝向 / 冷却 / 目标状态 / 部位命中全部在服务端做。

    命中判定见 ``hitbox``（protocol.md §11.2 / §11.3）：

    * 原点 = 攻击者**头部**；方向 = 攻击者 ``yaw`` + ``pitch`` 推出的 **3D 前向**
      （``pitch`` = 最近一次 ``state`` 的第 9 段，缺省 0 即水平）；
    * 60° 朝向锥用同一条 3D 前向校验，不过 → ``NOT_FACING``；射程内没打到任何盒子 → ``MISS``；
    * 命中部位伤害 = ``ATTACK_DAMAGE(25) × 倍率``（头 50 / 颈 37.5 / 躯干 25 …）。

    返回 ``{"ok": True, ...}`` 或 ``{"ok": False, "error": ..., "code": ...}``。
    HP 归零进入濒死（5 秒内可被 revive 救回）；**红名不在这里结算**，
    只在濒死窗口结束判真死亡时结算（§12.1，见 ``_settle_karma_on_death``）。
    """
    now = time.time() if now is None else now
    if not target_id:
        return {"ok": False, "error": "缺少目标"}
    if target_id == attacker_id:
        return {"ok": False, "error": "不能攻击自己"}

    with mutate():
        attacker = players.get(attacker_id)
        target = players.get(target_id)
        if attacker is None or not attacker.get("online"):
            return {"ok": False, "error": "攻击者不在线"}
        if target is None or not target.get("online"):
            return {"ok": False, "error": "目标不在线"}

        _tick_spirit(attacker, now)
        _resolve_timers(attacker, now)
        _tick_spirit(target, now)
        _resolve_timers(target, now)

        if _is_spectating(attacker, now) or _is_dying(attacker):
            return {"ok": False, "error": "濒死或旁观时无法攻击"}
        if _is_spectating(target, now):
            return {"ok": False, "error": "目标处于旁观状态"}

        last = _num(attacker.get("attack_at"))
        if now - last < ATTACK_COOLDOWN:
            return {
                "ok": False, "error": "攻击冷却中",
                "cooldown": ATTACK_COOLDOWN - (now - last),
            }

        dist = _distance(attacker, target)
        if dist > ATTACK_RANGE:
            return {
                "ok": False, "error": "目标超出射程",
                "code": "OUT_OF_RANGE", "distance": round(dist, 2),
            }

        attacker_yaw = _norm_yaw(attacker.get("yaw"))
        # 瞄准俯仰角（§4 第 9 段）：0 = 水平、正 = 抬头；缺省 / 非法一律 0（旧行为不变）
        attacker_pitch = hitbox.norm_pitch(attacker.get("pitch"))
        attacker_head = (
            _num(attacker.get("pos_x")), _num(attacker.get("pos_y")),
            _num(attacker.get("pos_z")),
        )
        target_head = (
            _num(target.get("pos_x")), _num(target.get("pos_y")),
            _num(target.get("pos_z")),
        )
        # 朝向锥：目标必须在大致前方（§11.3 第 2 条，用与射线相同的 3D 前向）
        if not hitbox.facing_ok(attacker_yaw, attacker_head, target_head, attacker_pitch=attacker_pitch):
            return {
                "ok": False, "error": "目标不在攻击朝向内",
                "code": "NOT_FACING", "distance": round(dist, 2),
            }

        # 到这里就算真挥了一刀：MISS 也吃冷却
        attacker["attack_at"] = now
        attacker["rev"] = int(attacker.get("rev", 0)) + 1
        attacker["updated_at"] = now
        if _is_dying(target):
            return {"ok": False, "error": "目标已濒死"}

        hit = hitbox.resolve_hit(
            attacker_head, attacker_yaw, target_head,
            _norm_yaw(target.get("yaw")), ATTACK_RANGE,
            attacker_pitch=attacker_pitch,
        )
        if hit is None:
            bump_players_rev()      # attack_at / rev 已变，通知拉取方
            return {
                "ok": False, "error": "没有打中身体",
                "code": "MISS", "distance": round(dist, 2),
            }
        part, multiplier, hit_t = hit
        damage = hitbox.damage_for(multiplier, ATTACK_DAMAGE)

        target["hp"] = max(0.0, _num(target.get("hp"), 100.0) - damage)
        target["last_attacker"] = attacker_id
        target["rev"] = int(target.get("rev", 0)) + 1
        target["updated_at"] = now
        if target["hp"] <= 0.0:
            target["hp"] = 0.0
            target["death_at"] = now
            target["state"] = "dead"

        result = {
            "ok": True,
            "target": target_id,
            "part": part,
            "multiplier": multiplier,
            "damage": damage,
            "hp": target["hp"],
            "hp_max": _num(target.get("hp_max"), 100.0),
            "dying": _is_dying(target),
            "dying_remaining": DYING_SECONDS if _is_dying(target) else 0.0,
            "distance": round(dist, 2),
            "hit_t": round(hit_t, 3),
            "killer_red": bool(attacker.get("red_name")),
            "victim_red": bool(target.get("red_name")),
            "attack_at": now,
        }
    bump_players_rev()
    return result


def revive(healer_id: str, target_id: str, now: float | None = None) -> dict:
    """复活类治疗：把 5 秒濒死窗口里的玩家拉起来。"""
    now = time.time() if now is None else now
    if not target_id:
        return {"ok": False, "error": "缺少目标"}
    if target_id == healer_id:
        return {"ok": False, "error": "不能治疗自己"}

    with mutate():
        healer = players.get(healer_id)
        target = players.get(target_id)
        if healer is None or not healer.get("online"):
            return {"ok": False, "error": "治疗者不在线"}
        if target is None or not target.get("online"):
            return {"ok": False, "error": "目标不在线"}

        _resolve_timers(healer, now)
        _resolve_timers(target, now)
        if _is_spectating(healer, now) or _is_dying(healer):
            return {"ok": False, "error": "濒死或旁观时无法治疗"}
        if not _is_dying(target):
            return {"ok": False, "error": "目标没有处于濒死"}

        dist = _distance(healer, target)
        if dist > REVIVE_RANGE:
            return {"ok": False, "error": "目标超出治疗距离", "distance": round(dist, 2)}

        target["death_at"] = None
        target["last_attacker"] = ""
        target["hp"] = max(1.0, _num(target.get("hp_max"), 100.0) * REVIVE_HP_RATIO)
        target["state"] = "idle"
        target["rev"] = int(target.get("rev", 0)) + 1
        target["updated_at"] = now
        healer["rev"] = int(healer.get("rev", 0)) + 1
        healer["updated_at"] = now

        result = {
            "ok": True,
            "target": target_id,
            "hp": target["hp"],
            "hp_max": _num(target.get("hp_max"), 100.0),
            "distance": round(dist, 2),
        }
    bump_players_rev()
    return result


def settle_all(now: float | None = None) -> int:
    """推进所有缓存玩家的计时器（灵力小时结算 / 濒死 / 旁观到期）。

    返回发生状态变化的玩家数。后台线程每秒调一次；离线玩家也覆盖
    （他们仍在缓存里时，灵力与计时器要照常走）。
    """
    now = time.time() if now is None else now
    changed: dict[str, int] = {}
    with mutate():
        for uid, row in players.items():
            before = (row.get("spirit"), row.get("level"), row.get("death_at"),
                      row.get("spectator_until"), row.get("hp"), row.get("red_name"))
            spirit_changed = _tick_spirit(row, now)
            _resolve_timers(row, now)
            after = (row.get("spirit"), row.get("level"), row.get("death_at"),
                     row.get("spectator_until"), row.get("hp"), row.get("red_name"))
            if spirit_changed or before != after:
                row["rev"] = int(row.get("rev", 0)) + 1
                row["updated_at"] = now
                changed[uid] = int(row.get("rev", 0))
    if changed:
        bump_players_rev()
    return len(changed)


def _timers_loop(interval: float, stop_event: threading.Event) -> None:
    while not stop_event.wait(max(0.05, interval)):
        try:
            settle_all()
        except Exception:  # pragma: no cover - 后台线程不允许因单次异常退出
            log.exception("结算线程异常")


_timers_stop: threading.Event | None = None
_timers_thread: threading.Thread | None = None


def start_timers(interval: float = 1.0) -> None:
    """启动后台结算线程（灵力 / 濒死 / 旁观）。重复调用是安全的。"""
    global _timers_stop, _timers_thread
    if _timers_thread is not None and _timers_thread.is_alive():
        return
    _timers_stop = threading.Event()
    _timers_thread = threading.Thread(
        target=_timers_loop, args=(interval, _timers_stop),
        name="game-timers", daemon=True,
    )
    _timers_thread.start()
    log.info("计时线程已启动：灵力每小时 +%s，濒死 %.0fs，旁观 %.0fs",
             SPIRIT_PER_HOUR, DYING_SECONDS, SPECTATOR_SECONDS)


def stop_timers() -> None:
    global _timers_stop, _timers_thread
    if _timers_stop is not None:
        _timers_stop.set()
    if _timers_thread is not None:
        _timers_thread.join(timeout=2.0)
    _timers_stop = None
    _timers_thread = None


def online_count() -> int:
    with _lock:
        return sum(1 for p in players.values() if p.get("online", False))


def get_player(user_id: str) -> dict | None:
    with _lock:
        row = players.get(user_id)
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------

_dump_path = Path(__file__).resolve().parent / "tmp" / "databaseErrordb.json"
_store = None
_timer: threading.Timer | None = None
_interval = 60

## “已经落过库”的修订号：只把比它新的行交给数据库。
## 没有它的话，什么都没变也会每轮重写一遍全表（库在远端，代价很大），
## 而且库里 rev 相等的行会被当成“落后”而报一堆假警告。
_persisted_world_rev = 0
_persisted_item_revs: dict[str, int] = {}
_persisted_player_revs: dict[str, int] = {}


def save(store=None) -> bool:
    """把**有变化**的行 upsert 到数据库；落后数据会被库拒掉（只记日志，不算失败）。

    判定“有变化”用行级 ``rev``：内存 rev 比“已经落过库的 rev”大才算脏。
    这样 10Hz 上报的玩家每轮只写自己一行，而挂机的玩家一行也不写。
    """
    backend = store or _store
    if backend is None:
        log.warning("落库跳过：存储层未初始化")
        return False

    global _persisted_world_rev, _persisted_item_revs, _persisted_player_revs

    with _lock:
        all_items, all_players = _snapshot_lists_unlocked()
        meta = dict(world_meta.get(DEFAULT_WORLD) or _default_world_row())
        meta["rev"] = max(int(meta.get("rev", 0) or 0), _world_rev)
        world_row = meta if int(meta.get("rev", 0) or 0) > _persisted_world_rev else None
        items = [it for it in all_items
                 if int(it.get("rev", 0) or 0) > _persisted_item_revs.get(it["id"], -1)]
        ps = [p for p in all_players
              if p.get("user_id")
              and int(p.get("rev", 0) or 0) > _persisted_player_revs.get(p["user_id"], -1)]

    try:
        world_result = backend.save_world(world_row, items)
        player_result = backend.save_players(ps)
    except Exception as exc:  # noqa: BLE001 - 落库失败不能拖垮整个服务
        log.error("落库失败，写本地快照：%s", exc)
        _dump_snapshot(all_items, all_players)
        return False

    stale = list(world_result.get("stale") or []) + list(player_result.get("stale") or [])
    written = int(world_result.get("written") or 0) + int(player_result.get("written") or 0)
    stale_set = set(str(x) for x in stale)

    # 写入成功的行记下“已落库 rev”；被拒的行**不记**，
    # 但也不再重试（库里那个值更权威，下一轮仍然会被拒）。
    with _lock:
        if world_row and str(world_row["id"]) not in stale_set:
            _persisted_world_rev = int(world_row.get("rev", 0) or 0)
        for it in items:
            if str(it["id"]) not in stale_set:
                _persisted_item_revs[str(it["id"])] = int(it.get("rev", 0) or 0)
        for p in ps:
            if str(p["user_id"]) not in stale_set:
                _persisted_player_revs[str(p["user_id"])] = int(p.get("rev", 0) or 0)

    if stale:
        log.warning(
            "有 %d 条数据落后于数据库，已被拒绝写入（以库为准）：%s",
            len(stale), ", ".join(str(x) for x in stale[:8]),
        )
    if written or stale:
        log.info("落库：写入 %d 行，落后被拒 %d 行", written, len(stale))
    else:
        log.debug("落库：无变化，跳过")
    return True


def save_player(user_id: str, store=None) -> bool:
    """单独 flush 一名玩家（不走定时落库）。

    下线 / 宽限期超时时用：先把最后一帧写进库，再销毁内存行。
    落后于库的行会被 rev 门禁拒掉，但仍返回 True（不报错，以库为准）。
    """
    backend = store or _store
    if backend is None:
        log.warning("单行落库跳过：存储层未初始化")
        return False
    if not user_id:
        return False

    with _lock:
        row = players.get(user_id)
        payload = dict(row) if row else None

    if payload is None:
        return True      # 已经没有这一行（重复下线），没东西可写

    try:
        result = backend.save_players([payload])
    except Exception as exc:  # noqa: BLE001 - 落库失败也要留证据
        log.error("玩家 %s 落库失败，写本地快照：%s", user_id, exc)
        _dump_snapshot()
        return False

    stale = [str(x) for x in (result.get("stale") or [])]
    written = int(result.get("written") or 0)
    if user_id not in stale:
        with _lock:
            _persisted_player_revs[str(user_id)] = int(payload.get("rev", 0) or 0)
    if stale:
        log.warning("玩家 %s 的数据落后于库，本次落库被拒（以库为准）", user_id)
    elif written:
        log.info("玩家 %s 已落库（rev=%d）", user_id, int(payload.get("rev", 0) or 0))
    return True


def _snapshot_lists_unlocked() -> tuple[list[dict], list[dict]]:
    """同 ``_snapshot_lists()``，但调用方已持锁。"""
    return ([dict(it) for it in world.values()],
            [dict(p) for p in players.values()])


def _dump_snapshot(items: list[dict] | None = None, ps: list[dict] | None = None) -> None:
    """数据库不可用时的兜底：全量快照写 ./tmp/databaseErrordb.json。"""
    try:
        if items is None or ps is None:
            with snapshot() as (snap_items, snap_ps):
                items, ps = snap_items, snap_ps
        _dump_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "world": items,
            "players": ps,
        }
        _dump_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        log.info("快照已写入 %s", _dump_path.resolve())
    except Exception as exc:  # noqa: BLE001
        log.error("快照写入也失败了：%s", exc)


# ---------------------------------------------------------------------------
# 启动 / 定时落库
# ---------------------------------------------------------------------------

def load_from_store(store) -> bool:
    """从数据库载入权威数据（启动阶段 s2）。

    * 世界元信息 + 世界实体全部读回；
    * 玩家全部读回，但 ``online`` 一律强制 False（重启后没人在线）；
    * 内存里的行级 ``rev`` 全部继承库里的值，下次写入才能正确地只接受领先数据；
    * **坐标基准迁移**（protocol.md §11.1）：``coord_base='feet'`` 的旧行在这里
      一次性 +1.65m 改写为 ``'head'``，并把 rev 抬高一格让它成为「脏行」，
      下一轮 ``save()`` / autosave 就把 ``coord_base='head'`` 与新坐标写回库。
      迁移是幂等的：已经是 head 的行不动。
    """
    global _world_rev, _players_rev
    global _persisted_world_rev
    ok = True

    try:
        world_data = store.load_world(DEFAULT_WORLD) or {}
    except Exception as exc:  # noqa: BLE001
        log.error("读取世界数据失败：%s", exc)
        world_data = {}
        ok = False

    try:
        player_rows = store.load_players() or []
    except Exception as exc:  # noqa: BLE001
        log.error("读取玩家数据失败：%s", exc)
        player_rows = []
        ok = False

    world_row = world_data.get("world") or {}
    item_rows = world_data.get("items") or []
    ## 本次启动从 feet 迁到 head 的玩家（这些行要尽快写回库）
    migrated: list[str] = []

    with mutate():
        world.clear()
        players.clear()
        world_meta.clear()

        if world_row.get("id"):
            world_meta[str(world_row["id"])] = {
                "id": str(world_row["id"]),
                "name": str(world_row.get("name") or ""),
                "seed": int(world_row.get("seed") or 0),
                "rev": int(world_row.get("rev") or 0),
            }

        for row in item_rows:
            item_id = str(row.get("id") or "").strip()
            if not item_id:
                continue
            world[item_id] = {
                "id": item_id,
                "world_id": str(row.get("world_id") or DEFAULT_WORLD),
                "kind": str(row.get("kind") or "generic"),
                "pos_x": _num(row.get("pos_x")),
                "pos_y": _num(row.get("pos_y")),
                "pos_z": _num(row.get("pos_z")),
                "yaw": _norm_yaw(row.get("yaw")),
                "state": str(row.get("state") or "idle"),
                "rev": int(row.get("rev") or 0),
                "updated_at": _num(row.get("updated_at"), time.time()),
            }

        for row in player_rows:
            new_row = _normalize_player_row(row)
            if new_row is None:
                continue
            if coord_base_of(row) == COORD_BASE_FEET:
                # 库里的 rev 是旧基准那一版，+1 才能穿过 rev 门禁写回迁移结果
                new_row["rev"] = int(new_row.get("rev") or 0) + 1
                new_row["updated_at"] = time.time()
                migrated.append(new_row["user_id"])
            players[new_row["user_id"]] = new_row

        _world_rev = max(
            [int(it.get("rev") or 0) for it in world.values()]
            + [int((world_meta.get(DEFAULT_WORLD) or {}).get("rev") or 0), 0]
        )
        _players_rev = max([int(p.get("rev") or 0) for p in players.values()] + [0])

        # 刚从库里读回来的行就是“已落库”的状态，不需要再写一遍
        _persisted_world_rev = int((world_meta.get(DEFAULT_WORLD) or {}).get("rev") or 0)
        _persisted_item_revs.clear()
        _persisted_item_revs.update(
            {iid: int(it.get("rev") or 0) for iid, it in world.items()}
        )
        _persisted_player_revs.clear()
        _persisted_player_revs.update(
            {uid: int(p.get("rev") or 0) for uid, p in players.items()}
        )
        # 迁移过的行内存 rev 比库里大 1：把「已落库 rev」退回一格，让它进下一轮 save()
        for uid in migrated:
            _persisted_player_revs[uid] = int(_persisted_player_revs.get(uid, 0)) - 1

    if migrated:
        log.info(
            "坐标基准迁移：%d 条旧行 feet → head（+%.2fm），下一轮落库写回库：%s",
            len(migrated), HEAD_OFFSET_Y, ", ".join(migrated[:8]),
        )
    log.info(
        "已从数据库载入权威数据：世界 %s（rev=%d）/ 实体 %d 个 / 玩家 %d 条",
        (world_meta.get(DEFAULT_WORLD) or {}).get("name") or DEFAULT_WORLD,
        _world_rev, len(world), len(players),
    )
    return ok


def start_autosave(store, interval: int = 60) -> None:
    """启动定时落库循环（daemon 线程，进程退出自动结束）。

    注意：这里**不再**顺手把库里 online 清成 False。
    那件事必须在 ``load_from_store()`` **之前**做（见 main.py 的 s2），
    否则会把刚上线的玩家又刷成离线。
    """
    global _store, _interval
    _store = store
    _interval = max(5, int(interval))
    _schedule_next()
    log.info("定时落库已启动（每 %d 秒）", _interval)


def stop_autosave() -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None


def _schedule_next() -> None:
    global _timer
    _timer = threading.Timer(_interval, _tick)
    _timer.daemon = True
    _timer.start()


def _tick() -> None:
    try:
        save()
    except Exception:  # noqa: BLE001 - 定时线程不能死
        log.exception("定时落库异常")
    finally:
        _schedule_next()  # 无论成败都排下一轮，避免一次失败终止落库


def reset() -> None:
    """清空热缓存（测试用；不改数据库）。"""
    global _world_rev, _players_rev, _persisted_world_rev
    with mutate():
        world.clear()
        players.clear()
        world_meta.clear()
        _world_rev = 0
        _players_rev = 0
        _persisted_world_rev = 0
        _persisted_item_revs.clear()
        _persisted_player_revs.clear()
