from pathlib import Path

from mapgen.config import Config, load_config, save_config


def test_load_returns_defaults_when_absent(tmp_path):
    config = load_config(tmp_path / "absent.json")
    assert config.tile_size_m == 2000.0
    assert config.output_root


def test_save_then_load_round_trips(tmp_path):
    target = tmp_path / "config.json"
    save_config(Config(output_root="D:/Surveys", last_region="South Wales"), target)
    loaded = load_config(target)
    assert loaded.output_root == "D:/Surveys"
    assert loaded.last_region == "South Wales"


def test_a_corrupt_config_falls_back_to_defaults(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("{not json", encoding="utf-8")
    assert load_config(target).tile_size_m == 2000.0


def test_unknown_keys_are_ignored(tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"output_root": "D:/S", "from_the_future": 1}', encoding="utf-8")
    assert load_config(target).output_root == "D:/S"
