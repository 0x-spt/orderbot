from orderbot.config import ConfigError, load_config


def test_mac_window_settings(tmp_path):
    cfg = load_config({"RDP_WINDOW_TITLE_CONTAINS": "rdp.example", "IDLE_MIN_SECONDS": "45"},
                      warehouses_path=tmp_path / "none.yaml")
    assert cfg.rdp_window_title_contains == "rdp.example"
    assert cfg.idle_min_seconds == 45.0


def test_mac_window_defaults(tmp_path):
    cfg = load_config({}, warehouses_path=tmp_path / "none.yaml")
    assert cfg.rdp_window_title_contains == ""
    assert cfg.idle_min_seconds == 30.0
    assert not hasattr(cfg, "rdp_password")  # the robot never logs in to RDP itself


import pytest


@pytest.mark.parametrize("raw", ["9.9", "0", "-5", "abc", "nan"])
def test_idle_min_seconds_floor(tmp_path, raw):
    with pytest.raises(ConfigError):
        load_config({"IDLE_MIN_SECONDS": raw}, warehouses_path=tmp_path / "none.yaml")


def test_idle_min_seconds_floor_is_inclusive(tmp_path):
    cfg = load_config({"IDLE_MIN_SECONDS": "10"}, warehouses_path=tmp_path / "none.yaml")
    assert cfg.idle_min_seconds == 10.0


# ------------------------------------------------- WAREHOUSES_FILE and DATA_DIR
def test_empty_warehouses_file_means_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "warehouses.yaml").write_text('ozon:\n  "Склад": "СПБ"\n', encoding="utf-8")
    cfg = load_config({"WAREHOUSES_FILE": ""})
    assert cfg.warehouses == {"ozon": {"Склад": "СПБ"}}


def test_missing_default_warehouses_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config({}).warehouses == {}


def test_explicit_warehouses_file_must_exist(tmp_path):
    with pytest.raises(ConfigError, match="WAREHOUSES_FILE"):
        load_config({"WAREHOUSES_FILE": str(tmp_path / "typo.yaml")})


def test_warehouses_file_must_be_a_file(tmp_path):
    with pytest.raises(ConfigError, match="не файл"):
        load_config({"WAREHOUSES_FILE": str(tmp_path)})


def test_broken_warehouses_yaml_is_a_config_error(tmp_path):
    f = tmp_path / "w.yaml"
    f.write_text("ozon: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="не читается"):
        load_config({"WAREHOUSES_FILE": str(f)})


def test_unreadable_warehouses_file_is_a_config_error(tmp_path):
    f = tmp_path / "w.yaml"
    f.write_text("ozon: {}\n", encoding="utf-8")
    f.chmod(0)
    try:
        with pytest.raises(ConfigError, match="не читается"):
            load_config({"WAREHOUSES_FILE": str(f)})
    finally:
        f.chmod(0o600)


@pytest.mark.parametrize("text", ["- a\n- b\n", "ozon: [1, 2]\n", "ozon:\n  wh: [x]\n"])
def test_warehouses_must_be_marketplace_to_name_maps(tmp_path, text):
    f = tmp_path / "w.yaml"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config({"WAREHOUSES_FILE": str(f)})


def test_data_dir_empty_means_default(tmp_path):
    none = tmp_path / "none.yaml"
    assert load_config({"DATA_DIR": ""}, warehouses_path=none).data_dir == "data"
    assert load_config({}, warehouses_path=none).data_dir == "data"
    assert load_config({"DATA_DIR": "/srv/robot"}, warehouses_path=none).data_dir == "/srv/robot"


def test_example_warehouses_yaml_loads():
    from pathlib import Path
    cfg = load_config({}, warehouses_path=Path(__file__).parent.parent / "warehouses.example.yaml")
    assert cfg.warehouses["yandex"]["100000001"] == "Тест 1С"
    assert "ozon" in cfg.warehouses
    assert cfg.ym_campaign_id == ""  # optional


def test_price_exceptions_file(tmp_path):
    from decimal import Decimal

    import pytest

    from orderbot.config import ConfigError, _price_exceptions

    f = tmp_path / "ex.txt"
    f.write_text("# comment\nFIX-0001 5900\n  SET-0002/A-2   21000  # set\n\n", encoding="utf-8")
    assert _price_exceptions({"PRICE_EXCEPTIONS_FILE": str(f)}) == {
        "fix-0001": Decimal("5900"), "set-0002/a-2": Decimal("21000")}
    assert _price_exceptions({"PRICE_EXCEPTIONS_FILE": str(tmp_path / "none.txt")}) == {}
    f.write_text("FIX-0001\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        _price_exceptions({"PRICE_EXCEPTIONS_FILE": str(f)})


def test_example_price_exceptions_list_loads():
    from decimal import Decimal
    from pathlib import Path

    from orderbot.config import _price_exceptions

    root = Path(__file__).resolve().parent.parent
    ex = _price_exceptions({"PRICE_EXCEPTIONS_FILE": str(root / "price_exceptions.example.txt")})
    assert ex == {"art-0001": Decimal("5900"), "set-02/a-2": Decimal("21000")}


def test_wb_settings_from_env(tmp_path):
    cfg = load_config({"WB_TOKEN": "t", "WB_PAID_FIELD": "price"},
                      warehouses_path=tmp_path / "none.yaml")
    assert cfg.wb_token == "t" and cfg.wb_paid_field == "price"


def test_express_section_read(tmp_path):
    p = tmp_path / "w.yaml"
    p.write_text('ozon:\n  "Склад": "СПБ"\nexpress:\n  ozon: ["А EXPRESS 🚀"]\n'
                 '  yandex: ["900000009"]\n', encoding="utf-8")
    cfg = load_config({}, warehouses_path=p)
    assert cfg.express == {"ozon": {"А EXPRESS 🚀"}, "yandex": {"900000009"}}
    assert cfg.warehouses == {"ozon": {"Склад": "СПБ"}}
    p.write_text('ozon:\n  "Склад": "СПБ"\n', encoding="utf-8")
    assert load_config({}, warehouses_path=p).express == {}
    assert load_config({}, warehouses_path=tmp_path / "none.yaml").express == {}


def test_config_reads_alert_chat_and_managers(tmp_path):
    cfg = load_config({"ALERT_CHAT_ID": "-1001234", "MANAGER_USER_IDS": "5, 6",
                       "ALLOWED_USER_IDS": "1"}, tmp_path / "w.yaml")
    assert cfg.alert_chat_id == -1001234 and cfg.manager_user_ids == [5, 6]
    empty = load_config({}, tmp_path / "w.yaml")
    assert empty.alert_chat_id is None and empty.manager_user_ids == []


@pytest.mark.parametrize("env", [{"ALERT_CHAT_ID": "abc"}, {"MANAGER_USER_IDS": "1,x"}])
def test_bad_alert_chat_or_managers_is_config_error(tmp_path, env):
    with pytest.raises(ConfigError):
        load_config(env, tmp_path / "w.yaml")


def test_config_reads_alert_thread(tmp_path):
    cfg = load_config({"ALERT_CHAT_ID": "-1001234", "ALERT_THREAD_ID": " 42 "}, tmp_path / "w.yaml")
    assert cfg.alert_thread_id == 42
    assert load_config({}, tmp_path / "w.yaml").alert_thread_id is None


@pytest.mark.parametrize("raw", ["abc", "4.5", "-"])
def test_bad_alert_thread_is_config_error(tmp_path, raw):
    with pytest.raises(ConfigError):
        load_config({"ALERT_THREAD_ID": raw}, tmp_path / "w.yaml")
