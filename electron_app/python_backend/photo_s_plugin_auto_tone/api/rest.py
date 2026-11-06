"""REST 路由（可选集成）

集成方式（photo_s.server 的 handler 类）：
    from photo_s_plugin_auto_tone.api.rest import register_routes
    register_routes(handler_class)

注意 photo_s.server._PhotoSHandler._send_json 的签名是
``_send_json(status, payload)``，且会自动用 contract.versioned 包装。
"""
import json


def register_routes(handler_class):
    """注册 /v1/auto_tone* 等 REST 路由到 photo_s.server handler"""

    def _read_json(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        return json.loads(body) if body else {}

    def _do_auto_tone(self):
        """POST /v1/auto_tone"""
        from photo_s_plugin_auto_tone.core.pipeline import run_auto_tone

        try:
            data = _read_json(self)
            image_path = data.get('image_path')
            if not image_path:
                self._send_json(400, {"error": "image_path required"})
                return
            result = run_auto_tone(
                image_path,
                strength=float(data.get('strength', 1.0)),
                render=bool(data.get('render', True)),
                use_rag=bool(data.get('use_rag', True)),
            )
            self._send_json(200, result)
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _do_aesthetic(self):
        """POST /v1/aesthetic/score"""
        from photo_s_plugin_auto_tone.core.aesthetic import AestheticScorer

        try:
            data = _read_json(self)
            image_path = data.get('image_path')
            if not image_path:
                self._send_json(400, {"error": "image_path required"})
                return
            result = AestheticScorer().score(image_path)
            self._send_json(200, {
                "score": result["score"],
                "bucket": result["bucket"],
                "confidence": result["confidence"],
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _do_aesthetic_verify(self):
        """POST /v1/aesthetic/verify（v2.4 —— 组合验证入口）"""
        from photo_s_plugin_auto_tone.core.verifier import verify_aesthetic

        try:
            data = _read_json(self)
            image_path = data.get('image_path')
            if not image_path:
                self._send_json(400, {"error": "image_path required"})
                return
            result = verify_aesthetic(image_path,
                                      prefer=str(data.get('prefer', 'auto')))
            self._send_json(200, {"image_path": image_path, **result})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _do_advisor(self):
        """POST /v1/advisor"""
        from photo_s_plugin_auto_tone.core.advisor import ToneAdvisor
        from photo_s_plugin_auto_tone.core.pipeline import run_auto_tone

        try:
            data = _read_json(self)
            image_path = data.get('image_path')
            if not image_path:
                self._send_json(400, {"error": "image_path required"})
                return
            current = data.get('current_options')
            if not current:
                current = run_auto_tone(image_path, render=False,
                                        use_rag=True).get("options", {})
            self._send_json(200, ToneAdvisor().advise(image_path, current))
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _do_batch_auto_tone(self):
        """POST /v1/auto_tone/batch"""
        import time

        from photo_s_plugin_auto_tone.core.pipeline import run_auto_tone

        try:
            data = _read_json(self)
            image_paths = data.get('image_paths', [])
            if not image_paths:
                self._send_json(400, {"error": "image_paths required"})
                return

            t0 = time.time()
            results = []
            for path in image_paths:
                try:
                    r = run_auto_tone(path, strength=float(data.get('strength', 1.0)),
                                      render=True, use_rag=True)
                    results.append({
                        "image_path": path,
                        "status": "skipped" if r["metadata"].get("skipped") else "ok",
                        "confidence": r["confidence"],
                        "rendered_path": r.get("rendered_path"),
                    })
                except Exception as e:
                    results.append({
                        "image_path": path,
                        "status": "failed",
                        "reason": str(e),
                    })

            self._send_json(200, {
                "total": len(image_paths),
                "processed": sum(1 for r in results if r["status"] == "ok"),
                "failed": sum(1 for r in results if r["status"] == "failed"),
                "elapsed_sec": round(time.time() - t0, 2),
                "results": results,
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _plugin_route(self):
        if self.path == '/v1/auto_tone':
            _do_auto_tone(self)
        elif self.path == '/v1/aesthetic/score':
            _do_aesthetic(self)
        elif self.path == '/v1/aesthetic/verify':
            _do_aesthetic_verify(self)
        elif self.path == '/v1/advisor':
            _do_advisor(self)
        elif self.path == '/v1/auto_tone/batch':
            _do_batch_auto_tone(self)
        else:
            self.send_error(404, "Not Found")

    handler_class._do_auto_tone = _do_auto_tone
    handler_class._do_aesthetic = _do_aesthetic
    handler_class._do_aesthetic_verify = _do_aesthetic_verify
    handler_class._do_advisor = _do_advisor
    handler_class._do_batch_auto_tone = _do_batch_auto_tone

    # 幂等守卫：photo-s 每次创建 server 都会调用 register_routes（类级
    # 补丁是进程全局的）——重复包裹 do_POST 会让同一路由分发多次
    if getattr(handler_class, "_auto_tone_routes_patched", False):
        return
    handler_class._auto_tone_routes_patched = True

    _orig_do_POST = handler_class.do_POST

    def _patched_do_POST(self):
        if self.path.startswith('/v1/auto_tone') \
                or self.path in ('/v1/aesthetic/score',
                                 '/v1/aesthetic/verify', '/v1/advisor'):
            _plugin_route(self)
        else:
            _orig_do_POST(self)

    handler_class.do_POST = _patched_do_POST
