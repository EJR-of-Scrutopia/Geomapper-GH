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


def test_an_empty_object_falls_back_to_defaults(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("{}", encoding="utf-8")
    assert load_config(target) == Config()


def test_a_bare_top_level_int_falls_back_to_defaults_without_raising(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("123", encoding="utf-8")
    assert load_config(target) == Config()


def test_a_bare_top_level_null_falls_back_to_defaults_without_raising(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("null", encoding="utf-8")
    assert load_config(target) == Config()


def test_a_null_field_value_falls_back_to_that_fields_default_without_raising(tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"output_root": null}', encoding="utf-8")
    assert load_config(target) == Config()


def test_a_bare_top_level_string_falls_back_to_defaults_without_raising(tmp_path):
    target = tmp_path / "config.json"
    target.write_text('"a string"', encoding="utf-8")
    assert load_config(target) == Config()


def test_a_bare_top_level_list_falls_back_to_defaults_without_raising(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_config(target) == Config()


def test_an_integer_value_for_a_float_field_is_accepted_and_coerced(tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"tile_size_m": 1500}', encoding="utf-8")
    config = load_config(target)
    assert config.tile_size_m == 1500.0
    assert isinstance(config.tile_size_m, float)


def test_a_bool_value_for_a_float_field_falls_back_to_the_default(tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"tile_size_m": true}', encoding="utf-8")
    assert load_config(target).tile_size_m == 2000.0


# --- Task 18: the elevation API key field -----------------------------
#
# opentopography_api_key is a string field like output_root, so it already
# rides load_config's existing per-field type check (see the isinstance
# branch above) rather than needing one of its own. These tests exist to
# prove that generalisation actually holds for this specific field, not to
# add new fallback logic.


def test_api_key_defaults_to_an_empty_string():
    assert Config().opentopography_api_key == ""


def test_save_then_load_round_trips_the_api_key(tmp_path):
    target = tmp_path / "config.json"
    save_config(Config(opentopography_api_key="sk-real-key-value"), target)
    assert load_config(target).opentopography_api_key == "sk-real-key-value"


def test_a_null_api_key_falls_back_to_the_default_without_raising(tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"opentopography_api_key": null}', encoding="utf-8")
    assert load_config(target).opentopography_api_key == ""


def test_a_numeric_api_key_falls_back_to_the_default(tmp_path):
    # A key is always a string; a bare number in hand-edited JSON is the
    # wrong shape entirely, not a value worth coercing the way an int is
    # accepted for a float field.
    target = tmp_path / "config.json"
    target.write_text('{"opentopography_api_key": 12345}', encoding="utf-8")
    assert load_config(target).opentopography_api_key == ""
