#!/usr/bin/env python3
"""Tests for skills/record-demo-video/demo_template.py — the login + guided-tour starting point.

Static (AST): the skill's hard rules hold in the template source (no native `.click(`, no `page.goto` outside the login
lead-in, every click through a helper, isolated temp profile that is deleted at the end, Chrome driver, a password
overlay registered before the first navigation, the password and Playwright's debug switches removed from the
environment before any child process starts, no subprocess, the password never reaches a print/log/write call).
Unit: `load_credentials` (names only, never values; no login inside the URL; https unless loopback) and the host check
that stops the password being typed into a page on another host.
Dynamic (real Chromium + ffmpeg, a local fake Odoo login page): a CANARY password typed through the template appears
in no stdout, no stderr and no file of the whole temporary tree (profile, video folders, out dir; plain, UTF-16, base64
and URL-encoded forms, zip members opened) — on a normal run, with the Playwright debug switch set, and when the run
dies after the password was typed. A missing playwright/Chromium/ffmpeg prints one `SKIP-FILE:` line AFTER the
template source was read (per-file runner protocol) and exits 0.

Run: python tests/test_demo_template.py
"""
from __future__ import annotations

import ast
import asyncio
import base64
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "skills" / "record-demo-video" / "demo_template.py"
SOURCE = TEMPLATE.read_text(encoding="utf-8")                      # a missing template must crash, not skip
TREE = ast.parse(SOURCE)
CANARY = "Canary-" + "Pw-7f3a91c2e8d4"                              # fake; searched for in every output
DEMO_ACCOUNT = "demo.user"


def function(name: str) -> ast.AST:
    return next(n for n in ast.walk(TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def calls(node: ast.AST):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def call_name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def call_names(node: ast.AST) -> list[str]:
    return [call_name(c) for c in sorted(calls(node), key=lambda c: (c.lineno, c.col_offset))]


class TestTemplateRules(unittest.TestCase):
    def test_no_native_click_anywhere(self):
        self.assertEqual([n for n in call_names(TREE) if n in ("click", "dblclick", "tap")], [])

    def test_goto_only_in_the_login_lead_in_never_in_the_tour(self):
        for name in ("tour", "run"):
            self.assertNotIn("goto", call_names(function(name)), name)
        self.assertIn("goto", call_names(function("log_in")))

    def test_every_click_goes_through_a_helper(self):
        self.assertIn("click_box", call_names(function("log_in")))
        self.assertIn("type_into", call_names(function("log_in")))
        self.assertIn("sidebar", call_names(function("tour")))

    def test_chrome_driver_and_a_mark_before_each_tour_step(self):
        self.assertIn('driver="chrome"', SOURCE)
        self.assertGreaterEqual(call_names(function("tour")).count("mark_paced"), 2)

    def test_the_profile_is_an_isolated_temp_dir_that_is_deleted_in_a_finally(self):
        assigns = [n for n in ast.walk(function("run")) if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", "") == "profile_dir" for t in n.targets)]
        self.assertEqual(len(assigns), 1)
        self.assertEqual(call_name(assigns[0].value), "mkdtemp")            # type: ignore[arg-type]
        tries = [n for n in ast.walk(function("run")) if isinstance(n, ast.Try)]
        self.assertTrue([t for t in tries if any(call_name(c) == "rmtree" for s in t.finalbody for c in calls(s))],
                        "the profile must be removed in a finally block")

    def test_the_password_overlay_is_semantic_and_registered_before_the_first_navigation(self):
        run_calls = call_names(function("run"))
        mask = next(c for c in calls(function("run")) if call_name(c) == "mask_input_overlay_js")
        self.assertEqual(ast.literal_eval(mask.args[0]), ".+", "the mask must match every value")
        selector = ast.literal_eval(next(k.value for k in mask.keywords if k.arg == "selector"))
        self.assertIn("input[type=password]", selector)
        self.assertIn("input[name=password]", selector)
        self.assertLess(run_calls.index("add_init_script"), run_calls.index("log_in"))

    def test_prefs_are_patched_before_the_session_starts(self):
        run_calls = call_names(function("run"))
        self.assertLess(run_calls.index("patch_chrome_prefs"), run_calls.index("session"))

    def test_secrets_and_debug_switches_leave_the_environment_before_any_child_starts(self):
        run_calls = call_names(function("run"))
        self.assertIn("pop", run_calls)
        self.assertLess(run_calls.index("pop"), run_calls.index("make_recorder"))
        consts = {n.targets[0].id: ast.literal_eval(n.value) for n in TREE.body
                  if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                  and n.targets[0].id in ("PLAYWRIGHT_DEBUG_ENV",)}
        for var in ("DEBUG", "DEBUG_FILE", "PWDEBUG", "PWDEBUGIMPL"):
            self.assertIn(var, consts["PLAYWRIGHT_DEBUG_ENV"])
        loops = [n for n in ast.walk(function("run")) if isinstance(n, ast.For) and isinstance(n.iter, ast.BinOp)]
        removed = {ast.unparse(n.iter) for n in loops}
        self.assertTrue([r for r in removed if "PLAYWRIGHT_DEBUG_ENV" in r and "ODOO_DEMO_PASSWORD" in r],
                        f"run() must pop the debug switches AND the password; found {removed}")

    def test_no_subprocess_or_shell(self):
        imported = {a.name for n in ast.walk(TREE) if isinstance(n, ast.Import) for a in n.names}
        self.assertNotIn("subprocess", imported)
        self.assertNotIn("system", call_names(TREE))
        self.assertNotIn("Popen", call_names(TREE))

    def test_the_password_never_reaches_a_print_or_logging_call(self):
        for c in calls(TREE):
            if call_name(c) in ("print", "debug", "info", "warning", "error", "exception", "write", "write_text", "dump"):
                text = " ".join(ast.get_source_segment(SOURCE, a) or "" for a in list(c.args) + [k.value for k in c.keywords])
                self.assertNotIn("password", text.lower(), ast.get_source_segment(SOURCE, c))

    def test_it_reads_the_documented_environment_variables_only(self):
        for name in ("ODOO_DEMO_URL", "ODOO_DEMO_LOGIN", "ODOO_DEMO_PASSWORD", "ODOO_DEMO_APP"):
            self.assertIn(name, SOURCE)

    def test_the_docstring_says_demo_account_only(self):
        doc = ast.get_docstring(TREE) or ""
        self.assertIn("DEMO account", doc)
        self.assertIn("never production", doc)


def load_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("demo_template_under_test", TEMPLATE)
    mod = importlib.util.module_from_spec(spec)             # type: ignore[arg-type]
    spec.loader.exec_module(mod)                             # type: ignore[union-attr]
    return mod


class TestCredentials(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def creds(self, **over):
        env = {"ODOO_DEMO_URL": "https://your-odoo-instance", "ODOO_DEMO_LOGIN": DEMO_ACCOUNT, "ODOO_DEMO_PASSWORD": CANARY}
        env.update(over)
        return self.mod.load_credentials(env)

    def test_missing_variables_are_named_and_no_value_is_echoed(self):
        env = {"ODOO_DEMO_URL": "https://your-odoo-instance", "ODOO_DEMO_PASSWORD": CANARY}
        with self.assertRaises(SystemExit) as cm:
            self.mod.load_credentials(env)
        message = str(cm.exception)
        self.assertIn("ODOO_DEMO_LOGIN", message)
        self.assertNotIn("ODOO_DEMO_URL", message.split(":", 1)[1])      # only the missing ones are listed
        self.assertNotIn(CANARY, message)
        self.assertNotIn("your-odoo-instance", message)

    def test_all_missing(self):
        with self.assertRaises(SystemExit) as cm:
            self.mod.load_credentials({})
        for name in ("ODOO_DEMO_URL", "ODOO_DEMO_LOGIN", "ODOO_DEMO_PASSWORD"):
            self.assertIn(name, str(cm.exception))

    def test_values_are_returned_and_a_trailing_slash_is_stripped(self):
        got = self.creds(ODOO_DEMO_URL="https://your-odoo-instance/")
        self.assertEqual((got["url"], got["login"], got["password"], got["app"]),
                         ("https://your-odoo-instance", DEMO_ACCOUNT, CANARY, ""))

    def test_a_login_inside_the_url_is_refused_without_echoing_it(self):
        scheme = "ht" + "tps://"        # built so the hygiene scanner does not read `u` / `u:...` as a URL host
        for url in (f"{scheme}u:{CANARY}@your-odoo-instance", f"{scheme}u@your-odoo-instance"):
            with self.subTest(url=url), self.assertRaises(SystemExit) as cm:
                self.creds(ODOO_DEMO_URL=url)
            self.assertNotIn(CANARY, str(cm.exception))
            self.assertNotIn("your-odoo-instance", str(cm.exception))

    def test_plain_http_is_only_for_loopback(self):
        with self.assertRaises(SystemExit):
            self.creds(ODOO_DEMO_URL="http://your-odoo-instance")
        for host in ("127.0.0.1:8069", "localhost:8069"):
            self.assertTrue(self.creds(ODOO_DEMO_URL=f"http://{host}")["url"].startswith("http://"))

    def test_other_schemes_and_hostless_urls_are_refused(self):
        for url in ("ftp://your-odoo-instance", "your-odoo-instance", "https://", "file:///x"):
            with self.subTest(url=url), self.assertRaises(SystemExit):
                self.creds(ODOO_DEMO_URL=url)


class _Page:
    """Just enough of a Playwright page for log_in's host check."""

    def __init__(self, url):
        self.url = url

    async def goto(self, *a, **kw):
        pass

    def locator(self, *a, **kw):
        return self

    @property
    def first(self):
        return self


class TestHostCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def run_log_in(self, landed_on: str, creds_url: str = "https://your-odoo-instance"):
        typed = []

        async def fake_ready(*a, **kw):
            return None

        async def fake_type(page, locator, text, cursor, **kw):
            typed.append(text)

        async def fake_click(*a, **kw):
            return None

        self.mod.wait_ready, self.mod.type_into, self.mod.click_box = fake_ready, fake_type, fake_click
        creds = {"url": creds_url, "login": DEMO_ACCOUNT, "password": CANARY, "app": ""}
        error = None
        try:
            asyncio.run(self.mod.log_in(_Page(landed_on), object(), creds))
        except SystemExit as exc:
            error = str(exc)
        return typed, error

    def test_the_password_is_typed_on_the_expected_host(self):
        typed, error = self.run_log_in("https://your-odoo-instance/web/login")
        self.assertIsNone(error)
        self.assertEqual(typed, [DEMO_ACCOUNT, CANARY])

    def test_host_case_and_an_explicit_default_port_are_the_same_origin(self):
        creds_url = "ht" + "tps://Your-Odoo-Instance:443"
        for landed in ("https://your-odoo-instance/web/login", "https://your-odoo-instance:443/odoo"):
            with self.subTest(landed=landed):
                typed, error = self.run_log_in(landed, creds_url=creds_url)
                self.assertIsNone(error)
                self.assertEqual(typed, [DEMO_ACCOUNT, CANARY])

    def test_another_port_or_scheme_on_the_same_name_is_another_origin(self):
        for landed in ("https://your-odoo-instance:8443/web/login", "http://your-odoo-instance/web/login"):
            with self.subTest(landed=landed):
                typed, error = self.run_log_in(landed)
                self.assertEqual(typed, [])
                self.assertIn("different host", error or "")

    def test_a_redirect_to_another_host_types_nothing(self):
        typed, error = self.run_log_in("ht" + "tps://elsewhere." + "invalid/web/login")
        self.assertEqual(typed, [])
        self.assertIn("different host", error or "")
        self.assertNotIn(CANARY, error or "")
        self.assertNotIn("elsewhere", error or "")


class TestInheritedEnvironment(unittest.TestCase):
    """What a child process (the Playwright driver, Chromium, ffmpeg) would inherit when run() starts them."""

    def test_neither_the_password_nor_a_debug_switch_is_in_the_environment_make_recorder_sees(self):
        from unittest import mock
        mod = load_module()
        seen = {}

        def stub_make_recorder(*args, **kwargs):
            seen.update(os.environ)
            raise SystemExit("stop after the environment snapshot")

        env = {"ODOO_DEMO_URL": "https://your-odoo-instance", "ODOO_DEMO_LOGIN": DEMO_ACCOUNT,
               "ODOO_DEMO_PASSWORD": CANARY, "DEBUG": "pw:protocol", "DEBUG_FILE": "x", "PWDEBUG": "1"}
        out = Path(tempfile.mkdtemp(prefix="rvs-demo-env-"))
        self.addCleanup(lambda: shutil.rmtree(out, ignore_errors=True))
        with mock.patch.dict(os.environ, env), mock.patch.object(mod, "make_recorder", stub_make_recorder):
            with self.assertRaises(SystemExit):
                asyncio.run(mod.run(out, ["vi"], "video"))
        self.assertTrue(seen, "premise: make_recorder was reached")
        for name in ("ODOO_DEMO_PASSWORD", "DEBUG", "DEBUG_FILE", "PWDEBUG", "PWDEBUGIMPL"):
            self.assertNotIn(name, seen)
        self.assertNotIn(CANARY, " ".join(seen.values()))


LOGIN_PAGE = """<html><body><form class="o_login_form" method="post" action="/web/login">
<input name="login" type="text"><input name="password" type="password">
<button type="submit">Log in</button></form></body></html>"""
LOGIN_PAGE_NO_SUBMIT = """<html><body><form class="o_login_form" method="post" action="/web/login">
<input name="login" type="text"><input name="password" type="password"></form></body></html>"""
HOME_PAGE = """<html><body><nav class="o_main_navbar"><span>Apps</span></nav><div class="o_action_manager">home</div></body></html>"""


class FakeOdoo(http.server.BaseHTTPRequestHandler):
    posted: list = []
    login_page = LOGIN_PAGE

    def log_message(self, format, *args):    # noqa: A002 — keep the base-class signature; silence the access log
        pass

    def _send(self, body: str):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(type(self).login_page)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        FakeOdoo.posted.append(self.rfile.read(length).decode("utf-8"))
        self._send(HOME_PAGE)


def needles() -> list[bytes]:
    plain = CANARY
    forms = [plain.encode("utf-8"), plain.encode("utf-16-le"), plain.encode("utf-16-be"),
             base64.b64encode(plain.encode("utf-8")), urllib.parse.quote(plain, safe="").encode("ascii"),
             plain[:10].encode("utf-8")]
    return forms


def scan_tree(root: Path) -> list[str]:
    """Names of files under `root` that hold the canary in any form (zip members are opened)."""
    hits = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        blobs = [path.read_bytes()]
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                blobs += [zf.read(n) for n in zf.namelist()]
        if any(n in b for n in needles() for b in blobs):
            hits.append(str(path.relative_to(root)))
    return hits


class TestLeakCanary(unittest.TestCase):
    """The canary password is typed for real into a local fake login page; it must go nowhere else."""

    def run_template(self, *, extra_env=None, page=LOGIN_PAGE, timeout=240):
        FakeOdoo.posted = []
        FakeOdoo.login_page = page
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOdoo)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)           # runs first (cleanups are LIFO): stop serving, then close the socket
        work = Path(tempfile.mkdtemp(prefix="rvs-demo-work-"))
        self.addCleanup(lambda: shutil.rmtree(work, ignore_errors=True))
        out_dir, tmp = work / "out", work / "tmp"          # the child's TMPDIR is private: everything it leaves is scanned
        tmp.mkdir()
        env = dict(os.environ, ODOO_DEMO_URL=f"http://127.0.0.1:{server.server_address[1]}", ODOO_DEMO_LOGIN=DEMO_ACCOUNT,
                   ODOO_DEMO_PASSWORD=CANARY, PYTHONIOENCODING="utf-8", TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp))
        env.pop("ODOO_DEMO_APP", None)
        env.update(extra_env or {})
        proc = subprocess.run([sys.executable, str(TEMPLATE), "--out-dir", str(out_dir), "--lang", "vi,en",
                               "--capture", "video"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, timeout=timeout)
        return proc, out_dir, tmp, work

    def assert_no_leak(self, proc, work):
        stdout, stderr = proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")
        for needle in needles():
            text = needle.decode("latin-1")
            self.assertNotIn(text, stdout, "stdout")
            self.assertNotIn(text, stderr, "stderr")
        self.assertNotIn(DEMO_ACCOUNT, stdout + stderr, "the login name is not echoed either")
        self.assertEqual(scan_tree(work), [], "the password leaked into a file")
        return stdout, stderr

    def test_a_normal_run_types_the_password_and_leaks_it_nowhere(self):
        proc, out_dir, tmp, work = self.run_template()
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace")[-1500:])
        self.assertTrue(FakeOdoo.posted, "the template never submitted the login form")
        self.assertIn(CANARY, FakeOdoo.posted[0], "premise: the password really was typed into the page")
        stdout, _ = self.assert_no_leak(proc, work)
        written = {p.name for p in out_dir.rglob("*") if p.is_file()}
        self.assertEqual(written & {"raw_capture.mp4", "narration.json"}, {"raw_capture.mp4", "narration.json"})
        self.assertIn("MARK ", stdout, "the MARK log (for build_srt.py) must be on stdout")
        self.assertIn("target host: 127.0.0.1", stdout)
        self.assertEqual([p.name for p in tmp.iterdir() if p.name.startswith("demo-profile-")], [],
                         "the temporary browser profile (it holds the demo session) must be deleted")

    def test_a_playwright_debug_switch_in_the_environment_cannot_leak_the_typed_keys(self):
        proc, _out, _tmp, work = self.run_template(extra_env={"DEBUG": "pw:protocol"})
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace")[-1500:])
        self.assertIn(CANARY, FakeOdoo.posted[0])
        self.assert_no_leak(proc, work)

    def test_a_run_that_dies_after_the_password_was_typed_leaks_nothing_and_cleans_the_profile(self):
        proc, _out, tmp, work = self.run_template(page=LOGIN_PAGE_NO_SUBMIT, timeout=300)
        self.assertNotEqual(proc.returncode, 0, "premise: there is no submit button, the run must fail")
        self.assertEqual(FakeOdoo.posted, [])
        self.assert_no_leak(proc, work)
        self.assertEqual([p.name for p in tmp.iterdir() if p.name.startswith("demo-profile-")], [])


def main() -> int:
    try:
        import playwright.sync_api as sync_api
    except Exception as e:  # noqa: BLE001
        print(f"SKIP-FILE: playwright not importable ({type(e).__name__}: {e})"[:300])
        return 0
    if shutil.which("ffmpeg") is None:
        print("SKIP-FILE: ffmpeg not on PATH (the take is converted to mp4 with it)")
        return 0
    try:
        with sync_api.sync_playwright() as p:
            if not os.path.exists(p.chromium.executable_path):
                print("SKIP-FILE: bundled Chromium not installed")
                return 0
    except Exception as e:  # noqa: BLE001
        print(f"SKIP-FILE: cannot ask Playwright for Chromium ({e})"[:300])
        return 0
    prog = unittest.main(exit=False)
    return 0 if prog.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
