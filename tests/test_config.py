"""Settings come from the environment; a comment that rode along on a value must not count."""

from dse_alerts.config import Settings


def _settings(**env):
    return Settings(_env_file=None, **env)


def test_inline_comments_are_not_values():
    s = _settings(smtp_user="   # e.g. yourname@gmail.com", smtp_password='# Gmail "App password" (not your login password)',
                  alert_from="# usually the same as SMTP_USER", dse_duckdb_threads="0                # 0 = all cores")
    assert s.smtp_user == "" and s.smtp_password == "" and s.alert_from == ""
    assert not s.email_enabled                       # the settings page says "not configured", nothing tries to log in
    assert s.dse_duckdb_threads == 0


def test_a_trailing_note_is_cut_off_but_the_value_stays():
    s = _settings(smtp_user="me@gmail.com   # mine", smtp_password="abcd efgh ijkl mnop", app_public_url="https://x.example  # public")
    assert s.smtp_user == "me@gmail.com" and s.email_enabled
    assert s.base_url == "https://x.example"


def test_example_env_has_no_notes_beside_values():
    from pathlib import Path
    for line in Path(".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            assert " #" not in line, line
