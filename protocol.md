# 众生之门 套接字协议 v1.4（GameTCP）

> 实现：`protocol.py`（解析/编码）· `server.py`（监听/帧读取）· `session.py`（长连接、会话 Token、宽限期、坐标基准）· `actions.py`（动作表）· `game.py`（热缓存/战斗/死亡）· `hitbox.py`（身体碰撞箱）· `karma.py`（红名判定）
> 业务流程：`flows.md`（注册 / 登录 / 正常下线 / 意外断开宽限期 / 客户端重连 / 生死流转）
> 状态：**已实现并通过冒烟测试**（`scripts/tcp_smoke_test.py`，242 项）

v1.4 相对 v1.3 的三处变更（**只加语义，报文结构与哈希规则不变**）：

1. **坐标基准改为头部中心**（§11.1）：协议里所有玩家位置字段都是头部中心的世界坐标，
   脚底 = 头部 − 1.65m；`client_version < 0.2.0` 的旧客户端由服务端自动 ±1.65m 换算。
2. **身体碰撞箱 + 部位命中**（§11.2 / §11.3）：服务端按 11 个盒子判定命中部位，
   伤害 = 25 × 倍率；新增错误码 `NOT_FACING` / `MISS` / `OUT_OF_RANGE`。
3. **红名（Karma）**（§12）：人类击杀同势力人类 → 击杀者变红；确认死亡解除受害者红名。

服务版本 **4.1.1** 的新增（**协议号仍是 1.4**：纯追加可选字段，8 段报文与旧服务端都不受影响）：

1. **瞄准俯仰 `pitch`**（§4 第 9 段 / §11.3）：`state` 可选第 9 段上报俯仰角（0 = 水平，正 = 抬头），
   攻击方向由「水平前向」改为 `yaw × pitch` 的 **3D 前向**，60° 朝向锥也用同一条 3D 前向。
   `hello` 追加能力位 `"aim_pitch": true`。
2. 其余全部不动：11 个盒子的数值、倍率、字段名、错误码 `MISS` / `NOT_FACING` / `OUT_OF_RANGE`。

v1.3 的三处变更（继续生效）：

1. **取消所有服务端主动推送**：不再有 10Hz 世界广播、建连 `PUSH | token`、join/leave 事件广播。
   一切数据由客户端按需拉取（`peers` / `world` / `state`），**询问频率由客户端自行决定**。
   会话 Token 改为随首个 `hello` 响应下发。
2. **请求尾哈希改为「请求头 + 正文」联合哈希**：`plain_hash = sha256(第1行 + "\n" + 第2行 + "\n" + 第3行)`。
3. **第 2 行支持批量多 action**：多个 `action|data` 组用 `;;` 连接，服务端逐个执行、**按序返回多行** OK/FAIL。

---

## 1. 传输层

| 项 | 值 |
|---|---|
| 协议 | TCP，**长连接**（同一条连接可以连续发多条请求） |
| 端口 | `.env` 的 `TCP_PORT`（默认跟随 `PORT`，即 **5423**）；可通过 HTTP 发现服务 `GET /api/v1/endpoint` 查询 |
| 编码 | UTF-8 文本，`\n` 为唯一帧边界 |
| 单行上限 | `MAX_LINE_BYTES`（默认 16384 字节），超限 → `TOO_LARGE` |
| 空闲超时 | `READ_TIMEOUT`（默认 180 秒）无任何上行数据即断开 |
| 并发上限 | `MAX_CONNECTIONS`（默认 256），满了直接回 `TOO_MANY_CONNECTIONS` 并关连接 |
| 断线宽限期 | `GRACE_SECONDS`（默认 **5 秒**）：已登录连接意外断开后，玩家仍算在线，期内重连可接管旧会话（见 §3.2） |

### 1.1 拉取模型（v1.3：请求即响应，服务端不主动发帧）

v1.2 及更早，登录后服务端以 `PUSH_HZ`（默认 10Hz）主动下发世界快照。**v1.3 起全部取消**：
服务端只在收到请求时才回帧，一条请求对应（批量时对应多条）**按序**返回。世界/在线玩家数据
由客户端用 `peers` / `world` 自行拉取，频率由客户端把握（要更快就多发几次请求）。
好处：没有广播线程拖慢、没有 PUSH 与响应交错、慢连接不再影响他人。

### 1.2 客户端连接顺序（推荐）

```
connect TCP                       建连后服务端不发任何帧
  → hello                         握手，响应里带 server_version / 动作表 / session_token（客户端存下）
  → device                        （本地没有 device_id 时）签发，只回票据不落库
  → device（带 device_id）         确认设备，拿回已绑定账号（可能为 null）
  → account 存在 ? login : check_name → register
  → 进入世界，按自选频率上报 state 并拉取 peers / world
```

此后**每一条请求的第 3 行都带上 token**（见 §2.2）。丢了 token 或拿了上一条连接的
旧 token，请求会被 `SESSION_TOKEN_EXPIRED` 拒掉（**不再附推 reset_token**），
客户端应重新走一次 `hello` + `login`（详见 flows.md 流程 4）。

---

## 2. 请求报文（4 行）

每行以 `\n` 结尾，第 4 行之后的 `\n` 就是帧边界（**不要**额外发空行）。

```
TheDoorOfBings | 1.3 | <ts> | <encryption> | <client_version> | <platform> | <locale>
<action> | <action_data>[ ;; <action> | <action_data>]*
<session_token> | <device_id_hex> | <user_id>
<plain_hash> | <cipher_hash>
```

### 2.1 请求头（第 1 行）

| 字段 | 说明 |
|---|---|
| 固定魔头 | `TheDoorOfBings`，不符 → `BAD_MAGIC` |
| version | `1.4` / `1.3` / `1.2` / `1.1` / `1.0`（1.0 是老格式，头行只有前 4 个字段，仍然接受） |
| ts | Unix 秒级时间戳；与服务端偏差 > `REQUEST_TIME_WINDOW`（默认 **300s**）→ `REQUEST_EXPIRED` |
| encryption | 目前仅 `none` |
| client_version | **客户端软件版本**，例 `0.1.0`。每个请求都要带，服务端记进 `devices.client_version` |
| platform | `windows` / `linux` / `android` / `web` … |
| locale | 例 `zh_CN` |

> 第 5–7 字段可以整体省略（按空串处理），但**不要少写前 4 个**。

### 2.2 正文（第 2、3 行）

- **第 2 行**：单动作是 `<action> | <action_data>`；v1.3 **批量**时用 `;;` 连接多组
  `a1|d1;;a2|d2`，每组内部多参数仍用 `&` 分隔。服务端逐个执行，**按请求顺序**回多帧。
- **第 3 行**：`<session_token> | <device_id_hex> | <user_id>`，三段都可为空串

| 字段 | 说明 |
|---|---|
| `session_token` | 本连接专属的会话 Token，32 位 hex；v1.3 起**随首个 `hello` 响应下发**（不再建连推送），`expire=180` 只是参考值，token 实际随连接存活（见 §3.2）。与本会话持有的不一致 → `SESSION_TOKEN_EXPIRED`；形状不对（非 32 位 hex）→ `BAD_SESSION_TOKEN` |
| `device_id_hex` | `device_id.py` 输出 bytes 的 `.hex()`。1024B → **2048 字符**，2048B → 4096 字符。非空时服务端做 HMAC 校验，不过 → `BAD_DEVICE_ID` |
| `user_id` | 形如 `ZWME-2WKQ`。带了合法 token 时只在「与本连接已登录账号不一致」时拒；没带 token 则校验「该账号确实绑定在本设备上」 → `BAD_USER_ID` |

**向后兼容**：第 3 行只写 2 段（`<device_id_hex> | <user_id>`）仍被接受，服务端按「没带 token」
处理，退回 v1.1 的身份规则（不校 token，带 `user_id` 时查库确认绑定）。第一条 `hello`
就常按 2 段发（此时还没有 token），拿到响应里的 `session_token` 后再带上。

**批量注意**：身份（token / device / user）是**报文级**的，只校验一次；某个动作 `UNKNOWN_ACTION`
或抛错只让**那一帧**回 FAIL，不影响同批其它动作。

**登录完成后**（`login` 成功），后续请求可以把 `device_id_hex` / `user_id` 都留空——身份已记在会话上。
（带 `user_id` 就必须同时带 `device_id`；带上了合法 token 则两者都可省，省掉 2KB 的 device_id。）

### 2.3 请求尾（第 4 行）

```
plain_hash | cipher_hash
```

- **v1.3 联合哈希**：`plain_hash = sha256(第1行原文 + "\n" + 第2行原文 + "\n" + 第3行原文)` 的 hex（64 位小写）。
  即哈希同时覆盖**请求头与正文两行**，改头部（版本、时间戳、client_version…任一字段）都会失效。
  **v1.4 与 v1.3 规则完全相同**：版本号升级不改变哈希算法，`1.4` 走同一个联合哈希分支。
- **v1.2 及更早**：`plain_hash = sha256(第2行 + "\n" + 第3行)`（仅正文两行）。服务端按 version 自动区分。
- **逐字节计算，不要 strip / 不要 trim 行尾空格**
- `encryption=none` 时 `cipher_hash == plain_hash`
- 不符 → `BAD_PLAIN_HASH` / `BAD_CIPHER_HASH`

### 2.4 参考实现（GDScript）

```gdscript
func build_packet(action: String, data: String, token: String, device_hex: String, user_id: String, calls: Array = []) -> PackedByteArray:
	var ts := int(Time.get_unix_time_from_system())
	var head := "%s | 1.3 | %d | none | %s | %s | %s" % [MAGIC, ts, client_version, platform, locale]
	# 批量：calls 是 [[a1,d1],[a2,d2],…]，用 ";;" 拼第 2 行；否则单动作
	var body1 := _join_batch(calls) if not calls.is_empty() else "%s | %s" % [action, data]
	# token 为空时退回 v1.1 的 2 字段写法，服务端自动兼容
	var body2 := "%s | %s | %s" % [token, device_hex, user_id] if token != "" else "%s | %s" % [device_hex, user_id]
	var ctx := HashingContext.new()
	ctx.start(HashingContext.HASH_SHA256)
	# v1.3 联合哈希：覆盖 头部 + 正文两行
	ctx.update((head + "\n" + body1 + "\n" + body2).to_utf8_buffer())
	var digest := ctx.finish().hex_encode()
	return (head + "\n" + body1 + "\n" + body2 + "\n" + digest + " | " + digest + "\n").to_utf8_buffer()

func _join_batch(calls: Array) -> String:
	var parts: PackedStringArray = []
	for c in calls:
		parts.append("%s | %s" % [c[0], c[1]])
	return ";;".join(parts)
```

> 第一条 `hello` 用空 token（第 3 行 2 段）发出；从响应的 `session_token` 存下后再带上后续请求。

---

## 3. 响应（单行）

```
OK   | <action> | <json>
FAIL | <action> | <code> | <message>
```

- 所有协议级错误也走 `FAIL`，`<action>` 位置填 `-`（**不再返回裸文本**）
- `<message>` 是给人看的，**可能含中文与空格**，客户端只能凭 `<code>` 分支
- `<json>` 是紧凑 JSON（无空格、无换行），中文不转义
- **v1.3 服务端不再发 `PUSH` 帧**（帧类型保留仅为兼容老服务端）；批量请求会连续收到多行 OK/FAIL，
  按发出的动作顺序一一对应即可。

### 3.1 错误码

| code | 含义 | 客户端建议处理 |
|---|---|---|
| `BAD_MAGIC` | 魔头不符 | 代码写错了 |
| `BAD_VERSION` | 协议版本不支持 | 提示升级客户端 |
| `REQUEST_EXPIRED` | 时间戳偏移过大 | 校正本机时间后重试 |
| `DUPLICATE_REQUEST` | 重复报文（同一会话 + 同一秒 + 同一正文） | 忽略，不要重试 |
| `BAD_ENCRYPTION` | 加密方式不支持 | 提示升级 |
| `BAD_PLAIN_HASH` / `BAD_CIPHER_HASH` | 哈希不对（v1.3 覆盖头部，改头也会触发） | 代码写错了，检查哈希是否按头+正文逐字节算 |
| `MALFORMED` | 结构不对（行数/段数/UTF-8） | 代码写错了 |
| `TOO_LARGE` | 单行超长 | 检查 device_id 是否异常 |
| `BAD_DEVICE_ID` | 设备 ID 非法（伪造/损坏/过期） | 丢弃本地 ID，重新 `device` |
| `BAD_USER_ID` | user_id 非法或不匹配 | 回登录页 |
| `UNKNOWN_ACTION` | 动作不存在 | 代码写错了 |
| `BAD_PAYLOAD` | 动作参数格式不对 | 代码写错了 |
| `NOT_AUTHENTICATED` | 未登录就调需要登录的动作 | 先 `login` |
| `NOT_READY` | 服务端未就绪（如密钥未配置） | 稍后重试 / 提示运维 |
| `NAME_TAKEN` | 用户名称已被占用 | 换名字 |
| `USER_NOT_FOUND` | 账号不存在 | 回注册页 |
| `DEVICE_MISMATCH` | 账号不属于本设备 | 提示换回原机器 |
| `DEVICE_ALREADY_BOUND` | 本设备已绑定账号 | **再调一次 `device`** 取回 account |
| `ACCOUNT_LOCKED` / `ACCOUNT_DISABLED` | 账号被锁 / 停用 | 提示联系管理员 |
| `FILE_HASH_MISMATCH` | 登录时带的 `file_hash` 与库里不一致（本地文件被改） | 提示校验客户端文件 |
| `BAD_SESSION_TOKEN` | 会话 Token 格式非法 | 代码写错了，检查是否原样回传 |
| `SESSION_TOKEN_EXPIRED` | Token 与本会话不一致（已失效 / 拿的是上一条连接的） | 清掉本地 token，重新 `hello` + `login` |
| `OUT_OF_RANGE` | 攻击目标超出 `ATTACK_RANGE`（3m） | 走近再打 |
| `NOT_FACING` | 目标不在攻击朝向锥（60°）内 | 转向目标（按自己的 `yaw` 校正） |
| `MISS` | 射程与朝向都过，但射线没打到任何身体盒子 | 别乱挥，瞄准身体（第 9 段 `pitch` 可调俯仰） |
| `INTERNAL_ERROR` | 服务端内部异常 | 稍后重试 |
| `TOO_MANY_CONNECTIONS` | 连接数已满 | 退避重连 |

### 3.2 会话 Token 与宽限期

**签发**：每条 TCP 连接建立后，服务端立刻在内部 `issue_token()`，但**不主动推送**。
Token 随**首个 `hello` 响应**下发（`login` 响应里也重复回写一份，便于补存）：

```
OK | hello | {"…":"…","session_token":"<32 hex>","server_push":false,"push_hz":0,"actions":[…]}
```

- 客户端把 `session_token` 存下来，放进**后续每一条请求的第 3 行**
- token 不写库、不落盘，只对当前这条连接有效，进程重启即作废

**校验**：`req.session_token` 非空且与本会话持有的不相等 → 只回一帧 FAIL（**不再附推 reset_token**）：

```
FAIL | <action> | SESSION_TOKEN_EXPIRED | Token mismatch, please use the latest token
```

客户端收到该错误码应清空本地 token，重新走 `hello`（取新 token）+ `login`，而不是重试原动作。

**宽限期**：已登录的连接意外断开（未调 `logout`）时，服务端不立即销毁玩家：

| 情况 | 服务端行为 |
|---|---|
| `GRACE_SECONDS` 内同设备重连并 `login` | 接管旧会话（取消挂起项），位置/状态不丢，旧 token 作废 |
| `GRACE_SECONDS` 超时 | `online=False` → 落库 → 销毁热缓存（**不再广播 leave**，其他客户端拉 `peers` 时自然感知） |
| 客户端主动调了 `logout` | 当场完整拆除，**不走宽限期** |

> 完整业务描述见 `flows.md` 流程 3 / 4 / 5。

---

## 4. 动作表

| action | action_data | 需要登录 | 说明 |
|---|---|---|---|
| `ping` | 空 | 否 | 心跳/连通性测试 |
| `hello` | 空（可选 `coord_base=head\|feet` 显式声明基准） | 否 | 握手：服务端版本、动作表、坐标基准与碰撞箱能力，**并下发 session_token** |
| `device` | `<fp_hex>&<length>` | 否 | 签发 / 确认设备 ID（见 §5）。**新签发不写库**，到 `register` 才落库 |
| `check_name` | `<user_name>` | 否 | 注册前名称查重，回 `{"name","available","reason"}` |
| `register` | `<user_name>&<race>&<player_id>` | 否 | 注册，`user_id` 由**服务端**生成；后两段可省（`race` 缺省 `human`，`player_id` 为客户端 UUID） |
| `login` | JSON `{"player_id":"<user_id>","file_hash":"<hex>"}` | 否 | 登录。旧写法兼容：非 JSON 时整段当 `user_id`（不参与文件校验） |
| `logout` | 空 | 否 | 正常下线：**完整拆除**（置离线 → 落库 → 销毁 → 作废 token；不再停推送/广播） |
| `whoami` | 空 | 否 | 当前会话账号信息 |
| `state` | `<x>&<y>&<z>&<yaw>[&hp][&state][&stamina][&mana][&pitch]` | **是** | 上报自身位置/状态 |
| `peers` | 空 | **是** | **拉取**一次在线玩家快照（取代旧 PUSH world） |
| `world` | 空 | 否 | **拉取**一次世界元信息 + 全部物品快照 |
| `item` | `<id>&[kind]&<x>&<y>&<z>&<yaw>[&state]` | **是** | 增/改一个世界物品 |

`state` 的 `state` 取值：`idle` / `moving` / `dead`（其他值归为 `idle`）。
坐标绝对值上限 `100000`（非法坐标回 `BAD_PAYLOAD`）。

后五段全部**可省略**，省略就保持原值（第 9 段 `pitch` 例外：省略 / 空 / 非法一律按 **0**，见下）：

| 段位 | 字段 | 范围 | 说明 |
|---|---|---|---|
| 5 | `hp` | `0 .. hp_max` | 当前生命值，超出会被夹回去 |
| 6 | `state` | 见上 | 与 v1.1 位置一致（放中间是为了不破坏老报文） |
| 7 | `stamina` | `0 .. stamina_max` | 当前体力 |
| 8 | `mana` | `0 .. mana_max` | 当前灵力 |
| 9 | `pitch` | `-90 .. 90`（度） | **瞄准俯仰角**：`0` = 水平，**正数 = 抬头**，负数 = 低头。可选，缺省 / 空串 / 非法值一律按 `0` 处理（等价于旧客户端行为） |

> 第 9 段是 **v1.4 内追加的可选字段**：8 段报文完全合法、语义不变；老服务端会忽略多出来的第 9 段。
> 服务端只把这个值用在**攻击方向**上（§11.3），**不落库**、也不参与 `peers` 下发。
> 实现上超出 ±90° 会被夹到边界（`cos(pitch) >= 0`，射线不会朝后翻）。

> 三围上限（`hp_max` / `stamina_max` / `mana_max`）默认都是 100，**客户端目前不能改**（将来由 `admin` 或成长系统改）。
> 每次上报都会让该玩家的 `rev += 1`，响应里的 `rev` 就是回写给客户端的行修订号。

`state` 响应：

```
OK | state | {"t":1761379200,"seq":42,"rev":7,"hp":100.0,"hp_max":100.0,"spirit":1.0,"level":1,
              "kills":0,"deaths":0,"dying":false,"dying_remaining":0.0,
              "spectating":false,"spectator_remaining":0.0,"red":false,
              "pos_x":12.5,"pos_y":1.65,"pos_z":-8.0,"yaw":90.0,"coord_base":"head"}
```

> v1.4：`state` 的 `pos_y` 按**本连接**的坐标基准下发（`client_version < 0.2.0` 时为脚底坐标），
> 并新增 `red`（红名）与 `coord_base`（本连接的实际基准）。

`peers` 响应（拉取式在线快照）：

```
OK | peers | {"players":[{"user_id":"ZWME-2WKQ","user_name":"勇者","player_name":"勇者","race":"human",
                          "level":1,"pos_x":12.5,"pos_y":1.65,"pos_z":-8.0,"yaw":90.0,
                          "coord_base":"head","state":"moving",
                          "hp_max":100.0,"hp":100.0,"stamina_max":100.0,"stamina":100.0,
                          "mana_max":100.0,"mana":100.0,"dying":false,"red":false}],
              "players_rev":7,"world_rev":5,"coord_base":"head","t":1761379200}
```

> v1.4：每行新增 `red`（红名）与 `coord_base`（该行坐标的基准，等于请求方的实际基准，§11.4）。

`world` 响应（含 `world` 元信息 + 全部 `items`）：

```
OK | world | {"world":{"id":"default","name":"众生之门","seed":0,"rev":5},
              "items":[{"id":"door_01","kind":"door","pos_x":0,"pos_y":0,"pos_z":-8,
                        "yaw":90,"state":"idle"}],
              "world_rev":5,"t":1761379200}
```

`register` 响应（`player_id` 未传时退化为 `user_id`）：

```
OK | register | {"user_id":"ZWME-2WKQ","user_name":"勇者",
                  "player_id":"a3f1c8d2-…","player_name":"勇者","race":"elf",
                  "server_version":"4.1.1","protocol_version":"1.4"}
```

`login` 响应（`server_push=false` / `push_hz=0`，并回写 `session_token`）：

```
OK | login | {"user_id":"ZWME-2WKQ","user_name":"勇者",
              "player_id":"a3f1c8d2-…","player_name":"勇者","race":"human",
              "pos_x":12.5,"pos_y":1.65,"pos_z":-8.0,"coord_base":"head","yaw":90.0,
              "server_push":false,"push_hz":0,"session_token":"9f2c…(32 hex)",
              "server_version":"4.1.1","protocol_version":"1.4"}
```

> `pos_x/pos_y/pos_z/yaw` 是该玩家**上次退出的存档位置**（`enter_world` 已从库/缓存恢复），
> 客户端进世界首帧应先把本机玩家放到这一点、**再**开始 `state` 上报，否则会用场景默认出生点
> 覆盖掉存档位置。断线重连不重放（人已在地里）。yaw 单位度，0 = 正北 / -Z，顺时针为正。
> v1.4：`pos_y` 是**头部中心**（脚底 = `pos_y - 1.65`）；旧客户端（`client_version < 0.2.0`）
> 收到的 `pos_y` 已自动换回脚底值，`coord_base` 字段如实标记为 `"feet"`。

`logout` 响应：

```
OK | logout | {"authenticated":false,"saved":true,"user_id":"ZWME-2WKQ"}
```

> `login` 的 `file_hash` 三态：**不传 / 非 JSON payload** ⇒ 不参与文件校验；
> 传空串 ⇒ 与库里已有值不等就拒（`FILE_HASH_MISMATCH`）；传非空 ⇒ 库里为空则记下来，
> 已有且不等则拒。重连时 `player` 行不在内存时会自动从库重载（位置不丢）。

---

## 5. `device`：签发 / 确认设备 ID

设备 ID 是**服务端签发**的无状态凭证（客户端**不持密钥**）。
结构：`[ver(2)][lenFlag(2)][ts(4)][nonce(32)][fp(16)][tag(32)][HKDF 扩展]`，总长 1024 或 2048 字节。

- `tag = HMAC-SHA256(DEVICE_ID_SECRET, ver|lenFlag|ts | nonce | fp)`
- 改任意一个字节（含末尾扩展段）都会校验失败
- 校验**不查库**，靠自身 HMAC 即可

### 5.1 两种用法

| 第 3 行 device_id 段 | 行为 | 返回 |
|---|---|---|
| **空** | 签发一张新 ID，**不写库** | `device_id`（新的）、`known=false`、`stored=false` |
| **非空且合法** | **不重新签发、不写库**，只回读该设备绑定的账号 | `device_id`（原样）、`known=true`、`stored=true`、`account` |

```
OK | device | {"device_id":"ab12…","length":1024,"issued_at":1761379200,
                "known":true,"stored":true,"account":{"user_id":"ZWME-2WKQ","user_name":"勇者"},
                "server_version":"4.1.1","protocol_version":"1.4"}
```

`account` 为 `null` 表示这台设备还没绑账号 → 走 `check_name` + `register`。

> v1.2 起 **`device` 不再写 `users` 表**（新机阶段根本没有账号，
> 提前落库会留一堆永不可用的孤行）。设备行只在 `register` 里首次 `touch_device`。

> **客户端务必把返回的 `device_id` 原样存到本地**（`user://`）。
> 每次调 `device` 不带 ID 都会拿到一张**不同的**新 ID（nonce 随机），
> 拿旧的 ID 去登录新设备是绑不上的。

---

## 6. 拉取帧（取代旧 PUSH）

v1.3 服务端**不再主动下发任何帧**（旧 topic `token` / `reset_token` / `world` / `event` 全部废弃）。
客户端要「别人动了没有」就用 `peers`（在线玩家快照）与 `world`（世界物品快照）按自选频率拉取；
要感知某人上下线，就在下一次 `peers` 里看他出现/消失。`PUSH` 帧类型解析仍保留，仅为兼容老服务端。

---

## 7. 完整往返示例

```
C→S  TheDoorOfBings | 1.4 | 1761379200 | none | 0.2.0 | windows | zh_CN
     hello | 
      |  |
     <64hex> | <64hex>          ← 首条 hello 无 token（第 3 行 2 段），哈希覆盖头+正文

S→C  OK | hello | {"magic":"TheDoorOfBings","server_version":"4.1.1","protocol_version":"1.4",
                    "server_push":false,"push_hz":0,"autosave_interval":60,"device_id_length":1024,
                    "user_name_max_len":24,"session_token":"9f2c…(32 hex)",
                    "actions":["assets","attack","check_name","device","hello","item","login","logout",
                    "peers","ping","races","register","revive","state","whoami","world"],
                    "coord_base":"head","coord_base_applied":"head","hitboxes":true,
                    "attack_cone_deg":60,"aim_pitch":true,"now":1761379200}

C→S  … device | 
     9f2c…(token) |  |
     …

S→C  OK | device | {"device_id":"ab12…(2048 hex)","length":1024,"issued_at":1761379200,
                     "known":false,"stored":false,"account":null,
                     "server_version":"4.1.1","protocol_version":"1.4"}

C→S  … check_name | 勇者
     9f2c…(token) | <device_hex> | 
     …

S→C  OK | check_name | {"name":"勇者","available":true,"reason":""}

C→S  … register | 勇者&elf&a3f1c8d2-…
     9f2c…(token) | <device_hex> | 
     …

S→C  OK | register | {"user_id":"ZWME-2WKQ","user_name":"勇者",
                       "player_id":"a3f1c8d2-…","player_name":"勇者","race":"elf",
                       "server_version":"4.1.1","protocol_version":"1.4"}

C→S  … state | 12.5&1.65&-8&180&77&moving&33&44&-12;;peers |
     9f2c…(token) |  |
     …                            ← 批量：一次报文里上报位置 + 拉取在线列表
                                    （v1.4 的 y=1.65 是头部中心；旧客户端写脚底值即可；
                                     第 9 段 -12 = 低头 12°，可选，不写就是 0）

S→C  OK | state | {"t":1761379200,"seq":2,"rev":2,"pos_y":1.65,"red":false,"coord_base":"head",…}
S→C  OK | peers | {"players":[…],"players_rev":2,"world_rev":5,"coord_base":"head","t":1761379200}

C→S  … attack | ZWME-2WKQ
     9f2c…(token) |  |
     …

S→C  OK | attack | {"ok":true,"target":"ZWME-2WKQ","part":"head","multiplier":2.0,"damage":50.0,
                     "hp":50.0,"hp_max":100.0,"dying":false,"dying_remaining":0.0,
                     "distance":2.31,"hit_t":2.2,"killer_red":false,"victim_red":false,"attack_at":1761379200}

C→S  … logout | 
     9f2c…(token) |  |
     …

S→C  OK | logout | {"authenticated":false,"saved":true,"user_id":"ZWME-2WKQ"}
```

> 登录后面的请求里 `device_id` 都留空了：带上了合法 token 就不必再发 2KB 票据。
> `register` / `login` 之前必须带 `device_id`（服务端要拿它绑账号）。

---

## 8. HTTP 旁路（只做发现，已拆到独立组件）

游戏通信全走 TCP，HTTP 只回答「客户端该连哪里」。
本组件（server-socket）**不提供 HTTP 接口**，发现接口由独立的 **server 组件**
（仓库 `server` 分支，默认监听 4231）承担，路径与响应形状不变：

| 路径 | 说明 |
|---|---|
| `GET /` | 服务名与版本 |
| `GET /api/v1/endpoint` | `{"tcp":{"host":"…","port":5423},"protocol_version":"1.4","service_version":"4.1.1","transport":"tcp","encoding":"utf-8"}` |
| `GET /game/api` | 同上（旧路径，过渡期保留） |
| `GET /healthz` | 健康检查（仅 HTTP 服务自身，不含游戏服状态） |

`host` / `port` 来自 server 组件的 `.env`（`HOST` / `PORT`），需与本组件的实际监听地址一致。

> ⚠️ 下发版本号写死在 server 组件的 `config.py`（`PROTOCOL_VERSION` / `SERVICE_VERSION`），
> 该分支若仍是旧值，**需要跟着改成 `1.4` / `4.1.1`**，否则客户端会拿到旧的兼容提示。
> 协议本身向后兼容（v1.4 服务端仍接受 v1.0~v1.3 报文），不影响连入。

---

## 9. 已知限制 / TODO

1. **没有 nonce 字段**：防重放目前只能在「同一会话 + 同一秒 + 同一正文」内去重
   （`server.py::ReplayGuard`），跨连接重放挡不住。会话 Token 只标识连接、
   不防重放（拿到同一连接的 token 就能重放）。等上加密时一起加 nonce。
2. **无 `encryption` 实现**：`cipher_hash` 目前恒等于 `plain_hash`，链路是明文。
   token 也是明文发的，生产环境要么走内网要么等加密层。
   （v1.3 的联合哈希覆盖头部，能防「改头部字段」的篡改，但仍是明文，不是加密。）
3. **`device_id` 每次请求占 2KB**：带上合法 token 后，第 3 行的 `device_id` / `user_id` 都可以留空。
4. **`register` 的 FAIL 帧装不下 `account`**：设备已绑定时客户端需要再调一次 `device` 取回。
   将来可给 `FAIL` 加一列 JSON。
5. **token 不续期**：`expire=180` 只是给客户端的参考值，服务端实际只在连接存活期间持有它；
   长时间不发上行时需要靠 `ping` 撑住 `READ_TIMEOUT`。
6. **拉取频率由客户端把握**：没有服务端推送后，`peers` / `world` 的实时性取决于客户端轮询频率；
   轮询过密会加重服务端读放大，需要客户端做退避/合并（可用批量把 `state` + `peers` 塞进一条报文）。

---

## 10. 动作扩展（种族 / 战斗 / 资源）

| action | payload | 鉴权 | OK data |
|---|---|---|---|
| `races` | 空 | 不需要 | `{"races": [{"key","name","dev_only"}]}` |
| `attack` | `<target_user_id>` | 需要 | `{"ok","target","part","multiplier","damage","hp","hp_max","dying","dying_remaining","distance","hit_t","killer_red","victim_red"}` |
| `revive` | `<target_user_id>` | 需要 | `{"ok","target","hp","hp_max","distance"}` |
| `assets` | `<sub>&<path>[&<offset>][&<length>]` | 需要 | 见下 |

* `races`：种族清单**由服务端裁定**，开发阶段只返回 `human`。`race.tscn` 据此渲染选项，
  `register` 的 race 字段若不在清单里一律落回 `human`。
* `attack`：服务端判定射程（3m）/ 朝向锥（60°）/ 冷却（0.8s）/ 目标状态，并按 11 个身体盒子
  判定**命中部位**（§11.2 / §11.3），伤害 = `25 × 倍率`（头部 50 / 躯干 25 …）；
  目标 HP 归零进入**濒死**（`dying=true`，5 秒窗口），不是立刻死亡。
* `revive`：治疗者需在 5m 内，把濒死目标拉起（恢复到 30% 生命），并清空击杀归属。
* `assets` 子动作：
  * `list&path` —— 列目录，返回 `entries`（name / is_dir / size / mtime）与 `chunk_bytes`；
  * `stat&path` —— 返回 `exists` / `is_dir` / `size` / `mtime`；
  * `get&path&offset&length` —— 分块读取，`length` 被服务端夹到 `chunk_bytes`（默认 8192），
    返回 `data`（base64）/ `total` / `next_offset` / `eof`，客户端按 `next_offset` 续拉。
  资源根目录由 `ASSETS_DIR` 指定（默认 `/home/mimi/obj/assets/The-Doors-of-Bings`），
  路径穿越（`..`、绝对路径、盘符）一律拒绝。

`state` 的 OK data 在 v1.3 扩展里追加了权威自身状态：
`hp / hp_max / spirit / level / kills / deaths / dying / dying_remaining / spectating / spectator_remaining / pos_* / yaw`。
客户端只读，不再用本地 HP 覆盖服务端。v1.4 再追加 `red`（红名）与 `coord_base`（坐标基准，见 §11.1）。

---

## 11. 坐标基准与身体碰撞箱（v1.4 / 服务 4.1.1）

> 本节是**冻结契约**：客户端与服务端各自实现时必须逐字段对齐。改动先改这里。

### 11.1 坐标基准：头部

- 协议里所有"玩家位置"字段（`state` 上报的 `x & y & z`、`login` 回写的 `pos_x/pos_y/pos_z`、`peers` 每行的 `pos_*`）**统一表示头部中心**的世界坐标。
- 脚底 = 头部 − `HEAD_OFFSET_Y`（默认 **1.65m**）。
- 数据库 `player.pos_*` 同步改为头部坐标；`player.coord_base` 列记录该行是按哪种基准写的：
  - `'feet'`：旧版客户端遗留（v0.1.x），服务端在 `load_player` / `enter_world` 时 **一次性 +1.65m** 并改写为 `'head'`；
  - `'head'`：已迁移（缺省 / 非法值也按 head 处理——服务端自己造的行一定是 head）。
- **旧客户端兼容**：`hello` 会记录本连接的 `client_version`（请求头第 1 行第 5 段）。
  服务端对 `client_version < 0.2.0`（或空）的连接按旧语义处理——收到的 `state` 位置 **+1.65m** 换成头部再入库、
  下发时 **−1.65m** 换回脚底（这样旧客户端收发都是脚底坐标，往返一致）。
  客户端本次升级必须把 `project.godot` 的 `application/config/version` 提到 **0.2.0**，否则会被当成旧客户端。
- `hello` 响应新增：`"coord_base":"head"`、`"hitboxes":true`、`"attack_cone_deg":60`、
  `"aim_pitch":true`（服务端读懂 `state` 第 9 段 `pitch` 并按 3D 前向判命中，§11.3）。

> **实现补充（服务端 `session.Session.coord_base`）**：
> * `hello` 的 payload 允许显式声明基准：`hello | head` 或 `hello | coord_base=feet`，优先级高于 `client_version`；
> * `hello` 响应里 `coord_base` 恒为协议基准 `"head"`，另给 `coord_base_applied` 表示**本连接**实际生效的基准；
> * `state` 响应、`peers` 每行与 `peers` 顶层也都带 `coord_base`（= 本次下发坐标所用基准），
>   客户端只应拿这个字段决定如何解释 `pos_y`，不要自己猜客户端版本；
> * 迁移写回库：启动加载时把 `'feet'` 行 +1.65m、`rev` 抬高一格，下一轮 autosave 写回 `coord_base='head'`（幂等）。

### 11.2 人体模型：11 个盒子

单位米，**相对头部中心**（+Y 向上，−Z 为角色正面），绕 Y 轴随 `yaw` 旋转。
`l` = 角色左手侧（+X），`r` = 右手侧（−X）。

| part | 中心 (x, y, z) | 半尺寸 (hx, hy, hz) | 伤害倍率 |
|---|---|---|---|
| `head` | 0, 0, 0 | 0.11, 0.10, 0.11 | **2.0** |
| `neck` | 0, −0.15, 0 | 0.06, 0.05, 0.06 | **1.5** |
| `torso` | 0, −0.45, 0 | 0.18, 0.25, 0.11 | **1.0** |
| `upper_arm_l` | +0.22, −0.40, 0 | 0.05, 0.15, 0.05 | 0.6 |
| `upper_arm_r` | −0.22, −0.40, 0 | 0.05, 0.15, 0.05 | 0.6 |
| `forearm_l` | +0.26, −0.68, 0 | 0.045, 0.14, 0.045 | 0.5 |
| `forearm_r` | −0.26, −0.68, 0 | 0.045, 0.14, 0.045 | 0.5 |
| `thigh_l` | +0.09, −0.92, 0 | 0.07, 0.215, 0.07 | 0.8 |
| `thigh_r` | −0.09, −0.92, 0 | 0.07, 0.215, 0.07 | 0.8 |
| `shin_l` | +0.09, −1.35, 0 | 0.055, 0.22, 0.055 | 0.6 |
| `shin_r` | −0.09, −1.35, 0 | 0.055, 0.22, 0.055 | 0.6 |

- 基础伤害仍是 `ATTACK_DAMAGE = 25`，实际伤害 = `25 × 倍率`（头部一击 50、颈部 37.5、躯干 25、小臂 12.5…），向下取整到 0.1。
- 两侧命名固定为 `_l` / `_r`，禁止出现 `left/right` 混写。

### 11.3 命中判定（服务端权威）

1. 原点 = **攻击者头部**；方向 = 攻击者 `yaw` 与 `pitch` 推出的 **3D 单位前向**：

   ```
   direction = (sin(yaw) * cos(pitch), sin(pitch), -cos(yaw) * cos(pitch))
   ```

   `yaw` 约定不变（0 = 正北 / −Z，顺时针为正）；`pitch` 单位度，`0` = 水平、**正数 = 抬头**
   （来源：`state` 第 9 段，缺省 / 非法 = `0`，见 §4）。
2. 朝向校验：`dot(3D 前向, normalize(目标头部 − 攻击者头部)) ≥ cos(ATTACK_CONE_DEG = 60°)`，
   否则 `FAIL | attack | NOT_FACING`（**用同一条 3D 前向**，抬头 / 低头都会影响锥校验结果）。
3. 把射线变换到目标局部系（先减目标头部、再绕 Y 轴反向旋转 `yaw`），对 11 个 AABB 做 slab 测试，取 `t` 最小且 `t ≤ ATTACK_RANGE(3m)` 的盒子。
4. 全部不命中 → `FAIL | attack | MISS`（射程内没打中身体）。超出距离仍是 `OUT_OF_RANGE`。
5. 命中盒子的倍率 × 基础伤害，写 `last_attacker`、按 §12 判红名。

> **实现补充（射线方向，必须两边一致）**：第 1 条的“方向”若照字面取“朝目标头部”，
> 射线终点正好是 `head` 盒中心（半尺寸 0.10~0.11m），**任何射程内的攻击都会先命中头部盒**，
> §11.2 的 11 个盒子、部位倍率与 `MISS` 全部不可达。因此服务端 `hitbox.resolve_hit` 用
> **攻击者朝向推出的 3D 前向**（4.1.1 起带 `pitch`，此前是水平前向）：
>
> * 射线方向 = `forward(yaw, pitch)`，`yaw` 由攻击者最近一次 `state` 决定，`pitch` 取同一次上报的第 9 段
>   （只在内存里记住，**不落库**；缺省 / 非法 = `0`，等价于 4.1.0（无 pitch）的水平射线，旧客户端行为不变）；
> * "目标头部 − 攻击者头部"只用于第 2 条的 60° 朝向锥校验；
> * 等高平射（`pitch = 0`）穿过 `head` 盒（命中头部 ×2）；**低头**（`pitch < 0`）射线依次穿过
>   `neck` / `torso` / `thigh` / `shin`；**抬头**（`pitch > 0`）射线从头顶越过 → `MISS`；
> * 于是 11 个盒子的倍率、`MISS` 与“打头要瞄”的手感才真正生效——水平射线不再是永远爆头的作弊线。
> * 客户端做本地表现时请用同一规则（`yaw` + 相机俯仰推出的 3D 前向射线 + 目标局部系 11 盒 slab 测试），
>   否则命中特效会和服务端伤害对不上；客户端相机俯仰以 **抬头为正** 上报（Godot 里 `Head.rotation.x`
>   正值即抬头，与协议同号）。
> * `hit_t` 是命中处的射线距离（米，调试用），客户端可忽略。

### 11.4 `attack` 响应（新增字段）

```
OK | attack | {"ok":true,"target":"ZWME-2WKQ","part":"head","multiplier":2.0,"damage":50.0,
                "hp":50.0,"hp_max":100.0,"dying":false,"dying_remaining":0.0,
                "distance":2.31,"hit_t":2.2,"killer_red":true,"victim_red":false}
```

- `damage` 已按部位倍率算好并取整到 0.1（头 50 / 颈 37.5 / 躯干 25 / 上臂 15 / 小臂 12.5 / 大腿 20 / 小腿 15）。
- `killer_red` / `victim_red` 是**本次攻击之后**双方的红名标记（红名只在真死亡时变化，见 §12）。
- `hit_t` 为命中点的射线距离（米），调试用，客户端可忽略。
- 4.1.1 起命中方向带 `pitch`（§11.3），**响应字段与本节完全不变**（没有新增 `pitch` 回显）。
- `revive` 不变（治疗不需要部位）。
- `state` 响应新增自身 `"red": false`；`peers` 每行新增 `"red": false`、`"coord_base":"head"`（详见 §11.1）。

---

## 12. 红名（Karma，v1.4）

规则来源：`main/材料/文档/规则/rule.md` 第三章。代码落地按下面执行。

### 12.1 触发

**人类击杀同势力人类** → 击杀者进入红名（`red = true`）。
"击杀"的时点 = 中毒者濒死 5 秒窗口结束、被服务端判定为**真死亡**的那一刻（不是打空血那一瞬）。
本期只有 `human` 一个种族、没有势力系统 → 所有人类视为同势力，因此**人类杀人类必定触发**。

### 12.2 豁免（判定函数留参，当前无对应实体）

不同势力人类互杀 / 人类杀妖精 / 妖精杀任何玩家 / BOSS 身份击杀，均不触发。
实现为 `karma.should_become_red(killer, victim) -> bool`，参数里带 `race` / `faction` / `is_boss`，本期这些字段取不到就落默认值。

### 12.3 惩罚（本期能力边界）

- "无法接取高阶任务"：**没有任务系统**，本期不实现，只在红名字段上留好读取口。
- "被标记危险目标、可被无限制攻击"：攻击本来就没有限制，服务端**不额外加限制**；
  红名只是给客户端看的标记（`peers[].red` / `state.red`），客户端负责标红。

### 12.4 解除

- **确认死亡**（5 秒濒死窗内未被 `revive`）→ 清红名。
- 账号注销后重建角色 → 新行天然为 `false`。
- 没有"时间到自动解除"，也没有主动解除动作。

### 12.5 存储与观测

| 位置 | 字段 |
|---|---|
| `player` 表 | `red_name BOOLEAN NOT NULL DEFAULT FALSE`、`red_at DOUBLE PRECISION`（变红时间，审计用） |
| `player` 表 | `coord_base TEXT NOT NULL DEFAULT 'feet'`（**迁移用默认值**：ALTER 补出来的旧行是脚底基准，加载时 +1.65m 并改写为 `'head'`，之后恒为 `'head'`） |
| `state` 响应 | `red` |
| `peers` 行 | `red` |
| 日志 | 变红 / 解除各打一条 INFO（含击杀者与被击杀者 user_id） |

> 实现补充：判定函数是 `karma.should_become_red(killer, victim)`，两个参数都是玩家的 dict 行；
> 取不到的字段落默认值（`race='human'` / `faction='common'` / `is_boss=False`），
> 所以本期"人类杀人类"必定触发。结算点只在 `game._resolve_timers` 的 `died` 分支
> （濒死窗口结束、判真死亡），`revive` 成功路径不碰红名。

