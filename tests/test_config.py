import pytest

from solidworks_mcp.config import (
    ENV_ALLOW_MACRO,
    ENV_CONFIG,
    ENV_READ_ONLY,
    Config,
    ConfigError,
    load_config,
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No configuration file is reachable unless a test creates one."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv(ENV_CONFIG, raising=False)
    monkeypatch.delenv(ENV_ALLOW_MACRO, raising=False)
    monkeypatch.delenv(ENV_READ_ONLY, raising=False)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_file():
    cfg = load_config()
    assert cfg == Config()
    assert cfg.export_formats == ["step"]
    assert cfg.allow_macro is False
    assert cfg.auto_launch is True


def test_explicit_file(tmp_path):
    file = write(tmp_path / "my.toml", """
[solidworks]
auto_launch = false
command_timeout_s = 30
[export]
formats = ["STEP", ".stl"]
directory = "../{format}"
[macro]
enabled = true
""")
    cfg = load_config(file)
    assert cfg.auto_launch is False
    assert cfg.command_timeout_s == 30
    assert cfg.export_formats == ["step", "stl"]
    assert cfg.export_directory == "../{format}"
    assert cfg.allow_macro is True
    assert cfg.source == str(file)


def test_lookup_order(tmp_path, monkeypatch):
    write(tmp_path / "appdata" / "solidworks-mcp" / "config.toml", '[export]\nformats = ["pdf"]\n')
    assert load_config().export_formats == ["pdf"]

    write(tmp_path / "solidworks-mcp.toml", '[export]\nformats = ["stl"]\n')
    assert load_config().export_formats == ["stl"]

    env_file = write(tmp_path / "env.toml", '[export]\nformats = ["iges"]\n')
    monkeypatch.setenv(ENV_CONFIG, str(env_file))
    assert load_config().export_formats == ["iges"]

    explicit = write(tmp_path / "explicit.toml", '[export]\nformats = ["x_t"]\n')
    assert load_config(explicit).export_formats == ["x_t"]


def test_env_overrides_macro(tmp_path, monkeypatch):
    file = write(tmp_path / "my.toml", "[macro]\nenabled = true\n")
    monkeypatch.setenv(ENV_ALLOW_MACRO, "0")
    assert load_config(file).allow_macro is False
    monkeypatch.setenv(ENV_ALLOW_MACRO, "1")
    assert load_config().allow_macro is True


@pytest.mark.parametrize("text", [
    "[nope]\nx = 1\n",                          # unknown section
    "[export]\nformat = ['step']\n",            # unknown key
    "[export]\nformats = 'step'\n",             # wrong type
    "[export]\nformats = ['dwg']\n",            # unsupported format
    "[solidworks]\nauto_launch = 1\n",          # int where a bool is expected
    "[solidworks]\ncommand_timeout_s = true\n", # bool where a number is expected
    "[solidworks]\ncommand_timeout_s = 0\n",    # non-positive timeout
    "[export\n",                                # invalid TOML
])
def test_invalid_files_are_rejected(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path / "bad.toml", text))


def test_missing_explicit_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")


def test_projects_directory_is_relative_to_the_config_file(tmp_path, monkeypatch):
    file = write(tmp_path / "conf" / "my.toml", '[projects]\ndirectory = "parts"\n')
    monkeypatch.chdir(tmp_path)  # not the folder of the file
    assert load_config(file).projects_directory == str((tmp_path / "conf" / "parts").resolve())

    absolute = tmp_path / "elsewhere"
    file = write(tmp_path / "conf" / "abs.toml", f"[projects]\ndirectory = '{absolute}'\n")
    assert load_config(file).projects_directory == str(absolute.resolve())
    assert load_config().projects_directory == ""


def test_safety_read_only(tmp_path, monkeypatch):
    file = write(tmp_path / "safety.toml", "[safety]\nread_only = true\n")
    cfg = load_config(file)
    assert cfg.read_only is True

    # Environment variable override
    monkeypatch.setenv(ENV_READ_ONLY, "0")
    assert load_config(file).read_only is False
    monkeypatch.setenv(ENV_READ_ONLY, "true")
    assert load_config().read_only is True

