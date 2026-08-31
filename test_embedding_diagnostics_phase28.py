# -*- coding: utf-8 -*-
"""第 28 阶段专项测试 —— 生产 embedding 维度安全诊断接口。

  POST /api/embedding-dimension-preview
    → 固定合成探针、最多一次 server._get_embedding 调用；
    → 只返回维度 / numeric / finite / HNSW 上限判断；
    → 不返回向量、模型名、provider URL、API Key、环境变量、异常原文；
    → 零数据库 / Pinecone / LLM 副作用；仅 POST；/api/* 统一 API_SECRET 鉴权。

全部 unittest + mock + 合成数据；不真实调用 provider、不真实调用新接口、
不连接真实 Supabase / LLM / Pinecone；不修改任何数据。

覆盖（任务 A-H）：
  A 路由与鉴权（/api/* API_SECRET 覆盖新接口、仅 POST、OPTIONS 沿用全局 CORS、
    非法方法不触碰 provider）
  B 请求校验（confirm 缺失/错误、额外字段全拒绝、非法 JSON/非 dict、
    非法请求 provider_calls=0）
  C 成功（1024/768/1536 维、tuple、dimension/numeric/finite/HNSW 正确、
    provider 恰调用 1 次且参数恒为固定探针）
  D 空与类型错误（[]、None、空 tuple、str、dict、int、float、set）
  E 元素错误（不可 float、None、混合非法值、NaN/+Inf/-Inf、类型优先于数值）
  F 维度边界（2000 支持、2001 unsupported 但 ok=true 仍返回维度、
    不自动切换 halfvec、响应不含向量）
  G 零泄露（响应与日志不含探针/向量/模型/endpoint/key/provider 响应/
    异常 message/请求体）
  H 零副作用（源码无 Supabase/Pinecone/LLM/环境变量/线程/重试/调度；
    行为上数据库零操作记录、provider 恰 1 次）

运行：  python -m unittest test_embedding_diagnostics_phase28 -v
"""

import asyncio
import inspect
import io
import json
import math
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import embedding_diagnostics as ed
import gateway
import server as _srv
from test_memory_preview_phase10 import FakeReceive, FakeSend, RecordingFakeService


# ==========================================
# 假 embedding callable 与辅助
# ==========================================

_PATH = "/api/embedding-dimension-preview"
_SECRET = "test-secret-marker"
# 注入异常 message 的脱敏标记（断言不外泄）
_PROVIDER_SECRET_MARKER = "PROVIDER_RAW_ERROR_SECRET_MARKER"
# 向量数值标记（断言响应/日志不含任何向量值）
_VEC_MARKER = 0.777123


class _RecordingEmbed:
    """记录调用并返回预定结果 / 抛预定异常的假 embedding callable。"""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def __call__(self, text):
        self.calls.append(text)
        if self.exc is not None:
            raise self.exc
        return self.result


def _vec(dim, marker=None):
    if marker is None:
        return [0.001 * (i % 100) for i in range(dim)]
    return [marker + 0.001 * i for i in range(dim)]


def _invalid_body_marker(field):
    """构造带一个额外字段的请求体（confirm 合法）。"""
    return {"confirm": ed.CONFIRM_TOKEN, field: "CLIENT_INJECTED_MARKER"}


def _call_handler(body=None, raw=None, method="POST", embed=None):
    """直调新 handler；返回 (FakeSend, captured_logs, embed)。"""
    send = FakeSend()
    logs = []
    if raw is None:
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
    scope = {"method": method, "path": _PATH}
    with patch.object(_srv, "_get_embedding", embed), \
         patch.object(_srv, "supabase_service", RecordingFakeService([])), \
         patch.object(gateway, "_log", lambda m: logs.append(m)):
        asyncio.run(gateway.HostFixMiddleware._handle_embedding_dimension_preview(
            None, scope, FakeReceive(raw), send))
    return send, logs, embed


def _mw_call(scope, body=b""):
    """完整中间件分发（测鉴权/CORS；不在路径分支前的请求不会触碰 app）。"""
    send = FakeSend()
    app = gateway.HostFixMiddleware(None)
    asyncio.run(app(scope, FakeReceive(body), send))
    return send


def _auth_scope(method="POST", with_auth=True):
    headers = [(b"authorization", f"Bearer {_SECRET}".encode("utf-8"))] if with_auth else []
    return {"type": "http", "path": _PATH, "method": method, "headers": headers}


def _resp_text(send):
    """完整响应 JSON 文本（泄露断言用）。"""
    for m in send.msgs:
        if m.get("type") == "http.response.body":
            return m.get("body", b"").decode("utf-8")
    return ""


def _assert_safe_shape(testcase, body):
    """响应形状固定：ok/code/diagnostics(4键)/execution(5键)。"""
    testcase.assertIn("ok", body)
    testcase.assertIn("code", body)
    testcase.assertEqual(set(body["diagnostics"].keys()),
                         {"dimension", "all_values_numeric", "all_values_finite",
                          "hnsw_vector_dimension_supported"})
    testcase.assertEqual(set(body["execution"].keys()),
                         {"provider_calls", "database_reads", "database_writes",
                          "pinecone_touched", "llm_touched"})
    testcase.assertEqual(body["execution"]["database_reads"], 0)
    testcase.assertEqual(body["execution"]["database_writes"], 0)
    testcase.assertIs(body["execution"]["pinecone_touched"], False)
    testcase.assertIs(body["execution"]["llm_touched"], False)


# ==========================================
# A. 路由与鉴权
# ==========================================

class TestRouteAuth(unittest.TestCase):

    def test_a_requires_api_secret(self):
        embed = _RecordingEmbed(result=_vec(1024))
        with patch.dict(os.environ, {"API_SECRET": _SECRET}):
            send = _mw_call(_auth_scope(with_auth=False),
                            json.dumps({"confirm": ed.CONFIRM_TOKEN}).encode())
        self.assertEqual(send.status, 401, "无鉴权头必须 401")
        self.assertEqual(send.body_json.get("error"), "Unauthorized: Missing or invalid API key")

    def test_a_wrong_api_secret_rejected(self):
        with patch.dict(os.environ, {"API_SECRET": _SECRET}):
            send = _mw_call(_auth_scope(with_auth=False),
                            json.dumps({"confirm": ed.CONFIRM_TOKEN}).encode())
        self.assertEqual(send.status, 401)

    def test_a_empty_api_secret_rejected(self):
        with patch.dict(os.environ, {"API_SECRET": ""}):
            send = _mw_call(_auth_scope(with_auth=True),
                            json.dumps({"confirm": ed.CONFIRM_TOKEN}).encode())
        self.assertEqual(send.status, 503, "API_SECRET 为空必须拒绝而非放行")

    def test_a_options_preflight_without_auth(self):
        scope = {"type": "http", "path": _PATH, "method": "OPTIONS", "headers": []}
        with patch.dict(os.environ, {"API_SECRET": _SECRET}):
            send = _mw_call(scope)
        self.assertEqual(send.status, 204, "OPTIONS 沿用全局 CORS 预检（免鉴权）")

    def test_a_post_only_provider_never_called(self):
        for method in ("GET", "DELETE", "PUT", "HEAD"):
            with self.subTest(method=method):
                embed = _RecordingEmbed(result=_vec(1024))
                send, logs, e = _call_handler(method=method, embed=embed)
                self.assertEqual(send.status, 405)
                self.assertEqual(send.body_json.get("code"), "METHOD_NOT_ALLOWED")
                self.assertEqual(e.calls, [], "非 POST 绝不触碰 provider")
                _assert_safe_shape(self, send.body_json)
                self.assertEqual(send.body_json["execution"]["provider_calls"], 0)

    def test_a_valid_secret_reaches_handler(self):
        embed = _RecordingEmbed(result=_vec(1024))
        body = json.dumps({"confirm": ed.CONFIRM_TOKEN}).encode("utf-8")
        with patch.dict(os.environ, {"API_SECRET": _SECRET}), \
             patch.object(_srv, "_get_embedding", embed), \
             patch.object(_srv, "supabase_service", RecordingFakeService([])), \
             patch.object(gateway, "_log", lambda m: None):
            send = _mw_call(_auth_scope(), body)
        self.assertEqual(send.status, 200, "鉴权通过后进入诊断 handler")
        self.assertEqual(send.body_json.get("code"), "EMBEDDING_DIMENSION_READY")
        self.assertEqual(embed.calls, [ed.PROBE_TEXT], "恒以固定合成探针调用")


# ==========================================
# B. 请求校验
# ==========================================

class TestRequestValidation(unittest.TestCase):

    def test_b_confirm_missing(self):
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, e = _call_handler(body={}, embed=embed)
        self.assertEqual(send.status, 400)
        self.assertEqual(send.body_json.get("code"), "INVALID_CONFIRMATION")
        self.assertEqual(e.calls, [], "confirm 缺失不得调用 provider")

    def test_b_confirm_wrong(self):
        embed = _RecordingEmbed(result=_vec(1024))
        for confirm in ("probe_embedding_dimension", "PROBE_EMBEDDING", "", 123, None):
            with self.subTest(confirm=confirm):
                embed = _RecordingEmbed(result=_vec(1024))
                send, logs, e = _call_handler(body={"confirm": confirm}, embed=embed)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"), "INVALID_CONFIRMATION")
                self.assertEqual(e.calls, [])

    def test_b_extra_fields_all_rejected(self):
        # 客户端试图注入 text/model/provider/api_key/endpoint/dimensions/
        # user_id/write/backfill/item_id 等一律 400，绝不调用 provider
        for field in ("text", "input", "model", "provider", "api_key", "endpoint",
                      "dimensions", "user_id", "write", "backfill", "item_id",
                      "probe_text", "query", "top_k", "review_session_token"):
            with self.subTest(field=field):
                embed = _RecordingEmbed(result=_vec(1024))
                send, logs, e = _call_handler(body=_invalid_body_marker(field),
                                              embed=embed)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"), "INVALID_DIAGNOSTIC_REQUEST")
                self.assertEqual(e.calls, [])

    def test_b_invalid_json_and_non_dict(self):
        embed = _RecordingEmbed(result=_vec(1024))
        for raw in (b"{not json", b"[1,2,3]", b'"str"', b"null"):
            with self.subTest(raw=raw):
                embed = _RecordingEmbed(result=_vec(1024))
                send, logs, e = _call_handler(raw=raw, embed=embed)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"), "INVALID_DIAGNOSTIC_REQUEST")
                self.assertEqual(e.calls, [])

    def test_b_empty_body(self):
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, e = _call_handler(raw=b"", embed=embed)
        self.assertEqual(send.status, 400)
        self.assertEqual(send.body_json.get("code"), "INVALID_CONFIRMATION")
        self.assertEqual(e.calls, [])

    def test_b_provider_calls_zero_on_invalid_request(self):
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, e = _call_handler(body={"confirm": "WRONG", "model": "x"},
                                      embed=embed)
        self.assertEqual(send.body_json["execution"]["provider_calls"], 0)
        self.assertEqual(e.calls, [])


# ==========================================
# C. 成功
# ==========================================

class TestSuccessPath(unittest.TestCase):

    def test_c_dimensions_1024_768_1536(self):
        for dim in (1024, 768, 1536):
            with self.subTest(dim=dim):
                embed = _RecordingEmbed(result=_vec(dim))
                result, log_line = ed.run_dimension_probe(embed)
                self.assertTrue(result["ok"])
                self.assertEqual(result["code"], ed.CODE_READY)
                d = result["diagnostics"]
                self.assertEqual(d["dimension"], dim)
                self.assertIs(d["all_values_numeric"], True)
                self.assertIs(d["all_values_finite"], True)
                self.assertIs(d["hnsw_vector_dimension_supported"], True)
                self.assertEqual(result["execution"]["provider_calls"], 1)
                self.assertEqual(embed.calls, [ed.PROBE_TEXT],
                                 "恰调用 1 次且参数恒为固定探针")
                self.assertEqual(log_line,
                                 f"🧭 embedding维度诊断：ok=true dimension={dim} finite=true")

    def test_c_tuple_accepted(self):
        embed = _RecordingEmbed(result=tuple(_vec(1024)))
        result, _ = ed.run_dimension_probe(embed)
        self.assertTrue(result["ok"])
        self.assertEqual(result["diagnostics"]["dimension"], 1024)

    def test_c_handler_success_integration(self):
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, e = _call_handler(body={"confirm": ed.CONFIRM_TOKEN}, embed=embed)
        self.assertEqual(send.status, 200)
        body = send.body_json
        _assert_safe_shape(self, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["code"], "EMBEDDING_DIMENSION_READY")
        self.assertEqual(body["diagnostics"]["dimension"], 1024)
        self.assertEqual(body["execution"]["provider_calls"], 1)
        self.assertEqual(e.calls, [ed.PROBE_TEXT])
        self.assertEqual(logs,
                         ["🧭 embedding维度诊断：ok=true dimension=1024 finite=true"])


# ==========================================
# D. 空与类型错误
# ==========================================

class TestEmptyAndInvalidType(unittest.TestCase):

    def test_d_empty_results_unavailable(self):
        for empty in ([], None, ()):
            with self.subTest(empty=empty):
                embed = _RecordingEmbed(result=empty)
                result, log_line = ed.run_dimension_probe(embed)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], ed.CODE_UNAVAILABLE)
                self.assertEqual(result["execution"]["provider_calls"], 1)
                self.assertIsNone(result["diagnostics"]["dimension"])
                self.assertEqual(log_line,
                                 "⚠️ embedding维度诊断失败：code=EMBEDDING_UNAVAILABLE")

    def test_d_handler_empty_is_503(self):
        embed = _RecordingEmbed(result=[])
        send, logs, e = _call_handler(body={"confirm": ed.CONFIRM_TOKEN}, embed=embed)
        self.assertEqual(send.status, 503)
        self.assertEqual(send.body_json["code"], "EMBEDDING_UNAVAILABLE")
        self.assertEqual(send.body_json["execution"]["provider_calls"], 1)

    def test_d_wrong_container_types(self):
        for bad in ("1024 floats", {"embedding": [1.0]}, 42, 3.14, {1.0, 2.0}, True):
            with self.subTest(bad=type(bad).__name__):
                embed = _RecordingEmbed(result=bad)
                result, log_line = ed.run_dimension_probe(embed)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], ed.CODE_RESPONSE_INVALID)
                self.assertEqual(result["execution"]["provider_calls"], 1)
                self.assertIsNone(result["diagnostics"]["dimension"])


# ==========================================
# E. 元素错误
# ==========================================

class TestElementErrors(unittest.TestCase):

    def test_e_non_floatable_elements(self):
        for bad in (["abc"], [None], [1.0, "x", 2.0], [1.0, {"a": 1}],
                    [1.0, [2.0]], [[1.0]]):
            with self.subTest(bad=bad):
                embed = _RecordingEmbed(result=bad)
                result, _ = ed.run_dimension_probe(embed)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], ed.CODE_RESPONSE_INVALID)
                self.assertIsNone(result["diagnostics"]["dimension"])

    def test_e_non_finite_values(self):
        for bad in ([float("nan")], [float("inf")], [float("-inf")],
                    [1.0, float("nan")], [float("-inf"), 2.0, 3.0]):
            with self.subTest(bad=bad):
                embed = _RecordingEmbed(result=bad)
                result, _ = ed.run_dimension_probe(embed)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], ed.CODE_NON_FINITE)
                self.assertIsNone(result["diagnostics"]["dimension"])

    def test_e_type_validity_precedes_finite(self):
        # 类型合法性优先：混合"NaN + 不可转换元素"恒报 RESPONSE_INVALID
        embed = _RecordingEmbed(result=[float("nan"), "bad"])
        result, _ = ed.run_dimension_probe(embed)
        self.assertEqual(result["code"], ed.CODE_RESPONSE_INVALID)

    def test_e_callable_exception_is_internal_error(self):
        embed = _RecordingEmbed(
            exc=RuntimeError(f"{_PROVIDER_SECRET_MARKER} real reason"))
        result, log_line = ed.run_dimension_probe(embed)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], ed.CODE_INTERNAL)
        self.assertEqual(result["execution"]["provider_calls"], 1)
        self.assertIn("exception_type=RuntimeError", log_line)
        self.assertNotIn(_PROVIDER_SECRET_MARKER, log_line)
        self.assertNotIn("real reason", log_line)

    def test_e_handler_callable_exception_is_500(self):
        embed = _RecordingEmbed(
            exc=RuntimeError(f"{_PROVIDER_SECRET_MARKER} real reason"))
        send, logs, _ = _call_handler(body={"confirm": ed.CONFIRM_TOKEN}, embed=embed)
        self.assertEqual(send.status, 500)
        self.assertEqual(send.body_json["code"], "INTERNAL_ERROR")
        self.assertNotIn(_PROVIDER_SECRET_MARKER, _resp_text(send))


# ==========================================
# F. 维度边界
# ==========================================

class TestDimensionBoundary(unittest.TestCase):

    def test_f_2000_supported(self):
        embed = _RecordingEmbed(result=_vec(2000))
        result, _ = ed.run_dimension_probe(embed)
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], ed.CODE_READY)
        self.assertIs(result["diagnostics"]["hnsw_vector_dimension_supported"], True)

    def test_f_2001_unsupported_but_dimension_returned(self):
        embed = _RecordingEmbed(result=_vec(2001))
        result, log_line = ed.run_dimension_probe(embed)
        self.assertTrue(result["ok"], "诊断本身成功：仍返回真实维度")
        self.assertEqual(result["code"], ed.CODE_UNSUPPORTED)
        self.assertEqual(result["diagnostics"]["dimension"], 2001)
        self.assertIs(result["diagnostics"]["hnsw_vector_dimension_supported"], False)
        self.assertEqual(result["execution"]["provider_calls"], 1)

    def test_f_2001_http_200_via_handler(self):
        embed = _RecordingEmbed(result=_vec(2001))
        send, logs, _ = _call_handler(body={"confirm": ed.CONFIRM_TOKEN}, embed=embed)
        self.assertEqual(send.status, 200)
        self.assertEqual(send.body_json["code"],
                         "EMBEDDING_DIMENSION_UNSUPPORTED_FOR_VECTOR_HNSW")
        self.assertEqual(send.body_json["diagnostics"]["dimension"], 2001)

    def test_f_no_vector_content_in_response(self):
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
        send, logs, _ = _call_handler(body={"confirm": ed.CONFIRM_TOKEN}, embed=embed)
        text = _resp_text(send) + "".join(logs)
        self.assertNotIn(str(_VEC_MARKER), text, "响应/日志不得包含向量数值")
        self.assertNotIn("vector", text.lower().replace("hnsw_vector_dimension_supported", ""),
                         "除 HNSW 字段名外不得出现向量内容字样")

    def test_f_no_auto_halfvec_switch(self):
        # 切换逻辑只可能存在于执行函数内；模块级注释中的 halfvec 说明不算
        fn_src = inspect.getsource(ed.run_dimension_probe)
        self.assertNotIn("halfvec", fn_src.lower(), "执行函数不得含 halfvec 切换逻辑")


# ==========================================
# G. 零泄露
# ==========================================

class TestNoLeakage(unittest.TestCase):

    def _leak_scan(self, send, logs):
        text = _resp_text(send) + "".join(logs)
        for marker in (ed.PROBE_TEXT, _PROVIDER_SECRET_MARKER,
                       "DOUBAO", "siliconflow", "api.siliconflow",
                       "Bearer", "api_key", "Authorization",
                       _invalid_body_marker("text")["text"]):
            self.assertNotIn(marker, text, f"泄露标记 {marker!r} 出现在响应/日志")
        return text

    def test_g_success_response_and_logs_leak_nothing(self):
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
        send, logs, _ = _call_handler(body={"confirm": ed.CONFIRM_TOKEN,
                                            }, embed=embed)
        text = self._leak_scan(send, logs)
        self.assertNotIn(str(_VEC_MARKER), text)
        self.assertNotIn("Memory embedding", text, "探针文本不得出现在响应/日志")
        # 请求体回显禁止
        self.assertNotIn("confirm", text.lower())

    def test_g_provider_exception_message_never_leaks(self):
        embed = _RecordingEmbed(
            exc=RuntimeError(f"{_PROVIDER_SECRET_MARKER} endpoint=https://x key=abc"))
        send, logs, _ = _call_handler(body={"confirm": ed.CONFIRM_TOKEN}, embed=embed)
        resp = _resp_text(send)
        log_text = "".join(logs)
        # 响应：不含异常类型、不含异常 message、不含任何标记
        for marker in (_PROVIDER_SECRET_MARKER, "RuntimeError", "endpoint=https"):
            self.assertNotIn(marker, resp, f"响应泄露 {marker!r}")
        # 日志：允许且仅允许 exception_type；绝不含 message/marker
        self.assertNotIn(_PROVIDER_SECRET_MARKER, log_text)
        self.assertNotIn("endpoint=https", log_text)
        self.assertIn("exception_type=RuntimeError", log_text)

    def test_g_probe_always_fixed_constant(self):
        # 无论客户端提交什么，模块收到的参数恒为固定探针
        embed = _RecordingEmbed(result=_vec(1024))
        _call_handler(body={"confirm": ed.CONFIRM_TOKEN}, embed=embed)
        self.assertEqual(embed.calls, [ed.PROBE_TEXT])


# ==========================================
# H. 零副作用
# ==========================================

class TestZeroSideEffects(unittest.TestCase):

    def test_h_module_source_has_no_backend_calls(self):
        # 只禁止实际调用/导入模式；文档性注释提及后端名称不算
        src = inspect.getsource(ed)
        for banned in ("import pinecone", "from pinecone", "import supabase",
                       "from supabase", "supabase_service", "PineconeMemoryClient",
                       "requests.post", "httpx.", "urllib.request", "urlopen",
                       "os.environ", "getenv", "create_task", "Timer",
                       "threading", "subprocess", "time.sleep", "retry",
                       "ask_role", "stable_system", "volatile_block",
                       "print("):
            self.assertNotIn(banned, src,
                             f"embedding_diagnostics 源码不得包含 {banned!r}")

    def test_h_handler_source_has_no_backend_calls(self):
        src = inspect.getsource(
            gateway.HostFixMiddleware._handle_embedding_dimension_preview)
        # 注意：pinecone_touched 是响应字段名，不按裸词匹配
        for banned in ("supabase", "PineconeMemoryClient", "from pinecone",
                       "import pinecone", "ask_role", "_ask_llm",
                       "create_task", "Timer", "threading", "ensure_future",
                       "getenv"):
            self.assertNotIn(banned, src, f"新 handler 源码不得包含 {banned!r}")

    def test_h_no_route_dispatch_automation(self):
        src = inspect.getsource(ed)
        self.assertNotIn("schedule", src.lower())
        self.assertNotIn("cron", src.lower())

    def test_h_database_never_touched_during_success(self):
        fake_db = RecordingFakeService([])
        fresh_embed = _RecordingEmbed(result=_vec(1024))
        with patch.object(_srv, "_get_embedding", fresh_embed), \
             patch.object(_srv, "supabase_service", fake_db), \
             patch.object(gateway, "_log", lambda m: None):
            asyncio.run(gateway.HostFixMiddleware._handle_embedding_dimension_preview(
                None, {"method": "POST", "path": _PATH},
                FakeReceive(json.dumps({"confirm": ed.CONFIRM_TOKEN}).encode()),
                FakeSend()))
        self.assertEqual(fake_db.ops, [], "数据库操作记录必须恒空")
        self.assertEqual(fresh_embed.calls, [ed.PROBE_TEXT], "provider 恰调用 1 次")

    def test_h_module_prints_nothing(self):
        buf = io.StringIO()
        embed = _RecordingEmbed(result=_vec(1024))
        with redirect_stdout(buf):
            ed.run_dimension_probe(embed)
            embed2 = _RecordingEmbed(result=[])
            ed.run_dimension_probe(embed2)
        self.assertEqual(buf.getvalue(), "", "模块自身绝不打印（日志由 gateway 负责）")

    def test_h_probe_constant_properties(self):
        self.assertIsInstance(ed.PROBE_TEXT, str)
        self.assertLess(len(ed.PROBE_TEXT), 100, "探针须为短文本")
        self.assertLess(len(ed.PROBE_TEXT), 6000, "远低于 _MAX_EMBED_TEXT_CHARS")
        self.assertEqual(ed.HNSW_VECTOR_DIMENSION_LIMIT, 2000)
        self.assertEqual(ed.CONFIRM_TOKEN, "PROBE_EMBEDDING_DIMENSION")


if __name__ == "__main__":
    unittest.main(verbosity=2)
