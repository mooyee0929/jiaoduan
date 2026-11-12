from __future__ import annotations

from jiaoduan.cli import _parser, tabs_lost


def test_tabs_lost_keeps_real_web_tabs_only() -> None:
    listing = "\n".join([
        'web:1 (kyndryl) "Apply" https://kyndryl.wd5.myworkdayjobs.com/apply',
        'web:2 "New Tab" about:blank',
        'mac:Finder "Finder" com.apple.finder',
        'web:3 (salesforce) "My Information" https://salesforce.wd12.myworkdayjobs.com/x',
    ])
    assert [line.split(" ", 1)[0] for line in tabs_lost(listing)] == ["web:1", "web:3"]


def test_tabs_lost_on_empty_daemon() -> None:
    assert tabs_lost("no surfaces open") == []


def test_login_force_flag() -> None:
    assert _parser().parse_args(["login"]).force is False
    assert _parser().parse_args(["login", "--force", "https://x.test"]).force is True
