import shutil
from pathlib import Path

from source_hash import source_hash

OVERLAY = Path(__file__).resolve().parents[1]


def copy_of_overlay(tmp_path):
    shutil.copy(OVERLAY / "Dockerfile", tmp_path / "Dockerfile")
    shutil.copytree(
        OVERLAY / "posthog_k8s_authproxy",
        tmp_path / "posthog_k8s_authproxy",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return tmp_path


def test_changes_with_the_module_and_with_the_dockerfile(tmp_path):
    root = copy_of_overlay(tmp_path)
    before = source_hash(root)
    with (root / "posthog_k8s_authproxy" / "core.py").open("a") as f:
        f.write("\n")
    after_module = source_hash(root)
    with (root / "Dockerfile").open("a") as f:
        f.write("\n")
    assert len({before, after_module, source_hash(root)}) == 3


def test_ignores_tests_and_compiled_files(tmp_path):
    root = copy_of_overlay(tmp_path)
    before = source_hash(root)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("x = 1\n")
    (root / "posthog_k8s_authproxy" / "__pycache__").mkdir()
    (root / "posthog_k8s_authproxy" / "__pycache__" / "core.cpython-314.pyc").write_bytes(b"\0")
    assert source_hash(root) == before
