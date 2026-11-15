extends CharacterBody3D
## 第一人称角色：走 / 跑（Shift）/ 跳（Space）+ 鼠标视角。
##
## 约定：节点原点在脚底，胶囊碰撞体向上偏移半个身高，所以摆在 (x, 0, z) 就是站在地面上。
## 头节点（Head）只负责俯仰，角色本体负责左右转身。
##
## 坐标基准（协议 v1.4 / §11.1）：**协议里的一切位置字段都是头部中心**，
## 脚底 = 头部 − HEAD_OFFSET_Y（1.65m）。因此：
##   * 上行（net_sync 的 state）与落位（login / peers 的 pos_*）都走头部，
##     入口是 head_world_position() 与 teleport_head_to()；
##   * 场景内的落点（Spawn 节点 / 地形高度）仍是脚底语义，继续用 teleport_to / teleport_to_point；
##   * 掉落保护 / 复位看脚底 global_position.y。
## 头部高度与 §11.2 的 11 个盒子同源（见 code/actors/body_hitbox.gd 的 HEAD_OFFSET_Y）。

## 头部中心相对脚底的高度（m）。§11.1 契约值 1.65，与服务端 HEAD_OFFSET_Y 一致，勿改。
const HEAD_OFFSET_Y := 1.65

@export_group("移动 Movement")
@export var walk_speed: float = 3.6
@export var run_speed: float = 6.8
@export var jump_height: float = 1.1
@export var gravity: float = 22.0
@export var ground_acceleration: float = 26.0
@export var air_acceleration: float = 7.0

@export_group("视角 Camera")
@export var mouse_sensitivity: float = 0.0022
@export var touch_sensitivity: float = 0.0035
@export var pitch_limit_degrees: float = 85.0

@export_group("旁观 Spectator")
## 旁观状态下的飞行速度（无实体、不受物理影响）。
@export var spectator_speed: float = 12.0

const MAX_FALL_SPEED := 60.0

@onready var _head: Node3D = $Head

var _pitch_degrees: float = 0.0

## 濒死：原地冻结，等 5 秒窗口内的复活治疗。
var frozen: bool = false
## 旁观：无实体、可自由飞行、不受物理影响（死亡 30 分钟窗口）。
var spectating: bool = false

## 触屏控制层（方案B）。故意不写类型标注：它是自定义脚本，静态类型成 Node 会报找不到成员。
## 仅在移动端 / 触屏设备由 world_base.gd 注入；为 null 时本脚本完全按键鼠走。
var touch_controls = null


func _ready() -> void:
	_apply_pitch(0.0)


func _process(_delta: float) -> void:
	# 触屏转向：取走本帧累计的拖拽增量，按 touch_sensitivity 转成视角。
	if touch_controls != null and touch_controls.has_method("take_look"):
		var look: Vector2 = touch_controls.take_look()
		if look != Vector2.ZERO:
			_apply_look(look.x, look.y, touch_sensitivity)


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion and Input.mouse_mode == Input.MOUSE_MODE_CAPTURED:
		var motion := event as InputEventMouseMotion
		_apply_look(motion.relative.x, motion.relative.y, mouse_sensitivity)


## 视角统一入口：鼠标相对位移与触屏拖拽增量都汇到这里，只是灵敏度不同。
func _apply_look(delta_x: float, delta_y: float, sensitivity: float) -> void:
	rotate_y(-delta_x * sensitivity)
	_apply_pitch(_pitch_degrees - rad_to_deg(delta_y * sensitivity))


func _physics_process(delta: float) -> void:
	# 旁观优先：无实体、不受物理影响，只按输入自由飞行
	if spectating:
		_fly(delta)
		return
	# 濒死：原地冻结（仍然接受视角输入，看起来像倒地）
	if frozen:
		velocity = Vector3.ZERO
		return

	var input_dir := Input.get_vector("move_left", "move_right", "move_forward", "move_back")
	var sprint := Input.is_action_pressed("sprint")
	# 触屏激活时用摇杆轴覆盖键盘（移动平台上没有键盘），推满行程即疾行。
	if touch_controls != null:
		input_dir = touch_controls.move_axis
		if bool(touch_controls.sprint_held):
			sprint = true
	var direction := Vector3(input_dir.x, 0.0, input_dir.y)
	if direction.length_squared() > 1.0:
		direction = direction.normalized()
	direction = transform.basis * direction
	direction.y = 0.0

	var speed := run_speed if sprint else walk_speed
	var target := direction * speed
	var accel := ground_acceleration if is_on_floor() else air_acceleration
	var horizontal := Vector3(velocity.x, 0.0, velocity.z).move_toward(target, accel * delta)
	velocity.x = horizontal.x
	velocity.z = horizontal.z

	if is_on_floor():
		if Input.is_action_just_pressed("jump") or _wants_touch_jump():
			velocity.y = sqrt(2.0 * gravity * jump_height)
	else:
		velocity.y = maxf(velocity.y - gravity * delta, -MAX_FALL_SPEED)

	move_and_slide()


## 旁观飞行：Space 上升、Shift 下降，水平方向沿用移动输入，不落地、不碰撞。
func _fly(delta: float) -> void:
	var input_dir := Input.get_vector("move_left", "move_right", "move_forward", "move_back")
	if touch_controls != null:
		input_dir = touch_controls.move_axis
	var direction := Vector3(input_dir.x, 0.0, input_dir.y)
	if direction.length_squared() > 1.0:
		direction = direction.normalized()
	direction = transform.basis * direction
	direction.y = 0.0

	var vertical := 0.0
	if Input.is_action_pressed("jump") or _wants_touch_jump():
		vertical += 1.0
	if Input.is_action_pressed("sprint"):
		vertical -= 1.0

	global_position += (direction + Vector3.UP * vertical) * spectator_speed * delta
	velocity = Vector3.ZERO


## 濒死冻结开关（服务端判定）。
func set_frozen(enabled: bool) -> void:
	frozen = enabled
	if enabled:
		velocity = Vector3.ZERO


## 旁观开关（服务端判定）。旁观时关闭胶囊碰撞，避免被地形 / 物理卡住。
func set_spectator(enabled: bool) -> void:
	if spectating == enabled:
		return
	spectating = enabled
	velocity = Vector3.ZERO
	frozen = false
	var shape := get_node_or_null("CollisionShape3D")
	if shape is CollisionShape3D:
		(shape as CollisionShape3D).disabled = enabled


## 传送到某个落点：位置取 spawn 的原点，朝向取 spawn 的 -Z 方向，俯仰归零。
## 所以“落点朝哪”完全由场景里 Spawn 节点的旋转决定。
func teleport_to(spawn: Node3D) -> void:
	velocity = Vector3.ZERO
	global_position = spawn.global_position
	var forward := -spawn.global_transform.basis.z
	forward.y = 0.0
	if forward.length_squared() > 0.0001:
		look_at(global_position + forward, Vector3.UP)
	_apply_pitch(0.0)


## 落到“某一个点”，朝向用角度（度）指定 —— 门内世界的随机落点用这个。
## point 是**脚底**（场景内的落点语义）。服务端下发的坐标请用 teleport_head_to()。
func teleport_to_point(point: Vector3, yaw_degrees: float) -> void:
	velocity = Vector3.ZERO
	global_position = point
	rotation = Vector3(0.0, deg_to_rad(yaw_degrees), 0.0)
	_apply_pitch(0.0)


## 按**头部中心**落位（协议 v1.4 / §11.1）：login 回写的 pos_*、peers 每行的 pos_*、
## state 响应里的死亡点，都是头部坐标；脚底 = 头 − HEAD_OFFSET_Y。
func teleport_head_to(point: Vector3, yaw_degrees: float) -> void:
	teleport_to_point(point - Vector3(0.0, HEAD_OFFSET_Y, 0.0), yaw_degrees)


## 头部中心的世界坐标（协议 v1.4 上报用）。俯仰只转 Head 自身，不影响它的位置；
## Head 节点缺失时按“脚底 + HEAD_OFFSET_Y”兜底，保证上报永远是头部基准。
func head_world_position() -> Vector3:
	if _head != null:
		return _head.global_position
	return global_position + Vector3(0.0, HEAD_OFFSET_Y, 0.0)


## 当前水平速度（m/s），给 HUD 用。
func get_horizontal_speed() -> float:
	return Vector2(velocity.x, velocity.z).length()


## 当前朝向角（度）。0 度对应 -Z 方向（= HUD 里的"北"），顺时针为正。
func get_yaw_degrees() -> float:
	return rad_to_deg(rotation.y)


## 瞄准俯仰角（度，protocol.md §4 第 9 段 / §11.3）：**抬头为正**、低头为负、0 = 水平。
##
## 符号约定（务必别搞反，服务端按 `sin(pitch)` 取 Y 分量）：
##   * 协议：`pitch` 是**正数 = 抬头**；
##   * Godot：`_head.rotation.x = deg_to_rad(_pitch_degrees)`，绕 +X 正转会把 −Z 前向抬向 +Y
##     （实测 `Basis(X, +30°) * (0,0,-1) = (0, +0.5, -0.87)`）；`_apply_look` 里鼠标下移让
##     `_pitch_degrees` 变小，也就等于低头。
##   ⇒ `_pitch_degrees` **本身就是「抬头为正」**，与协议同号，这里直接返回，**不要取反**。
func aim_pitch_degrees() -> float:
	return _pitch_degrees


func _apply_pitch(degrees: float) -> void:
	_pitch_degrees = clampf(degrees, -pitch_limit_degrees, pitch_limit_degrees)
	if _head != null:
		_head.rotation.x = deg_to_rad(_pitch_degrees)


## 触屏跳跃请求（落地时才取用，未取用则保留成跳跃缓冲）。
func _wants_touch_jump() -> bool:
	return touch_controls != null and touch_controls.has_method("take_jump") and touch_controls.take_jump()
