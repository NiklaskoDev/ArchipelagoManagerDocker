"""Pure unit tests: no server, no Archipelago."""
import pytest

from archipelago_manager import apbridge, settings, yamlform, yamlinfo
from archipelago_manager.config import CONFIG
from archipelago_manager.generation import _player_file_errors, _summarize_failure
from archipelago_manager.gamestore import classify
from archipelago_manager.security import hash_password, verify_password, LoginThrottle


class TestYamlInfo:
    def test_single_player(self):
        info = yamlinfo.analyze("name: Alice\ngame: Zillion\nZillion: {}\n", "alice")
        assert info.player_names == ["Alice"]
        assert info.games == ["Zillion"]
        assert info.warnings == [] and info.problems == []

    def test_missing_game_section_is_a_problem_the_generator_would_hit(self):
        info = yamlinfo.analyze("name: Alice\ngame: Zillion\n", "alice")
        assert len(info.problems) == 1 and "Zillion:" in info.problems[0]
        assert info.problems[0] in info.warnings  # shown to the user when saving
        # a section that is not a mapping (e.g. an empty "Zillion:" which YAML reads as null) counts as missing too
        assert yamlinfo.analyze("name: A\ngame: Zillion\nZillion:\n", "x").problems

    def test_bom_and_description(self):
        info = yamlinfo.analyze("\ufeffname: Bob\ndescription: my run\ngame: Zillion\n", "bob")
        assert info.player_names == ["Bob"] and info.description == "my run"

    def test_multiple_documents_and_weighted_games(self):
        text = "name: A\ngame:\n  Zillion: 50\n  Timespinner: 50\n  Gone: 0\n---\nname: B\ngame: Zillion\nZillion: {}\n"
        info = yamlinfo.analyze(text, "x")
        assert info.player_names == ["A", "B"]
        assert info.games == ["Timespinner", "Zillion"]  # weight 0 can never be rolled

    def test_missing_name_uses_file_stem_like_the_generator(self):
        info = yamlinfo.analyze("game: Zillion\n", "my_file")
        assert info.player_names == ["my_file"]
        assert any("file name" in w for w in info.warnings)

    def test_name_is_truncated_to_16_characters(self):
        info = yamlinfo.analyze("name: ABCDEFGHIJKLMNOPQRSTUVWXYZ\ngame: Zillion\n", "x")
        assert info.player_names == ["ABCDEFGHIJKLMNOP"]
        assert any("shortened" in w for w in info.warnings)

    @pytest.mark.parametrize("name", ["Player{number}", "P{PLAYER}", "Name%number%"])
    def test_placeholder_names_are_not_predictable(self, name):
        info = yamlinfo.analyze(f"name: {name}\ngame: Zillion\n", "x")
        assert info.player_names == []

    @pytest.mark.parametrize("text, message", [
        ("name: x\n", "no 'game'"),
        ("game: [unclosed\n", "Invalid YAML"),
        ("name: a\nname: b\ngame: Zillion\n", "duplicate key"),
        ("- just\n- a list\n", "mapping"),
        ("meta_description: hi\n", "meta.yaml"),
        ("", "no player settings"),
        ("game:\n  Zillion: 0\n", "weight 0"),
        ("name: [1]\ngame: Zillion\n", "must be text"),
    ])
    def test_rejects_unusable_files(self, text, message):
        with pytest.raises(yamlinfo.YamlError, match=message):
            yamlinfo.analyze(text, "x")

    def test_rejects_binary_and_huge_files(self):
        with pytest.raises(yamlinfo.YamlError):
            yamlinfo.decode_text(b"\xff\xfe\x00bad")
        with pytest.raises(yamlinfo.YamlError, match="too large"):
            yamlinfo.decode_text(b"x" * (yamlinfo.MAX_YAML_BYTES + 1))

    @pytest.mark.parametrize("raw, expected", [
        ("../../etc/passwd", "passwd"), ("a b.yaml", "a b.yaml"), ("we<ird>*.txt", "we_ird_.txt"),
        ("C:\\temp\\x.yaml", "x.yaml"), ("...", "file"), ("", "file"),
    ])
    def test_safe_filename(self, raw, expected):
        assert yamlinfo.safe_filename(raw) == expected


class TestServerOptions:
    def test_defaults_roundtrip(self):
        assert settings.clean_server_options({}) == settings.DEFAULT_SERVER_OPTIONS

    def test_overrides_merge_over_base(self):
        base = settings.clean_server_options({"hint_cost": 50})
        assert settings.clean_server_options({"release_mode": "goal"}, base=base)["hint_cost"] == 50

    @pytest.mark.parametrize("bad", [
        {"nope": 1}, {"hint_cost": -1}, {"hint_cost": 101}, {"hint_cost": "10"}, {"hint_cost": True},
        {"release_mode": "sometimes"}, {"remaining_mode": "auto"}, {"compatibility": 5}, {"disable_item_cheat": "yes"},
        {"auto_shutdown_minutes": 10 ** 9},
    ])
    def test_rejects_bad_values(self, bad):
        with pytest.raises(settings.SettingsError):
            settings.clean_server_options(bad)


class TestSecurity:
    def test_password_hashing(self):
        stored = hash_password("correct horse")
        assert stored != "correct horse" and stored.startswith("scrypt$")
        assert verify_password("correct horse", stored)
        assert not verify_password("wrong", stored)
        assert hash_password("correct horse") != stored  # salted

    @pytest.mark.parametrize("garbage", ["", "plain", "scrypt$1$2", "md5$a$b$c$d$e", "scrypt$x$y$z$!!$!!"])
    def test_verify_never_raises_on_garbage(self, garbage):
        assert verify_password("x", garbage) is False

    def test_login_throttle(self):
        throttle = LoginThrottle(limit=3, window=60)
        for _ in range(3):
            assert not throttle.blocked("ip|user")
            throttle.fail("ip|user")
        assert throttle.blocked("ip|user")
        assert not throttle.blocked("other")
        throttle.clear("ip|user")
        assert not throttle.blocked("ip|user")


class TestClassify:
    @pytest.mark.parametrize("name, kind, slot", [
        ("AP_123_P1_Alice.apz5", "patch", 1),
        ("AP_123_P12_Some_Name.apmc", "patch", 12),
        ("AP_123_Spoiler.txt", "spoiler", None),
        ("AP_123.archipelago", "multidata", None),
        ("alice.yaml", "yaml", None),
        ("player.YML", "yaml", None),
        ("README.txt", "other", None),
    ])
    def test_classify(self, name, kind, slot):
        assert classify(name) == (kind, slot)


class TestBaseRomMessages:
    TRACEBACK = ["Traceback (most recent call last):", '  File "Generate.py", line 1, in <module>']

    def test_a_missing_rom_says_which_file_goes_where(self):
        error = _summarize_failure(self.TRACEBACK + [
            "FileNotFoundError: Zelda no Densetsu - Kamigami no Triforce (Japan).sfc does not exist, but "
            "ALTTPSettings.rom_file is required", "Press enter to close."])
        first, rest = error.split("\n", 1)
        assert first.startswith("A file needed to generate is missing on the server.")
        assert '"Zelda no Densetsu - Kamigami no Triforce (Japan).sfc" into ' in first
        assert "FileNotFoundError" in rest  # the generator's own words stay below

    def test_a_wrong_rom_is_called_the_wrong_version(self):
        error = _summarize_failure(self.TRACEBACK + [
            "Exception: Supplied Base Rom does not match known MD5 for NA (1.0) release. Get the correct game and "
            "version, then dump it"])
        assert error.startswith("The base ROM on the server is not the version the game needs.")

    def test_other_failures_are_left_alone(self):
        error = _summarize_failure(["ValueError: player_sprite: banana is not a valid choice"])
        assert error == "ValueError: player_sprite: banana is not a valid choice"

    def test_files_in_the_data_folder_are_shown_relative_to_it(self):
        inside = apbridge.describe_file(CONFIG.data_dir / "ap-data" / "Archipelago" / "Game (USA).sfc")
        assert inside == {"file": "Game (USA).sfc", "folder": "ap-data/Archipelago", "in_data_dir": True}
        assert apbridge.file_location_text(inside) == '"Game (USA).sfc" into ap-data/Archipelago/ in the data folder'
        outside = apbridge.describe_file("/opt/archipelago/Game (USA).sfc")
        assert apbridge.file_location_text(outside) == '"Game (USA).sfc" into /opt/archipelago/'


class TestPlayerFileErrors:
    def test_each_file_gets_the_causes_listed_under_it(self):
        lines = [
            "ValueError: Encountered 2 error(s) in player files. See logs for full tracebacks.",
            "",
            "1. File Alice.yaml document #1 (with name: Alice) is invalid. Please fix your yaml.",
            "KeyError: 'Could not find option \"banana\" for \"PlayerSprite\"'",
            " Which caused: Options.OptionError: Error generating option player_sprite in APQuest",
            "",
            "2. File /data/jobs/3/Players/Bob-2.yaml is invalid. Please fix your yaml.",
            "Press enter to close.Exception ignored in atexit callback: <built-in function input>",
            "EOFError: EOF when reading a line",
        ]
        assert _player_file_errors(lines) == {
            "Alice.yaml": "KeyError: 'Could not find option \"banana\" for \"PlayerSprite\"'\n"
                          "Which caused: Options.OptionError: Error generating option player_sprite in APQuest",
            # nothing listed under it: the line naming the file is all there is
            "Bob-2.yaml": "2. File /data/jobs/3/Players/Bob-2.yaml is invalid. Please fix your yaml.",
        }

    def test_failures_of_the_whole_multiworld_name_no_file(self):
        assert _player_file_errors(["Exception: Game appears as unbeatable. Aborting."]) == {}


class TestYamlForm:
    """The options form's reading and writing, with a made-up game described the way ap_helper.py describes one."""
    SCHEMA = {"game": "G", "presets": {}, "items": ["Sword", "Shield"], "item_groups": ["Weapons"], "locations": [],
              "location_groups": [], "groups": [{"name": "Game Options", "collapsed": False, "options": [
                  {"key": "mode", "label": "Mode", "type": "choice", "choices": [["easy", "Easy", 0], ["hard", "Hard", 1]],
                   "aliases": {"on": "hard"}, "random": True, "default": "easy"},
                  {"key": "boss", "label": "Boss", "type": "text_choice", "choices": [["none", "None", 0]], "aliases": {},
                   "random": True, "default": "none"},
                  {"key": "size", "label": "Size", "type": "range", "min": 1, "max": 10, "named": [["unlimited", -1]],
                   "random": True, "default": 5},
                  {"key": "fast", "label": "Fast", "type": "toggle", "random": True, "default": False},
                  {"key": "seed", "label": "Seed", "type": "text", "random": False, "default": ""},
                  {"key": "traps", "label": "Traps", "type": "counter", "keys": ["Bee", "Ice"], "min": 0, "max": None,
                   "random": False, "default": {}},
                  {"key": "local_items", "label": "Local Items", "type": "set", "names": "items", "groups": True,
                   "random": False, "default": []},
              ]}]}

    def read(self, key, value):
        doc = {"name": "P", "game": "G", "G": {key: value}}
        return yamlform.form_state(doc, "G", self.SCHEMA)["options"][key]

    @pytest.mark.parametrize("key,raw,expected", [
        ("mode", {"easy": 0, "hard": 50}, {"value": "hard"}),  # the one value the weights can roll
        ("mode", {"easy": 1, "hard": 1}, {"kept": "{easy: 1, hard: 1}"}),
        ("mode", "on", {"value": "hard"}),  # an alias
        ("mode", 1, {"value": "hard"}),  # the choice's number
        ("mode", "Random", {"random": True}),
        ("mode", "medium", {"kept": "medium"}),
        ("boss", "Moldorm", {"value": "Moldorm"}),  # a text of one's own
        ("size", "high", {"value": 10}),
        ("size", "unlimited", {"value": -1}),
        ("size", "7", {"value": 7}),
        ("size", 11, {"kept": "11"}),
        ("size", "random-range-2-4", {"kept": "random-range-2-4"}),
        ("fast", "yes", {"value": True}),
        ("fast", {"false": 50, "true": 0}, {"value": False}),
        ("seed", "random", {"value": "random"}),  # for text, just a word
        ("traps", {"Bee": 2}, {"value": {"Bee": 2}}),
        ("traps", {"Wasp": 1}, {"kept": "{Wasp: 1}"}),
        ("local_items", ["Weapons", "Shield"], {"value": ["Weapons", "Shield"]}),
        ("local_items", ["Bow"], {"kept": "[Bow]"}),
    ])
    def test_reading(self, key, raw, expected):
        assert self.read(key, raw) == expected

    def test_writing_keeps_the_rest_of_the_file(self):
        text = "# a comment\nname: Old\ngame: G\nrequires: {version: 0.6.0}\nG:\n  mode: {easy: 1, hard: 1}\n  extra: 3\n"
        written = yamlform.write(text, "G", self.SCHEMA, name="New", description="d",
                                 values={"size": -1, "local_items": ["Sword", "Sword"], "traps": {"Bee": 0}})
        assert written == ("name: New\ndescription: d\ngame: G\nrequires:\n  version: 0.6.0\nG:\n"
                           "  mode:\n    easy: 1\n    hard: 1\n  extra: 3\n  size: -1\n  local_items:\n  - Sword\n"
                           "  traps:\n    Bee: 0\n")

    @pytest.mark.parametrize("values", [{"mode": "medium"}, {"mode": ["easy"]}, {"size": 0}, {"fast": "yes"},
                                        {"seed": "random", "local_items": ["Bow"]}, {"traps": {"Bee": -1}},
                                        {"unknown": 1}])
    def test_values_the_options_do_not_take_are_refused(self, values):
        with pytest.raises(yamlform.FormError):
            yamlform.write(None, "G", self.SCHEMA, name="P", description="", values=values)

    def test_files_the_form_cannot_edit(self):
        for text in ("name: A\ngame: G\n---\nname: B\ngame: G\n", "game: {G: 1, H: 1}\n", "- a list\n", "name: {A: 1, B: 1}\ngame: G\n"):
            with pytest.raises(yamlform.NoFormError):
                yamlform.single_player(text)
