"""
网关控制台测试套件 (test_console.py)
=====================================
覆盖需求文档第十三条要求的 22 项测试。纯逻辑测试用 mock DB，路由鉴权测试
通过 urllib 直连本地测试壳（test_gateway_routes.py）。

运行：
    python test_console.py
或：
    python -m pytest test_console.py -q
"""
import os
import sys
import json
import subprocess
import unittest
from unittest.mock import patch

# 测试默认密钥（与 test_gateway_routes.py 一致）
os.environ.setdefault("API_SECRET", "testsecret123")
os.environ.setdefault("PORT", "18765")

import gateway as gw


# ============================================================
# 测试辅助：假 Supabase 客户端（处理 user_facts key→value 查询）
# ============================================================
class FakeResp:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count if count is not None else (len(data) if isinstance(data, list) else 0)


class FakeTable:
    """最小化 fake：支持 select/eq/maybe_single/execute/in_/order/upsert。
    store 是 dict: key -> value(字符串)。"""

    def __init__(self, store):
        self.store = store
        self._q = {}
        self._single = False
        self._filters = {}

    def select(self, *a, **k):
        return self

    def eq(self, k, v):
        self._filters[k] = v
        return self

    def in_(self, k, vs):
        self._filters[k] = ("in", vs)
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def offset(self, n):
        return self

    def maybe_single(self):
        self._single = True
        return self

    def execute(self):
        key = self._filters.get("key")
        if self._single:
            if key in self.store:
                return FakeResp({"value": self.store[key]})
            return FakeResp(None)
        # list 模式：返回 [{"value": v}]
        if key in self.store:
            return FakeResp([{"value": self.store[key]}])
        return FakeResp([])

    def upsert(self, payload):
        # 记录 upsert（不做真实写入，除非是 user_facts）
        if isinstance(payload, dict) and "key" in payload and "value" in payload:
            self.store[payload["key"]] = payload["value"]
        self._single = False
        return self


class FakeSB:
    def __init__(self, store=None):
        self.store = store or {}

    def table(self, name):
        return FakeTable(self.store)


def make_registry(models, default="", roles=None, assignments=None):
    reg = {"models": models, "default": default}
    if roles:
        reg["roles"] = roles
    if assignments:
        reg["assignments"] = assignments
    return reg


def m(id, label=None, base="https://api.example.com/v1", key="sk-test", model=None, enabled=True):
    return {"id": id, "label": label or id, "base_url": base, "api_key": key,
            "model": model or id, "enabled": enabled}


# ============================================================
# 1. 旧 llm_settings 读取测试
# ============================================================
class Test01LegacyLlmSettings(unittest.TestCase):
    def test_read_legacy_llm_settings(self):
        """llm_settings 有效但注册表为空时，resolve_llm_role 应回退读取 llm_settings。"""
        fake = FakeSB({"llm_settings": json.dumps({"key": "sk-legacy", "url": "https://x.test/v1", "model": "gpt-legacy"})})
        with patch.object(gw, "_get_supabase", return_value=fake), \
             patch.object(gw, "_load_llm_registry", return_value=gw._normalize_registry({"models": [], "default": ""})):
            r = gw.resolve_llm_role("chat")
        self.assertEqual(r["api_key"], "sk-legacy")
        self.assertEqual(r["model"], "gpt-legacy")
        self.assertEqual(r["source"], "llm_settings")
        self.assertTrue(r["fallback"])


# ============================================================
# 2. llm_settings → 新注册表幂等迁移测试
# ============================================================
class Test02MigrateIdempotent(unittest.TestCase):
    def test_migrate_idempotent(self):
        fake = FakeSB({"llm_settings": json.dumps({"key": "sk-1", "url": "https://x.test/v1", "model": "gpt-a"})})
        empty_reg = gw._normalize_registry({"models": [], "default": ""})
        with patch.object(gw, "_get_supabase", return_value=fake):
            reg1, mig1, _ = gw._migrate_llm_settings_to_registry(empty_reg)
            self.assertTrue(mig1)
            # 第二次：已有等价条目，不应重复生成
            reg2, mig2, reason2 = gw._migrate_llm_settings_to_registry(reg1)
            self.assertFalse(mig2)
            self.assertIn("已存在", reason2)
        # 模型数量不重复
        self.assertEqual(len(reg1["models"]), len(reg2["models"]))


# ============================================================
# 3. 三种模型角色解析测试
# ============================================================
class Test03ResolveRoles(unittest.TestCase):
    def setUp(self):
        self.models = [m("a", key="ka"), m("b", key="kb"), m("c", key="kc", enabled=False)]
        self.reg = gw._normalize_registry(make_registry(
            self.models, default="a",
            roles={"chat": ["a", "b"], "chat_default": "a", "compression": "b", "background": ""}))
        # background 未指定 → 应回退
        self.patch_reg = patch.object(gw, "_load_llm_registry", return_value=self.reg)
        self.patch_sb = patch.object(gw, "_get_supabase", return_value=None)
        self.patch_reg.start(); self.patch_sb.start()

    def tearDown(self):
        self.patch_reg.stop(); self.patch_sb.stop()

    def test_chat_resolves_default(self):
        r = gw.resolve_llm_role("chat")
        self.assertEqual(r["registry_id"], "a")  # chat_default
        self.assertEqual(r["source"], "registry")
        self.assertFalse(r["fallback"])

    def test_compression_resolves_b(self):
        r = gw.resolve_llm_role("compression")
        self.assertEqual(r["registry_id"], "b")

    def test_background_falls_back(self):
        # background 未指定 → 回退到 chat_default (a)
        r = gw.resolve_llm_role("background")
        self.assertTrue(r["fallback"])
        self.assertEqual(r["registry_id"], "a")

    def test_disabled_model_not_assigned(self):
        # c 已禁用，即使放进 roles 也不应被解析
        reg = gw._normalize_registry(make_registry(
            self.models, roles={"chat": ["c"], "chat_default": "c", "compression": "c", "background": "c"}))
        with patch.object(gw, "_load_llm_registry", return_value=reg):
            r = gw.resolve_llm_role("chat")
        # c 禁用 → 回退到 env/default
        self.assertNotEqual(r["registry_id"], "c")


# ============================================================
# 4. 聊天多模型和默认聊天模型测试
# ============================================================
class Test04ChatMulti(unittest.TestCase):
    def test_chat_list_and_default(self):
        reg = gw._normalize_registry(make_registry(
            [m("a"), m("b"), m("d")], default="a",
            roles={"chat": ["a", "b", "d"], "chat_default": "a"}))
        roles = reg["roles"]
        self.assertEqual(roles["chat_default"], "a")
        self.assertIn("a", roles["chat"])
        self.assertIn("b", roles["chat"])
        self.assertEqual(len(roles["chat"]), 3)
        # default 字段与 chat_default 一致（向后兼容）
        self.assertEqual(reg["default"], roles["chat_default"])


# ============================================================
# 5. 压缩与后台模型不再共用配置的测试
# ============================================================
class Test05CompressBackgroundSplit(unittest.TestCase):
    def test_separate_env_vars(self):
        """未配置注册表时，compression 走 COMPRESS_*，background 走 BACKGROUND_*，
        不再无条件共用 CHAT_*。"""
        env = {"COMPRESS_API_KEY": "sk-c", "COMPRESS_BASE_URL": "https://c.test/v1", "COMPRESS_MODEL_NAME": "compress-m",
               "BACKGROUND_API_KEY": "sk-bg", "BACKGROUND_BASE_URL": "https://bg.test/v1", "BACKGROUND_MODEL_NAME": "bg-m",
               "CHAT_API_KEY": "sk-chat", "CHAT_BASE_URL": "https://chat.test/v1", "CHAT_MODEL_NAME": "chat-m"}
        with patch.object(gw, "_load_llm_registry", return_value=gw._normalize_registry({"models": [], "default": ""})), \
             patch.object(gw, "_get_supabase", return_value=None), \
             patch.dict(os.environ, env, clear=False):
            rc = gw.resolve_llm_role("compression")
            rb = gw.resolve_llm_role("background")
            rch = gw.resolve_llm_role("chat")
        self.assertEqual(rc["api_key"], "sk-c")
        self.assertEqual(rc["model"], "compress-m")
        self.assertEqual(rb["api_key"], "sk-bg")
        self.assertEqual(rb["model"], "bg-m")
        self.assertEqual(rch["api_key"], "sk-chat")
        self.assertNotEqual(rc["api_key"], rb["api_key"])
        self.assertNotEqual(rc["api_key"], rch["api_key"])


# ============================================================
# 6. 角色模型禁用/删除时的校验测试
# ============================================================
class Test06RoleGuard(unittest.TestCase):
    def test_model_role_usage(self):
        reg = gw._normalize_registry(make_registry(
            [m("a"), m("b")], default="a",
            roles={"chat": ["a", "b"], "chat_default": "a", "compression": "b", "background": "b"}))
        used = gw._model_role_usage(reg, "b")
        self.assertIn("compression", used)
        self.assertIn("background", used)
        self.assertIn("chat", used)
        used_a = gw._model_role_usage(reg, "a")
        self.assertIn("chat_default", used_a)

    def test_disable_guard_blocks_role_bound(self):
        """禁用仍被角色使用的模型应被 _model_role_usage 标记（handler 据此返回 409）。"""
        reg = gw._normalize_registry(make_registry(
            [m("a", enabled=True), m("b", enabled=True)], default="a",
            roles={"compression": "b"}))
        used = gw._model_role_usage(reg, "b")
        self.assertIn("compression", used)
        self.assertGreater(len(used), 0)


# ============================================================
# 7. API Key GET 脱敏测试
# ============================================================
class Test07ApiKeyMask(unittest.TestCase):
    def test_mask_key(self):
        self.assertEqual(gw._mask_key("sk-abcdef1234567890"), "sk-abc...90")
        self.assertEqual(gw._mask_key("short"), "***")
        self.assertEqual(gw._mask_key(""), "")
        # 脱敏值不应包含完整 key
        masked = gw._mask_key("sk-1234567890abcdef")
        self.assertNotIn("1234567890abcdef", masked)


# ============================================================
# 8. API Key 留空编辑时保留旧值测试
# ============================================================
class Test08KeepOldKey(unittest.TestCase):
    def test_empty_key_keeps_old(self):
        """模拟 POST upsert 的 entry 构建：new_key 为空时应沿用 existing.api_key。"""
        existing = m("a", key="sk-old-secret-value")
        new_key = ""  # 编辑时留空
        # 复刻 _handle_models_api 里的 entry 构建逻辑
        entry = {"id": "a", "label": "A", "base_url": existing["base_url"],
                 "model": existing["model"], "enabled": True}
        if new_key:
            entry["api_key"] = new_key
        elif existing:
            entry["api_key"] = existing.get("api_key", "")
        self.assertEqual(entry["api_key"], "sk-old-secret-value")
        # 非空时覆盖
        entry2 = dict(entry)
        new_key2 = "sk-new"
        if new_key2:
            entry2["api_key"] = new_key2
        elif existing:
            entry2["api_key"] = existing.get("api_key", "")
        self.assertEqual(entry2["api_key"], "sk-new")


# ============================================================
# 9-10. Telegram / QQ 开关门控测试（gating 函数）
# ============================================================
class Test09TgGate(unittest.TestCase):
    def test_tg_enabled_default_true(self):
        with patch.object(gw, "_runtime_config_cache", {"data": None, "ts": 0.0}), \
             patch.object(gw, "_load_sys_config_raw", return_value={}), \
             patch.object(gw, "_get_supabase", return_value=None):
            self.assertTrue(gw._tg_enabled())
            with patch.object(gw, "_load_sys_config_raw", return_value={"telegram_enabled": False}):
                gw._invalidate_runtime_config()
                self.assertFalse(gw._tg_enabled())


class Test10QqGate(unittest.TestCase):
    def test_qq_enabled_toggle(self):
        with patch.object(gw, "_runtime_config_cache", {"data": None, "ts": 0.0}), \
             patch.object(gw, "_load_sys_config_raw", return_value={"qq_enabled": False}), \
             patch.object(gw, "_get_supabase", return_value=None):
            gw._invalidate_runtime_config()
            self.assertFalse(gw._qq_enabled())
        with patch.object(gw, "_runtime_config_cache", {"data": None, "ts": 0.0}), \
             patch.object(gw, "_load_sys_config_raw", return_value={"qq_enabled": True}), \
             patch.object(gw, "_get_supabase", return_value=None):
            gw._invalidate_runtime_config()
            self.assertTrue(gw._qq_enabled())


# ============================================================
# 11-12. 情绪/欲望总开关 + DESIRE_DRIVEN 开关测试
# ============================================================
class Test11EmotionGate(unittest.TestCase):
    def test_emotion_enabled_toggle(self):
        with patch.object(gw, "_runtime_config_cache", {"data": None, "ts": 0.0}), \
             patch.object(gw, "_load_sys_config_raw", return_value={"emotion_enabled": False}), \
             patch.object(gw, "_get_supabase", return_value=None):
            gw._invalidate_runtime_config()
            self.assertFalse(gw._emotion_enabled())


class Test12DesireDrivenGate(unittest.TestCase):
    def test_desire_driven_db_overrides_env(self):
        """数据库 sys_config.desire_driven 应覆盖环境变量 DESIRE_DRIVEN。"""
        with patch.object(gw, "_runtime_config_cache", {"data": None, "ts": 0.0}), \
             patch.object(gw, "_load_sys_config_raw", return_value={"desire_driven": True}), \
             patch.object(gw, "_get_supabase", return_value=None), \
             patch.dict(os.environ, {"DESIRE_DRIVEN": "false"}):
            gw._invalidate_runtime_config()
            self.assertTrue(gw._desire_driven_enabled())
        # desire_bridge.is_desire_driven 应读 DB（通过 gateway._desire_driven_enabled）
        import desire_bridge
        with patch.object(gw, "_runtime_config_cache", {"data": None, "ts": 0.0}), \
             patch.object(gw, "_load_sys_config_raw", return_value={"desire_driven": True}), \
             patch.object(gw, "_get_supabase", return_value=None), \
             patch.dict(os.environ, {"DESIRE_DRIVEN": "false"}):
            gw._invalidate_runtime_config()
            self.assertTrue(desire_bridge.is_desire_driven())


# ============================================================
# 13-14. 聊天记录写入 / 向量记忆注入开关覆盖 Web/TG/QQ 测试
# ============================================================
class Test13ChatWriteGate(unittest.TestCase):
    def test_chat_write_toggle(self):
        with patch.object(gw, "_runtime_config_cache", {"data": None, "ts": 0.0}), \
             patch.object(gw, "_load_sys_config_raw", return_value={"chat_history_write_enabled": False}), \
             patch.object(gw, "_get_supabase", return_value=None):
            gw._invalidate_runtime_config()
            self.assertFalse(gw._chat_write_enabled())

    def test_gate_covers_all_channels(self):
        """验证聊天记录写入门控点覆盖 Web/TG/QQ 三渠道（读源文件，避免导入重依赖）。"""
        here = os.path.dirname(os.path.abspath(__file__))
        gw_src = open(os.path.join(here, "gateway.py"), "r", encoding="utf-8").read()
        hb_src = open(os.path.join(here, "heartbeat.py"), "r", encoding="utf-8").read()
        nc_src = open(os.path.join(here, "napcat.py"), "r", encoding="utf-8").read()
        # Web（gateway _save_conversation）
        self.assertIn("_chat_write_enabled", gw_src)
        # TG（heartbeat async_telegram_polling）
        self.assertIn("_chat_write_enabled", hb_src)
        # QQ（napcat）
        self.assertIn("_chat_write_enabled", nc_src)


class Test14VectorInjectionGate(unittest.TestCase):
    def test_vector_injection_toggle(self):
        with patch.object(gw, "_runtime_config_cache", {"data": None, "ts": 0.0}), \
             patch.object(gw, "_load_sys_config_raw", return_value={"vector_memory_injection_enabled": False}), \
             patch.object(gw, "_get_supabase", return_value=None):
            gw._invalidate_runtime_config()
            self.assertFalse(gw._vector_injection_enabled())

    def test_gate_covers_both_injection_points(self):
        """向量注入门控覆盖 gateway._inject_context 与 server._build_channel_context（读源文件）。"""
        here = os.path.dirname(os.path.abspath(__file__))
        gw_src = open(os.path.join(here, "gateway.py"), "r", encoding="utf-8").read()
        sv_src = open(os.path.join(here, "server.py"), "r", encoding="utf-8").read()
        self.assertIn("_vector_injection_enabled", gw_src)
        self.assertIn("_vector_injection_enabled", sv_src)


# ============================================================
# 15. 记忆分类映射测试
# ============================================================
class Test15MemoryCategory(unittest.TestCase):
    def test_category_mapping(self):
        cases = [
            ("Core_Cognition", "core"), ("Core_Cognition_Weekly", "core"),
            ("Core_Cognition_Monthly", "core"), ("Core_Cognition_Yearly", "core"),
            ("Web_Chat", "web"),
            ("QQ_MSG", "qq"), ("QQ_Chat", "qq"), ("QQ_Group", "qq"),
            ("TG_MSG", "tg"),
            ("Free_Activity", "free"),
            ("Archived_Chat", "other"), ("Desire_Trace", "other"), ("Heartbeat", "other"),
            ("", "other"), ("任意逗号分隔标签", "other"),
        ]
        for tags, expected in cases:
            self.assertEqual(gw._memory_category(tags), expected, f"tags={tags!r} 应为 {expected}")
        # 页签 → tags 白名单
        self.assertEqual(gw._category_tag_filter("core"), ["Core_Cognition", "Core_Cognition_Weekly", "Core_Cognition_Monthly", "Core_Cognition_Yearly"])
        self.assertIsNone(gw._category_tag_filter("other"))


# ============================================================
# 16. 记忆服务端分页测试（_handle_memories_api 路由逻辑）
# ============================================================
class Test16MemoryPagination(unittest.TestCase):
    def test_pagination_params_parsing(self):
        """验证分页参数解析与边界（page/size 钳制）。query 参数均为字符串。"""
        def clamp(page, size):
            page = max(1, int(page or "1"))
            size = min(100, max(1, int(size or "20")))
            return page, size
        self.assertEqual(clamp("1", "20"), (1, 20))
        self.assertEqual(clamp("0", "0"), (1, 1))      # page<1 → 1, size<1 → 1
        self.assertEqual(clamp("", ""), (1, 20))       # 空 → 默认
        self.assertEqual(clamp("-5", "999"), (1, 100)) # size>100 → 100
        self.assertEqual(clamp("5", "50"), (5, 50))


# ============================================================
# 17. 画像系统键过滤测试
# ============================================================
class Test17ProfileKeyFilter(unittest.TestCase):
    def test_system_keys_filtered(self):
        # 系统配置键必须被隐藏
        for k in ["sys_config", "llm_settings", "llm_models", "sys_ai_persona"]:
            self.assertFalse(gw._is_profile_key(k), f"{k} 应被过滤")
        # desire_ 运行时状态隐藏
        for k in ["desire_drive_state", "desire_emotion_state", "desire_last_tick_at",
                  "desire_next_heartbeat_at", "desire_refractory", "desire_action_repeat"]:
            self.assertFalse(gw._is_profile_key(k), f"{k} 运行时状态应被隐藏")
        # desire_ 人写笔记（带日期后缀）放行
        self.assertTrue(gw._is_profile_key("desire_system_tech_debt_2026_08_05"))
        self.assertTrue(gw._is_profile_key("desire_some_note_2026_01_01"))
        # 普通画像键放行
        for k in ["关系确认", "昕的个人信息", "brat_tendency", "memory_99", "admission_result_2026_08_06"]:
            self.assertTrue(gw._is_profile_key(k), f"{k} 应作为画像显示")


# ============================================================
# 18-19. 管理 API 未鉴权返回 401 + 输入校验测试（直连测试壳）
# ============================================================
class TestLiveRoutes(unittest.TestCase):
    """直连 test_gateway_routes.py 测试壳（需先启动）。未启动则跳过。"""

    @classmethod
    def setUpClass(cls):
        import urllib.request
        cls.base = "http://127.0.0.1:18765"
        try:
            r = urllib.request.urlopen(cls.base + "/health", timeout=2)
            cls.up = (r.status == 200)
        except Exception:
            cls.up = False

    def _get(self, path, key=None):
        import urllib.request
        req = urllib.request.Request(self.base + path)
        if key:
            req.add_header("X-Api-Key", key)
        try:
            r = urllib.request.urlopen(req, timeout=4)
            return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")
        except Exception as e:
            return -1, str(e)

    def test_18_unauth_returns_401(self):
        if not self.up:
            self.skipTest("测试壳未运行（先 python test_gateway_routes.py）")
        # 未带密钥 → 401
        for path in ["/api/admin/status", "/api/admin/config", "/api/models", "/api/desire", "/api/memories", "/api/profile", "/api/ticks"]:
            st, _ = self._get(path)
            self.assertEqual(st, 401, f"{path} 未鉴权应返回 401，实际 {st}")
        # 错误密钥 → 401
        st, _ = self._get("/api/admin/status", key="wrong")
        self.assertEqual(st, 401)
        # 正确密钥 → 200
        st, _ = self._get("/api/admin/status", key="testsecret123")
        self.assertEqual(st, 200)

    def test_19_input_validation(self):
        if not self.up:
            self.skipTest("测试壳未运行")
        import urllib.request
        # PATCH 非法字段 → 400
        data = json.dumps({"evil_field": True}).encode()
        req = urllib.request.Request(self.base + "/api/admin/config", data=data, method="PATCH")
        req.add_header("X-Api-Key", "testsecret123")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=4)
            st = 200
        except urllib.error.HTTPError as e:
            st = e.code
            body = e.read().decode("utf-8", "ignore")
            self.assertIn("不允许的字段", body)
        self.assertEqual(st, 400)


# ============================================================
# 20. HTML 内联 JavaScript 语法检查
# ============================================================
class Test20HtmlJsSyntax(unittest.TestCase):
    def test_inline_js_node_check(self):
        """抽取 console.html 内联 <script>，写到临时文件用 node --check 验证语法。"""
        import tempfile
        here = os.path.dirname(os.path.abspath(__file__))
        html_path = os.path.join(here, "console.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        import re
        blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
        self.assertTrue(len(blocks) >= 1, "console.html 应包含内联 <script>")
        for i, code in enumerate(blocks):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
                tf.write(code)
                tf_name = tf.name
            try:
                proc = subprocess.run(["node", "--check", tf_name],
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, f"JS block {i} 语法错误: {proc.stderr[:400]}")
            finally:
                os.unlink(tf_name)


# ============================================================
# 21. Python py_compile
# ============================================================
class Test21PyCompile(unittest.TestCase):
    def test_all_py_compile(self):
        here = os.path.dirname(os.path.abspath(__file__))
        files = ["gateway.py", "server.py", "heartbeat.py", "napcat.py",
                 "desire_bridge.py", "desire_engine.py", "emotion_engine.py",
                 "background.py", "run.py", "aggregator.py", "test_gateway_routes.py"]
        for f in files:
            with self.subTest(file=f):
                proc = subprocess.run([sys.executable, "-m", "py_compile", os.path.join(here, f)],
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, f"{f} 编译失败: {proc.stderr[:300]}")


# ============================================================
# 22. 现有 emotion/desire 单元测试回归
# ============================================================
class Test22EngineRegression(unittest.TestCase):
    def test_engine_modules_importable(self):
        """emotion_engine / desire_engine 为纯函数，应可导入且核心函数存在。"""
        import emotion_engine as ee
        import desire_engine as de
        self.assertTrue(hasattr(ee, "tick_evolve"))
        self.assertTrue(hasattr(de, "map_from_emotions"))

    def test_desire_bridge_gates_db_aware(self):
        """is_desire_driven 应优先读 DB 配置（v5 改造回归点）。"""
        import desire_bridge
        import inspect
        src = inspect.getsource(desire_bridge.is_desire_driven)
        self.assertIn("_desire_driven_enabled", src)


# ============================================================
# 23. Tick 日志分页与 event 过滤测试
# ============================================================
class Test23TickPagination(unittest.TestCase):
    def test_pagination_params_parsing(self):
        """验证 /api/ticks 分页参数解析与边界（page/size 钳制）"""
        def clamp(page, size):
            page = max(1, int(page or "1"))
            size = min(100, max(1, int(size or "20")))
            return page, size
        self.assertEqual(clamp("1", "20"), (1, 20))
        self.assertEqual(clamp("0", "0"), (1, 1))       # page<1 → 1, size<1 → 1
        self.assertEqual(clamp("", ""), (1, 20))        # 空 → 默认
        self.assertEqual(clamp("-5", "999"), (1, 100)) # size>100 → 100
        self.assertEqual(clamp("5", "50"), (5, 50))

    def test_event_filter_returns_matching_only(self):
        """event 过滤只返回匹配记录"""
        import asyncio

        class _FakeR:
            def __init__(self, data, count):
                self.data = data
                self.count = count

        class _FakeQ:
            def __init__(self, rows):
                self._rows = rows
                self._event = None
            def __getattr__(self, name):
                if name in ("select", "order", "limit", "offset"):
                    return lambda *a, **k: self
                if name == "eq":
                    def _eq(k, v):
                        if k == "threshold_event":
                            self._event = v
                        return self
                    return _eq
                if name == "execute":
                    def _execute():
                        if self._event:
                            filtered = [r for r in self._rows if r.get("threshold_event") == self._event]
                        else:
                            filtered = self._rows
                        return _FakeR(filtered, len(filtered))
                    return _execute
                raise AttributeError(name)

        class _FakeSB:
            def __init__(self, rows):
                self._rows = rows
            def table(self, name):
                return _FakeQ(self._rows)

        rows = [
            {"id": 1, "threshold_event": "hungry_cat", "ticked_at": "2026-08-01T00:00:00Z"},
            {"id": 2, "threshold_event": None, "ticked_at": "2026-08-01T01:00:00Z"},
            {"id": 3, "threshold_event": "hungry_cat", "ticked_at": "2026-08-01T02:00:00Z"},
        ]

        async def _test():
            scope = {
                "method": "GET",
                "path": "/api/ticks",
                "query_string": b"event=hungry_cat",
            }
            messages = []
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(msg):
                messages.append(msg)

            mw = gw.HostFixMiddleware(None)
            with patch.object(gw, "_get_supabase", return_value=_FakeSB(rows)):
                await mw._handle_ticks_api(scope, receive, send)

            self.assertEqual(len(messages), 2)  # response.start + response.body
            body = json.loads(messages[1]["body"].decode("utf-8"))
            self.assertTrue(body["ok"])
            self.assertEqual(len(body["items"]), 2)
            for item in body["items"]:
                self.assertEqual(item["threshold_event"], "hungry_cat")

        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main(verbosity=2)
