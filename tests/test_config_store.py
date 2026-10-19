# test_config_store.py
# -*- coding: utf-8 -*-
"""config_store 单元测试：加载 / get 归一 / set_many 原子写 / 损坏 JSON 报错。"""
import json
import os
import stat

import pytest

from src.config import config_store


@pytest.fixture(autouse=True)
def _reset():
    """每个用例前重置模块态，避免用例间串扰。"""
    config_store._data = {}
    config_store._path = ""
    yield


def test_init_missing_file_empty(tmp_path):
    config_store.init(str(tmp_path / "config.json"))
    assert config_store.get("CODEARTS_ACCOUNT_NAME") == ""
    assert config_store.get("CODEARTS_ACCOUNT_NAME", "dft") == "dft"


def test_init_loads_json(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"CODEARTS_ACCOUNT_NAME": "账号A", "CODEARTS_PROXY_PORT": 51818}', encoding="utf-8")
    config_store.init(str(p))
    assert config_store.get("CODEARTS_ACCOUNT_NAME") == "账号A"
    assert config_store.get("CODEARTS_PROXY_PORT") == "51818"  # int 归一为 str


def test_init_broken_json_exits(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{broken", encoding="utf-8")
    with pytest.raises(SystemExit, match="config.json 解析失败"):
        config_store.init(str(p))


def test_set_many_atomic_write(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"A": "1"}', encoding="utf-8")
    config_store.init(str(p))
    config_store.set_many({"A": "2", "B": "3"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"A": "2", "B": "3"}
    assert config_store.get("B") == "3"
    assert not os.path.exists(str(p) + ".tmp")  # 原子替换后 tmp 不残留


def test_set_many_preserves_mode(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"A": "1"}', encoding="utf-8")
    os.chmod(p, 0o600)
    config_store.init(str(p))
    config_store.set_many({"A": "9"})
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_as_dict_snapshot(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"A": "1"}', encoding="utf-8")
    config_store.init(str(p))
    d = config_store.as_dict()
    d["A"] = "mutated"
    assert config_store.get("A") == "1"  # 浅拷贝，外部修改不影响内部态
