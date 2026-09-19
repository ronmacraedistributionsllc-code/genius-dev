import shutil
import subprocess
from pathlib import Path

import pytest

import genius_dev
from genius_dev.config import PRESETS, ProviderConfig
from genius_dev.runtime import build_runtime

DEMO = Path(genius_dev.__file__).parent / "demo_project"


class MemoryKeyring:
    """Dict-backed keyring so tests never touch the real macOS Keychain."""
    store: dict = {}

    @classmethod
    def install(cls):
        import keyring
        from keyring.backend import KeyringBackend

        class _K(KeyringBackend):
            priority = 1
            def set_password(self, service, username, password): cls.store[(service, username)] = password
            def get_password(self, service, username): return cls.store.get((service, username))
            def delete_password(self, service, username):
                from keyring.errors import PasswordDeleteError
                if (service, username) not in cls.store:
                    raise PasswordDeleteError("missing")
                del cls.store[(service, username)]
        cls.store.clear()
        keyring.set_keyring(_K())


@pytest.fixture(autouse=True)
def fake_keychain():
    MemoryKeyring.install()
    yield MemoryKeyring.store


@pytest.fixture(autouse=True)
def genius_home(tmp_path, monkeypatch):
    home = tmp_path / "ghome"
    monkeypatch.setenv("GENIUS_HOME", str(home))
    return home


@pytest.fixture
def demo_dir(tmp_path):
    d = tmp_path / "calc"
    shutil.copytree(DEMO, d)
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    return d


@pytest.fixture
def make_rt():
    def _make(path: Path, providers: dict[str, dict] | None = None, permission: str = "autonomous", **kw):
        rt = build_runtime(path, permission=permission, **kw)
        for name, over in (providers if providers is not None else {"mock": {}}).items():
            rt.project.cfg.save_provider(ProviderConfig.from_dict(name, {**PRESETS["mock"], **over}), "project")
        rt.router.refresh()
        return rt
    return _make


@pytest.fixture
def demo_rt(demo_dir, make_rt):
    return make_rt(demo_dir)


@pytest.fixture
def py_project(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "app.py").write_text("def greet(name):\n    return 'hi ' + name\n\nclass Thing:\n    pass\n")
    (d / "tests").mkdir()
    (d / "tests" / "test_app.py").write_text("from app import greet\n\ndef test_greet():\n    assert greet('a') == 'hi a'\n")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    return d
