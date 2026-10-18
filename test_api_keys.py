# -*- coding: utf-8 -*-
"""api_keys 模块单元测试：增删改查 / 原子写与权限 / 降级 / 恒定时间认证。"""
import json
import os
import stat

import pytest

import api_keys


@pytest.fixture
def store(tmp_path):
    """每个用例独立的 api-keys.json 路径与干净模块状态。"""
    path = str(tmp_path / "api-keys.json")
    api_keys.init(path)
    yield path
    api_keys.init(str(tmp_path / "unused.json"))  # 重置内存态，防跨用例泄漏


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class TestCrud:
    def test_create_returns_plaintext_and_persists(self, store):
        entry = api_keys.create_key("测试")
        assert entry["key"].startswith("sk-")
        assert len(entry["key"]) == len("sk-") + 32
        assert entry["name"] == "测试"
        assert entry["enabled"] is True
        assert entry["id"] == 1
        # 已落盘且可被重新读出
        with open(store, "r", encoding="utf-8") as handle:
            on_disk = json.load(handle)
        assert on_disk[0]["key"] == entry["key"]

    def test_ids_are_unique_and_increment(self, store):
        first = api_keys.create_key("a")
        second = api_keys.create_key("b")
        assert first["id"] == 1
        assert second["id"] == 2

    def test_list_returns_deep_copy(self, store):
        api_keys.create_key("a")
        listed = api_keys.list_keys()
        listed[0]["name"] = "被外部篡改"
        assert api_keys.list_keys()[0]["name"] == "a"

    def test_update_name_and_enabled(self, store):
        entry = api_keys.create_key("a")
        updated = api_keys.update_key(entry["id"], name="b", enabled=False)
        assert updated["name"] == "b"
        assert updated["enabled"] is False
        assert updated["key"] == entry["key"]  # 明文不变

    def test_update_unknown_id_returns_none(self, store):
        api_keys.create_key("a")
        assert api_keys.update_key(999, name="x") is None

    def test_delete_removes_and_reports(self, store):
        entry = api_keys.create_key("a")
        assert api_keys.delete_key(entry["id"]) is True
        assert api_keys.list_keys() == []
        assert api_keys.delete_key(entry["id"]) is False


class TestPersistence:
    def test_reload_keeps_keys(self, tmp_path):
        path = str(tmp_path / "api-keys.json")
        api_keys.init(path)
        created = api_keys.create_key("持久化")
        api_keys.init(path)  # 模拟重启
        assert [e["key"] for e in api_keys.list_keys()] == [created["key"]]

    def test_file_permission_is_0600(self, store):
        api_keys.create_key("perm")
        assert _mode(store) == 0o600

    def test_tmp_file_is_not_left_behind(self, store, tmp_path):
        api_keys.create_key("tmp")
        assert not os.path.exists(store + ".tmp")

    def test_write_failure_rolls_back_memory(self, store, monkeypatch):
        api_keys.create_key("a")

        def boom(entries):
            raise OSError("disk full")

        monkeypatch.setattr(api_keys, "_write_locked", boom)
        with pytest.raises(OSError):
            api_keys.create_key("b")
        # 内存态未受污染：仍只有原来那一个 key
        assert [e["name"] for e in api_keys.list_keys()] == ["a"]
        # 且磁盘内容未变
        with open(store, "r", encoding="utf-8") as handle:
            assert [e["name"] for e in json.load(handle)] == ["a"]


class TestDegradation:
    def test_missing_file_starts_empty_and_is_not_created(self, tmp_path):
        path = str(tmp_path / "api-keys.json")
        api_keys.init(path)
        assert api_keys.list_keys() == []
        assert not os.path.exists(path)

    def test_corrupt_json_degrades_to_empty(self, tmp_path, capsys):
        path = str(tmp_path / "api-keys.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ 不是合法 JSON")
        api_keys.init(path)
        assert api_keys.list_keys() == []
        assert "api-keys.json" in capsys.readouterr().out

    def test_non_list_top_level_degrades(self, tmp_path, capsys):
        path = str(tmp_path / "api-keys.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"key": "sk-x"}, handle)
        api_keys.init(path)
        assert api_keys.list_keys() == []
        assert "数组" in capsys.readouterr().out

    def test_invalid_entries_are_skipped(self, tmp_path, capsys):
        path = str(tmp_path / "api-keys.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump([{"id": 1, "key": "sk-ok", "name": "ok"},
                       {"id": 2, "name": "缺 key"},
                       "不是对象"], handle)
        api_keys.init(path)
        assert [e["name"] for e in api_keys.list_keys()] == ["ok"]
        assert "非法的条目" in capsys.readouterr().out

    def test_operations_without_init_are_harmless(self, monkeypatch):
        monkeypatch.setattr(api_keys, "_initialized", False)
        monkeypatch.setattr(api_keys, "_keys", [])
        assert api_keys.list_keys() == []
        assert api_keys.authenticate("sk-x") == (api_keys.AUTH_NOT_FOUND, None)
        with pytest.raises(RuntimeError):
            api_keys.create_key("x")


class TestAuthenticate:
    def test_valid_key(self, store):
        entry = api_keys.create_key("a")
        status, matched = api_keys.authenticate(entry["key"])
        assert status == api_keys.AUTH_VALID
        assert matched["id"] == entry["id"]

    def test_unknown_key(self, store):
        api_keys.create_key("a")
        assert api_keys.authenticate("sk-不存在") == (api_keys.AUTH_NOT_FOUND, None)

    def test_disabled_key(self, store):
        entry = api_keys.create_key("a")
        api_keys.update_key(entry["id"], enabled=False)
        status, matched = api_keys.authenticate(entry["key"])
        assert status == api_keys.AUTH_DISABLED
        assert matched is None

    def test_non_string_candidate(self, store):
        api_keys.create_key("a")
        assert api_keys.authenticate(None) == (api_keys.AUTH_NOT_FOUND, None)
        assert api_keys.authenticate(123) == (api_keys.AUTH_NOT_FOUND, None)

    def test_non_ascii_candidate_does_not_raise(self, store):
        """Werkzeug 按 latin-1 解码请求头，非 ASCII 值不得让比较抛异常。"""
        entry = api_keys.create_key("a")
        assert api_keys.authenticate("sk-\xff\xfe") == (api_keys.AUTH_NOT_FOUND, None)
        assert api_keys.authenticate("sk-中文") == (api_keys.AUTH_NOT_FOUND, None)
        assert api_keys.authenticate(entry["key"])[0] == api_keys.AUTH_VALID

    def test_very_long_candidate(self, store):
        api_keys.create_key("a")
        assert api_keys.authenticate("sk-" + "a" * 10000) == (api_keys.AUTH_NOT_FOUND, None)

    def test_matched_entry_is_a_copy(self, store):
        entry = api_keys.create_key("a")
        _, matched = api_keys.authenticate(entry["key"])
        matched["name"] = "篡改"
        assert api_keys.list_keys()[0]["name"] == "a"

    def test_compares_every_entry_without_short_circuit(self, store, monkeypatch):
        """命中首条后仍须遍历全部条目，避免比较耗时泄露命中位置。"""
        entries = [api_keys.create_key(f"k{i}") for i in range(3)]
        calls = []
        real_compare = api_keys.hmac.compare_digest
        monkeypatch.setattr(
            api_keys.hmac, "compare_digest",
            lambda a, b: calls.append(1) or real_compare(a, b),
        )
        status, matched = api_keys.authenticate(entries[0]["key"])
        assert status == api_keys.AUTH_VALID
        assert matched["id"] == entries[0]["id"]
        assert len(calls) == 3

    def test_non_ascii_stored_key_does_not_crash(self, store):
        """磁盘上的历史 key 含非 ASCII 时，比较不得抛 TypeError。"""
        with open(store, "w", encoding="utf-8") as handle:
            json.dump(
                [{"id": 1, "key": "sk-中文", "name": "legacy", "enabled": True}],
                handle, ensure_ascii=False,
            )
        api_keys.init(store)
        assert api_keys.authenticate("sk-任意") == (api_keys.AUTH_NOT_FOUND, None)
        assert api_keys.authenticate("sk-中文")[0] == api_keys.AUTH_VALID


class TestNoPlaintextInOutput:
    def test_operations_do_not_print_plaintext(self, store, capsys):
        """明文只应落文件，不得出现在 stdout/stderr（日志/异常）。"""
        entry = api_keys.create_key("日志检查")
        api_keys.authenticate(entry["key"])
        api_keys.authenticate("sk-错误值")
        api_keys.update_key(entry["id"], name="改名")
        api_keys.delete_key(entry["id"])
        captured = capsys.readouterr()
        assert "sk-" not in captured.out
        assert "sk-" not in captured.err
