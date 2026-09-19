"""Plugin-style technology detection. Register a detector with @detector or via the
``genius_dev.detectors`` entry-point group."""
from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable



@dataclass
class ProjectInfo:
    root: str = ""
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    databases: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    test_framework: str = ""
    test_cmd: str = ""
    build_cmd: str = ""
    lint_cmd: str = ""
    typecheck_cmd: str = ""
    format_cmd: str = ""
    dev_cmd: str = ""
    dev_port: int = 0
    kind: str = "unknown"                    # web | api | cli | library | mobile | desktop | unknown
    structure: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    python: str = ""

    def add(self, attr: str, *vals: str) -> None:
        cur: list[str] = getattr(self, attr)
        for v in vals:
            if v and v not in cur:
                cur.append(v)

    @property
    def is_web(self) -> bool:
        return self.kind == "web" or bool(self.dev_port)

    def to_dict(self) -> dict:
        return asdict(self)


Detector = Callable[[Path, ProjectInfo], None]
_DETECTORS: list[Detector] = []      # legacy/extra detectors; built-ins are exposed as plugins (see plugins.py)
BUILTIN_DETECTORS: dict[str, Detector] = {}


def detector(fn: Detector) -> Detector:
    _DETECTORS.append(fn)
    return fn


def builtin(name: str):
    """Register a built-in detector under a plugin name instead of the legacy global list."""
    def deco(fn: Detector) -> Detector:
        BUILTIN_DETECTORS[name] = fn
        return fn
    return deco


def _free_port(start: int) -> int:
    """Kept for callers that want a genuinely free port (e.g. preview helpers)."""
    import socket
    for port in range(start, start + 50):
        with socket.socket() as sk:
            if sk.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def _stable_port(root: Path) -> int:
    """Deterministic per-project port for static servers, so re-detection never moves a running server."""
    import zlib
    return 8100 + zlib.crc32(str(root).encode()) % 800


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}


def project_python(root: Path) -> str:
    for c in (root / ".venv/bin/python", root / "venv/bin/python"):
        if c.exists():
            return str(c)
    return sys.executable


@builtin("python")
def _python(root: Path, i: ProjectInfo) -> None:
    files = [root / n for n in ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile", "setup.cfg")]
    if not any(f.exists() for f in files) and not list(root.glob("*.py")) and not (root / "tests").is_dir():
        return
    if not any(f.exists() for f in files) and not list(root.glob("*.py")):
        return
    i.add("languages", "python")
    i.python = project_python(root)
    py = shlex.quote(i.python)
    text = ""
    for f in files:
        if f.exists():
            text += f.read_text(errors="ignore").lower()
    if (root / "pyproject.toml").exists():
        i.add("package_managers", "uv" if (root / "uv.lock").exists() else "poetry" if "[tool.poetry]" in text else "pip")
    elif (root / "requirements.txt").exists():
        i.add("package_managers", "pip")
    for name, key in (("fastapi", "FastAPI"), ("django", "Django"), ("flask", "Flask"), ("sqlalchemy", "SQLAlchemy"),
                      ("textual", "Textual"), ("streamlit", "Streamlit")):
        if name in text:
            i.add("frameworks", key)
    if "sqlalchemy" in text or "django" in text:
        i.add("databases", "SQL (ORM)")
    has_tests = (root / "tests").is_dir() or list(root.glob("test_*.py")) or "pytest" in text
    uses_unittest = "pytest" not in text and any("import unittest" in f.read_text(errors="ignore") for f in list((root / "tests").glob("test*.py")) + list(root.glob("test*.py")))
    if has_tests and uses_unittest:
        i.test_framework, i.test_cmd = "unittest", f"{py} -m unittest discover -s tests -t . -q"
    elif has_tests:
        i.test_framework, i.test_cmd = "pytest", f"{py} -m pytest -q"
    if "ruff" in text:
        i.lint_cmd, i.format_cmd = f"{py} -m ruff check .", f"{py} -m ruff format ."
    if "mypy" in text:
        section = re.split(r"\n\[", text.split("[tool.mypy]", 1)[1])[0] if "[tool.mypy]" in text else ""
        i.typecheck_cmd = f"{py} -m mypy" + ("" if re.search(r"(?m)^files\s*=", section) else " .")      # respect `files=` in the mypy config
    elif "pyright" in text:
        i.typecheck_cmd = "pyright"
    if "fastapi" in text:
        i.kind, i.dev_cmd, i.dev_port = "api", f"{py} -m uvicorn main:app --port 8000", 8000
    elif "flask" in text:
        i.kind, i.dev_cmd, i.dev_port = "web", f"{py} -m flask run --port 5000", 5000
    elif "django" in text:
        i.kind, i.dev_cmd, i.dev_port = "web", f"{py} manage.py runserver 8000", 8000
    elif i.kind == "unknown":
        i.kind = "library"
    i.build_cmd = i.build_cmd or (f"{py} -m compileall -q ." if not (root / "pyproject.toml").exists() else f"{py} -m compileall -q src" if (root / "src").is_dir() else f"{py} -m compileall -q .")


@builtin("node")
def _node(root: Path, i: ProjectInfo) -> None:
    pj = root / "package.json"
    if not pj.exists():
        return
    d = _read_json(pj)
    i.add("languages", "typescript" if (root / "tsconfig.json").exists() else "javascript")
    pm = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "bun" if (root / "bun.lockb").exists() else "npm"
    i.add("package_managers", pm)
    deps = {**d.get("dependencies", {}), **d.get("devDependencies", {})}
    run = "npm run" if pm == "npm" else f"{pm}"
    scripts = d.get("scripts", {})
    fw = {"next": "Next.js", "react": "React", "vue": "Vue", "svelte": "Svelte", "@sveltejs/kit": "SvelteKit", "vite": "Vite",
          "express": "Express", "@nestjs/core": "NestJS", "nuxt": "Nuxt", "astro": "Astro", "electron": "Electron",
          "react-native": "React Native", "expo": "Expo", "tailwindcss": "Tailwind"}
    for k, v in fw.items():
        if k in deps:
            i.add("frameworks", v)
    for k, v in (("prisma", "Prisma"), ("@prisma/client", "Prisma"), ("@supabase/supabase-js", "Supabase"), ("firebase", "Firebase"),
                 ("mongoose", "MongoDB"), ("pg", "PostgreSQL"), ("sqlite3", "SQLite"), ("better-sqlite3", "SQLite")):
        if k in deps:
            (i.add("services", v) if v in ("Supabase", "Firebase") else i.add("databases", v))
    for fw_name, cmd in (("vitest", "vitest"), ("jest", "jest"), ("mocha", "mocha"), ("@playwright/test", "playwright"), ("cypress", "cypress")):
        if fw_name in deps and not i.test_framework:
            i.test_framework = cmd
    if "test" in scripts and "no test specified" not in scripts["test"]:
        i.test_cmd = f"{pm} test" if pm != "yarn" else "yarn test"
        if not i.test_framework:
            i.test_framework = "npm test"
    if not i.test_cmd and i.test_framework == "playwright":
        i.test_cmd = "npx playwright test"
    elif not i.test_cmd and i.test_framework == "cypress":
        i.test_cmd = "npx cypress run"
    if "build" in scripts:
        i.build_cmd = f"{run} build"
    if "lint" in scripts:
        i.lint_cmd = f"{run} lint"
    for k in ("typecheck", "type-check", "tsc"):
        if k in scripts:
            i.typecheck_cmd = f"{run} {k}"
    if not i.typecheck_cmd and (root / "tsconfig.json").exists():
        i.typecheck_cmd = "npx tsc --noEmit"
    dev = "dev" if "dev" in scripts else "start" if "start" in scripts else ""
    if dev:
        i.dev_cmd = f"{run} {dev}"
        i.kind = "web" if any(f in i.frameworks for f in ("Next.js", "React", "Vue", "Svelte", "SvelteKit", "Vite", "Nuxt", "Astro")) else "api" if "Express" in i.frameworks or "NestJS" in i.frameworks else "web"
        m = re.search(r"(?:--port|-p)[ =](\d+)", scripts[dev])
        i.dev_port = int(m.group(1)) if m else 5173 if "Vite" in i.frameworks else 3000
    if "Electron" in i.frameworks:
        i.kind = "desktop"
    if "React Native" in i.frameworks or "Expo" in i.frameworks:
        i.kind = "mobile"


@builtin("rust")
def _rust(root: Path, i: ProjectInfo) -> None:
    if (root / "Cargo.toml").exists():
        i.add("languages", "rust"); i.add("package_managers", "cargo")
        i.test_framework, i.test_cmd, i.build_cmd = "cargo test", "cargo test", "cargo build"
        i.lint_cmd, i.format_cmd = "cargo clippy", "cargo fmt"
        i.kind = i.kind if i.kind != "unknown" else "cli"


@builtin("go")
def _go(root: Path, i: ProjectInfo) -> None:
    if (root / "go.mod").exists():
        i.add("languages", "go"); i.add("package_managers", "go modules")
        i.test_framework, i.test_cmd, i.build_cmd, i.lint_cmd = "go test", "go test ./...", "go build ./...", "go vet ./..."
        i.kind = i.kind if i.kind != "unknown" else "cli"


@builtin("ruby-php")
def _ruby_php(root: Path, i: ProjectInfo) -> None:
    if (root / "Gemfile").exists():
        i.add("languages", "ruby"); i.add("package_managers", "bundler")
        if "rails" in (root / "Gemfile").read_text(errors="ignore").lower():
            i.add("frameworks", "Rails"); i.dev_cmd, i.dev_port, i.kind = "bin/rails server", 3000, "web"
        i.test_cmd = i.test_cmd or "bundle exec rspec"
    if (root / "composer.json").exists():
        i.add("languages", "php"); i.add("package_managers", "composer")
        if "wordpress" in (root / "composer.json").read_text(errors="ignore").lower():
            i.add("frameworks", "WordPress")
        i.test_cmd = i.test_cmd or "composer test"


@builtin("mobile")
def _jvm_mobile(root: Path, i: ProjectInfo) -> None:
    if any((root / n).exists() for n in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")):
        i.add("languages", "kotlin/java"); i.add("package_managers", "gradle")
        g = "./gradlew" if (root / "gradlew").exists() else "gradle"
        i.test_framework, i.test_cmd, i.build_cmd = "gradle test", f"{g} test", f"{g} build"
        if (root / "app/src/main/AndroidManifest.xml").exists():
            i.add("frameworks", "Android"); i.kind = "mobile"
    xc = list(root.glob("*.xcodeproj")) + list(root.glob("*.xcworkspace"))
    if xc:
        i.add("languages", "swift"); i.add("frameworks", "Xcode"); i.kind = "mobile"
        i.test_framework, i.build_cmd = "XCTest", f"xcodebuild -project {xc[0].name} build" if xc[0].suffix == ".xcodeproj" else ""
    if (root / "Package.swift").exists():
        i.add("languages", "swift"); i.add("package_managers", "swiftpm")
        i.test_framework, i.test_cmd, i.build_cmd = "XCTest (swift)", "swift test", "swift build"
    if (root / "pubspec.yaml").exists():
        i.add("languages", "dart"); i.add("frameworks", "Flutter"); i.kind = "mobile"
        i.test_framework, i.test_cmd, i.build_cmd = "flutter test", "flutter test", "flutter build apk"


@builtin("services")
def _services(root: Path, i: ProjectInfo) -> None:
    if (root / "Dockerfile").exists() or (root / "docker-compose.yml").exists() or (root / "compose.yaml").exists():
        i.add("services", "Docker")
    if (root / "supabase").is_dir():
        i.add("services", "Supabase")
    if (root / "firebase.json").exists():
        i.add("services", "Firebase")
    if (root / "prisma").is_dir():
        i.add("databases", "Prisma")
    if (root / "playwright.config.ts").exists() or (root / "playwright.config.js").exists():
        i.test_framework = i.test_framework or "playwright"
    if (root / "cypress.config.ts").exists() or (root / "cypress.config.js").exists():
        i.add("services", "Cypress")
    if any(root.glob("*.html")) and i.kind == "unknown":
        port = _stable_port(root)
        i.kind, i.dev_cmd, i.dev_port = "web", f"{shlex.quote(sys.executable)} -m http.server {port}", port
        i.add("languages", "html")


@builtin("structure")
def _structure(root: Path, i: ProjectInfo) -> None:
    fe, be = [], []
    for d in ("frontend", "client", "web", "app", "src", "ui", "public", "pages", "components"):
        if (root / d).is_dir():
            fe.append(d)
    for d in ("backend", "server", "api", "services", "db", "migrations", "prisma", "supabase"):
        if (root / d).is_dir():
            be.append(d)
    tests = [d for d in ("tests", "test", "__tests__", "spec", "e2e") if (root / d).is_dir()]
    i.structure = {"frontend-ish": fe, "backend-ish": be, "tests": tests}


def detect_project(root: Path) -> ProjectInfo:
    """Run every plugin's detector (built-ins, entry points, trusted local plugins). A failing plugin never breaks detection."""
    from .plugins import load_registry
    info = ProjectInfo(root=str(root))
    reg = load_registry(root)
    for pl in reg.plugins:
        try:
            pl.detect(root, info)
        except Exception as e:  # noqa: BLE001
            reg.errors.append(f"{pl.name}.detect failed: {e}")
            info.notes.append(f"plugin {pl.name} detect failed: {e}")
    for d in list(_DETECTORS):                              # legacy @detector functions
        try:
            d(root, info)
        except Exception as e:  # noqa: BLE001
            info.notes.append(f"detector {getattr(d, '__name__', d)} failed: {e}")
    if (root / ".git").exists():
        info.add("services", "git")
    return info
