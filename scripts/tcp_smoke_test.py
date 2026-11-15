"""TCP 协议冒烟测试（v1.4：联合哈希 + 批量多 action + 部位命中 + 俯仰瞄准 + 红名 + 坐标基准）。

用法::

    uv run scripts/tcp_smoke_test.py
    uv run scripts/tcp_smoke_test.py -v      # 打印每条用例

它做的事：

1. 用 ``DB_BACKEND=memory`` 拉起一整套 Service（TCP 监听 127.0.0.1:0 随机端口）；
2. 用原始 socket + `protocol.build_request/parse_response` 说完整链路；
3. 覆盖：hello 下发 Token / 握手 / 联合哈希篡改（改头部即失效）/ v1.2 向后兼容 /
   批量多 action（一次请求多个动作，按序多帧返回）/ 设备签发与确认 / 名称查重 /
   注册 / 登录（含 file_hash）/ 重放 / 时间戳过期 / 未知动作 / 设备伪造 /
   拉取式 peers & world（不再有 10Hz PUSH）/ 双客户端同屏 / 正常下线完整拆除 /
   意外断开宽限期与超时销毁（无 leave 广播，靠 peers 拉取感知）/
   **v1.4 新增**：身体碰撞箱部位命中（头部 50 / 躯干 25）/ MISS / NOT_FACING /
   OUT_OF_RANGE / 真死亡后击杀者变红 / 死亡清红名 / legacy(0.1.0) 脚底坐标换算 /
   coord_base='feet' 旧行的一次性迁移。
   **4.1.1 新增**：`state` 第 9 段 `pitch` 的俯仰瞄准（平射爆头 / 低头打躯干 / 抬头 MISS /
   缺省与非法值等价于 0）。

退出码 0 = 全绿。
"""

from __future__ import annotations

import argparse
import base64
import os
import pathlib
import re
import socket
import sys
import tempfile
import time
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Windows 控制台默认 GBK，→ / ✓ 这类字符会直接抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# 必须在 import config 之前，否则 .env 里的 DB_BACKEND=postgres 会盖掉它
os.environ["DB_BACKEND"] = "memory"

import config  # noqa: E402
import game  # noqa: E402
import protocol  # noqa: E402

USER_ID_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")


# ---------------------------------------------------------------------------
# 测试框架（极小）
# ---------------------------------------------------------------------------
class Runner:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.passed = 0
        self.failed: list[tuple[str, str]] = []
        self._current = ""

    def case(self, name: str) -> None:
        self._current = name
        if self.verbose:
            print(f"\n[case] {name}")

    def check(self, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            if self.verbose:
                print(f"  ✓ {detail or self._current}")
            return True
        self.failed.append((self._current, detail or self._current))
        print(f"  ✗ {self._current} :: {detail}")
        return False

    def eq(self, actual, expected, detail: str = "") -> bool:
        return self.check(
            actual == expected,
            f"{detail} expect={expected!r} got={actual!r}",
        )

    def summary(self) -> int:
        total = self.passed + len(self.failed)
        print(f"\n{'=' * 60}")
        print(f"PASSED {self.passed}/{total}    FAILED {len(self.failed)}")
        for case, detail in self.failed:
            print(f"  ✗ {case} :: {detail}")
        print("=" * 60)
        return 0 if not self.failed else 1


# ---------------------------------------------------------------------------
# TCP 客户端
# ---------------------------------------------------------------------------
class Client:
    """极简客户端：一次请求 / 一条响应。

    v1.3：服务端建连后**不再推送任何帧**，会话 Token 随首个 ``hello`` 响应下发；
    因此构造时不做任何等待，``hello()`` 会把响应里的 ``session_token`` 记下来，
    后续请求自动携带（想测匿名可用 ``call(..., session_token="")``）。

    ``client_version`` 决定服务端按哪种坐标基准理解本连接（protocol.md §11.1）：
    默认 ``0.2.0``（头部基准）；测旧客户端时传 ``0.1.0``。
    """

    def __init__(self, host: str, port: int, client_version: str = "0.2.0") -> None:
        self.sock = socket.create_connection((host, port), timeout=5.0)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._buf = b""
        self.device_id = ""
        self.user_id = ""
        self.token = ""
        self.client_version = client_version

    # -- 低层 ---------------------------------------------------------
    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read_line(self, timeout: float = 5.0) -> str | None:
        deadline = time.time() + timeout
        while True:
            index = self._buf.find(b"\n")
            if index >= 0:
                line = self._buf[:index]
                self._buf = self._buf[index + 1 :]
                return line.decode("utf-8", "replace")
            left = deadline - time.time()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return None
            if not chunk:
                return None
            self._buf += chunk

    def read_frame(self, timeout: float = 5.0) -> protocol.Response | None:
        """读下一条响应帧（v1.3 服务端不再发 PUSH）。"""
        while True:
            line = self.read_line(timeout)
            if line is None:
                return None
            resp = protocol.parse_response(line)
            if resp.kind == "PUSH":      # 防御性：万一旧服务端还推帧就跳过
                continue
            return resp

    def read_frames(self, n: int, timeout: float = 5.0) -> list[protocol.Response]:
        """批量请求：按序读 n 条响应帧。"""
        out: list[protocol.Response] = []
        for _ in range(n):
            resp = self.read_frame(timeout)
            if resp is None:
                break
            out.append(resp)
        return out

    # -- 高层 ---------------------------------------------------------
    def call(
        self,
        action: str,
        data: str = "",
        *,
        device: str | None = None,
        user: str | None = None,
        raw_device_id: str | None = None,
        session_token: str | None = None,
        **kw,
    ) -> protocol.Response | None:
        """发一条请求并等响应。device/user/token 默认沿用本会话已记住的值。"""
        dev = self.device_id if device is None else device
        usr = self.user_id if user is None else user
        tok = self.token if session_token is None else session_token
        if raw_device_id is not None:
            dev = raw_device_id
        kw.setdefault("client_version", self.client_version)
        packet = protocol.build_request(action, data, dev, usr, session_token=tok, **kw)
        self.send_raw(packet)
        return self.read_frame()

    def call_batch(
        self,
        calls: list[tuple[str, str]],
        *,
        device: str | None = None,
        user: str | None = None,
        session_token: str | None = None,
        **kw,
    ) -> list[protocol.Response]:
        """一条报文批量携带多个动作，按序返回多个响应帧。"""
        dev = self.device_id if device is None else device
        usr = self.user_id if user is None else user
        tok = self.token if session_token is None else session_token
        kw.setdefault("client_version", self.client_version)
        packet = protocol.build_request(
            "", "", dev, usr, session_token=tok, calls=calls, **kw
        )
        self.send_raw(packet)
        return self.read_frames(len(calls))

    def hello(self) -> protocol.Response | None:
        """握手并采纳服务端下发的 session_token。"""
        resp = self.call("hello")
        if resp is not None and resp.ok:
            self.token = str(resp.data.get("session_token") or "") or self.token
        return resp

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(verbose: bool = False) -> int:
    import dataclasses

    import main as main_mod

    t = Runner(verbose)

    # assets 用例用临时目录，避免依赖真实服务器路径
    assets_dir = pathlib.Path(tempfile.mkdtemp(prefix="tdob_assets_"))
    (assets_dir / "hello.txt").write_bytes(b"hello-assets")
    (assets_dir / "sub").mkdir()
    (assets_dir / "sub" / "a.bin").write_bytes(b"A" * 5000)

    cfg = dataclasses.replace(
        config.load_config(),
        db_backend="memory",
        tcp_bind="127.0.0.1",
        tcp_port=0,
        autosave_interval=3600,
        grace_seconds=2,      # 宽限期缩到 2s，超时销毁的用例才能跑得快
        device_id_secret="",  # 内存后端允许临时密钥
        assets_dir=str(assets_dir),
        asset_chunk_bytes=4096,
    )
    service = main_mod.Service(cfg)
    # 战斗 / 生死流程默认要等 5s + 30min，测试里压缩成秒级
    game.ATTACK_COOLDOWN = 0.05
    game.DYING_SECONDS = 1.0
    game.SPECTATOR_SECONDS = 2.0
    # 四阶段启动（与 python main.py serve 完全同一条路径）
    service.s1_init()
    service.s2_warmup()
    service.s3_start()
    host, port = service.server.bind()  # 幂等，只为拿到真实端口（cfg 里是 0）
    print(f"服务端已启动 tcp://{host}:{port}（backend=memory, v1.4 无推送）")

    a = Client(host, port)
    b = Client(host, port)
    extra: list[Client] = []      # 中途新开的连接，统一在 finally 里关
    try:
        # ------------------------------------------------------------------
        t.case("v1.3 建连后服务端不推送，hello 下发会话 Token")
        r = a.hello()
        t.check(r is not None and r.ok, "hello 应 OK")
        t.check(len(a.token) == 32, f"token 应为 32 位 hex，实得 {a.token!r}")
        if r is not None and r.ok:
            t.eq(r.data.get("protocol_version"), protocol.CURRENT_VERSION, "协议版本")
            t.eq(r.data.get("server_push"), False, "server_push 应为 False")
            t.eq(r.data.get("push_hz"), 0, "push_hz 应为 0（无主动推送）")
            actions = r.data.get("actions") or []
            for wanted in ("device", "check_name", "register", "login", "logout",
                           "state", "peers", "world", "item",
                           "races", "attack", "revive", "assets"):
                t.check(wanted in actions, f"动作表包含 {wanted}")
        r = b.hello()
        t.check(a.token != b.token, "两条连接的 token 应不同")

        t.case("v1.4 hello：坐标基准 / 碰撞箱 / 朝向锥")
        r = a.hello()
        t.check(r is not None and r.ok, "hello 应 OK")
        if r is not None and r.ok:
            t.eq(r.data.get("protocol_version"), "1.4", "协议版本升到 1.4")
            t.eq(r.data.get("server_version"), "4.1.1", "服务版本 4.1.1")
            t.eq(r.data.get("coord_base"), "head", "协议坐标基准 = 头部")
            t.eq(r.data.get("coord_base_applied"), "head", "0.2.0 客户端实际按 head")
            t.eq(r.data.get("hitboxes"), True, "服务端启用身体碰撞箱")
            t.eq(r.data.get("attack_cone_deg"), 60, "攻击朝向锥 60°")
            t.eq(r.data.get("aim_pitch"), True, "能力位 aim_pitch（state 第 9 段俯仰瞄准）")

        t.case("联合哈希：篡改请求头会让哈希失效")
        pkt = protocol.build_request("ping", session_token=a.token)
        head, body1, body2, tail = pkt.split(b"\n")[:4]
        tampered_head = head + b" "       # 只在头部尾随一个空格，正文/尾部原样
        a.send_raw(b"\n".join([tampered_head, body1, body2, tail]) + b"\n")
        r = a.read_frame()
        t.check(r is not None and r.kind == "FAIL", "应返回 FAIL")
        if r is not None:
            t.eq(r.code, protocol.E_BAD_PLAIN_HASH, "改头部应破坏联合哈希")

        t.case("向后兼容：v1.2 报文（仅正文哈希）仍被接受")
        old = protocol.build_request("ping", version="1.2", session_token="")
        a.send_raw(old)
        r = a.read_frame()
        t.check(r is not None and r.ok, "v1.2 ping 应 OK（未被联合哈希规则误杀）")

        t.case("带错 token 的请求被拒（v1.3 不再回推 reset_token）")
        r = a.call("ping", session_token="deadbeef" + a.token[8:])
        t.check(r is not None and r.kind == "FAIL", "应返回 FAIL")
        if r is not None:
            t.eq(r.code, protocol.E_TOKEN_EXPIRED, "错误码")

        t.case("批量多 action：一条请求两个动作，按序两帧返回")
        frames = a.call_batch([("ping", ""), ("whoami", "")])
        t.eq(len(frames), 2, "应收到两帧")
        if len(frames) == 2:
            t.eq(frames[0].action, "ping", "第一帧对应 ping")
            t.check(frames[0].ok, "第一帧应 OK")
            t.eq(frames[1].action, "whoami", "第二帧对应 whoami")
            t.check(frames[1].ok, "第二帧应 OK")

        t.case("批量含未知动作：逐帧定位失败，前帧不受影响")
        frames = a.call_batch([("ping", ""), ("dance", "")])
        t.eq(len(frames), 2, "应收到两帧")
        if len(frames) == 2:
            t.check(frames[0].ok, "ping 仍 OK")
            t.eq(frames[1].code, protocol.E_UNKNOWN_ACTION, "dance 应 UNKNOWN_ACTION")

        t.case("协议级错误：坏 magic")
        a.send_raw(b"EvilMagic | 1.3 | 0 | none\nx | \ny | \nz | z\n")
        r = a.read_frame()
        t.check(r is not None and r.kind == "FAIL", "应返回 FAIL")
        if r is not None:
            t.eq(r.code, protocol.E_BAD_MAGIC, "错误码")

        t.case("协议级错误：明文哈希不对")
        bad = protocol.build_request("ping", session_token=a.token)
        lines = bad.split(b"\n")
        lines[3] = b"0" * 64 + b" | " + b"0" * 64
        a.send_raw(b"\n".join(lines))
        r = a.read_frame()
        t.check(r is not None and r.kind == "FAIL", "应返回 FAIL")
        if r is not None:
            t.eq(r.code, protocol.E_BAD_PLAIN_HASH, "错误码")

        t.case("协议级错误：时间戳过期")
        a.send_raw(protocol.build_request("ping", session_token=a.token,
                                          ts=int(time.time()) - 10000))
        r = a.read_frame()
        t.check(r is not None and r.kind == "FAIL", "应返回 FAIL")
        if r is not None:
            t.eq(r.code, protocol.E_REQUEST_EXPIRED, "错误码")

        t.case("ping 心跳")
        r = a.call("ping", device="", user="")
        t.check(r is not None and r.ok, "ping 应 OK")

        # ------------------------------------------------------------------
        t.case("device 签发新设备 ID")
        r = a.call("device", "", device="", user="")
        t.check(r is not None and r.ok, "应 OK")
        if r is not None and r.ok:
            dev = r.data.get("device_id", "")
            t.eq(len(dev), 2048, "1024 字节 → 2048 hex 字符")
            t.eq(r.data.get("known"), False, "known 应为 False")
            t.eq(r.data.get("account"), None, "新设备没有账号")
            a.device_id = dev

        t.case("device 确认既有设备（不重新签发）")
        r = a.call("device")
        t.check(r is not None and r.ok, "应 OK")
        if r is not None and r.ok:
            t.eq(r.data.get("device_id"), a.device_id, "应原样返回同一个 ID")
            t.eq(r.data.get("known"), True, "known 应为 True")
            t.eq(r.data.get("account"), None, "还没注册，account 应为 null")

        t.case("设备伪造被拒")
        tampered = bytearray(bytes.fromhex(a.device_id))
        tampered[-1] ^= 0xFF
        r = a.call("whoami", raw_device_id=tampered.hex(), user="")
        t.check(r is not None and r.kind == "FAIL", "应返回 FAIL")
        if r is not None:
            t.eq(r.code, protocol.E_BAD_DEVICE_ID, "错误码")

        t.case("未登录调 state 被拒")
        r = a.call("state", "0&0&0&0")
        t.check(r is not None and r.kind == "FAIL", "应返回 FAIL")
        if r is not None:
            t.eq(r.code, protocol.E_NOT_AUTHENTICATED, "错误码")

        # ------------------------------------------------------------------
        t.case("check_name 重名检测")
        r = a.call("check_name", "勇者")
        t.check(r is not None and r.ok, "应 OK")
        if r is not None and r.ok:
            t.eq(r.data.get("available"), True, "还未注册，应用")

        t.case("register 注册（带客户端 player_id UUID）")
        r = a.call("register", "勇者&human&11111111-2222-3333-4444-555555555555")
        t.check(r is not None and r.ok, "应 OK")
        if r is not None and r.ok:
            t.eq(r.data.get("user_name"), "勇者", "用户名")
            t.eq(r.data.get("player_id"), "11111111-2222-3333-4444-555555555555",
                 "回显客户端生成的 player_id")
            uid = r.data.get("user_id", "")
            t.check(bool(USER_ID_RE.match(uid)), f"user_id 格式 {uid!r}")
            a.user_id = uid

        t.case("check_name 注册后变重名")
        r = a.call("check_name", "勇者")
        t.check(r is not None and r.ok, "应 OK")
        if r is not None and r.ok:
            t.eq(r.data.get("available"), False, "已注册应不可用")
            t.eq(r.data.get("reason"), "taken", "不可用原因")
        r = a.call("check_name", "bad&name")
        t.check(r is not None and r.ok, "应 OK")
        if r is not None and r.ok:
            t.eq(r.data.get("reason"), "invalid", "非法字符算 invalid")

        t.case("register 时设备才入库")
        r = a.call("device")
        t.check(r is not None and r.ok, "device 应 OK")
        if r is not None and r.ok:
            t.eq((r.data or {}).get("stored"), True, "register 后设备已在库里")

        t.case("重放同一报文被去重")
        packet = protocol.build_request("register", "另一个名字", a.device_id, "",
                                        session_token=a.token)
        a.send_raw(packet)
        r1 = a.read_frame()
        a.send_raw(packet)
        r2 = a.read_frame()
        t.check(r2 is not None and r2.kind == "FAIL", "第二条应被拒")
        if r2 is not None:
            t.eq(r2.code, protocol.E_DUPLICATE, "错误码")
        t.check(r1 is not None, "第一条应有响应")

        t.case("whoami 已认证")
        r = a.call("whoami")
        t.check(r is not None and r.ok, "应 OK")
        if r is not None and r.ok:
            t.eq(r.data.get("authenticated"), True, "authenticated")
            t.eq(r.data.get("user_id"), a.user_id, "user_id")
            t.eq(r.data.get("device_id_len"), 1024, "设备 ID 字节数")

        t.case("state 上报位置")
        r = a.call("state", "12.5&0&-8&90&80&moving")
        t.check(r is not None and r.ok, "应 OK")

        t.case("peers 拉取看到自己（替代旧 PUSH world）")
        r = a.call("peers")
        t.check(r is not None and r.ok, "应 OK")
        if r is not None and r.ok:
            players = r.data.get("players") or []
            me = [p for p in players if p.get("user_id") == a.user_id]
            t.eq(len(me), 1, "在线列表里的自己")
            if me:
                t.eq(me[0].get("pos_x"), 12.5, "pos_x")
                t.eq(me[0].get("state"), "moving", "state")

        t.case("item 与世界快照（拉取式 world）")
        r = a.call("item", "door_01&door&0&0&-8&90&idle")
        t.check(r is not None and r.ok, "item 应 OK")
        r = a.call("world")
        t.check(r is not None and r.ok, "world 应 OK")
        if r is not None and r.ok:
            items = r.data.get("items") or []
            t.eq(len(items), 1, "世界物品数")
            if items:
                t.eq(items[0].get("id"), "door_01", "物品 id")

        t.case("服务端不再主动推送任何帧")
        # 读一条请求的响应后，短时间内不应再凭空收到任何帧
        r = a.call("ping")
        t.check(r is not None and r.ok, "ping 应 OK")
        idle = a.read_frame(timeout=0.6)
        t.check(idle is None, "请求响应之后不应再收到服务端主动推的帧")

        # ------------------------------------------------------------------
        t.case("第二个客户端：重名被拒 + 同屏")
        r = b.call("device", "")
        b.device_id = (r.data or {}).get("device_id", "") if r is not None else ""
        t.check(len(b.device_id) == 2048, "B 也拿到设备 ID")
        r = b.call("register", "勇者")
        t.check(r is not None and r.kind == "FAIL", "重名应失败")
        if r is not None:
            t.eq(r.code, protocol.E_NAME_TAKEN, "错误码")
        r = b.call("register", "法师")
        t.check(r is not None and r.ok, "改名后应成功")
        if r is not None and r.ok:
            b.user_id = r.data.get("user_id", "")
        r = b.call("peers")
        ids = {p.get("user_id") for p in ((r.data or {}).get("players") or [])} if r else set()
        t.check(a.user_id in ids and b.user_id in ids, f"双方应互相可见 ids={ids}")

        t.case("未知动作")
        r = a.call("dance", "")
        t.check(r is not None and r.kind == "FAIL", "应失败")
        if r is not None:
            t.eq(r.code, protocol.E_UNKNOWN_ACTION, "错误码")

        t.case("带 user_id 但设备不匹配")
        r = a.call("peers", user=b.user_id, session_token="")
        t.check(r is not None and r.kind == "FAIL", "应失败")
        if r is not None:
            t.eq(r.code, protocol.E_BAD_USER_ID, "错误码")

        # ------------------------------------------------------------------
        t.case("legacy 0.1.0 客户端：脚底坐标自动 ±1.65m 换算")
        c = Client(host, port, client_version="0.1.0")
        extra.append(c)
        r = c.hello()
        t.check(r is not None and r.ok, "hello 应 OK")
        if r is not None and r.ok:
            t.eq(r.data.get("coord_base"), "head", "协议基准恒为 head")
            t.eq(r.data.get("coord_base_applied"), "feet", "旧客户端实际按 feet 处理")
        r = c.call("device", "")
        c.device_id = (r.data or {}).get("device_id", "") if r is not None else ""
        r = c.call("register", "老客户端")
        t.check(r is not None and r.ok, "注册应 OK")
        if r is not None and r.ok:
            c.user_id = r.data.get("user_id", "")
        r = c.call("state", "0&0&5&0")            # 旧语义：y=0 是脚底
        t.check(r is not None and r.ok, "老客户端 state 应 OK")
        crow = game.get_player(c.user_id) or {}
        t.check(
            abs(float(crow.get("pos_y", 0.0)) - 1.65) < 1e-6,
            f"入库时 +1.65m 换成头部 got={crow.get('pos_y')}",
        )
        if r is not None and r.ok:
            t.eq(r.data.get("pos_y"), 0.0, "state 响应按脚底基准下发")
            t.eq(r.data.get("coord_base"), "feet", "state 响应标记 feet")
        r = c.call("peers")
        mine = [p for p in ((r.data or {}).get("players") or [])
                if p.get("user_id") == c.user_id]
        t.check(bool(mine) and mine[0].get("coord_base") == "feet", "旧客户端 peers 行标记 feet")
        t.check(
            bool(mine) and abs(float(mine[0].get("pos_y", 0.0))) < 1e-6,
            f"旧客户端 peers y 换回脚底 got={mine[0].get('pos_y') if mine else None}",
        )
        r = a.call("peers")
        modern = [p for p in ((r.data or {}).get("players") or [])
                  if p.get("user_id") == c.user_id]
        t.check(
            bool(modern) and abs(float(modern[0].get("pos_y", 0.0)) - 1.65) < 1e-6,
            f"新客户端看到头部坐标 got={modern[0].get('pos_y') if modern else None}",
        )
        t.check(bool(modern) and modern[0].get("coord_base") == "head", "新客户端 peers 行标记 head")

        # ------------------------------------------------------------------
        t.case("races 由服务端下发（开发阶段仅人类）")
        r = a.call("races", "")
        t.check(r is not None and r.ok, "races 应 OK")
        if r is not None and r.ok:
            races = r.data.get("races") or []
            t.eq(len(races), 1, "可用种族数")
            if races:
                t.eq(races[0].get("key"), "human", "默认种族 key")
                t.eq(races[0].get("dev_only"), True, "开发阶段标记")

        t.case("assets 分块下载与路径穿越防护")
        r = a.call("assets", "list&")
        t.check(r is not None and r.ok, f"list 应 OK got={r}")
        names = {e.get("name") for e in ((r.data or {}).get("entries") or [])} if r else set()
        t.check({"hello.txt", "sub"} <= names, f"列目录 names={names}")
        r = a.call("assets", "get&hello.txt")
        t.check(r is not None and r.ok, "get 应 OK")
        if r is not None and r.ok:
            t.eq(base64.b64decode(r.data.get("data", "")), b"hello-assets", "文件内容一致")
            t.eq(r.data.get("eof"), True, "小文件一次读完")
        r = a.call("assets", "get&sub/a.bin&0&8192")
        t.check(r is not None and r.ok, "大文件分块 get 应 OK")
        if r is not None and r.ok:
            t.eq(r.data.get("length"), 4096, "单块被夹到 chunk_bytes")
            t.eq(r.data.get("next_offset"), 4096, "next_offset")
            t.eq(r.data.get("eof"), False, "尚未读完")
        r = a.call("assets", "get&../secret.txt")
        t.check(r is not None and not r.ok, "路径穿越必须被拒绝")

        t.case("attack 部位命中：头部 ×2（50） / 躯干 ×1（25）")
        r = a.call("state", "0&1.65&0&0")          # a 头部 (0,1.65,0)，yaw 0 = 正北/−Z
        t.check(r is not None and r.ok, "a 上报位置")
        r = b.call("state", "0&1.65&-2&180")       # b 头部在 a 正前方 2m
        t.check(r is not None and r.ok, "b 上报位置")
        time.sleep(0.08)
        r = a.call("attack", b.user_id)
        t.check(r is not None and r.ok, f"等高平射应命中 got={r}")
        if r is not None and r.ok:
            t.eq(r.data.get("target"), b.user_id, "target")
            t.eq(r.data.get("part"), "head", "等高平射先命中头部盒")
            t.eq(r.data.get("multiplier"), 2.0, "头部倍率 2.0")
            t.eq(r.data.get("damage"), 50.0, "头部伤害 = 25 × 2 = 50")
            t.eq(r.data.get("hp"), 50.0, "目标 HP 100 - 50")
            t.eq(r.data.get("hp_max"), 100.0, "目标 HP 上限")
            t.eq(r.data.get("dying"), False, "还没进濒死")
            t.eq(r.data.get("distance"), 2.0, "距离 2m")
            t.eq(r.data.get("killer_red"), False, "击杀者初始不红")
            t.eq(r.data.get("victim_red"), False, "受害者初始不红")
        time.sleep(0.08)
        r = a.call("state", "0&1.35&0&0")          # 攻击者头部低 0.30m → 平射打在躯干高度
        t.check(r is not None and r.ok, "a 下移后上报")
        time.sleep(0.08)
        r = a.call("attack", b.user_id)
        t.check(r is not None and r.ok, f"低位平射应命中躯干 got={r}")
        if r is not None and r.ok:
            t.eq(r.data.get("part"), "torso", "低 0.30m 平射命中躯干盒")
            t.eq(r.data.get("multiplier"), 1.0, "躯干倍率 1.0")
            t.eq(r.data.get("damage"), 25.0, "躯干伤害 = 25 × 1 = 25")
            t.eq(r.data.get("hp"), 25.0, "目标 HP 50 - 25")

        t.case("aim pitch：state 第 9 段俯仰瞄准改变命中部位")
        # 等高、间距 1m：pitch 只把射线压 / 抬，水平距离不变。
        # 注意 `state` 的 hp 段服务端只读不收（客户端不能改 HP），所以用 mutate 直接回满。
        with game.mutate():
            brow = game.players[b.user_id]
            brow["hp"] = 100.0
            brow["rev"] = int(brow.get("rev", 0)) + 1
        r = a.call("state", "0&1.65&0&0&100&idle&100&100&0")
        t.check(r is not None and r.ok, "a 上报（pitch=0，平射）")
        r = b.call("state", "0&1.65&-1&180&100&idle&100&100&0")
        t.check(r is not None and r.ok, "b 报到 1m")
        time.sleep(0.08)
        r = a.call("attack", b.user_id)
        t.check(r is not None and r.ok, f"等高平射应命中 got={r}")
        if r is not None and r.ok:
            t.eq(r.data.get("part"), "head", "pitch=0 → 平射穿过头部盒")
            t.eq(r.data.get("multiplier"), 2.0, "头部倍率 2.0")
            t.eq(r.data.get("damage"), 50.0, "头部伤害 50")

        r = a.call("state", "0&1.65&0&0&100&idle&100&100&-25")   # 低头 25°
        t.check(r is not None and r.ok, "a 低头 25° 上报")
        time.sleep(0.08)
        r = a.call("attack", b.user_id)
        t.check(r is not None and r.ok, f"pitch=-25 应命中躯干或更低部位 got={r}")
        if r is not None and r.ok:
            t.eq(r.data.get("part"), "torso", "pitch=-25 → 1m 处压低约 0.47m，命中躯干盒")
            t.eq(r.data.get("multiplier"), 1.0, "躯干倍率 1.0")
            t.eq(r.data.get("damage"), 25.0, "躯干伤害 25")

        r = a.call("state", "0&1.65&0&0&100&idle&100&100&35")    # 抬头 35°
        t.check(r is not None and r.ok, "a 抬头 35° 上报")
        time.sleep(0.08)
        r = a.call("attack", b.user_id)
        t.check(r is not None and r.kind == "FAIL", f"pitch=+35 应打空 got={r}")
        if r is not None:
            t.eq(r.code, protocol.E_MISS, "抬头越过目标头部 → MISS（不是 NOT_FACING）")

        # 第 9 段缺省 / 非法值都必须等价于 pitch=0（旧客户端行为不变）
        with game.mutate():
            brow = game.players[b.user_id]
            brow["hp"] = 100.0
            brow["rev"] = int(brow.get("rev", 0)) + 1
        r = a.call("state", "0&1.65&0&0&100&idle&100&100")       # 只有 8 段
        t.check(r is not None and r.ok, "8 段报文（省略 pitch）上报")
        time.sleep(0.08)
        r = a.call("attack", b.user_id)
        t.check(r is not None and r.ok, f"省略 pitch 应等价平射 got={r}")
        if r is not None and r.ok:
            t.eq(r.data.get("part"), "head", "省略第 9 段 → pitch=0（命中头部）")

        with game.mutate():
            brow = game.players[b.user_id]
            brow["hp"] = 100.0
            brow["rev"] = int(brow.get("rev", 0)) + 1
        r = a.call("state", "0&1.65&0&0&100&idle&100&100&abc")   # 非法值
        t.check(r is not None and r.ok, "非法 pitch 应被宽容收下（不报 BAD_PAYLOAD）")
        time.sleep(0.08)
        r = a.call("attack", b.user_id)
        t.check(r is not None and r.ok, f"非法 pitch 应等价平射 got={r}")
        if r is not None and r.ok:
            t.eq(r.data.get("part"), "head", "非法 pitch → 0（命中头部）")

        # 收尾：回到下一组用例要求的位置与朝向（4 段报文 → 第 9 段归 0）
        r = a.call("state", "0&1.65&0&0")
        t.check(r is not None and r.ok, "a 回到起点（俯仰归 0）")
        r = b.call("state", "0&1.65&-2&180")
        t.check(r is not None and r.ok, "b 回到 2m")
        time.sleep(0.08)

        t.case("attack 三种失败：MISS / NOT_FACING / OUT_OF_RANGE")
        r = a.call("state", "0&1.65&0&20")          # 偏 20°：仍在 60° 锥内但打空
        t.check(r is not None and r.ok, "a 偏转 20° 上报")
        time.sleep(0.08)
        r = a.call("attack", b.user_id)
        t.check(r is not None and r.kind == "FAIL", f"偏 20° 平射应打空 got={r}")
        if r is not None:
            t.eq(r.code, protocol.E_MISS, "MISS")
        time.sleep(0.08)
        r = a.call("state", "0&1.65&0&90")          # 偏 90°：出朝向锥
        t.check(r is not None and r.ok, "a 偏转 90° 上报")
        time.sleep(0.08)
        r = a.call("attack", b.user_id)
        t.check(r is not None and r.kind == "FAIL", f"背对目标应被拒 got={r}")
        if r is not None:
            t.eq(r.code, protocol.E_NOT_FACING, "NOT_FACING")
        r = b.call("state", "0&1.65&-5&180")        # 拉开到 5m：超出射程
        t.check(r is not None and r.ok, "b 后撤上报")
        r = a.call("state", "0&1.65&0&0")
        time.sleep(0.08)
        r = a.call("attack", b.user_id)
        t.check(r is not None and r.kind == "FAIL", f"超距应被拒 got={r}")
        if r is not None:
            t.eq(r.code, protocol.E_OUT_OF_RANGE, "OUT_OF_RANGE")
        r = b.call("state", "0&1.65&-2&180")        # 回到 2m，供濒死流程用
        t.check(r is not None and r.ok, "b 回到 2m")

        t.case("攻击 → 濒死 → 复活（5 秒窗口内救回）")
        with game.mutate():
            brow = game.players[b.user_id]
            brow["hp"] = 100.0
            brow["spirit"] = 4.0
            brow["rev"] = int(brow.get("rev", 0)) + 1
        r = a.call("state", "0&1.35&0&0")           # 躯干命中 25 伤害/次，4 次打空 100 HP
        t.check(r is not None and r.ok, "a 回到低位")
        for _ in range(4):
            r = a.call("attack", b.user_id)
            t.check(r is not None and r.ok, "攻击应 OK")
            time.sleep(0.08)
        r = b.call("state", "0&1.65&-2&180")
        t.check(r is not None and r.ok, "濒死者仍可上报（坐标被冻结）")
        if r is not None and r.ok:
            t.eq(r.data.get("hp"), 0.0, "HP 归零")
            t.eq(r.data.get("dying"), True, "进入濒死")
        r = a.call("revive", b.user_id)
        t.check(r is not None and r.ok, f"5 秒内复活应成功 got={r}")
        if r is not None and r.ok:
            t.eq(r.data.get("hp"), 30.0, "复活后回到 30% 生命")
        r = b.call("state", "0&1.65&-2&180")
        t.check(r is not None and r.ok and r.data.get("dying") is False, "濒死状态已解除")

        t.case("红名可见：state.red / peers 每行 red + coord_base")
        with game.mutate():
            brow = game.players[b.user_id]
            brow["red_name"] = True                  # 模拟“已经是红名”的玩家
            brow["red_at"] = time.time()
            brow["rev"] = int(brow.get("rev", 0)) + 1
        r = b.call("state", "0&1.65&-2&180")
        t.check(r is not None and r.ok, "红名玩家 state 应 OK")
        if r is not None and r.ok:
            t.eq(r.data.get("red"), True, "state 响应带 red=True")
        r = a.call("peers")
        line = [p for p in ((r.data or {}).get("players") or [])
                if p.get("user_id") == b.user_id]
        t.check(bool(line) and line[0].get("red") is True, "peers 行带 red=True")
        t.check(bool(line) and line[0].get("coord_base") == "head", "peers 行带 coord_base=head")
        r = a.call("state", "0&1.65&0&0")
        t.check(r is not None and r.ok and r.data.get("red") is False, "自己不是红名")

        t.case("濒死不救 → 确认死亡 → 灵力转移 → 旁观 → 清空复活")
        for _ in range(4):
            a.call("attack", b.user_id)
            time.sleep(0.08)
        time.sleep(game.DYING_SECONDS + 1.2)       # 等计时线程确认死亡
        r = a.call("peers")
        ids = {p.get("user_id") for p in ((r.data or {}).get("players") or [])} if r else set()
        t.check(b.user_id not in ids, f"旁观者不进入 peers ids={ids}")
        arow = game.get_player(a.user_id) or {}
        t.eq(arow.get("kills"), 1, "击杀数 +1")
        t.eq(arow.get("level"), 2, "灵力 2 触发升级（本级消耗 2）")
        t.eq(arow.get("spirit"), 0.0, "升级消耗后灵力归零")
        t.eq(arow.get("red_name"), True, "真死亡那一刻击杀者变红（§12.1 时点）")
        t.check(arow.get("red_at") is not None, "red_at 已记录变红时间")
        brow = game.get_player(b.user_id) or {}
        t.check(brow.get("spectator_until") is not None, "进入 30 分钟旁观（测试压缩）")
        t.eq(brow.get("deaths"), 1, "死亡数 +1")
        t.eq(brow.get("red_name"), False, "确认死亡 → 受害者清红名（§12.4）")
        r = a.call("state", "0&1.65&0&0")
        t.check(r is not None and r.ok and r.data.get("red") is True,
                "state 响应回写自己的红名")
        r = a.call("peers")
        mine_red = [p for p in ((r.data or {}).get("players") or [])
                    if p.get("user_id") == a.user_id]
        t.check(bool(mine_red) and mine_red[0].get("red") is True, "peers 里他人能看到红名")

        time.sleep(game.SPECTATOR_SECONDS + 1.2)   # 等旁观结束
        r = a.call("peers")
        ids = {p.get("user_id") for p in ((r.data or {}).get("players") or [])} if r else set()
        t.check(b.user_id in ids, f"旁观结束回到世界 ids={ids}")
        brow = game.get_player(b.user_id) or {}
        t.eq(brow.get("spirit"), 0.0, "灵力已清空")
        t.eq(brow.get("inventory"), {}, "背包已清空")
        t.eq(brow.get("hp"), 100.0, "满血复活")

        t.case("logout 完整拆除（流程 3）")
        b_user = b.user_id
        r = b.call("logout")
        t.check(r is not None and r.ok, "logout 应 OK")
        if r is not None:
            t.eq(r.data.get("saved"), True, "已落库")
            t.eq(r.data.get("authenticated"), False, "已解除认证")
        t.check(game.get_player(b_user) is None, "player 热缓存已销毁")
        r = b.call("state", "0&0&0&0")          # 拿已作废的 token 再来
        t.check(r is not None and r.kind == "FAIL", "旧 token 应被拒")
        if r is not None:
            t.eq(r.code, protocol.E_TOKEN_EXPIRED, "错误码")

        t.case("device 确认后能拿回账号（免登录入口）")
        r = a.call("device")
        account = (r.data or {}).get("account") if r is not None else None
        t.check(isinstance(account, dict), f"account 应为对象 got={account!r} resp={r!r}")
        if isinstance(account, dict):
            t.eq(account.get("user_id"), a.user_id, "account.user_id")
            t.eq(account.get("user_name"), "勇者", "account.user_name")

        t.case("下线后重连：login 带 file_hash，player 从库重载")
        b2 = Client(host, port)
        extra.append(b2)
        b2.hello()
        b2.device_id = b.device_id
        r = b2.call(
            "login", '{"player_id":"%s","file_hash":"hash-1"}' % b_user, user=""
        )
        t.check(r is not None and r.ok, f"login 应 OK got={r}")
        if r is not None and r.ok:
            t.eq(r.data.get("user_id"), b_user, "user_id")
            t.eq(r.data.get("session_token"), b2.token, "响应回显会话 token")
        r = b2.call("peers")
        ids = {p.get("user_id") for p in ((r.data or {}).get("players") or [])} if r else set()
        t.check(b_user in ids, f"重载后应重新在线 ids={ids}")

        t.case("file_hash 不一致时拒登（流程 2 完整性校验）")
        b3 = Client(host, port)
        extra.append(b3)
        b3.hello()
        b3.device_id = b.device_id
        r = b3.call(
            "login", '{"player_id":"%s","file_hash":"hash-2"}' % b_user, user=""
        )
        t.check(r is not None and r.kind == "FAIL", "哈希不一致应失败")
        if r is not None:
            t.eq(r.code, protocol.E_FILE_HASH_MISMATCH, "错误码")
        b3.close()

        # ------------------------------------------------------------------
        t.case("意外断开：宽限期内仍在线")
        r = b2.call("state", "77&0&0&0")
        t.check(r is not None and r.ok, "上报位置应 OK")
        b2.sock.close()                       # 硬断，不发 logout
        time.sleep(0.4)
        t.check(game.get_player(b_user) is not None, "宽限期内热缓存仍在")
        row = game.get_player(b_user) or {}
        t.eq(row.get("online"), True, "宽限期内仍算在线")
        r = a.call("peers")
        ids = {p.get("user_id") for p in ((r.data or {}).get("players") or [])} if r else set()
        t.check(b_user in ids, f"其他玩家此时仍能看到他 ids={ids}")

        t.case("宽限期内重连：同设备接管，位置不丢")
        b4 = Client(host, port)
        extra.append(b4)
        b4.hello()
        b4.device_id = b.device_id
        r = b4.call(
            "login", '{"player_id":"%s","file_hash":"hash-1"}' % b_user, user=""
        )
        t.check(r is not None and r.ok, f"重连登录应 OK got={r}")
        row = game.get_player(b_user) or {}
        t.eq(row.get("pos_x"), 77.0, "宽限期内重连保留实时位置")
        t.eq(len(service.hub._suspended), 0, "挂起会话已被接管")

        t.case("宽限期超时：落库 + 销毁（靠 peers 感知，无 leave 广播）")
        b4.sock.close()
        time.sleep(cfg.grace_seconds + 1.0)
        t.check(game.get_player(b_user) is None, "宽限期超时后 player 已销毁")
        r = a.call("peers")
        ids = {p.get("user_id") for p in ((r.data or {}).get("players") or [])} if r else set()
        t.check(b_user not in ids, f"超时后应离线 ids={ids}")
        t.check(a.user_id in ids, "A 仍在线")

        t.case("运行统计")
        stats = service.server.snapshot()
        t.check(stats["packets"] > 0, f"packets={stats['packets']}")
        t.check(stats["rejected"] > 0, f"rejected={stats['rejected']}")
        t.check(stats["online_players"] >= 1, f"online_players={stats['online_players']}")

        # ------------------------------------------------------------------
        # 放在最后：这会清空内存热缓存并从另一份 store 重新载入
        t.case("coord_base 迁移：库里的 feet 旧行加载时 +1.65m 并改写为 head")
        import store as store_mod

        scratch = store_mod.MemoryStore()
        scratch.save_players([{
            "user_id": "MIGR-0001",
            "player_name": "旧档",
            "race": "human",
            "pos_x": 1.0, "pos_y": 0.0, "pos_z": 2.0, "yaw": 0.0,
            "state": "idle", "online": False,
            "hp_max": 100.0, "hp": 100.0,
            "stamina_max": 100.0, "stamina": 100.0,
            "mana_max": 100.0, "mana": 100.0,
            "spirit": 0.0, "level": 1, "spirit_at": 0.0,
            "death_at": None, "spectator_until": None, "last_attacker": "",
            "inventory": {}, "kills": 0, "deaths": 0,
            "red_name": False, "red_at": None,
            "coord_base": "feet",          # 老库里的行：脚底基准
            "rev": 1,
        }])
        t.check(game.load_from_store(scratch), "load_from_store 应成功")
        row = game.get_player("MIGR-0001") or {}
        t.check(
            abs(float(row.get("pos_y", 0.0)) - 1.65) < 1e-6,
            f"加载时 y 由 0 变 1.65 got={row.get('pos_y')}",
        )
        t.eq(row.get("coord_base"), "head", "内存行已改写为 head")
        game.save(scratch)
        back = scratch.load_player("MIGR-0001") or {}
        t.eq(back.get("coord_base"), "head", "落库后库里也是 head")
        t.check(
            abs(float(back.get("pos_y", 0.0)) - 1.65) < 1e-6,
            f"库里的 y 也是头部坐标 got={back.get('pos_y')}",
        )
        # 幂等：再载入一次不应再加 1.65
        game.load_from_store(scratch)
        again = game.get_player("MIGR-0001") or {}
        t.check(
            abs(float(again.get("pos_y", 0.0)) - 1.65) < 1e-6,
            f"重复加载不再平移 got={again.get('pos_y')}",
        )
        t.eq(again.get("red_name"), False, "旧行的红名默认 False")

    except Exception:  # noqa: BLE001 - 测试脚本把异常当失败，不要吃掉
        t.failed.append(("<exception>", traceback.format_exc()))
        print(traceback.format_exc())
    finally:
        for client in [a, b, *extra]:
            client.close()
        service.close()

    return t.summary()


def main() -> int:
    parser = argparse.ArgumentParser(description="TCP 协议冒烟测试")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条断言")
    args = parser.parse_args()
    import logging

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s | %(message)s")
    return run(args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
