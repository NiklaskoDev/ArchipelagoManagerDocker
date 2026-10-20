"""Pure unit tests: no server, no Archipelago."""
import pytest

from archipelago_manager import settings, yamlinfo
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
