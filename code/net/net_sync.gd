extends Node
## 世界场景的网络同步层（联机）。
##
## 职责：
##   1. 进入世界后用本地账号静默重登（code/net/game_api.gd::login_with_saved_account）；
##   2. 以本地可配的间隔上报本机玩家的坐标 / 朝向 / 动作（STATE_INTERVAL）；
##   3. **主动拉取**在线玩家快照（PEERS_INTERVAL），把其它玩家渲染成 player.tscn 的
##      线条骨架代理，位置做插值，动作按 state 字段切 walk / idle（v1.3：服务端不再推）；
##      上报与拉取合进**同一条批量报文**（v1.3 批量多 action），一轮只有一次往返。
##   4. 某个玩家从 peers 里消失就地拆代理（取代旧 PUSH event join/leave）。
##   5. 意外断线时在服务端宽限期内自动重连 + 重登（flows.md 流程 4 / 5）。
##
## 坐标基准（v1.4 / §11.1，**冻结契约**）：
##   * 协议里的每个位置字段（state 上报、login 回写、peers 每行）都是**头部中心**；
##   * 本机：上报取 player_fp.head_world_position()，落位走 player_fp.teleport_head_to()；
##   * 远端代理：代理根节点在脚底，所以 peers 的头部坐标要减去 HEAD_OFFSET_Y 再插值；
##   * 掉落保护仍看脚底（world_base 读 player.global_position.y），不要跟着改。
##   * 4.1.1：`state` 追加**可选第 9 段** `pitch`（瞄准俯仰，抬头为正，§4 / §11.3），
##     来源 `player_fp.aim_pitch_degrees()`；8 段语义不变，老服务端忽略该段。
##
## 红名（v1.4 / §12）与战斗反馈：
##   * peers 行里的 `red` / `coord_base` 原样留在 _peer_rows 里；
##     `red` 为真时把远端代理整体染红（player_rig.set_red），仅是给客户端看的标记；
##   * 攻击结果（部位 / 倍率 / 伤害 / 未命中原因）通过 combat_message 交给 HUD 弹一条提示。
##
## 设计要点：
##   * 全部用代码 add_child 挂进场景，lobby / inner_main 场景文件不需要（也不能轻易）改动；
##   * 断线 / 未登录时静默降级，单机玩完全不受影响；
##   * **拉取频率由客户端把握**：PEERS_INTERVAL 越小越跟手但请求越多（可调）；
##   * 重连窗口取 5 秒（与服务端 GRACE_SECONDS 默认值对齐），窗口内登不回去就放弃；
##   * 远端代理是 player.tscn 的实例，preview_keys 关掉，免得按 1/2/3 也切它们的动画。

const GameApi = preload("res://code/net/game_api.gd")
const PlayerScene = preload("res://Scenes/actors/player.tscn")
## 只为取 §11.1 的 HEAD_OFFSET_Y（11 盒与头部高度同源，避免第三份魔数）。
const BodyHitboxScript = preload("res://code/actors/body_hitbox.gd")

## 坐标基准取值（§11.1）。`feet` 只会出现在旧版客户端写下的老行上，服务端会一次性迁移。
const COORD_BASE_HEAD := "head"
const COORD_BASE_FEET := "feet"


## 上报间隔（秒）。拉取频率由客户端自定（v1.3）。可调小以提跟手度。
const STATE_INTERVAL := 0.1
## 拉取在线玩家快照的间隔（秒）。取代旧 10Hz PUSH world，频率由客户端把握。
const PEERS_INTERVAL := 0.1
## 远端代理位置插值速度（越大越“硬”）。
const LERP_SPEED := 8.0
## 速度高于这个值就当走路（与服务端 state 字段的两档对齐）。
const WALK_SPEED := 0.5
## 移动中给服务端报的动作名。服务端只认 idle / moving / dead，
## 早期客户端报的 "walk" 会被归成 idle，远端就看不出你在走。
const MOTION_MOVING := "moving"
const MOTION_IDLE := "idle"
## 三围初值（客户端目前还没有成长 / 伤害系统，先用满值上报）。
const DEFAULT_STAT := 100.0
## 断线重连的时间窗口（秒）。与服务端 config.GRACE_SECONDS 默认值一致：
## 服务端视角的「意外断开」有这么久宽限期，窗口内登回来，会话与 player 行都还在。
const RECONNECT_WINDOW := 5.0
## 重连失败后的重试间隔（秒）。
const RECONNECT_INTERVAL := 1.0

## 近战 / 治疗的客户端侧距离上限（服务端会再判一次，这里只用来选目标）。
const ATTACK_RANGE := 3.0
const REVIVE_RANGE := 5.0

## 世界物品快照（首次拉取；v1.3 服务端不再推，后续变更重进世界时重拉）。
signal world_items_received(items: Array)
## 世界元信息（服务端 `world` 响应新增的 `world` 字段：id / name / seed / rev）。
signal world_meta_received(meta: Dictionary)
## 战斗反馈（攻击 / 复活的结果一句话）。world_base 接过来交给 HUD 弹一条短提示。
signal combat_message(text: String, good: bool)

## 业务层实例。故意不写类型标注：GameApi 上是自定义方法，静态类型成 Node 会报找不到成员。
var _api = null
## 本机玩家（player_fp.gd，提供 get_yaw_degrees / get_horizontal_speed）。
var _player: Node3D = null
## 远端代理的挂载点。
var _root: Node3D = null
var _avatars: Dictionary = {}
var _targets: Dictionary = {}
## 远端玩家的原始行（peers 返回的字典），攻击 / 复活选目标用（含 dying / level）。
var _peer_rows: Dictionary = {}
## 上一次自我状态的生死档位，用来识别「旁观结束 / 濒死开始」这类跳变。
var _was_spectating := false
var _was_dying := false
var _accum := 0.0
var _peers_accum := 0.0
var _busy := false
## 当前在线人数（含自己）。
var online_count := 0
## 本机上报的三围（当前值），后期由战斗 / 成长系统驱动。
var hp := DEFAULT_STAT
var stamina := DEFAULT_STAT
var mana := DEFAULT_STAT
## 世界元信息（拉 world 时缓下）。
var world_meta: Dictionary = {}
## 是否至少成功登录过一次（区分「启动时就连不上」与「玩到一半断了」）。
var _logged_in_once := false
## 是否正在重连（避免 connection_changed 与主循环互相踢出多次重连）。
var _reconnecting := false


func _ready() -> void:
	_api = GameApi.new()
	_api.name = "GameApi"
	add_child(_api)
	# v1.3：不再消费服务端 PUSH（已取消），在线数据改为主循环里轮询 peers。
	_api.authenticated_changed.connect(_on_authenticated)
	var tcp = _api.tcp()
	if tcp != null:
		tcp.connection_changed.connect(_on_connection_changed)
	set_process(false)


## 由 inner_main.gd（world_base.gd）调用：注入本机玩家与远端代理挂载点，然后开始同步。
func setup(player: Node3D, root_node: Node3D) -> void:
	_player = player
	_root = root_node if root_node != null else get_parent() as Node3D
	_bootstrap()


func _bootstrap() -> void:
	set_process(false)
	if not await _login_now():
		push_warning("联机同步未登录：%s" % str(_api.last_error))
		return
	# 先按服务器存档把本机玩家放到「上次退出位置」，再开主循环上报；
	# 顺序不能反，否则第一帧 state 会用场景默认点覆盖掉存档位置。
	_apply_spawn_to_player()
	await _after_login()


## 进世界首帧把本机玩家放到服务器存档的「上次退出位置」。仅初次进入调用；
## 断线重连不走这里（人已在地里，瞬移反而突兀）。无存档 / 旧服务端则保持场景默认点。
## §11.1：`login` 回写的 pos_* 是**头部中心**，所以走 teleport_head_to（脚底 = 头 − 1.65m）。
func _apply_spawn_to_player() -> void:
	if _player == null or _api == null or not bool(_api.has_spawn):
		return
	if not _player.has_method("teleport_head_to"):
		return
	_player.call("teleport_head_to", _api.spawn_position, float(_api.spawn_yaw))


## 静默重登（建连 + hello + device + login）。成功返回 true。
func _login_now() -> bool:
	if not await _api.login_with_saved_account():
		return false
	_logged_in_once = true
	return true


## 登录成功后的共同收尾：打日志 + 拉世界快照 + 开主循环。
## 首次进入与断线重连都走这里，避免两份逻辑走歪。
func _after_login() -> void:
	print("联机同步已就绪：%s @ 服务端 %s（协议 %s，拉取间隔 %.2fs）" % [
		_api.user_name, _api.server_version, _api.protocol_version, PEERS_INTERVAL,
	])
	if not is_inside_tree():
		return
	# 首次进来把世界物品拉一次（v1.3 服务端不再推，变更只能靠重进世界时重拉）；
	# 重连后也拉一次，补上断线期间错过的变更。
	var res: Dictionary = await _api.fetch_world()
	if bool(res.get("ok", false)):
		var data: Dictionary = res.get("data", {})
		world_meta = _api.world_meta
		if not world_meta.is_empty():
			world_meta_received.emit(world_meta)
		world_items_received.emit(data.get("items", []))
	if is_inside_tree():
		set_process(true)


# ---------------------------------------------------------------- 断线重连

## TCP 连接状态变化。断开时启动一次重连（服务端视角见 flows.md 流程 4 / 5）。
func _on_connection_changed(connected: bool) -> void:
	if connected:
		return
	_clear_avatars()
	set_process(false)
	if not is_inside_tree() or _player == null or not _logged_in_once or _reconnecting:
		return
	_try_reconnect()


## 在 RECONNECT_WINDOW 秒内重试「重连 + 重登」。
## 服务端对意外断开的会话有宽限期，窗口内登回来会把挂起的会话还给我们
## （游戏内状态不丢）；超过窗口就静默降级单机，不影响玩家继续玩。
func _try_reconnect() -> void:
	_reconnecting = true
	var deadline := Time.get_ticks_msec() + int(RECONNECT_WINDOW * 1000.0)
	var attempt := 0
	while is_inside_tree() and Time.get_ticks_msec() < deadline:
		attempt += 1
		if await _login_now():
			_reconnecting = false
			if is_inside_tree():
				print("断线重连成功（第 %d 次尝试）" % attempt)
				await _after_login()
			return
		if not is_inside_tree():
			break
		await get_tree().create_timer(RECONNECT_INTERVAL).timeout
	_reconnecting = false
	if is_inside_tree():
		push_warning("断线重连失败，已降级为单机：%s" % str(_api.last_error))


# ---------------------------------------------------------------- 主循环

func _process(delta: float) -> void:
	_tick_avatars(delta)
	if _busy or _player == null or _api == null:
		return
	if not bool(_api.authenticated):
		return
	_accum += delta
	_peers_accum += delta
	var want_state := _accum >= STATE_INTERVAL
	var want_peers := _peers_accum >= PEERS_INTERVAL
	if not want_state and not want_peers:
		return
	# v1.3 批量：上报与拉取拼进同一条报文的第 2 行（`a1|d1;;a2|d2`），
	# 服务端按序回多帧；一轮只花一次往返，比两条单发少一半 RTT 与读放大。
	var calls: Array = []
	if want_state:
		_accum = 0.0
		calls.append(["state", _state_payload()])
	if want_peers:
		_peers_accum = 0.0
		calls.append(["peers", ""])
	_busy = true
	var results: Array = await _api.call_batch(calls)
	_busy = false
	# 结果与 calls 等长且同序；数量对不上时宁可丢掉尾部也不要错位解释。
	for i in mini(calls.size(), results.size()):
		var call: Array = calls[i]
		match str(call[0]):
			"state":
				_on_state_result(results[i])
			"peers":
				_on_peers_result(results[i])


## 本机上报的 `state` payload（格式由 GameApi.state_payload 统一维护）。
## §11.1：x/y/z 必须是**头部中心**，不是脚底 —— 服务端按头部做命中判定、落库与回写。
## §4 第 9 段：最后追加瞄准俯仰角 `pitch`（度，抬头为正）；8 段语义完全不变，只是多一段可选值。
func _state_payload() -> String:
	var pos := _local_head_position()
	var yaw := 0.0
	var speed := 0.0
	if _player.has_method("get_yaw_degrees"):
		yaw = float(_player.call("get_yaw_degrees"))
	if _player.has_method("get_horizontal_speed"):
		speed = float(_player.call("get_horizontal_speed"))
	var motion := MOTION_MOVING if speed > WALK_SPEED else MOTION_IDLE
	return _api.state_payload(pos, yaw, hp, motion, stamina, mana, _local_aim_pitch())


## 本机瞄准俯仰角（度，抬头为正，§4 第 9 段 / §11.3）。
## player_fp 没提供时按 0（水平）兜底：老服务端与旧客户端都等价于「第 9 段缺省」。
func _local_aim_pitch() -> float:
	if _player != null and _player.has_method("aim_pitch_degrees"):
		return float(_player.call("aim_pitch_degrees"))
	return 0.0


## 本机头部中心的世界坐标（§11.1）。player_fp 没提供时按「脚底 + 1.65m」兜底。
func _local_head_position() -> Vector3:
	if _player != null and _player.has_method("head_world_position"):
		return _player.call("head_world_position")
	return _player.global_position + Vector3(0.0, BodyHitboxScript.HEAD_OFFSET_Y, 0.0)


## 处理批量里 `state` 那一帧：只有「服务器不认我了」这一档值得动手，其余交给下一轮。
func _on_state_result(res: Dictionary) -> void:
	_api.apply_state_response(res)
	if not bool(res.get("ok", false)):
		var code := str(res.get("code", ""))
		if code == "NOT_AUTHENTICATED" or code == "DISCONNECTED" or code == "BAD_USER_ID":
			push_warning("联机上报被拒：%s" % str(_api.explain(res, code)))
			_api.set_authenticated(false)
		return
	_apply_life_state()


## 把服务端权威的生死状态映射到本机玩家：
## 濒死 => 冻结原地等治疗；旁观 => 自由飞行（无实体、不受物理影响）；
## 旁观结束（30 分钟到）=> 传送到服务端记录的死亡点并解除冻结。
func _apply_life_state() -> void:
	if _player == null or _api == null:
		return
	var st: Dictionary = _api.self_state
	if st.is_empty():
		return
	var dying := bool(st.get("dying", false))
	var spectating := bool(st.get("spectating", false))

	if spectating and not _was_spectating:
		if _player.has_method("set_spectator"):
			_player.call("set_spectator", true)
	elif not spectating and _was_spectating:
		if _player.has_method("set_spectator"):
			_player.call("set_spectator", false)
		# 旁观结束：服务端把复活点冻结在死亡位置，客户端飞出去的身体要拉回来。
		# §11.1：state 回写的 pos_* 也是头部坐标，按头部落位。
		if _player.has_method("teleport_head_to") and st.has("pos_x"):
			var back := Vector3(
				float(st.get("pos_x", 0.0)),
				float(st.get("pos_y", 0.0)),
				float(st.get("pos_z", 0.0)),
			)
			_player.call("teleport_head_to", back, float(st.get("yaw", 0.0)))

	if not _player.has_method("set_frozen"):
		_was_spectating = spectating
		_was_dying = dying
		return
	_player.call("set_frozen", dying and not spectating)
	_was_spectating = spectating
	_was_dying = dying


# ---------------------------------------------------------------- 远端代理

## 处理批量里 `peers` 那一帧：拉到的快照交给 _apply_players 重绘代理。
func _on_peers_result(res: Dictionary) -> void:
	if bool(res.get("ok", false)):
		_apply_players(res.get("data", {}))


func _apply_players(payload: Dictionary) -> void:
	var players: Array = payload.get("players", [])
	online_count = int(payload.get("online", players.size()))
	var seen := {}
	for entry in players:
		if typeof(entry) != TYPE_DICTIONARY:
			continue
		var row: Dictionary = entry
		var uid := str(row.get("user_id", "")).strip_edges()
		if uid == "" or uid == str(_api.user_id):
			continue
		seen[uid] = true
		# 整行原样保留：`red`（§12 红名）/ `coord_base`（§11.1 行级坐标基准）后续都要用
		_peer_rows[uid] = row
		# §11.1：peers 的 pos_* 是**头部中心**；代理根节点在脚底，插值目标要减掉 HEAD_OFFSET_Y。
		# 行级 `coord_base` 为 'feet' 的是旧客户端写下的老数据（服务端会一次性迁移），按行换算。
		var head_pos := Vector3(
			float(row.get("pos_x", 0.0)),
			float(row.get("pos_y", 0.0)),
			float(row.get("pos_z", 0.0)),
		)
		var feet_pos := head_pos
		if str(row.get("coord_base", COORD_BASE_HEAD)) != COORD_BASE_FEET:
			feet_pos = head_pos - Vector3(0.0, BodyHitboxScript.HEAD_OFFSET_Y, 0.0)
		_targets[uid] = {
			"head": head_pos,
			"pos": feet_pos,
			"yaw": float(row.get("yaw", 0.0)),
		}
		var avatar = _avatars.get(uid, null)
		if avatar == null:
			avatar = _spawn_avatar(uid)
		if avatar != null:
			_set_motion(avatar, str(row.get("state", "idle")))
			_set_avatar_red(avatar, bool(row.get("red", false)))
	# 这一轮没出现的就地拆除（已下线）
	for uid in _avatars.keys():
		if not seen.has(uid):
			_despawn_avatar(uid)
	for uid in _peer_rows.keys():
		if not seen.has(uid):
			_peer_rows.erase(uid)


## 生成一个远端代理。代理根节点在**脚底**（player.tscn 的骨架原点），
## 所以 _targets 里存的是「头部 − 1.65m」的脚底目标点，盒子（BodyHitbox）挂在根下 +1.65m。
func _spawn_avatar(uid: String) -> Node3D:
	if _root == null:
		return null
	var node: Node = PlayerScene.instantiate()
	var avatar := node as Node3D
	if avatar == null:
		node.queue_free()
		return null
	avatar.name = "Remote_" + uid.replace("-", "_")
	# 预览键只留给本机 / 编辑器，远端代理不该响应 1 / 2 / 3。
	avatar.set("preview_keys", false)
	avatar.position = Vector3.ZERO
	_root.add_child(avatar)
	_avatars[uid] = avatar
	return avatar


func _despawn_avatar(uid: String) -> void:
	var avatar = _avatars.get(uid, null)
	_avatars.erase(uid)
	_targets.erase(uid)
	if avatar != null and is_instance_valid(avatar):
		avatar.queue_free()


func _clear_avatars() -> void:
	for uid in _avatars.keys():
		_despawn_avatar(uid)
	_targets.clear()
	_peer_rows.clear()
	_was_spectating = false
	_was_dying = false
	online_count = 0


func _set_motion(avatar: Node3D, state: String) -> void:
	if not avatar.has_method("play_motion"):
		return
	# 服务端只发 idle / moving / dead；walk / run 是早期客户端自己报的旧词，一并兼容。
	var motion := "walk" if state == "moving" or state == "walk" or state == "run" else "idle"
	if avatar.has_method("current_motion") and str(avatar.call("current_motion")) == motion:
		return
	avatar.call("play_motion", motion)


## 红名（§12）：远端代理整体染红（player_rig 把线框 / 关节 / 头环的材质换成红名色）。
## 服务端对红名**不加任何限制**，这里纯粹是「让别的玩家看得出来」。
func _set_avatar_red(avatar: Node3D, red: bool) -> void:
	if not avatar.has_method("set_red"):
		return
	if avatar.has_method("is_red") and bool(avatar.call("is_red")) == red:
		return
	avatar.call("set_red", red)


func _tick_avatars(delta: float) -> void:
	if _targets.is_empty():
		return
	var t := clampf(delta * LERP_SPEED, 0.0, 1.0)
	for uid in _avatars.keys():
		var avatar = _avatars.get(uid, null)
		if avatar == null or not is_instance_valid(avatar):
			_avatars.erase(uid)
			_targets.erase(uid)
			continue
		var target: Dictionary = _targets.get(uid, {})
		if target.is_empty():
			continue
		var current: Vector3 = avatar.global_position
		avatar.global_position = current.lerp(target["pos"], t)
		avatar.rotation.y = lerp_angle(avatar.rotation.y, deg_to_rad(float(target["yaw"])), t)


func _on_authenticated(value: bool) -> void:
	if not value:
		_clear_avatars()


# ---------------------------------------------------------------- 对外查询

func is_online() -> bool:
	return _api != null and bool(_api.authenticated)


func local_user_id() -> String:
	return "" if _api == null else str(_api.user_id)


## 网络往返耗时（毫秒，平滑值）；未登录 / 无样本返回 -1。
func latency_ms() -> float:
	if _api == null or not bool(_api.authenticated):
		return -1.0
	return float(_api.latency_ms())


## 当前世界名（来自服务端 `world` 元信息），没拿到返回空串。
func world_name() -> String:
	return str(world_meta.get("name", ""))


## 最近一次上报回写的行修订号（排错用：能看出自己的数据有没有被服务端收下）。
func state_rev() -> int:
	return 0 if _api == null else int(_api.state_rev)


## 改本机三围（当前值）。后期由战斗 / 成长系统调用。
func set_stats(new_hp: float, new_stamina: float, new_mana: float) -> void:
	hp = clampf(new_hp, 0.0, DEFAULT_STAT)
	stamina = clampf(new_stamina, 0.0, DEFAULT_STAT)
	mana = clampf(new_mana, 0.0, DEFAULT_STAT)


## 服务端权威自我状态（HUD 用）；未登录 / 尚无响应时为空字典。
## 含 hp / hp_max / spirit / level / kills / deaths / dying / dying_remaining /
## spectating / spectator_remaining / pos_* / yaw / red / coord_base。
func self_state() -> Dictionary:
	return {} if _api == null else _api.self_state


## 本机是否红名（§12，取 state 响应的 `red`）。HUD 徽标用。
func self_red() -> bool:
	return bool(self_state().get("red", false))


## 某个在线玩家的原始 peers 行（含 red / coord_base / dying 等），没有返回空字典。
func peer_row(user_id: String) -> Dictionary:
	return _peer_rows.get(user_id, {})


## 攻击最近的玩家（客户端只负责选目标，射程 / 朝向 / 部位 / 伤害由服务端判定）。
## 结果通过 combat_message 交给 HUD 弹一条短提示（命中部位 × 倍率 伤害 / 未命中原因）。
func attack_nearest() -> void:
	if _api == null or not bool(_api.authenticated) or _player == null:
		return
	var target := _nearest_peer(ATTACK_RANGE, false)
	if target == "":
		combat_message.emit("附近 3 米内没有可攻击的目标", false)
		return
	var res: Dictionary = await _api.attack(target)
	var info: Dictionary = res.get("combat", _api.last_attack)
	combat_message.emit(str(info.get("text", "")), bool(info.get("ok", false)))


## 复活最近的濒死玩家（5 秒窗口内、治疗距离内）。结果同样弹一条提示。
func revive_nearest() -> void:
	if _api == null or not bool(_api.authenticated) or _player == null:
		return
	var target := _nearest_peer(REVIVE_RANGE, true)
	if target == "":
		combat_message.emit("附近 5 米内没有濒死玩家", false)
		return
	var res: Dictionary = await _api.revive(target)
	if not bool(res.get("ok", false)):
		combat_message.emit(_api.explain(res, "复活失败"), false)
		return
	var data: Dictionary = res.get("data", {})
	combat_message.emit("复活成功（目标生命 %.0f / %.0f）" % [
		float(data.get("hp", 0.0)), float(data.get("hp_max", 0.0)),
	], true)


## 选出距离本机最近、且满足条件的目标 user_id；没有返回空串。
## §11.1：服务端判命中是「攻击者头 → 目标头」，所以选目标也用头部基准。
func _nearest_peer(max_range: float, need_dying: bool) -> String:
	if _api == null or not bool(_api.authenticated) or _player == null:
		return ""
	var origin := _local_head_position()
	var best_uid := ""
	var best_dist := max_range
	for uid in _peer_rows.keys():
		var row: Dictionary = _peer_rows[uid]
		if need_dying and not bool(row.get("dying", false)):
			continue
		if not need_dying and bool(row.get("dying", false)):
			continue
		var target: Dictionary = _targets.get(uid, {})
		if target.is_empty():
			continue
		var dist := origin.distance_to(target.get("head", target["pos"]))
		if dist <= best_dist:
			best_dist = dist
			best_uid = str(uid)
	return best_uid
