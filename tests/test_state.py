from timewarptv.state import load_state, save_state


def test_round_trip(tmp_path):
    path = tmp_path / "state.json"
    data = {2: {"path": "/media/a.mp4", "position": 12.5}, 9: {"path": "/media/b.mp4", "position": 0.0}}
    save_state(path, data)
    loaded = load_state(path)
    assert loaded == data


def test_missing_file_returns_empty(tmp_path):
    assert load_state(tmp_path / "nope.json") == {}


def test_none_path_returns_empty_and_never_writes(tmp_path):
    assert load_state(None) == {}
    save_state(None, {2: {"path": "x", "position": 1.0}})  # must not raise


def test_corrupt_json_degrades_to_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ not valid json")
    assert load_state(path) == {}


def test_unwritable_path_does_not_raise(tmp_path):
    # A path under a file (not a directory) can never be created.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    bad_path = blocker / "state.json"
    save_state(bad_path, {2: {"path": "x", "position": 1.0}})  # must not raise
    assert load_state(bad_path) == {}


def test_save_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"
    save_state(path, {2: {"path": "x", "position": 1.0}})
    assert path.is_file()
    assert load_state(path) == {2: {"path": "x", "position": 1.0}}
