"""
mcp_client_manager.py — MCP (Model Context Protocol) client integration.

Connects to MCP servers over stdio or Streamable HTTP, discovers their tools,
and exposes them as OpenAI-format function schemas for the agent loop.

Requires the official `mcp` Python SDK:
    pip install "mcp[cli]"

When the SDK is not installed, the module degrades gracefully and reports
MCP as unavailable rather than crashing the backend.
"""

import asyncio
import json
import os
import threading

from service import send_log

# ── Optional import ──

_MCP_OK = False
_MCP_ERR = ""

try:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    from mcp.client.sse import sse_client
    # streamable-http is available in mcp >= 1.10
    try:
        from mcp.client.streamable_http import streamablehttp_client
        _HTTP_OK = True
    except ImportError:
        _HTTP_OK = False
    _MCP_OK = True
except ImportError as e:
    _MCP_ERR = str(e)
    _HTTP_OK = False


def _stdio_connect_cm(command, args, env):
    """返回兼容新旧 mcp SDK 的 stdio_client 上下文管理器。

    新 SDK(>=1.9 左右): stdio_client(StdioServerParameters(command, args, env, ...))
    旧 SDK:            stdio_client(command, args, env=env)
    旧调用在 mcp 1.27 上抛 "unexpected keyword argument 'env'"，故先试新版。
    """
    try:
        from mcp import StdioServerParameters
        _params = StdioServerParameters(command=command, args=args or [], env=env)
        return stdio_client(_params)
    except Exception:
        return stdio_client(command, args, env=env)


# ── Tool schema converter ──

def _mcp_tool_to_openai(full_name, tool):
    """Convert an MCP tool definition to an OpenAI function-calling schema."""
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    if hasattr(tool, "inputSchema") and tool.inputSchema:
        s = tool.inputSchema
        if isinstance(s, dict):
            schema = {
                "type": s.get("type", "object"),
                "properties": s.get("properties", {}),
            }
            if "required" in s:
                schema["required"] = s["required"]
    return {
        "type": "function",
        "function": {
            "name": full_name,
            "description": getattr(tool, "description", "") or "",
            "parameters": schema,
        },
    }


# ── MCPClientManager ──

class MCPClientManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}    # server_name -> ClientSession（引用，worker 线程持有生命周期）
        self._queues = {}      # server_name -> queue.Queue（worker 命令队列：call / close）
        self._configs = {}     # server_name -> original config (for reconnect)
        self.tools = {}        # "mcp__<server>__<tool>" -> (server_name, tool_def)

    @property
    def available(self):
        return _MCP_OK

    @property
    def connected_servers(self):
        with self._lock:
            return list(self._sessions.keys())

    # ── 连接：每 server 一个 worker 线程，asyncio.run + async with 保活 ──
    # mcp SDK 的 stdio/http 传输必须用 `async with` 包住（手动 __aenter__ 会使
    # 子进程 stdout 读取任务不启动，initialize 挂起），且 loop 必须持续运行。
    # asyncio.run(coro) 是已验证可用的运行方式，但 coro 结束即关 loop/传输。
    # 因此 worker 线程跑 asyncio.run(_session_main)：在 async with 内 initialize/
    # 注册后常驻消费命令队列 → 会话持久存活，工具调用经队列派发。

    def connect_all(self, config, timeout=15):
        """Connect to all servers in `config["mcpServers"]` synchronously.
        Each connection is blocking; call from a background thread if needed.
        Returns a dict of {server_name: "ok"|error_message}.
        """
        if not _MCP_OK:
            return {"_error": f"MCP SDK 未安装: {_MCP_ERR}"}

        servers = config.get("mcpServers", {}) if isinstance(config, dict) else {}
        results = {}

        for name, cfg in servers.items():
            self._configs[name] = cfg  # save for reconnect
            try:
                self._connect_blocking(name, cfg, timeout)
                results[name] = "ok"
                send_log(f"[mcp] connected to {name} ({len(self.tools)} tools)", "info")
            except Exception as e:
                results[name] = str(e) or repr(e)
                send_log(f"[mcp] {name} connection failed: {e}", "warn")
        return results

    def _connect_blocking(self, name, cfg, timeout):
        import queue as _q
        cmd_q = _q.Queue()
        ready = threading.Event()
        err = []

        def _worker():
            asyncio.set_event_loop(asyncio.new_event_loop())
            async def _session_main():
                transport = cfg.get("type", "stdio")
                if transport == "http":
                    url = cfg.get("url", "")
                    if not url:
                        raise ValueError("缺少 url")
                    if _HTTP_OK:
                        async with streamablehttp_client(url) as (read, write, _):
                            await self._session_loop(name, read, write, cmd_q, ready)
                    else:
                        async with sse_client(url) as (read, write):
                            await self._session_loop(name, read, write, cmd_q, ready)
                else:
                    command = cfg.get("command", "")
                    if not command:
                        raise ValueError("缺少 command")
                    args = cfg.get("args", []) or []
                    env = {**os.environ}
                    if cfg.get("env"):
                        env.update(cfg["env"])
                    async with _stdio_connect_cm(command, args, env) as (read, write):
                        await self._session_loop(name, read, write, cmd_q, ready)
            try:
                asyncio.run(_session_main())
            except Exception as e:
                err.append(e)
                ready.set()

        t = threading.Thread(target=_worker, daemon=True, name=f"mcp-{name}")
        t.start()
        if not ready.wait(timeout + 5):
            raise TimeoutError(f"连接 {name} 超时")
        if err:
            raise err[0]
        with self._lock:
            self._queues[name] = cmd_q

    async def _session_loop(self, name, read, write, cmd_q, ready):
        """在 async with transport 上下文内：初始化 → 注册工具 → 常驻消费命令队列。

        注意: ClientSession 必须用 `async with` 包住 —— 其 __aenter__ 才启动接收循环，
        裸构造后 initialize 会永久挂起（mcp SDK 1.27 行为）。
        """
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            self._register_tools(name, session, tools_result)
            ready.set()  # 连接就绪
            while True:
                item = await asyncio.to_thread(cmd_q.get)  # 阻塞取命令，不卡 loop
                if item[0] == "close":
                    return
                _, arguments, fut = item
                try:
                    result = await session.call_tool(arguments.get("_name", ""), arguments.get("_args", {}) or {})
                    parts = []
                    is_error = getattr(result, "isError", False)
                    for block in getattr(result, "content", []):
                        if hasattr(block, "text") and block.text:
                            parts.append(block.text)
                        elif hasattr(block, "data") and block.data:
                            parts.append(f"[binary: {block.type}]")
                    fut.set_result({"content": "\n".join(parts), "is_error": is_error})
                except Exception as e:
                    fut.set_exception(e)

    def _register_tools(self, server_name, session, tools_result):
        """Store discovered tools in self.tools."""
        with self._lock:
            self._sessions[server_name] = session
            tools = tools_result.tools if hasattr(tools_result, "tools") else (tools_result or [])
            for tool in tools:
                full = f"mcp__{server_name}__{tool.name}"
                self.tools[full] = (server_name, tool)

    def disconnect_all(self):
        """Close all MCP sessions & transports. Best-effort.
        向各 server 队列投 close → worker 退出 async with → 传输关闭。"""
        with self._lock:
            queues = dict(self._queues)
            self._sessions.clear()
            self._queues.clear()
            self.tools.clear()
        for q in queues.values():
            try:
                q.put(("close", None, None))
            except Exception:
                pass

    # ── Tool execution ──

    def call_tool(self, full_name, arguments, timeout=300):
        """Call an MCP tool synchronously（经该 server 的 worker 命令队列）。
        Returns the tool result as a dict."""
        with self._lock:
            entry = self.tools.get(full_name)
            if not entry:
                return {"error": f"unknown MCP tool: {full_name}"}
            server_name, tool_def = entry
            queue = self._queues.get(server_name)

        if queue is None:
            # Auto-reconnect: try to reconnect if config saved
            cfg = self._configs.get(server_name)
            if cfg:
                send_log(f"[mcp] {server_name} disconnected, attempting reconnect...", "warn")
                result = self.connect_all({"mcpServers": {server_name: cfg}})
                if result.get(server_name) == "ok":
                    with self._lock:
                        queue = self._queues.get(server_name)
                        entry = self.tools.get(full_name)
                        if entry:
                            _, tool_def = entry
            if queue is None:
                return {"error": f"MCP server {server_name} not connected"}

        import concurrent.futures as _cf
        fut = _cf.Future()
        try:
            queue.put(("call", {"_name": tool_def.name, "_args": arguments or {}}, fut))
            return fut.result(timeout)
        except Exception as e:
            return {"error": f"MCP call failed: {e}"}

    # ── Tool schemas for LLM ──

    def get_tool_schemas(self):
        """Return OpenAI-format tool schemas for all discovered MCP tools."""
        with self._lock:
            return [_mcp_tool_to_openai(name, tool) for name, (_, tool) in self.tools.items()]

    # ── Connection test ──

    def test_connection(self, cfg):
        """Test a single MCP server config. Returns (ok: bool, message: str, tools: list)."""
        if not _MCP_OK:
            return False, f"MCP SDK 未安装: {_MCP_ERR}", []
        import asyncio
        try:
            transport = cfg.get("type", "stdio")
            if transport == "http":
                url = cfg.get("url", "")
                if not url:
                    return False, "缺少 url", []
                async def _test_http():
                    async with sse_client(url) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            r = await session.list_tools()
                            return [t.name for t in (r.tools if hasattr(r, "tools") else [])]
                tools = asyncio.run(_test_http())
            else:
                command = cfg.get("command", "")
                if not command:
                    return False, "缺少 command", []
                args = cfg.get("args", []) or []
                env = {**os.environ}
                if cfg.get("env"):
                    env.update(cfg["env"])
                async def _test_stdio():
                    async with _stdio_connect_cm(command, args, env) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            r = await session.list_tools()
                            return [t.name for t in (r.tools if hasattr(r, "tools") else [])]
                tools = asyncio.run(_test_stdio())
            return True, f"连接成功，{len(tools)} 个工具", tools
        except Exception as e:
            return False, str(e), []


# ── Module-level singleton ──

_mcp_manager = None


def get_mcp_manager():
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPClientManager()
    return _mcp_manager
