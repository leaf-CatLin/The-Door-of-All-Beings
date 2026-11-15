extends Control
## 种族选择页。
##
## 种族清单**由服务端决定**（`races` 动作）：本页不再硬编码 human / elf / …，
## 而是连上 socket 服务端、拉取可用列表后现场渲染按钮；开发阶段服务端只下发人类。
## 选中的 key 写入本机 race.txt，注册时随 payload 交给服务端（服务端仍会复核）。
## 服务端不可达时退回开发默认种族，避免离线开发卡在这一页。

const TEMP_MAIN_SCENE = preload("res://Scenes/outer/lobby.tscn")
const GameApiScript := preload("res://code/net/game_api.gd")
const AccountStore := preload("res://code/StartCreen/account_store.gd")

## 连不上服务器时的兜底等待（秒），到点自动用开发默认种族继续。
const OFFLINE_FALLBACK_DELAY := 2.5
## 选中后停顿一下再进主场景（让玩家看到确认反馈）。
const CONFIRM_DELAY := 0.7

var _api = null
var _status: Label
var _list: VBoxContainer
var _chosen := false


func _ready() -> void:
	# 旧占位文字直接藏掉，本页 UI 由代码构建（不改 .tscn）
	var placeholder := get_node_or_null("Label")
	if placeholder is Label:
		(placeholder as Label).visible = false
	_build_ui()
	_load_races()


func _build_ui() -> void:
	var root := VBoxContainer.new()
	root.name = "RaceRoot"
	root.alignment = BoxContainer.ALIGNMENT_CENTER
	root.add_theme_constant_override("separation", 18)
	root.anchor_left = 0.5
	root.anchor_right = 0.5
	root.anchor_top = 0.5
	root.anchor_bottom = 0.5
	root.offset_left = -240.0
	root.offset_right = 240.0
	root.offset_top = -180.0
	root.offset_bottom = 180.0
	add_child(root)

	var title := _make_label("选择种族", 34, Color(0.92, 0.96, 1.0, 0.95))
	title.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	root.add_child(title)

	var subtitle := _make_label("可用种族由服务器下发（开发阶段仅人类）", 15, Color(0.66, 0.74, 0.84, 0.8))
	subtitle.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	root.add_child(subtitle)

	_list = VBoxContainer.new()
	_list.add_theme_constant_override("separation", 10)
	root.add_child(_list)

	_status = _make_label("正在读取服务器种族列表…", 15, Color(0.66, 0.74, 0.84, 0.8))
	_status.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	root.add_child(_status)


func _make_label(text: String, size: int, color: Color) -> Label:
	var label := Label.new()
	label.text = text
	label.add_theme_font_size_override("font_size", size)
	label.add_theme_color_override("font_color", color)
	return label


## 拉取服务端种族清单；失败则走离线兜底。
func _load_races() -> void:
	_api = GameApiScript.new()
	_api.name = "GameApi"
	add_child(_api)

	var reachable: bool = await _api.connect_and_hello()
	if not bool(reachable):
		_offline_fallback("无法连接服务器，%0.1f 秒后使用开发默认种族" % OFFLINE_FALLBACK_DELAY)
		return
	await _api.ensure_device()

	var res: Dictionary = await _api.fetch_races()
	if not bool(res.get("ok", false)):
		_offline_fallback("种族列表读取失败：%s" % str(res.get("code", "")))
		return
	_render_races(_api.races)


func _render_races(races: Array) -> void:
	if races.is_empty():
		_offline_fallback("服务器没有下发任何可用种族")
		return

	_status.text = "服务器下发 %d 个可用种族" % races.size()
	for entry in races:
		if typeof(entry) != TYPE_DICTIONARY:
			continue
		var row: Dictionary = entry
		var key := str(row.get("key", "")).strip_edges()
		var display := str(row.get("name", key))
		if key == "":
			continue
		var button := Button.new()
		var label := display
		if bool(row.get("dev_only", false)):
			label += "（开发阶段）"
		button.text = label
		button.custom_minimum_size = Vector2(0.0, 58.0)
		button.add_theme_font_size_override("font_size", 20)
		button.pressed.connect(_select.bind(key, display))
		_list.add_child(button)


func _offline_fallback(reason: String) -> void:
	_status.text = "%s · 离线开发模式" % reason
	push_warning(reason)
	await get_tree().create_timer(OFFLINE_FALLBACK_DELAY).timeout
	if _chosen:
		return
	_select(AccountStore.DEFAULT_RACE, AccountStore.DEFAULT_RACE)


func _select(race_key: String, display_name: String) -> void:
	if _chosen:
		return
	_chosen = true
	AccountStore.write_race(race_key)
	_status.text = "已选择：%s" % display_name
	for child in _list.get_children():
		if child is Button:
			(child as Button).disabled = true
	await get_tree().create_timer(CONFIRM_DELAY).timeout
	get_tree().change_scene_to_packed(TEMP_MAIN_SCENE)
