extends Node3D
## 人体碰撞箱（服务端契约 §11.2）的客户端可视化 + 数值对表。
##
## 契约来源：server-socket/protocol.md **§11.2 人体模型：11 个盒子**（冻结契约）。
## 单位米，**相对头部中心**；+Y 向上，−Z 为角色正面，绕 Y 轴随 yaw 旋转；
## `l` = 角色左手侧（+X），`r` = 右手侧（−X）。下面的 PARTS 必须与表格**逐字一致**，
## 增删字段 / 改一个数值都要先改契约，再改这里。
##
## 挂法（本节点原点必须落在**头部中心**）：
##   * 本机 `Scenes/outer/player_fp.tscn`：挂在角色根下 `(0, HEAD_OFFSET_Y, 0)`。
##     根原点在脚底，所以这里正好是头部；挂根不挂 Head，才能只跟 yaw 转、不跟俯仰摇。
##   * 远端代理 `Scenes/actors/player.tscn`：同样 `(0, HEAD_OFFSET_Y, 0)`，
##     并打开 `faint`（线框骨架已经很花，盒子用更淡的材质）。
##
## 命中判定**永远是服务端权威**（§11.3：原点 = 攻击者头部、60° 攻击扇形、3m 射程、
## slab 测试取 t 最小且 ≤ 射程的盒子），本脚本不做任何判定，只负责：
##   1. 把 11 个盒子的中心 / 半尺寸 / 倍率集中一处，方便与服务端逐字对表；
##   2. F4 时把盒子画出来（BoxMesh 半透明体积 + 棱线），让玩家看得见服务端在打哪儿。
##
## F4（`toggle_hitbox`）默认全关：每个实例各自响应同一次按键，所以本机与全部远端代理
## 会一起开合；这里**不**吃输入事件（不 set_input_as_handled），免得别的节点收不到。

## 头部中心相对脚底的高度（m）。§11.1 契约值，与服务端 HEAD_OFFSET_Y 一致，勿改。
const HEAD_OFFSET_Y := 1.65

## 近战基础伤害（§11.2）：实际伤害 = ATTACK_DAMAGE × 倍率，向下取整到 0.1。
## 客户端不参与结算，这里只作为对表 / 提示文案的参照值。
const ATTACK_DAMAGE := 25.0

## 开关动作名（project.godot 里登记为 F4）。
const TOGGLE_ACTION := &"toggle_hitbox"

## 11 个部位：字段名与 §11.2 表格逐字一致。
## part 命名固定 `_l` / `_r`（角色左手侧 / 右手侧），禁止出现 left/right 混写。
const PARTS := [
	{"part": "head", "center": Vector3(0.0, 0.0, 0.0), "half": Vector3(0.11, 0.10, 0.11), "multiplier": 2.0},
	{"part": "neck", "center": Vector3(0.0, -0.15, 0.0), "half": Vector3(0.06, 0.05, 0.06), "multiplier": 1.5},
	{"part": "torso", "center": Vector3(0.0, -0.45, 0.0), "half": Vector3(0.18, 0.25, 0.11), "multiplier": 1.0},
	{"part": "upper_arm_l", "center": Vector3(0.22, -0.40, 0.0), "half": Vector3(0.05, 0.15, 0.05), "multiplier": 0.6},
	{"part": "upper_arm_r", "center": Vector3(-0.22, -0.40, 0.0), "half": Vector3(0.05, 0.15, 0.05), "multiplier": 0.6},
	{"part": "forearm_l", "center": Vector3(0.26, -0.68, 0.0), "half": Vector3(0.045, 0.14, 0.045), "multiplier": 0.5},
	{"part": "forearm_r", "center": Vector3(-0.26, -0.68, 0.0), "half": Vector3(0.045, 0.14, 0.045), "multiplier": 0.5},
	{"part": "thigh_l", "center": Vector3(0.09, -0.92, 0.0), "half": Vector3(0.07, 0.215, 0.07), "multiplier": 0.8},
	{"part": "thigh_r", "center": Vector3(-0.09, -0.92, 0.0), "half": Vector3(0.07, 0.215, 0.07), "multiplier": 0.8},
	{"part": "shin_l", "center": Vector3(0.09, -1.35, 0.0), "half": Vector3(0.055, 0.22, 0.055), "multiplier": 0.6},
	{"part": "shin_r", "center": Vector3(-0.09, -1.35, 0.0), "half": Vector3(0.055, 0.22, 0.055), "multiplier": 0.6},
]

## 头部 / 颈部（倍率 ≥ 1.5 的弱点）用暖色，其它部位用冷色，一眼分出该往哪打。
const WEAK_COLOR := Color(1.0, 0.44, 0.38, 1.0)
const BODY_COLOR := Color(0.36, 0.9, 1.0, 1.0)
## 半透明体积的不透明度（远端代理乘 0.5 的叫“更淡”）。
const FILL_ALPHA := 0.14
## 棱线的不透明度（同上）。
const LINE_ALPHA := 0.95

## 远端代理用更淡的材质（.tscn 里给 true）。
@export var faint := false
## 进场景就显示（默认关：F4 才亮）。
@export var visible_on_start := false

var _shown := false
var _built := false


func _ready() -> void:
	_build()
	set_shown(visible_on_start)


func _unhandled_input(event: InputEvent) -> void:
	if not _is_toggle_event(event):
		return
	set_shown(not _shown)
	# 故意不 set_input_as_handled：远端代理各有一份盒子，要让它们都收到同一次按键。


## 这一次输入是不是“开 / 关盒子”。优先走 InputMap 动作，动作丢了再按物理键兜底
## （project.godot 里登记的是 F4）。
func _is_toggle_event(event: InputEvent) -> bool:
	if InputMap.has_action(TOGGLE_ACTION) and event.is_action_pressed(TOGGLE_ACTION):
		return true
	if event is InputEventKey:
		var key := event as InputEventKey
		return key.pressed and not key.echo and key.physical_keycode == KEY_F4
	return false


## 盒子显示开关（F4 / 外部调用）。
func set_shown(value: bool) -> void:
	_shown = value
	visible = value


func is_shown() -> bool:
	return _shown


## 11 个部位的 part 名（顺序同契约表格），给调试 / 对表用。
static func part_names() -> PackedStringArray:
	var names := PackedStringArray()
	for entry in PARTS:
		names.append(str(entry["part"]))
	return names


## 按契约算出某个部位的世界坐标中心（盒子随本节点 yaw 转，不跟俯仰）。
func part_center(part_name: String) -> Vector3:
	for entry in PARTS:
		var part: Dictionary = entry
		if str(part["part"]) == part_name:
			var center: Vector3 = part["center"]
			return global_transform * center
	return global_position


# ---------------------------------------------------------------- 构建

func _build() -> void:
	if _built:
		return
	_built = true

	var weak_fill := _make_fill_material(WEAK_COLOR)
	var body_fill := _make_fill_material(BODY_COLOR)
	var weak_edges := ImmediateMesh.new()
	var body_edges := ImmediateMesh.new()
	weak_edges.surface_begin(Mesh.PRIMITIVE_LINES, _make_line_material(WEAK_COLOR))
	body_edges.surface_begin(Mesh.PRIMITIVE_LINES, _make_line_material(BODY_COLOR))

	for entry in PARTS:
		var part: Dictionary = entry
		var part_name := str(part["part"])
		var center: Vector3 = part["center"]
		var half: Vector3 = part["half"]
		var weak := float(part["multiplier"]) >= 1.5

		# 半透明体积：BoxMesh 的 size 是**全尺寸**，契约给的是半尺寸，所以乘 2。
		var box := BoxMesh.new()
		box.size = half * 2.0
		var solid := MeshInstance3D.new()
		solid.name = "Box_" + part_name
		solid.mesh = box
		solid.material_override = weak_fill if weak else body_fill
		solid.position = center
		solid.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
		add_child(solid)

		_add_box_edges(weak_edges if weak else body_edges, center, half)

	weak_edges.surface_end()
	body_edges.surface_end()
	_add_edge_instance("HitboxEdgesWeak", weak_edges)
	_add_edge_instance("HitboxEdgesBody", body_edges)


## 12 条棱：8 个角两两相连，只连“只差一个符号位”的两角。
func _add_box_edges(mesh: ImmediateMesh, center: Vector3, half: Vector3) -> void:
	var corners: Array[Vector3] = []
	for sx in [-1.0, 1.0]:
		for sy in [-1.0, 1.0]:
			for sz in [-1.0, 1.0]:
				corners.append(center + Vector3(half.x * float(sx), half.y * float(sy), half.z * float(sz)))
	for edge in [[0, 1], [2, 3], [4, 5], [6, 7], [0, 2], [1, 3], [4, 6], [5, 7], [0, 4], [1, 5], [2, 6], [3, 7]]:
		mesh.surface_add_vertex(corners[int(edge[0])])
		mesh.surface_add_vertex(corners[int(edge[1])])


func _add_edge_instance(node_name: String, mesh: ImmediateMesh) -> void:
	var node := MeshInstance3D.new()
	node.name = node_name
	node.mesh = mesh
	node.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	# ImmediateMesh 的 AABB 不会自动更新，给点余量免得被视锥裁掉（同 player_rig 的取法）。
	node.extra_cull_margin = 2.0
	add_child(node)


func _make_fill_material(base: Color) -> StandardMaterial3D:
	var mat := StandardMaterial3D.new()
	mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	mat.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	mat.cull_mode = BaseMaterial3D.CULL_DISABLED
	var alpha := FILL_ALPHA * (0.5 if faint else 1.0)
	mat.albedo_color = Color(base.r, base.g, base.b, alpha)
	return mat


func _make_line_material(base: Color) -> StandardMaterial3D:
	var mat := StandardMaterial3D.new()
	mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	mat.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	mat.cull_mode = BaseMaterial3D.CULL_DISABLED
	var alpha := LINE_ALPHA * (0.55 if faint else 1.0)
	mat.albedo_color = Color(base.r, base.g, base.b, alpha)
	return mat
