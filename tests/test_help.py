from job_bot.bot import build_help_embed, build_help_page_definitions


def _flatten_help_entries() -> str:
    return "\n".join(
        entry
        for _, sections in build_help_page_definitions()
        for _, entries in sections
        for entry in entries
    )


def test_help_pages_include_expected_grouped_sections() -> None:
    pages = build_help_page_definitions()

    assert [page_title for page_title, _ in pages] == ["Browse", "Process Tracking", "Admin"]
    assert [section_title for section_title, _ in pages[0][1]] == [
        "Browse",
        "Companies",
        "Requests",
    ]
    assert [section_title for section_title, _ in pages[1][1]] == [
        "Process Tracking",
        "Process Notes And Peers",
        "Aliases / Compatibility",
    ]


def test_help_pages_include_current_process_tracking_commands() -> None:
    flattened = _flatten_help_entries()

    assert "`!process` - dashboard for your active tracked roles" in flattened
    assert "`!process set <company> | <role> | <season> | <status>` - track a role scope" in flattened
    assert (
        "`!process set <company> | <role> | <track> | <season> | <status>` - set track explicitly"
        in flattened
    )
    assert "`!process company <company>` - list role scopes for one company" in flattened
    assert "`!process view <scope_id>` - view one role scope" in flattened
    assert "`!process status <scope_id> <status>` - update one role scope" in flattened
    assert "`!process peers <scope_id>` - find peers for one role scope" in flattened
    assert "`!process notes <scope_id>` - read visible notes" in flattened
    assert "`!process note <scope_id> | <note>` - save a note" in flattened
    assert "`!process report <note_id> [reason]` - report leaked/private content" in flattened
    assert "`!process deletenote <note_id> [reason]` - delete your own note" in flattened
    assert "`!process open|matched|private` - change process reply and peer discovery visibility" in flattened


def test_help_pages_mark_admin_commands_clearly() -> None:
    flattened = _flatten_help_entries()

    assert "`!runjobs` - trigger a manual discovery run [admin only]" in flattened
    assert "`!stats` - show stored job stats [admin only]" in flattened
    assert "`!sources` - list configured sources [admin only]" in flattened
    assert "`!dedupecheck <url or text>` - test duplicate detection [admin only]" in flattened
    assert "`!season [value]` - show season filter configuration [admin only]" in flattened
    assert "`!summary [daily|weekly]` - show recent summary stats [admin only]" in flattened
    assert "`!showchannels` - show effective posting channel routing [admin only]" in flattened
    assert "`!setinternchannel <channel_id>` - update the internship posting channel [admin only]" in flattened
    assert (
        "`!setnewgradchannel <channel_id|off>` - update or disable the new-grad posting channel [admin only]"
        in flattened
    )
    assert "`!companyrequests [pending|approved|denied|all]` - request queue [admin only]" in flattened
    assert "`!approvecompany <request_id>` - approve a company request [admin only]" in flattened
    assert "`!denycompany <request_id> [reason]` - deny a company request [admin only]" in flattened
    assert "`!firecompany <company|id>` - mark a watchlist company as hot [admin only]" in flattened
    assert "`!synccompanies` - reimport the company watchlist seed [admin only]" in flattened


def test_help_embed_uses_shared_grouped_pages() -> None:
    browse_embed = build_help_embed(0)
    process_embed = build_help_embed(1)
    admin_embed = build_help_embed(2)

    assert browse_embed.title == "Help • Browse"
    assert process_embed.title == "Help • Process Tracking"
    assert admin_embed.title == "Help • Admin"
    assert "Process commands stay in chat when your visibility is `open`; otherwise they reply in DM." in browse_embed.description
    assert "Admin commands require Discord administrator privileges." in browse_embed.description
    assert [field.name for field in browse_embed.fields] == ["Browse", "Companies", "Requests"]
    assert [field.name for field in process_embed.fields] == [
        "Process Tracking",
        "Process Notes And Peers",
        "Aliases / Compatibility",
    ]
    assert [field.name for field in admin_embed.fields] == ["Admin"]
