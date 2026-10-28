from dbt_enhance.actions import search_actions


def test_action_search_is_case_insensitive_and_fixed() -> None:
    assert [action.id for action in search_actions("SQL")] == ["toggle_sql", "copy_sql"]


def test_discoverable_actions_do_not_duplicate_commands():
    labels = [action.label for action in search_actions("")]
    assert len(labels) == len(set(labels))
