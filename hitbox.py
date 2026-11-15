"""身体碰撞箱与部位命中判定（服务端权威）。

对应 ``protocol.md`` §11.2 / §11.3 的落地实现。**盒子数值是冻结契约**：
11 个盒子、相对头部中心的偏移、半尺寸与伤害倍率，一个数都不要改
（客户端按同一张表做本地表现，两边对不上就是事故）。

坐标约定（与 §11.2 表格逐字一致）
--------------------------------
* 单位**米**；
* 全部**相对头部中心**（``pos_x/pos_y/pos_z`` 就是头部中心，见 §11.1）；
* ``+Y`` 向上，``−Z`` 为角色**正面**，``+X`` 为角色**左手侧**（``_l``）、
  ``−X`` 为右手侧（``_r``）；命名只用 ``_l`` / ``_r``，禁止 ``left/right`` 混写；
* 盒子绕 Y 轴随角色 ``yaw`` 旋转；``yaw`` 单位度，``0`` = 正北 / ``−Z``，顺时针为正
  （与 ``game._norm_yaw`` / 客户端 ``project.godot`` 的约定一致）。

关于射线方向（实现说明，契约只冻结了盒子与倍率）
--------------------------------------------
§11.3 第 1 条写的是「方向 = normalize(目标头部 − 攻击者头部)」。若照字面实现，
射线终点正好落在 ``head`` 盒中心（半尺寸 0.10~0.11m），**任何射程内攻击都必然先命中头部**，
11 个盒子、部位倍率与 ``MISS`` 错误码全部不可达。因此这里按「射线方向 = 攻击者
``yaw`` + ``pitch`` 推出的 3D 单位前向」实现（服务 4.1.1 起带俯仰，此前是水平前向）：

* 朝向（60° 锥）单独校验，见 :func:`facing_ok`，用**同一条 3D 前向**，不通过 → ``NOT_FACING``；
* 射线与目标身体求交，全不中 → ``MISS``；
* ``pitch = 0``（水平）时射线正好穿过 ``head`` 盒（头部一击伤害 ×2）；**低头**（``pitch < 0``）
  时依次穿过 ``neck`` / ``torso`` / ``thigh`` / ``shin``；**抬头**（``pitch > 0``）时从头顶越过 →
  ``MISS``。这才能让 11 个盒子的倍率真正生效，也才不会「等高平地对砍永远爆头」。

``resolve_hit`` 的 ``attacker_head`` 只用来给出射线**原点**，``target_head`` 是目标
局部系的**原点**（减目标头部、再绕 Y 反向旋转目标 ``yaw``）；两者都不能省。
"""

from __future__ import annotations

import math
from typing import NamedTuple

## 头部中心到脚底的距离（米）：feet = head − HEAD_OFFSET_Y（协议 §11.1，冻结）
HEAD_OFFSET_Y = 1.65

## 攻击朝向锥：攻击者前向与「指向目标头部」的夹角上限（度，冻结）
ATTACK_CONE_DEG = 60.0

## 瞄准俯仰角的合法范围（度）：超出按边界夹住，保证 cos(pitch) >= 0（射线不会朝后翻）。
## 客户端自身把视角夹在 ±85°，这里只是服务端的防御性上限。
AIM_PITCH_LIMIT = 90.0

## 近战射程（米）：与 game.ATTACK_RANGE 同值；这里只作为 resolve_hit 的默认值，
## 真正的射程判定由调用方（game.attack）用 game.ATTACK_RANGE 传入。
ATTACK_RANGE = 3.0

## 浮点比较余量：让「正好 60°」「正好贴着盒面」这类边界值判定稳定
_EPS = 1e-9


class BodyBox(NamedTuple):
    """一个轴对齐盒子（在角色局部系里），附带部位名与伤害倍率。"""

    part: str
    ## 中心（相对头部中心）
    cx: float
    cy: float
    cz: float
    ## 半尺寸
    hx: float
    hy: float
    hz: float
    ## 伤害倍率：实际伤害 = ATTACK_DAMAGE(25) × multiplier
    multiplier: float


## 人体模型：11 个盒子（protocol.md §11.2，冻结；顺序 = 表格顺序）
HUMAN_BODY: tuple[BodyBox, ...] = (
    BodyBox("head", 0.0, 0.0, 0.0, 0.11, 0.10, 0.11, 2.0),
    BodyBox("neck", 0.0, -0.15, 0.0, 0.06, 0.05, 0.06, 1.5),
    BodyBox("torso", 0.0, -0.45, 0.0, 0.18, 0.25, 0.11, 1.0),
    BodyBox("upper_arm_l", 0.22, -0.40, 0.0, 0.05, 0.15, 0.05, 0.6),
    BodyBox("upper_arm_r", -0.22, -0.40, 0.0, 0.05, 0.15, 0.05, 0.6),
    BodyBox("forearm_l", 0.26, -0.68, 0.0, 0.045, 0.14, 0.045, 0.5),
    BodyBox("forearm_r", -0.26, -0.68, 0.0, 0.045, 0.14, 0.045, 0.5),
    BodyBox("thigh_l", 0.09, -0.92, 0.0, 0.07, 0.215, 0.07, 0.8),
    BodyBox("thigh_r", -0.09, -0.92, 0.0, 0.07, 0.215, 0.07, 0.8),
    BodyBox("shin_l", 0.09, -1.35, 0.0, 0.055, 0.22, 0.055, 0.6),
    BodyBox("shin_r", -0.09, -1.35, 0.0, 0.055, 0.22, 0.055, 0.6),
)

## 部位名 -> 倍率（客户端按 part 取表现，给个直接可查的表）
HIT_MULTIPLIER: dict[str, float] = {box.part: box.multiplier for box in HUMAN_BODY}

## 部位名集合（校验 / 日志用）
PARTS: tuple[str, ...] = tuple(box.part for box in HUMAN_BODY)


def head_from_feet(feet_y: float) -> float:
    """脚底基准 → 头部基准（旧客户端上报的 Y 加 1.65）。"""
    return float(feet_y) + HEAD_OFFSET_Y


def feet_from_head(head_y: float) -> float:
    """头部基准 → 脚底基准（下发给旧客户端的 Y 减 1.65）。"""
    return float(head_y) - HEAD_OFFSET_Y


def norm_pitch(degrees) -> float:
    """把瞄准俯仰角归一成 ``[-90, 90]`` 度；非法值（非数字 / NaN / inf）回 0（水平）。

    协议 §4 第 9 段：``0`` = 水平、正数 = 抬头；**缺省 / 空 / 非法一律按 0**（旧客户端行为不变）。
    """
    try:
        value = float(degrees)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return max(-AIM_PITCH_LIMIT, min(AIM_PITCH_LIMIT, value))


def forward(yaw: float, pitch: float = 0.0) -> tuple[float, float, float]:
    """由 ``yaw`` / ``pitch``（度）推出单位 **3D** 前向（protocol.md §11.3）。

    * ``yaw``：0 = 正北 / −Z，顺时针为正（约定不变）；
    * ``pitch``：0 = 水平，**正数 = 抬头**（``+Y`` 分量为 ``sin(pitch)``）；缺省 0 = 水平射线。

    ``direction = (sin(yaw)·cos(pitch), sin(pitch), −cos(yaw)·cos(pitch))``

    yaw = 0, pitch = 0    → (0, 0, -1)
    yaw = 90, pitch = 0   → (1, 0, 0)    正东
    yaw = 0, pitch = 90   → (0, 1, 0)    正上
    yaw = 0, pitch = -90  → (0, -1, 0)   正下
    """
    yaw_rad = math.radians(float(yaw) % 360.0)
    pitch_rad = math.radians(norm_pitch(pitch))
    cos_p = math.cos(pitch_rad)
    return (math.sin(yaw_rad) * cos_p, math.sin(pitch_rad), -math.cos(yaw_rad) * cos_p)


def _sub(a, b) -> tuple[float, float, float]:
    return (float(a[0]) - float(b[0]), float(a[1]) - float(b[1]), float(a[2]) - float(b[2]))


def _length(vec) -> float:
    return math.sqrt(vec[0] * vec[0] + vec[1] * vec[1] + vec[2] * vec[2])


def facing_ok(
    attacker_yaw: float,
    attacker_head,
    target_head,
    cone_deg: float = ATTACK_CONE_DEG,
    attacker_pitch: float = 0.0,
) -> bool:
    """朝向锥校验（§11.3 第 2 条）。

    ``dot(攻击者 3D 前向, normalize(目标头部 − 攻击者头部)) >= cos(cone_deg)``。
    ``attacker_pitch`` 与射线方向同一个符号约定（0 = 水平，正 = 抬头）；
    抬头 / 低头都会把这条 3D 前向抬离水平面，因此锥校验与射线方向一致。
    两点重合（退化射线）一律视为不通过。
    """
    delta = _sub(target_head, attacker_head)
    dist = _length(delta)
    if dist <= _EPS:
        return False
    fx, fy, fz = forward(attacker_yaw, attacker_pitch)
    dot = (fx * delta[0] + fy * delta[1] + fz * delta[2]) / dist
    return dot >= math.cos(math.radians(cone_deg)) - _EPS


def to_local(
    point,
    origin,
    yaw: float,
) -> tuple[float, float, float]:
    """世界点 → 目标局部系：先减目标头部，再绕 Y 轴**反向**旋转目标 ``yaw``。

    局部系的 ``−Z`` 是目标正面、``+X`` 是目标左手侧，与 :data:`HUMAN_BODY` 一致。
    """
    dx, dy, dz = _sub(point, origin)
    rad = math.radians(float(yaw) % 360.0)
    cos_v, sin_v = math.cos(rad), math.sin(rad)
    return (dx * cos_v + dz * sin_v, dy, -dx * sin_v + dz * cos_v)


def direction_to_local(direction, yaw: float) -> tuple[float, float, float]:
    """世界方向向量 → 目标局部系（只旋转，不平移）。"""
    dx, dy, dz = float(direction[0]), float(direction[1]), float(direction[2])
    rad = math.radians(float(yaw) % 360.0)
    cos_v, sin_v = math.cos(rad), math.sin(rad)
    return (dx * cos_v + dz * sin_v, dy, -dx * sin_v + dz * cos_v)


def ray_box(
    origin: tuple[float, float, float],
    direction: tuple[float, float, float],
    box: BodyBox,
    max_t: float = math.inf,
) -> float | None:
    """射线 vs 轴对齐盒子（slab 测试），命中返回进入距离 ``t``，否则 ``None``。

    ``t`` 从射线原点起算（米），``0 <= t <= max_t``。
    方向分量为 0 时退化成「原点是否落在该轴的板内」。
    """
    t_min = 0.0
    t_max = max_t
    lo = (box.cx - box.hx, box.cy - box.hy, box.cz - box.hz)
    hi = (box.cx + box.hx, box.cy + box.hy, box.cz + box.hz)
    for axis in range(3):
        d = direction[axis]
        o = origin[axis]
        if abs(d) < 1e-12:
            if o < lo[axis] - _EPS or o > hi[axis] + _EPS:
                return None
            continue
        t1 = (lo[axis] - o) / d
        t2 = (hi[axis] - o) / d
        if t1 > t2:
            t1, t2 = t2, t1
        if t1 > t_min:
            t_min = t1
        if t2 < t_max:
            t_max = t2
        if t_min > t_max:
            return None
    if t_max < 0.0:
        return None
    return t_min if t_min >= 0.0 else 0.0


def resolve_hit(
    attacker_head,
    attacker_yaw: float,
    target_head,
    target_yaw: float,
    max_range: float = ATTACK_RANGE,
    attacker_pitch: float = 0.0,
) -> tuple[str, float, float] | None:
    """射线命中判定：返回 ``(part, multiplier, t)``，全不中返回 ``None``。

    * 原点 = ``attacker_head``；
    * 方向 = ``attacker_yaw`` + ``attacker_pitch`` 推出的 **3D 前向**
      （见模块顶部「关于射线方向」；``pitch`` 缺省 0 = 旧版水平射线）；
    * 把射线变换到目标局部系后，对 11 个盒子做 slab 测试，
      取 ``t`` 最小且 ``t <= max_range`` 的盒子（即最先进入的那个盒子）。
    """
    ox, oy, oz = (float(attacker_head[0]), float(attacker_head[1]), float(attacker_head[2]))
    origin_local = to_local((ox, oy, oz), target_head, target_yaw)
    fx, fy, fz = forward(attacker_yaw, attacker_pitch)
    direction_local = direction_to_local((fx, fy, fz), target_yaw)

    best: tuple[str, float, float] | None = None
    for box in HUMAN_BODY:
        t = ray_box(origin_local, direction_local, box, float(max_range))
        if t is None or t > max_range + _EPS:
            continue
        if best is None or t < best[2]:
            best = (box.part, box.multiplier, t)
    return best


def damage_for(multiplier: float, base_damage: float = 25.0) -> float:
    """实际伤害 = 基础伤害 × 倍率，向下取整到 0.1（§11.2）。"""
    return math.floor(float(base_damage) * float(multiplier) * 10.0 + 1e-9) / 10.0
