from types import SimpleNamespace

import pytest
from discord.ext import commands

from job_bot.bot import ensure_user_has_admin_privileges, user_has_admin_privileges


def _user_with_admin_flag(is_admin: bool):
    return SimpleNamespace(guild_permissions=SimpleNamespace(administrator=is_admin))


def test_user_has_admin_privileges_returns_true_for_administrator() -> None:
    user = _user_with_admin_flag(True)

    assert user_has_admin_privileges(user) is True


def test_ensure_user_has_admin_privileges_raises_for_unauthorized_user() -> None:
    user = _user_with_admin_flag(False)

    with pytest.raises(commands.CheckFailure, match="This command requires Discord administrator privileges."):
        ensure_user_has_admin_privileges(user)
