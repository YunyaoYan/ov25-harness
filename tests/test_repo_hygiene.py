"""What the repository must not contain: Chinese outside the Chinese locale and its fixtures, details
of a private deployment, configuration read from the environment, and the web framework outside the
modules that serve HTTP."""
import ast
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path, PurePosixPath
import re
import unittest
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ov25_harness"
SKIPPED_DIRS = {".git", "__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache", ".venv", "venv", "build", "dist"}
# Where Chinese may appear.
CHINESE_ALLOWED = (SOURCE / "locales" / "zh.toml", ROOT / "tests" / "data" / "zh")
# Han characters, CJK punctuation and full-width forms.
CJK = re.compile(r"[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]")
# RFC 5737 addresses are reserved for documentation and examples.
EXAMPLE_NETWORKS = tuple(IPv4Network(net) for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.]|/\d)")
DEPLOYMENT_PATTERNS = {
    "GPU UUID": re.compile(r"\bGPU-[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}\b", re.I),
    "machine-specific path": re.compile(r"(?<![\w/:])/(?:home|Users|root|mnt|srv|opt)/[\w.-]"),
}
# The modules that serve HTTP; nothing else may import the web framework.
WEB_MODULES = {"app.py", "runtime.py", "audio.py", "cli.py"}


def repository_files():
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and not SKIPPED_DIRS & set(path.relative_to(ROOT).parts[:-1]) and path.suffix != ".pyc":
            yield path


def text_of(path):
    """A file's text, or None for a binary file."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def deployment_markers(line):
    for label, pattern in DEPLOYMENT_PATTERNS.items():
        if pattern.search(line):
            yield label
    for value in IPV4.findall(line):
        try:
            address = IPv4Address(value)
        except ValueError:
            continue
        if (address.is_private and not address.is_loopback and not address.is_unspecified
                and not any(address in network for network in EXAMPLE_NETWORKS)):
            yield "private IPv4 address"
            break


def modules():
    for path in sorted(SOURCE.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class HygieneTests(unittest.TestCase):
    def test_no_chinese_outside_the_chinese_locale_and_fixtures(self):
        found = []
        for path in repository_files():
            if any(path == allowed or allowed in path.parents for allowed in CHINESE_ALLOWED):
                continue
            text = text_of(path)
            for number, line in enumerate((text or "").splitlines(), 1):
                if CJK.search(line):
                    found.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(found, [], "Chinese or full-width characters; use \\u escapes or the zh locale")

    def test_the_chinese_files_are_where_the_tests_look(self):
        self.assertTrue(CHINESE_ALLOWED[0].is_file())
        self.assertTrue(any(CHINESE_ALLOWED[1].glob("*.toml")))

    def test_no_details_of_a_private_deployment(self):
        found = []
        for path in repository_files():
            text = text_of(path)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for marker in deployment_markers(line):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {marker}")
        self.assertEqual(found, [])

    def test_settings_come_from_the_file_not_the_environment(self):
        found = []
        for path, tree in modules():
            for node in ast.walk(tree):
                reads = (isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "environb"}
                         or isinstance(node, ast.Name) and node.id in {"environ", "getenv"}
                         or isinstance(node, ast.ImportFrom) and node.module == "os"
                         and {alias.name for alias in node.names} & {"environ", "getenv", "environb"})
                if not reads:
                    continue
                line = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
                if path.name == "config.py":
                    continue
                if path.name == "rewrite.py" and self.is_key_lookup(tree, node):
                    continue
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}: {line}")
        self.assertEqual(found, [])

    @staticmethod
    def is_key_lookup(tree, node):
        """Whether node is os.environ.get(<...>.api_key_env), the one read the rewrite plugin may make."""
        for call in ast.walk(tree):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "get"
                    and call.func.value is node and len(call.args) == 1
                    and isinstance(call.args[0], ast.Attribute) and call.args[0].attr == "api_key_env"):
                return True
        return False

    def test_only_the_http_modules_import_the_web_framework(self):
        found = []
        for path, tree in modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                else:
                    continue
                if any(name.split(".")[0] == "fastapi" for name in names) and not (
                        path.parent == SOURCE and path.name in WEB_MODULES):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(found, [])

    def test_the_checks_catch_what_they_look_for(self):
        self.assertTrue(CJK.search("\u4f60\u597d"))
        self.assertTrue(CJK.search("a\uff1fb"))
        self.assertTrue(CJK.search("a\u3002"))
        self.assertFalse(CJK.search("plain ASCII, and caf\u00e9 \u2014 too"))
        example_private = IPv4Network("10.0.0.0/8")[42]
        self.assertEqual(list(deployment_markers(f"http://{example_private}:80")), ["private IPv4 address"])
        self.assertEqual(list(deployment_markers(f"GPU-{UUID(int=1)}")), ["GPU UUID"])
        self.assertEqual(list(deployment_markers(str(PurePosixPath("/", "mnt", "example")))), ["machine-specific path"])
        self.assertEqual(list(deployment_markers("127.0.0.1 0.0.0.0 192.0.2.5 /v1/sessions")), [])
        tree = ast.parse("import os\nkey = os.environ.get(config.api_key_env)\nother = os.environ['HOME']\n")
        reads = sorted((node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "environ"),
                       key=lambda node: node.lineno)
        self.assertEqual([self.is_key_lookup(tree, node) for node in reads], [True, False])


if __name__ == "__main__":
    unittest.main()
