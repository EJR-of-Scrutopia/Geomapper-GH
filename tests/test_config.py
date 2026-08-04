import pytest

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


# --- Task 22: the theme setting -----------------------------------------


def test_theme_defaults_to_auto():
    assert Config().theme == "auto"


def test_theme_round_trips_through_save_and_load(tmp_path):
    target = tmp_path / "config.json"
    save_config(Config(theme="dark"), target)
    assert load_config(target).theme == "dark"


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


# --- A coordinator review's free fix: a rejected field now says so -----
#
# Before this, a wrong-typed or null field value fell back to that field's
# own default with nothing anywhere to say it had happened. The tests
# above this comment (a null field, a bool for a float field, a null or
# numeric API key) all cover that fallback ITSELF, which this fix leaves
# completely unchanged; none of them noticed the silence. These do,
# without touching any of those, so both properties, "still falls back"
# and "now says so", stay pinned by tests of their own.


def test_a_wrong_typed_field_value_warns_and_names_the_field(tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"output_root": 123}', encoding="utf-8")
    with pytest.warns(UserWarning, match="output_root"):
        config = load_config(target)
    assert config.output_root == Config().output_root


def test_a_null_field_value_warns_too(tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"last_region": null}', encoding="utf-8")
    with pytest.warns(UserWarning, match="last_region"):
        load_config(target)


def test_a_bool_value_for_a_float_field_warns_specifically(tmp_path):
    # bool is a subclass of int, so this exercises the branch that
    # excludes it on purpose (see the isinstance checks above) rather
    # than the more general wrong-type branch the other two tests here
    # exercise.
    target = tmp_path / "config.json"
    target.write_text('{"tile_size_m": true}', encoding="utf-8")
    with pytest.warns(UserWarning, match="tile_size_m"):
        load_config(target)


def test_a_correctly_typed_field_value_does_not_warn(recwarn, tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"output_root": "D:/S"}', encoding="utf-8")
    load_config(target)
    assert len(recwarn) == 0


def test_an_unknown_key_does_not_warn(recwarn, tmp_path):
    # An unrecognised key (see test_unknown_keys_are_ignored above) is a
    # different, pre-existing case: there is no expected type to compare
    # it against for a field this Config does not have at all, so the
    # loop never reaches either branch that warns for a known one.
    target = tmp_path / "config.json"
    target.write_text('{"from_the_future": 1}', encoding="utf-8")
    load_config(target)
    assert len(recwarn) == 0


# --- Task 28: which elevation model, saved between runs -----------------


def test_the_elevation_model_defaults_to_cop30():
    # Every run before this task downloaded COP30 and every existing
    # config.json predates the field entirely, so the default is what
    # keeps those behaving identically.
    assert Config().elevation_demtype == "COP30"


def test_a_config_written_before_this_field_existed_still_loads(tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"output_root": "C:\\Surveys", "tile_size_m": 2000}', encoding="utf-8")
    loaded = load_config(target)
    assert loaded.elevation_demtype == "COP30"
    assert loaded.tile_size_m == 2000.0


def test_save_then_load_round_trips_the_elevation_model(tmp_path):
    target = tmp_path / "config.json"
    save_config(Config(elevation_demtype="EU_DTM"), target)
    assert load_config(target).elevation_demtype == "EU_DTM"


def test_a_numeric_elevation_model_falls_back_to_the_default(tmp_path):
    # load_config only ever checks a field's TYPE; it has no notion of the
    # model vocabulary, which is deliberately known in exactly one place
    # (see mapgen.elevation_models, refused at SurveyRequest). This covers
    # the type half only.
    target = tmp_path / "config.json"
    target.write_text('{"elevation_demtype": 30}', encoding="utf-8")
    with pytest.warns(UserWarning, match="elevation_demtype"):
        assert load_config(target).elevation_demtype == "COP30"


def test_an_unrecognised_but_string_model_is_loaded_unchanged(tmp_path):
    # And is refused later, by SurveyRequest, with a message naming the
    # valid models. Coercing it to the default here instead would be a
    # second, silent vocabulary check in the one module that is supposed
    # not to have one, and the owner would never learn their saved
    # setting was wrong.
    target = tmp_path / "config.json"
    target.write_text('{"elevation_demtype": "nonsense"}', encoding="utf-8")
    assert load_config(target).elevation_demtype == "nonsense"
