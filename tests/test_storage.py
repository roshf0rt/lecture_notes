from notes_bot.storage import BotState, Options


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def _state(tmp_path, **kwargs):
    clock = Clock()
    return BotState(tmp_path / "bot.sqlite3", Options(model="m"), clock=clock, **kwargs), clock


def test_options_have_defaults_and_keep_types(tmp_path):
    state, _ = _state(tmp_path)
    assert state.options() == Options(model="m")
    state.set_option("premium_emoji", False)
    state.set_option("daily_limit", 5)
    state.set_option("model", "gemini-x")
    options = state.options()
    assert options.premium_emoji is False and options.daily_limit == 5 and options.model == "gemini-x"


def test_env_allow_list_closes_the_bot_once(tmp_path):
    state, _ = _state(tmp_path, allowed_ids=frozenset({7}))
    assert state.options().access == "whitelist"
    assert state.is_whitelisted(7)
    state.set_option("access", "open")
    state.close()
    again, _ = _state(tmp_path, allowed_ids=frozenset({7}))
    assert again.options().access == "open"  # the admin's choice wins after a restart


def test_users_are_recorded_and_counted(tmp_path):
    state, clock = _state(tmp_path)
    assert state.touch(1, "anna", "Анна") is True
    assert state.touch(1, "anna2", "Анна") is False
    state.log_job(1, "audio", True, 12.0)
    state.log_job(1, "text", False, 1.0, "boom")
    record = state.user(1)
    assert record.username == "anna2" and record.notes == 1 and record.label == "@anna2"

    stats = state.stats()
    assert stats.users == 1 and stats.notes_total == 1 and stats.failures_day == 1
    assert stats.audio_share == 1.0
    assert state.recent_errors()[0][2] == "boom"

    clock.now += 2 * 86_400
    assert state.notes_last_day(1) == 0
    assert state.stats().active_day == 0


def test_ban_whitelist_and_admins(tmp_path):
    state, _ = _state(tmp_path, admin_ids=frozenset({100}))
    state.set_banned(5, True)
    assert state.is_banned(5) and state.recipients() == []
    state.set_banned(5, False)
    assert state.recipients() == [5]

    state.set_whitelisted(6, True)
    assert state.whitelist() == [6]

    state.set_admin(7, True)
    assert state.is_admin(7) and state.is_admin(100)
    assert state.admins() == [7, 100]
    state.set_admin(7, False)
    assert not state.is_admin(7)


def test_topic_bindings_are_per_user_and_replace_on_rebind(tmp_path):
    from notes_bot.storage import BotState, Options

    state = BotState(tmp_path / "bot.sqlite3", Options(model="m"))
    state.bind_topic(5, "Физика", -100, 3)
    state.bind_topic(5, "Физика", -200, 9)  # the same subject bound again, e.g. after the group became a supergroup
    state.bind_topic(6, "Физика", -300, 1)
    assert state.topics(5) == [("Физика", -200, 9)]
    assert state.unbind_topic(5, "Физика") is True
    assert state.unbind_topic(5, "Физика") is False
    assert state.topics(6) == [("Физика", -300, 1)]
