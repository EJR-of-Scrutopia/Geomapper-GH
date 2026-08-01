import re

import mapgen


def test_version_is_a_semver_string():
    assert isinstance(mapgen.__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", mapgen.__version__)
