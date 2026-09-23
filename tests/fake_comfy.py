"""Local fake ComfyUI HTTP + websocket server, driven by a scripted timeline.

Runs a real aiohttp server on 127.0.0.1:<random port> in a background thread so
tests exercise ComfyClient/senai_worker against a genuine TCP socket (real
connection-refused/connection-closed behaviour) instead of mocks.
"""
import asyncio
import threading
import time

from aiohttp import web


class FakeComfy:
    def __init__(self):
        self.calls = []
        self.object_info = {}
        self.system_stats_response = {
            "devices": [
                {
                    "name": "NVIDIA Fake GPU",
                    "type": "cuda",
                    "index": 0,
                    "vram_total": 51539607552,
                    "vram_free": 51539607552,
                }
            ]
        }
        self.system_stats_fail = False
        self.prompt_response = lambda graph, client_id: (200, {"prompt_id": "p1"})
        self.history_store = {}
        self.ws_script = []
        self.received_prompts = []  # [(client_id, graph), ...] — full body received on /prompt

        self.killed = threading.Event()
        self._loop = None
        self._thread = None
        self._runner = None
        self._site = None
        self.port = None

    def start(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        while self.port is None:
            time.sleep(0.005)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start())
        self._loop.run_forever()

    async def _start(self):
        app = web.Application()
        app.router.add_get("/system_stats", self._h_system_stats)
        app.router.add_get("/object_info", self._h_object_info)
        app.router.add_post("/prompt", self._h_prompt)
        app.router.add_get("/history/{prompt_id}", self._h_history)
        app.router.add_post("/interrupt", self._h_interrupt)
        app.router.add_post("/queue", self._h_queue)
        app.router.add_get("/ws", self._h_ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        self.port = self._site._server.sockets[0].getsockname()[1]

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self.killed.is_set():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            return
        fut = asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop)
        try:
            fut.result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def kill(self):
        """Simulate a hard process crash: stop answering HTTP and websocket."""
        if self.killed.is_set():
            return
        self.killed.set()
        fut = asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop)
        try:
            fut.result(timeout=5)
        except Exception:
            pass

    # ---- HTTP handlers ----
    async def _h_system_stats(self, request):
        if self.killed.is_set() or self.system_stats_fail:
            return web.Response(status=503)
        return web.json_response(self.system_stats_response)

    async def _h_object_info(self, request):
        if self.killed.is_set():
            return web.Response(status=503)
        return web.json_response(self.object_info)

    async def _h_prompt(self, request):
        if self.killed.is_set():
            return web.Response(status=503)
        body = await request.json()
        graph = body.get("prompt")
        client_id = body.get("client_id")
        self.calls.append(("prompt", client_id))
        self.received_prompts.append((client_id, graph))
        status, payload = self.prompt_response(graph, client_id)
        return web.json_response(payload, status=status)

    async def _h_history(self, request):
        if self.killed.is_set():
            return web.Response(status=503)
        prompt_id = request.match_info["prompt_id"]
        entry = self.history_store.get(prompt_id)
        return web.json_response({prompt_id: entry} if entry is not None else {})

    async def _h_interrupt(self, request):
        self.calls.append(("interrupt",))
        return web.json_response({})

    async def _h_queue(self, request):
        self.calls.append(("queue_delete",))
        return web.json_response({})

    async def _h_ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        for item in self.ws_script:
            if self.killed.is_set():
                return ws
            delay = item.get("delay", 0)
            if delay:
                await asyncio.sleep(delay)
            action = item.get("action")
            if action == "close":
                await ws.close()
                return ws
            if action == "kill":
                self.kill()
                return ws
            await ws.send_json(item["message"])
        while not self.killed.is_set():
            try:
                await asyncio.wait_for(ws.receive(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
        return ws


def executing(node, prompt_id="p1"):
    return {"type": "executing", "data": {"node": node, "prompt_id": prompt_id}}


def progress_state(nodes=None, prompt_id="p1"):
    return {"type": "progress_state", "data": {"prompt_id": prompt_id, "nodes": nodes or {}}}


def execution_error(node_id, class_type, *, exception_type="RuntimeError", exception_message="boom", prompt_id="p1"):
    return {
        "type": "execution_error",
        "data": {
            "prompt_id": prompt_id,
            "node_id": node_id,
            "node_type": class_type,
            "exception_type": exception_type,
            "exception_message": exception_message,
        },
    }
