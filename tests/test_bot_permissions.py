from types import SimpleNamespace

import pytest
from discord.ext import commands

from job_bot.bot import ensure_user_has_allowed_role, user_has_any_allowed_role


def _user_with_roles(*role_ids: int):
    return SimpleNamespace(roles=[SimpleNamespace(id=role_id) for role_id in role_ids])


def test_user_has_any_allowed_role_returns_true_for_matching_role() -> None:
    user = _user_with_roles(1, 2, 3)

    assert user_has_any_allowed_role(user, {3, 4}) is True


def test_ensure_user_has_allowed_role_raises_for_unauthorized_user() -> None:
    user = _user_with_roles(10, 20)

    with pytest.raises(commands.CheckFailure, match="This command requires one of the allowed roles."):
        ensure_user_has_allowed_role(user, {30, 40})
