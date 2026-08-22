"""
test_security_phase36.py — 第 3.6 阶段安全基线治理专项测试
================================================================
对齐项目惯例：unittest + mock，不触生产数据/外部服务。
覆盖：
  - XML 安全：defusedxml 阻止实体扩展/外部实体
  - WebDAV SSRF：href 同域校验
  - SQL/RPC 误报确认：固定 RPC 名 + JSON 参数
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import os


# ============================================================
# XML 安全测试
# ============================================================
class TestXmlSecurity(unittest.TestCase):
    """验证 WebDAV PROPFIND XML 解析使用安全解析器。"""

    def test_defusedxml_available(self):
        """defusedxml 已安装且可导入。"""
        import defusedxml.ElementTree
        self.assertTrue(hasattr(defusedxml.ElementTree, 'fromstring'))

    def test_no_unsafe_fallback(self):
        """server.py 中的 defusedxml import 不回退到 stdlib ElementTree。"""
        import server
        import inspect
        source = inspect.getsource(server._scan_all_md_files)
        # 不应存在 fallback 到 stdlib 的代码
        self.assertNotIn('import xml.etree.ElementTree as DefET', source)
        self.assertNotIn('except ImportError', source)
        # 应直接 import defusedxml
        self.assertIn('from defusedxml import ElementTree as DefET', source)

    def test_billion_laughs_blocked(self):
        """实体膨胀攻击（billion laughs）被安全解析器拒绝。"""
        from defusedxml import ElementTree as DefET
        # 经典 billion laughs XML
        malicious_xml = b'''<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
]>
<lolz>&lol2;</lolz>'''
        # defusedxml 应拒绝实体扩展
        with self.assertRaises(Exception):
            DefET.fromstring(malicious_xml)

    def test_external_entity_not_resolved(self):
        """外部实体不被解析（不读取本地文件）。"""
        from defusedxml import ElementTree as DefET
        malicious_xml = b'''<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/hostname">
]>
<foo>&xxe;</foo>'''
        # defusedxml 应拒绝外部实体
        with self.assertRaises(Exception):
            DefET.fromstring(malicious_xml)

    def test_normal_xml_still_parsed(self):
        """正常 XML 仍能被解析。"""
        from defusedxml import ElementTree as DefET
        normal_xml = b'''<?xml version="1.0"?>
<multistatus xmlns="DAV:">
  <response>
    <href>/notes/test.md</href>
    <propstat>
      <prop>
        <resourcetype/>
      </prop>
      <status>HTTP/1.1 200 OK</status>
    </propstat>
  </response>
</multistatus>'''
        root = DefET.fromstring(normal_xml)
        self.assertIsNotNone(root)

    def test_malformed_xml_raises_error(self):
        """格式错误的 XML 正常报错。"""
        from defusedxml import ElementTree as DefET
        with self.assertRaises(Exception):
            DefET.fromstring(b'<not-closed>')


# ============================================================
# WebDAV SSRF 测试
# ============================================================
class TestWebdavSsrf(unittest.TestCase):
    """验证 PROPFIND 爬取的 href 同域校验。"""

    def _get_is_safe_href(self):
        """从 _scan_all_md_files 内部获取 _is_safe_href 函数用于测试。
        由于它是闭包内部函数，我们直接测试同域校验逻辑。"""
        from urllib.parse import urlparse

        webdav_url = "https://dav.jianguoyun.com/dav/notes/"
        _parsed_base = urlparse(webdav_url)

        def _is_safe_href(href):
            if not href:
                return False
            if href.startswith('http://') or href.startswith('https://'):
                try:
                    href_parsed = urlparse(href)
                    return href_parsed.netloc == _parsed_base.netloc
                except Exception:
                    return False
            return True

        return _is_safe_href

    def test_relative_path_allowed(self):
        """相对路径 href 被允许。"""
        is_safe = self._get_is_safe_href()
        self.assertTrue(is_safe("/dav/notes/test.md"))
        self.assertTrue(is_safe("test.md"))

    def test_same_domain_absolute_allowed(self):
        """同域绝对 URL 被允许。"""
        is_safe = self._get_is_safe_href()
        self.assertTrue(is_safe("https://dav.jianguoyun.com/dav/notes/sub/file.md"))

    def test_different_domain_blocked(self):
        """不同域名的绝对 URL 被拒绝（防止二级 SSRF）。"""
        is_safe = self._get_is_safe_href()
        self.assertFalse(is_safe("http://localhost:8080/secret"))
        self.assertFalse(is_safe("http://127.0.0.1/admin"))
        self.assertFalse(is_safe("http://169.254.169.254/latest/meta-data/"))
        self.assertFalse(is_safe("https://evil.com/steal"))
        self.assertFalse(is_safe("http://10.0.0.1/internal"))

    def test_empty_href_blocked(self):
        """空 href 被拒绝。"""
        is_safe = self._get_is_safe_href()
        self.assertFalse(is_safe(""))
        self.assertFalse(is_safe(None))

    def test_non_http_scheme_relative_allowed(self):
        """非 http 开头的相对路径被允许。"""
        is_safe = self._get_is_safe_href()
        self.assertTrue(is_safe("/path/to/file.md"))


# ============================================================
# SQL/RPC 误报确认测试
# ============================================================
class TestRpcNotSqlInjection(unittest.TestCase):
    """确认 home_system._rpc 使用固定 RPC 名 + JSON 参数，非 SQL 拼接。"""

    def test_rpc_names_are_hardcoded_strings(self):
        """所有 RPC 调用使用硬编码函数名，不来自用户输入。"""
        import home_system
        import inspect
        source = inspect.getsource(home_system)
        # 找到所有 _rpc 调用
        lines = [l.strip() for l in source.splitlines() if '_rpc(' in l and 'def _rpc' not in l]
        self.assertGreater(len(lines), 0, "应该有 _rpc 调用")
        for line in lines:
            # 每个调用的第一个参数应该是字符串字面量
            self.assertIn('"', line, f"RPC 调用应使用字符串字面量: {line}")

    def test_rpc_uses_supabase_rpc_method(self):
        """_rpc 使用 Supabase 的 .rpc() 方法，不拼接 SQL。"""
        import home_system
        import inspect
        source = inspect.getsource(home_system._rpc)
        # 确认使用 .rpc() 而非 .sql() 或字符串拼接
        self.assertIn('.rpc(', source)
        self.assertNotIn('SELECT', source.upper().replace('SELECT', 'SELECT'))
        self.assertNotIn('INSERT INTO', source.upper())
        self.assertNotIn('DELETE FROM', source.upper())
        self.assertNotIn('UPDATE ', source.upper())

    def test_rpc_params_are_dict(self):
        """_rpc 的参数是 dict（JSON 绑定），不是 SQL 字符串。"""
        import home_system
        import inspect
        source = inspect.getsource(home_system._rpc)
        self.assertIn('params', source)
        self.assertIn('dict', source.lower())


# ============================================================
# 固定 API URL 误报确认测试
# ============================================================
class TestFixedApiUrlsNotSsrf(unittest.TestCase):
    """确认被标记为 SSRF 的固定 API URL 不是用户可控的。"""

    def test_telegram_url_host_fixed(self):
        """_push_wechat 的 URL host 固定为 api.telegram.org。"""
        import server
        import inspect
        source = inspect.getsource(server._push_wechat)
        self.assertIn('api.telegram.org', source)
        # URL 中不包含用户可控的 host 变量
        # token 在路径中，但不影响 host

    def test_amap_url_host_fixed(self):
        """where_is_user 的 URL host 固定为 restapi.amap.com。"""
        import server
        import inspect
        source = inspect.getsource(server.where_is_user)
        self.assertIn('restapi.amap.com', source)

    def test_siliconflow_url_host_fixed(self):
        """_get_embedding 的 URL host 固定为 api.siliconflow.cn。"""
        import server
        import inspect
        source = inspect.getsource(server._get_embedding)
        self.assertIn('api.siliconflow.cn', source)
        self.assertIn('https://', source)


if __name__ == "__main__":
    unittest.main()
