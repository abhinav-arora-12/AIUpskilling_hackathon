import os

from app.config import _load_dotenv


def test_dotenv_loads_without_overriding_existing(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\nFOO_TEST_KEY=abc\nEMPTY_TEST_KEY=\nQUOTED_TEST_KEY='x y'\n", encoding="utf-8")
    monkeypatch.setenv("FOO_TEST_KEY", "from-env")
    monkeypatch.delenv("EMPTY_TEST_KEY", raising=False)
    monkeypatch.delenv("QUOTED_TEST_KEY", raising=False)
    _load_dotenv(f)
    assert os.environ["FOO_TEST_KEY"] == "from-env"  # real environment wins
    assert os.environ["EMPTY_TEST_KEY"] == ""
    assert os.environ["QUOTED_TEST_KEY"] == "x y"
