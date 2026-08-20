"""os 级 STRM 删除守卫的 dir_fd 盲区回归测试。

2026-08-11 线上误删根因：CMS 增量同步消费 115 delete_file 生活事件后用
shutil.rmtree 删除本地转存目录，Python 3.12 的 rmtree 内部对目录内文件走
os.unlink(entry.name, dir_fd=topfd)——target 只是相对文件名，旧守卫按进程
cwd 解析 open() 失败 → 判定"非 self-share"放行，/s/ 转存 strm 被删且守卫
零日志。本测试直接 import 脚本自身，验证带 dir_fd 的删除会被拦截。

shutil.rmtree 不传 dir_fd 参数时同样触发 fd 路径（3.12 重构后总是先 open
顶层目录再 unlink 相对名），因此用真实 rmtree 即可覆盖真实调用形态。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_GUARD = Path(__file__).resolve().parents[1] / "scripts" / "cms-strm-guard" / "sitecustomize.py"

# 守卫在导入期全局挂钩 os.remove/os.unlink（这是它的设计：覆盖进程内一切删除
# 路径）。unittest discover 全部模块共享一个进程：若在模块顶层 import 守卫，
# 补丁会泄漏进早于本模块运行的测试（它们的 tempfile.TemporaryDirectory 清理
# rmtree 删含 /s/ strm 的目录时被守卫拦截 → "Directory not empty" 报错）。
# 因此守卫只在 setUpClass 里懒加载（此时早前模块已跑完），tester 结束后在
# tearDownModule 还原（注意 exec_module 时 os.unlink 已是守卫版本，真原始函数
# 存在守卫模块的 _orig_os_unlink/_orig_os_remove 里）。
_loaded_guard = None

_SELF_SHARE_URL = "http://192.0.2.1:9527/s/swsybrc3wul_1212_3493257953438336760.mp4?/哑舍 S01E01 2160p.mp4"
_DIRECT_URL = "http://192.0.2.1:9527/d/bie4sryllij7u8sgx.mkv?/probe.mkv"


def tearDownModule():
    global _loaded_guard
    if _loaded_guard is not None:
        os.unlink = _loaded_guard._orig_os_unlink
        os.remove = _loaded_guard._orig_os_remove


class DirFdGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _loaded_guard
        spec = importlib.util.spec_from_file_location("strm_guard_sitecustomize", _GUARD)
        _loaded_guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_loaded_guard)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_roots = os.environ.get("STRM_GUARD_LIBRARY_ROOTS")

    def tearDown(self):
        if self._old_roots is None:
            os.environ.pop("STRM_GUARD_LIBRARY_ROOTS", None)
        else:
            os.environ["STRM_GUARD_LIBRARY_ROOTS"] = self._old_roots
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_dir(self, name="dir", content=_SELF_SHARE_URL):
        d = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(d, "Season 01"))
        with open(os.path.join(d, "Season 01", "E01.strm"), "w", encoding="utf-8") as fh:
            fh.write(content)
        return d

    def test_rmtree_self_share_strm_is_protected(self):
        """rmtree 删含 /s/ strm 的目录：守卫逐文件拦截，目录因非空保留。"""
        d = self._make_dir()
        with self.assertRaises(OSError):
            shutil.rmtree(d)
        self.assertTrue(os.path.exists(os.path.join(d, "Season 01", "E01.strm")))

    def test_rmtree_direct_strm_still_deleted(self):
        """rmtree 删含 /d/ 直链 strm 的目录：不是 self-share，照常删除。"""
        d = self._make_dir(content=_DIRECT_URL)
        shutil.rmtree(d)
        self.assertFalse(os.path.exists(d))

    def test_rmtree_plain_file_dir_still_deleted(self):
        """rmtree 删普通文件目录（无 strm）：不受守卫影响。"""
        d = os.path.join(self.tmp, "plain")
        os.makedirs(d)
        with open(os.path.join(d, "a.txt"), "w") as fh:
            fh.write("x")
        shutil.rmtree(d)
        self.assertFalse(os.path.exists(d))

    def test_unlink_with_dir_fd_direct_call(self):
        """模拟 rmtree 的 os.unlink(name, dir_fd=fd) 直接调用形态。"""
        d = self._make_dir()
        strm = os.path.join(d, "Season 01", "E01.strm")
        with open(os.path.join(d, "Season 01", "placeholder.txt"), "w") as fh:
            fh.write("x")
        fd = os.open(os.path.join(d, "Season 01"), os.O_RDONLY)
        try:
            os.unlink("E01.strm", dir_fd=fd)  # 守卫拦截，静默跳过（无异常）
            self.assertTrue(os.path.exists(strm))
        finally:
            os.close(fd)
        shutil.rmtree(d, ignore_errors=True)

    def test_rmtree_keeps_same_dir_sidecar(self):
        """同目录有 /s/ strm 时，rmtree 不得删 thumb/nfo（飞驰人生2）。"""
        d = self._make_dir()
        thumb = os.path.join(d, "Season 01", "E01-thumb.jpg")
        with open(thumb, "w") as fh:
            fh.write("img")
        with self.assertRaises(OSError):
            shutil.rmtree(d)
        self.assertTrue(os.path.exists(thumb))
        self.assertTrue(os.path.exists(os.path.join(d, "Season 01", "E01.strm")))

    def test_rmtree_keeps_series_root_poster(self):
        """剧集根目录海报与 Season 不在同层：rmtree 仍须跳过（攻壳机动队 2026-08-20）。"""
        d = self._make_dir()
        poster = os.path.join(d, "poster.jpg")
        fanart = os.path.join(d, "fanart.jpg")
        nfo = os.path.join(d, "tvshow.nfo")
        for path in (poster, fanart, nfo):
            with open(path, "w") as fh:
                fh.write("meta")
        with self.assertRaises(OSError):
            shutil.rmtree(d)
        self.assertTrue(os.path.exists(poster))
        self.assertTrue(os.path.exists(fanart))
        self.assertTrue(os.path.exists(nfo))
        self.assertTrue(os.path.exists(os.path.join(d, "Season 01", "E01.strm")))

    def test_library_roots_alias_host_mount_and_share(self):
        """宿主机路径要同时覆盖容器 /media 挂载和 share 目录。"""
        os.environ["STRM_GUARD_LIBRARY_ROOTS"] = "/mnt/user/Unraid/strm/转存"
        roots = _loaded_guard._library_roots()
        self.assertIn("/mnt/user/Unraid/strm/转存", roots)
        self.assertIn("/media/转存", roots)
        self.assertIn("/mnt/user/Unraid/strm/share", roots)
        self.assertIn("/media/share", roots)

    def test_rmtree_keeps_any_file_under_library_root(self):
        """媒体库根下没有 /s/ strm 的普通文件也不得删（不再按文件名打地鼠）。"""
        os.environ["STRM_GUARD_LIBRARY_ROOTS"] = self.tmp
        show = os.path.join(self.tmp, "Show")
        os.makedirs(show)
        poster = os.path.join(show, "poster.jpg")
        with open(poster, "w") as fh:
            fh.write("img")
        with self.assertRaises(OSError):
            shutil.rmtree(show)
        self.assertTrue(os.path.exists(poster))

    def test_unlink_dir_fd_under_library_root_is_skipped(self):
        """rmtree 的 unlink(name, dir_fd=) 也要按目录是否在媒体库根下拦截。"""
        os.environ["STRM_GUARD_LIBRARY_ROOTS"] = self.tmp
        show = os.path.join(self.tmp, "Show")
        os.makedirs(show)
        poster = os.path.join(show, "poster.jpg")
        with open(poster, "w") as fh:
            fh.write("img")
        fd = os.open(show, os.O_RDONLY)
        try:
            os.unlink("poster.jpg", dir_fd=fd)
            self.assertTrue(os.path.exists(poster))
        finally:
            os.close(fd)

    def test_rmtree_direct_strm_under_library_root_still_deleted(self):
        """媒体库根下的 /d/ 直链 strm 仍可删（直链拦截守卫要先写后删）。"""
        os.environ["STRM_GUARD_LIBRARY_ROOTS"] = self.tmp
        d = self._make_dir(content=_DIRECT_URL)
        shutil.rmtree(d)
        self.assertFalse(os.path.exists(d))


if __name__ == "__main__":
    unittest.main()
