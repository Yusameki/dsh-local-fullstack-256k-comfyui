#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strata On-Demand Proxy —— 请求到达时拉起 Strata，闲置时优雅卸载。

DSH 的 strata-local 指向本代理（默认 127.0.0.1:8090），代理把请求原样转发给
Strata（127.0.0.1:8081）。Strata 没在跑就先 detached 拉起，等它就绪期间向客户端
持续吐 SSE 注释行保活；没有请求满 IDLE_TIMEOUT_SECONDS 就用 `strata launch --stop`
优雅停服、释放显存。

只用 Python 标准库，零第三方依赖。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import http.client
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 可配置常量
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# 代理自己监听的地址
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = _env_int("STRATA_PROXY_PORT", 8090)

# 真正的 Strata 服务
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = _env_int("STRATA_UPSTREAM_PORT", 8081)

# 闲置多久后卸载 Strata（秒）。主人定的 15 分钟。
IDLE_TIMEOUT_SECONDS = _env_int("STRATA_PROXY_IDLE_TIMEOUT", 900)

# 闲置检查的轮询间隔（秒）
IDLE_CHECK_INTERVAL_SECONDS = _env_int("STRATA_PROXY_IDLE_CHECK", 30)

# 拉起后最多等多久（秒）。冷启动 1-3 分钟，留足余量。
STARTUP_TIMEOUT_SECONDS = _env_int("STRATA_PROXY_STARTUP_TIMEOUT", 240)

# 等待期间给客户端发保活注释行的间隔（秒）
KEEPALIVE_INTERVAL_SECONDS = _env_int("STRATA_PROXY_KEEPALIVE", 15)

# 真实转发期间，上游超过这么久没有新 chunk 就向客户端插一行心跳（秒）
UPSTREAM_IDLE_KEEPALIVE_SECONDS = _env_int("STRATA_PROXY_UPSTREAM_KEEPALIVE", 15)

# 探活单次超时（秒）
PROBE_TIMEOUT_SECONDS = 2.0

# 两次拉起之间的冷却（秒），防止启动失败时疯狂重试
LAUNCH_COOLDOWN_SECONDS = _env_int("STRATA_PROXY_LAUNCH_COOLDOWN", 120)

# 只有本代理拉起的 Strata 才会被自动卸载吗？False = 只要闲置就卸载。
STOP_ONLY_IF_WE_STARTED = False

# Strata 安装位置
STRATA_CONTROL_DIR = r"<STRATA_HOME>\App\control"
STRATA_CMD = os.path.join(STRATA_CONTROL_DIR, "bin", "strata.cmd")
STRATA_DATA_DIR = r"<STRATA_HOME>\Models"

# 拉起 Strata 的完整参数
STRATA_LAUNCH_ARGS = [
    "launch",
    "--family", "gyro",
    "--model", "gyro-s",
    "--data-dir", STRATA_DATA_DIR,
    "--context", "128k",
    "--port", str(UPSTREAM_PORT),
    "--no-tui",
    "--yes",
    "--set", "vram-reserve-mib=1728",
]

# 日志与工作目录
LOG_DIR = r"<STRATA_HOME>\Logs"
LOG_PATH = os.path.join(LOG_DIR, "proxy.log")
LAUNCH_LOG_PATH = os.path.join(LOG_DIR, "proxy-launch.log")

# 转发时不透传的逐跳头
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
    with _log_lock:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 共享状态
# ---------------------------------------------------------------------------

class State:
    def __init__(self) -> None:
        # 用 Condition 而不是裸 Lock：卸载期间新请求要能等，而不是撞进正在停的实例
        self.cond = threading.Condition()
        self.launch_lock = threading.Lock()
        self.in_flight = 0
        self.last_activity = time.monotonic()
        self.last_launch_at = 0.0
        self.launched_by_us = False
        self.unloading = False
        self.stopping = False

    def touch(self) -> None:
        with self.cond:
            self.last_activity = time.monotonic()

    def enter(self) -> None:
        with self.cond:
            # 正在卸载就先等着，避免请求打到一个正在关闭的 Strata 上
            while self.unloading and not self.stopping:
                self.cond.wait(timeout=1.0)
            self.in_flight += 1
            self.last_activity = time.monotonic()

    def leave(self) -> None:
        with self.cond:
            self.in_flight = max(0, self.in_flight - 1)
            self.last_activity = time.monotonic()
            self.cond.notify_all()

    def snapshot(self) -> tuple[int, float]:
        with self.cond:
            return self.in_flight, self.last_activity

    def try_begin_unload(self, idle_for: float) -> bool:
        """在锁内一次性判定「没人用 + 确实在跑」，通过则占住卸载权。"""
        with self.cond:
            if self.unloading or self.stopping:
                return False
            if self.in_flight > 0:
                return False
            if STOP_ONLY_IF_WE_STARTED and not self.launched_by_us:
                return False
            # port_open 也放在锁内：锁外判会把「刚被新请求拉起」误判成可卸载
            if not port_open():
                return False
            self.unloading = True
            self.last_activity = time.monotonic()
            log(f"闲置 {int(idle_for)}s，占住卸载权")
            return True

    def end_unload(self) -> None:
        with self.cond:
            self.unloading = False
            self.launched_by_us = False
            self.last_activity = time.monotonic()
            self.cond.notify_all()


STATE = State()


# ---------------------------------------------------------------------------
# Strata 探活 / 拉起 / 停止
# ---------------------------------------------------------------------------

def probe_upstream(timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """Strata 的 /v1/models 能应答就算活着。状态码不重要。"""
    url = f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(1)
        return True
    except Exception:
        return False


def port_open(timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def run_strata(args: list[str], timeout: float) -> tuple[int, str, str]:
    """同步跑一条 strata 命令，返回 (returncode, stdout, stderr)。"""
    command = ["cmd.exe", "/c", STRATA_CMD, *args]
    try:
        completed = subprocess.run(
            command,
            cwd=STRATA_CONTROL_DIR,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (
            completed.returncode,
            completed.stdout.decode("utf-8", "replace"),
            completed.stderr.decode("utf-8", "replace"),
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except OSError as error:
        return -1, "", str(error)


def strata_status() -> dict | None:
    """用官方 --status --output json 拿运行状态，比 TCP 探测准。"""
    code, out, err = run_strata(["launch", "--status", "--output", "json"], timeout=60)
    if code != 0:
        log(f"status 失败 rc={code} err={err.strip()[:200]}")
        return None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("ok") is not True:
            return None
        for entry in payload.get("running") or []:
            if int(entry.get("port") or 0) == UPSTREAM_PORT:
                return entry
        return None
    return None


def launch_strata() -> bool:
    """detached 拉起 Strata，让 runtime host 脱离本进程存活。"""
    if not os.path.isfile(STRATA_CMD):
        log(f"找不到 strata.cmd: {STRATA_CMD}")
        return False
    command = ["cmd.exe", "/c", STRATA_CMD, *STRATA_LAUNCH_ARGS]
    flags = 0
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LAUNCH_LOG_PATH, "ab") as sink:
            sink.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} launch =====\n".encode())
            sink.flush()
            process = subprocess.Popen(
                command,
                cwd=STRATA_CONTROL_DIR,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                close_fds=True,
            )
        log(f"已发起拉起，launcher pid={process.pid}")
        return True
    except OSError as error:
        log(f"拉起失败: {error}")
        return False


def stop_strata() -> bool:
    """官方优雅停服：runtime host 关管道 -> server 收 EOF -> 排空退出。"""
    log("开始优雅停服 strata launch --stop")
    code, out, err = run_strata(["launch", "--stop", "--output", "json"], timeout=180)
    if code != 0:
        log(f"停服返回 rc={code} err={err.strip()[:300]} out={out.strip()[:200]}")
        return False
    log("停服完成")
    return True


def ensure_ready(keepalive) -> bool:
    """确保 Strata 在跑。keepalive 是等待期间反复调用的回调（可为 None）。"""
    if probe_upstream():
        return True

    with STATE.launch_lock:
        # 抢到锁后再确认一次，避免并发重复拉起
        if probe_upstream():
            return True

        now = time.monotonic()
        if now - STATE.last_launch_at >= LAUNCH_COOLDOWN_SECONDS:
            if not launch_strata():
                return False
            STATE.last_launch_at = now
            STATE.launched_by_us = True
        else:
            log("冷却期内，不重复拉起，只等待")

        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        # 调用方通常已经立刻发过一次 keepalive，这里从下一个周期开始计时
        next_keepalive = time.monotonic() + KEEPALIVE_INTERVAL_SECONDS
        while time.monotonic() < deadline:
            # 先用轻量 TCP 探测，端口通了才发真正的 HTTP 探活
            if port_open() and probe_upstream():
                log("Strata 已就绪")
                return True
            if keepalive is not None and time.monotonic() >= next_keepalive:
                try:
                    keepalive()
                except Exception:
                    return False
                next_keepalive = time.monotonic() + KEEPALIVE_INTERVAL_SECONDS
            time.sleep(1.0)

        log(f"等待 {STARTUP_TIMEOUT_SECONDS}s 仍未就绪，放弃")
        return False


# ---------------------------------------------------------------------------
# 闲置卸载线程
# ---------------------------------------------------------------------------

def idle_watcher() -> None:
    while not STATE.stopping:
        time.sleep(IDLE_CHECK_INTERVAL_SECONDS)
        if STATE.stopping:
            return
        in_flight, last_activity = STATE.snapshot()
        if in_flight > 0:
            continue
        idle_for = time.monotonic() - last_activity
        if idle_for < IDLE_TIMEOUT_SECONDS:
            continue
        # 判定 + 占权 + port_open 全在锁内，杜绝和新请求撞车
        if not STATE.try_begin_unload(idle_for):
            continue
        try:
            stop_strata()
        except Exception as error:  # 卸载失败不能让守护线程死掉
            log(f"停服异常: {error}")
        finally:
            STATE.end_unload()


# ---------------------------------------------------------------------------
# HTTP 代理
# ---------------------------------------------------------------------------

class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StrataOnDemand/1.0"
    sys_version = ""

    # 关掉默认的 stderr 访问日志，用我们自己的 log()
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log("access " + (fmt % args))

    # -- 工具方法 ---------------------------------------------------------

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size_line = self.rfile.readline(65536).strip()
                if not size_line:
                    break
                try:
                    size = int(size_line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline(65536)
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            remaining = int(length)
        except ValueError:
            return b""
        buffer = bytearray()
        while remaining > 0:
            piece = self.rfile.read(min(remaining, 65536))
            if not piece:
                break
            buffer.extend(piece)
            remaining -= len(piece)
        return bytes(buffer)

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            self.headers.get("Access-Control-Request-Headers", "*"),
        )

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n" % len(data))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_chunked(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _start_stream_response(self, status: int = 200, content_type: str = "text/event-stream") -> None:
        """提前提交响应头，好在等待拉起时发保活行。"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self._send_cors()
        self.end_headers()

    def _send_json_error(self, status: int, message: str) -> None:
        payload = json.dumps(
            {"error": {"message": message, "type": "strata_proxy_error"}},
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self._send_cors()
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- 转发 -------------------------------------------------------------

    def _forward(self, body: bytes, committed: bool) -> None:
        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            # 不要求上游压缩，避免 gzip 缓冲破坏流式
            if key.lower() == "accept-encoding":
                continue
            headers[key] = value
        headers["Accept-Encoding"] = "identity"
        if body:
            headers["Content-Length"] = str(len(body))

        connection = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=None)
        writer_lock = threading.Lock()
        last_write = [time.monotonic()]
        finished = threading.Event()

        def pump_keepalive() -> None:
            """上游久不出字时，往客户端插注释行，重置 DSH 侧空闲计时器。"""
            while not finished.wait(1.0):
                if time.monotonic() - last_write[0] < UPSTREAM_IDLE_KEEPALIVE_SECONDS:
                    continue
                try:
                    with writer_lock:
                        self._write_chunk(b": keepalive\n\n")
                        last_write[0] = time.monotonic()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                except Exception:
                    return

        ticker = None
        try:
            connection.putrequest(self.command, self.path, skip_host=False, skip_accept_encoding=True)
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.endheaders(body or None)
            response = connection.getresponse()

            if not committed:
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() in HOP_BY_HOP:
                        continue
                    self.send_header(key, value)
                self.send_header("Transfer-Encoding", "chunked")
                self._send_cors()
                self.end_headers()

            last_write[0] = time.monotonic()
            ticker = threading.Thread(target=pump_keepalive, name="upstream-keepalive", daemon=True)
            ticker.start()

            reader = getattr(response, "read1", None) or response.read
            while True:
                try:
                    chunk = reader(4096)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except socket.timeout:
                    continue
                if not chunk:
                    break
                with writer_lock:
                    self._write_chunk(chunk)
                    last_write[0] = time.monotonic()
            with writer_lock:
                self._end_chunked()
        finally:
            finished.set()
            if ticker is not None:
                ticker.join(timeout=2.0)
            try:
                connection.close()
            except Exception:
                pass

    def _handle_any(self) -> None:
        if self.command == "OPTIONS":
            self.send_response(204)
            self._send_cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        body = self._read_body()
        STATE.enter()
        try:
            ready = probe_upstream()
            if ready:
                STATE.touch()
                self._forward(body, committed=False)
                return

            # 需要拉起：先提交 SSE 头，边等边保活
            log("Strata 未就绪，开始拉起")
            self._start_stream_response()

            def keepalive() -> None:
                self._write_chunk(b": keepalive\n\n")

            try:
                keepalive()
            except (BrokenPipeError, ConnectionResetError):
                log("客户端在等待拉起期间断开")
                return

            if not ensure_ready(keepalive):
                # 拉起失败/超时：发一条 SSE error 事件后立刻断开，让 DSH 快速报错，
                # 而不是挂在那里等 5 分钟空闲超时。
                reason = "strata startup timeout" if os.path.isfile(STRATA_CMD) else "strata launcher missing"
                payload = json.dumps(
                    {
                        "error": reason,
                        "message": f"Strata 未能在 {STARTUP_TIMEOUT_SECONDS}s 内就绪，详见 {LAUNCH_LOG_PATH}",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                log(f"拉起失败，向客户端发错误帧: {reason}")
                try:
                    self._write_chunk(b"event: error\ndata: " + payload + b"\n\n")
                    self._end_chunked()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
                return

            self._forward(body, committed=True)
        except (BrokenPipeError, ConnectionResetError):
            log("客户端提前断开")
        except Exception as error:  # 任何异常都不能让服务崩
            log(f"处理 {self.command} {self.path} 出错: {error!r}")
            try:
                self._send_json_error(502, f"代理内部错误: {error}")
            except Exception:
                pass
        finally:
            STATE.leave()

    do_GET = _handle_any
    do_POST = _handle_any
    do_PUT = _handle_any
    do_DELETE = _handle_any
    do_PATCH = _handle_any
    do_HEAD = _handle_any


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    os.makedirs(LOG_DIR, exist_ok=True)
    log("=" * 60)
    log(f"Strata On-Demand Proxy 启动 监听 {LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM_HOST}:{UPSTREAM_PORT}")
    log(f"闲置阈值 {IDLE_TIMEOUT_SECONDS}s · 启动上限 {STARTUP_TIMEOUT_SECONDS}s · 保活 {KEEPALIVE_INTERVAL_SECONDS}s")

    watcher = threading.Thread(target=idle_watcher, name="idle-watcher", daemon=True)
    watcher.start()

    server = ProxyServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("收到中断，退出")
    finally:
        STATE.stopping = True
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
