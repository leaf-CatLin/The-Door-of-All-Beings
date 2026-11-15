extends CanvasLayer
## 平视显示：穿界白光（由远及近的擦除）、按 H 显示的按键说明、按 F3 显示的调试面板。
## 全部节点的 mouse_filter 都是 IGNORE，绝不抢鼠标事件（否则鼠标视角会被面板吃掉）。
##
## 默认状态是一块干净的画面，没有任何常驻文字：
##   F3 —— 显示 / 隐藏左上角调试面板（帧率、网络延迟、速度、坐标、朝向）
##   H  —— 显示 / 隐藏顶部按键说明
##
## 这层上有两块白：
##   Flash —— 传统的整体白闪（现在是按 R 复位时闪一下）；
##   Wipe  —— 穿界过场：用 white_wipe.gdshader 让白色从画面中心向四周铺开。

## 八个方位名，从正北开始顺时针。正北 = -Z。
const COMPASS_NAMES := ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]

## 读数的三档颜色：正常 / 偏坏 / 很坏。
## 显式给定颜色，而不是“删掉 override”——加了 override 之后不指定颜色是不行的。
const COLOR_NORMAL := Color(0.76, 0.86, 0.96, 0.72)
const COLOR_WARN := Color(1.0, 0.84, 0.42, 0.9)
const COLOR_BAD := Color(1.0, 0.46, 0.42, 0.95)

@onready var _flash: ColorRect = $Flash
@onready var _wipe: ColorRect = $Wipe
@onready var _hint: Label = $HintLabel
@onready var _info_panel: PanelContainer = $InfoPanel
@onready var _fps_label: Label = $InfoPanel/VBox/Fps
@onready var _latency_label: Label = $InfoPanel/VBox/Latency
@onready var _speed_label: Label = $InfoPanel/VBox/Speed
@onready var _coords_label: Label = $InfoPanel/VBox/Coords
@onready var _facing_label: Label = $InfoPanel/VBox/Facing

var _flash_tween: Tween
var _wipe_material: ShaderMaterial
## 按键说明的正文与显隐开关分开存：H 只切开关，不会把文本丢掉。
var _hint_text := ""
var _hint_shown := false

## 常驻状态面板：生命 / 灵力 / 等级 / 击杀 / 死亡（由 world_base 每 0.1 秒喂一次）。
var _life_root: Control
var _life_panel: PanelContainer
var _hp_label: Label
var _level_label: Label
var _spirit_label: Label
## 濒死 / 旁观的中央提示（大字）。
var _death_label: Label


func _ready() -> void:
	_flash.color = Color(1.0, 1.0, 1.0, 0.0)
	_wipe_material = _wipe.material as ShaderMaterial
	set_wipe_progress(0.0)
	set_wipe_fade(0.0)
	# 开场什么都不显示：调试面板与按键说明都先藏起来。
	_info_panel.visible = false
	_hint_shown = false
	_apply_hint()
	_build_life_ui()


## 运行时构建常驻状态面板与中央死亡提示（不改 .tscn，避免和现有布局打架）。
func _build_life_ui() -> void:
	_life_root = Control.new()
	_life_root.name = "LifeRoot"
	_life_root.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	_life_root.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(_life_root)

	var panel := PanelContainer.new()
	panel.name = "LifePanel"
	panel.mouse_filter = Control.MOUSE_FILTER_IGNORE
	panel.position = Vector2(20.0, 20.0)
	panel.add_theme_stylebox_override("panel", _make_panel_style())
	# 门外大厅没有联机层，也不该显示空血条：等第一次 set_life 再露面
	panel.visible = false
	_life_panel = panel
	_life_root.add_child(panel)

	var box := VBoxContainer.new()
	box.mouse_filter = Control.MOUSE_FILTER_IGNORE
	box.add_theme_constant_override("separation", 2)
	panel.add_child(box)

	_hp_label = _make_label(20, COLOR_NORMAL)
	_level_label = _make_label(16, COLOR_NORMAL)
	_spirit_label = _make_label(16, Color(0.72, 0.88, 1.0, 0.85))
	box.add_child(_hp_label)
	box.add_child(_level_label)
	box.add_child(_spirit_label)

	_death_label = _make_label(30, COLOR_BAD)
	_death_label.name = "DeathLabel"
	_death_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_death_label.anchor_left = 0.5
	_death_label.anchor_right = 0.5
	_death_label.offset_left = -420.0
	_death_label.offset_right = 420.0
	_death_label.offset_top = 120.0
	_death_label.offset_bottom = 210.0
	_death_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_death_label.visible = false
	_life_root.add_child(_death_label)


func _make_label(size: int, color: Color) -> Label:
	var label := Label.new()
	label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	label.add_theme_font_size_override("font_size", size)
	label.add_theme_color_override("font_color", color)
	label.add_theme_color_override("font_shadow_color", Color(0.0, 0.0, 0.0, 0.65))
	label.add_theme_constant_override("shadow_offset_x", 1)
	label.add_theme_constant_override("shadow_offset_y", 1)
	return label


func _make_panel_style() -> StyleBoxFlat:
	var style := StyleBoxFlat.new()
	style.bg_color = Color(0.04, 0.06, 0.09, 0.62)
	style.border_color = Color(0.62, 0.78, 0.95, 0.35)
	style.set_border_width_all(1)
	style.set_corner_radius_all(8)
	style.content_margin_left = 14.0
	style.content_margin_right = 14.0
	style.content_margin_top = 8.0
	style.content_margin_bottom = 8.0
	return style


## 生命 / 灵力 / 等级 / 台账 + 濒死与旁观倒计时。world_base 每 0.1 秒刷一次。
func set_life(
	hp: float, hp_max: float, spirit: float, level: int,
	kills: int, deaths: int, dying_remaining: float, spectator_remaining: float,
) -> void:
	if _hp_label == null:
		return
	if _life_panel != null:
		_life_panel.visible = true
	var hp_now := int(ceil(maxf(hp, 0.0)))
	var hp_cap := int(ceil(maxf(hp_max, 1.0)))
	_hp_label.text = "生命 %d / %d" % [hp_now, hp_cap]
	_level_label.text = "等级 %d   击杀 %d   死亡 %d" % [level, kills, deaths]
	_spirit_label.text = "灵力 %d" % int(floor(maxf(spirit, 0.0)))
	if hp_now <= hp_cap * 0.3:
		_hp_label.add_theme_color_override("font_color", COLOR_BAD)
	elif hp_now <= hp_cap * 0.6:
		_hp_label.add_theme_color_override("font_color", COLOR_WARN)
	else:
		_hp_label.add_theme_color_override("font_color", COLOR_NORMAL)

	if dying_remaining > 0.0:
		_death_label.visible = true
		_death_label.add_theme_color_override("font_color", COLOR_BAD)
		_death_label.text = "濒死 · %.1f 秒内需要复活治疗" % dying_remaining
	elif spectator_remaining > 0.0:
		_death_label.visible = true
		_death_label.add_theme_color_override("font_color", COLOR_NORMAL)
		_death_label.text = "旁观中 · 剩余 %s\n灵力与背包将在死亡 30 分钟后清空" % _format_seconds(spectator_remaining)
	else:
		_death_label.visible = false


func _format_seconds(total: float) -> String:
	var seconds := int(ceil(maxf(total, 0.0)))
	return "%02d:%02d" % [seconds / 60, seconds % 60]


func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed("toggle_debug"):
		_info_panel.visible = not _info_panel.visible
		get_viewport().set_input_as_handled()
		return
	if event.is_action_pressed("toggle_help"):
		_hint_shown = not _hint_shown
		_apply_hint()
		get_viewport().set_input_as_handled()


## 设置按键说明的正文（按 H 才看得到；正文为空则无论如何都隐藏）。
func set_hint(text: String) -> void:
	_hint_text = text
	_apply_hint()


## 调试面板当前是否可见。外部可据此决定要不要每帧算坐标。
func is_debug_visible() -> bool:
	return _info_panel.visible


func _apply_hint() -> void:
	if _hint == null:
		return
	_hint.text = _hint_text
	_hint.visible = _hint_shown and _hint_text != ""


## 调试面板：帧率（Engine 给的是上一秒的平均值）。
## 低于 30 标黄、低于 20 标红，一眼就能看出掉帧。
func set_fps(value: int) -> void:
	_fps_label.text = "帧率 %d FPS" % value
	if value < 20:
		_fps_label.add_theme_color_override("font_color", COLOR_BAD)
	elif value < 30:
		_fps_label.add_theme_color_override("font_color", COLOR_WARN)
	else:
		_fps_label.add_theme_color_override("font_color", COLOR_NORMAL)


## 调试面板：网络往返耗时（毫秒）。
## 传 -1（未连接 / 还没测到）时显示一个破折号，不要把 -1ms 这种数字给玩家看。
func set_latency(milliseconds: float) -> void:
	if milliseconds < 0.0:
		_latency_label.text = "延迟 —（未连接）"
		_latency_label.add_theme_color_override("font_color", COLOR_NORMAL)
		return
	_latency_label.text = "延迟 %.0f ms" % milliseconds
	if milliseconds > 200.0:
		_latency_label.add_theme_color_override("font_color", COLOR_BAD)
	elif milliseconds > 80.0:
		_latency_label.add_theme_color_override("font_color", COLOR_WARN)
	else:
		_latency_label.add_theme_color_override("font_color", COLOR_NORMAL)


## 调试面板：水平速度（m/s）。
func set_speed(value: float) -> void:
	_speed_label.text = "速度 %.1f m/s" % value


## 调试面板：世界坐标。
func set_coords(position: Vector3) -> void:
	_coords_label.text = "坐标 X %.1f   Y %.1f   Z %.1f" % [position.x, position.y, position.z]


## 调试面板：面对方向（方位 + 方位角，0° = 正北 = -Z）。
func set_facing(yaw_degrees: float) -> void:
	var bearing := fposmod(-yaw_degrees, 360.0)
	var index := int(floor((bearing + 22.5) / 45.0)) % COMPASS_NAMES.size()
	_facing_label.text = "朝向 %s（%.0f°）" % [COMPASS_NAMES[index], bearing]


## 穿界白光：progress 从 0 涨到 1.6。
## 白色先从画面中心（= 远处的光柱 / 地平线）出现，再向四周扩散，直到盖满整屏。
func set_wipe_progress(value: float) -> void:
	if _wipe_material != null:
		_wipe_material.set_shader_parameter("progress", value)


## 盖满之后的整体淡出：1 = 全白，0 = 完全透明（此时玩家已经在门内世界了）。
func set_wipe_fade(value: float) -> void:
	if _wipe_material != null:
		_wipe_material.set_shader_parameter("fade", value)


## 复位时的白光闪一下：先把白色推到全不透明，再 tween 回透明。
func flash_white(duration: float = 0.8) -> void:
	if _flash_tween != null and _flash_tween.is_valid():
		_flash_tween.kill()
	_flash.color = Color(1.0, 1.0, 1.0, 1.0)
	_flash_tween = create_tween()
	_flash_tween.tween_property(_flash, "color:a", 0.0, duration)

