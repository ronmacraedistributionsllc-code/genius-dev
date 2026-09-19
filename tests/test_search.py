import json
import subprocess

import pytest
from typer.testing import CliRunner

from genius_dev.cli import app
from genius_dev.project import Project
from genius_dev.semantic import expand, semantic_search, tokenize

runner = CliRunner()


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "shop"; d.mkdir()
    (d / "accounts.py").write_text('''class MerchantService:
    """Merchant accounts."""
    def sign_in(self, username, pw):
        """Authenticate a merchant and create a session token."""
        return self.session_for(username)

    def reset_password(self, email):
        """Send a password reset link with a one-time token."""
        return send_mail(email, make_token())

    def session_for(self, username):
        return {"user": username}
''')
    (d / "billing.py").write_text("def checkout(cart):\n    return charge_card(cart.total)\n\ndef refund_order(order_id):\n    return stripe_refund(order_id)\n")
    (d / "api.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n\n@app.post('/merchants/login')\nasync def merchant_login(): ...\n")
    (d / "Dashboard.tsx").write_text("export function MerchantDashboard() { return null }\nexport const OrderTable = () => null\n")
    (d / "schema.sql").write_text("CREATE TABLE merchants (id int);\nCREATE TABLE orders (id int);\n")
    (d / "docs.md").write_text("# Ops\n\n## Password recovery\nUsers can recover accounts through the reset email flow.\n\n## Deploy\nRun the pipeline.\n")
    (d / "tests").mkdir(); (d / "tests" / "test_accounts.py").write_text("def test_sign_in():\n    assert True\n")
    subprocess.run(["git", "init", "-q"], cwd=d)
    p = Project.open(d); p.index.refresh()
    return p


def test_tokenizer_splits_identifiers_and_drops_stopwords():
    assert tokenize("MerchantService.reset_password") == ["merchantservice", "merchant", "service", "reset", "password"] or "merchant" in tokenize("MerchantService")
    assert "where" not in tokenize("where is password reset handled") and "password" in tokenize("where is password reset handled")


def test_concept_expansion_and_typos():
    v = {"login", "session", "token", "checkout", "charge"}
    w = expand(tokenize("authentication"), v)
    assert w.get("login") == 0.35 and "session" in w and "checkout" not in w
    assert "checkout" in expand(["chekout"], v)


@pytest.mark.parametrize("query,expected_path,expected_title", [
    ("where is password reset handled", "accounts.py", "function reset_password"),
    ("merchant authentication", "accounts.py", "function sign_in"),
    ("how do we charge a customer's card", "billing.py", "function checkout"),
    ("refund", "billing.py", "function refund_order"),
    ("password recovery documentation", "docs.md", "Password recovery"),
    ("login endpoint", "api.py", "function merchant_login"),
])
def test_semantic_queries_find_the_right_code(repo, query, expected_path, expected_title):
    hits = semantic_search(repo.index, query, 5)
    assert any(h["path"] == expected_path and h["title"] == expected_title for h in hits[:3]), hits


def test_semantic_chunks_cover_components_docs_tests_and_are_incremental(repo):
    kinds = {h["kind"] for h in semantic_search(repo.index, "merchant dashboard order table", 10)} | {h["kind"] for h in semantic_search(repo.index, "sign in test", 10)}
    assert {"component", "test"} <= kinds
    (repo.root / "billing.py").write_text("def invoice_customer(x):\n    return x\n")
    repo.index.refresh()
    assert not any(h["title"] == "function checkout" for h in semantic_search(repo.index, "checkout", 10))
    assert any(h["title"] == "function invoice_customer" for h in semantic_search(repo.index, "invoice", 10))


def test_semantic_search_is_offline(repo, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")))
    assert semantic_search(repo.index, "password reset", 3)


def test_context_selection_uses_semantic_ranking(repo):
    files = [f for f, _ in repo.index.relevant_files("users can't recover their account access", 3)]
    assert "accounts.py" in files


def cli(repo, *args):
    return runner.invoke(app, ["search", *args, "--path", str(repo.root)], catch_exceptions=False)


def test_cli_search_modes(repo):
    r = cli(repo, "where is password reset handled", "--json")
    assert json.loads(r.output)[0]["path"] in ("accounts.py", "docs.md")
    d = json.loads(cli(repo, "--symbol", "MerchantService", "--json").output)
    assert d[0]["path"] == "accounts.py" and "MerchantService" in d[0]["title"]
    assert json.loads(cli(repo, "--file", "billng", "--json").output)[0]["path"] == "billing.py"
    t = json.loads(cli(repo, "--text", "stripe_refund", "--json").output)
    assert t[0]["path"] == "billing.py" and t[0]["kind"] == "text"
    assert json.loads(cli(repo, "merchant", "--semantic", "--json").output)
    assert cli(repo).exit_code == 2
    assert "accounts.py" in cli(repo, "merchant authentication").output


def test_requirements_and_tasks_are_searchable(repo):
    repo.reqs.add("Merchant can reset their password by email", verify="tests")
    repo.tasks.add("Build password reset flow")
    kinds = {h["kind"] for h in json.loads(cli(repo, "reset password", "--json").output)}
    assert {"requirement", "task"} <= kinds
