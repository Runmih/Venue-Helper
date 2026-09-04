# Venue Helper

A Discord bot for recurring venue/event staff presence checks.

The bot is deliberately **setup-driven**. It does not contain a hardcoded venue name, weekday, opening time, shift layout, reminder schedule, staff role, or announcement channel.

## V0.1 features

- One `/setup` command starts a step-by-step DM conversation.
- Each Discord server gets its own configuration.
- Presence-check and announcement channels may be in other servers the bot is also in.
- Configurable timezone, event weekday/time, and shifts.
- Configurable staff groups/roles.
- Any number of scheduled presence checks (up to 10 in this first version).
- Check audiences can be:
  - everyone
  - unanswered + unsure
  - unanswered only
  - unsure only
- Staff can choose one or multiple shifts, **Unsure**, or **Can't make it**.
- Presence messages keep live counts per staff group.
- Moderators can request a current review at any time.
- An optional scheduled moderator review can be configured.
- The bot does **not** decide whether an event should open. Moderators decide whether to announce.
- Optional announcement templates with dynamic placeholders and weekly values.
- Staff profile registration through a button + DM conversation.
- Discord forum/thread profile links are unarchived when possible before announcement.
- Website/profile embeds are suppressed in announcements.
- SQLite persistence survives bot restarts.

## Discord setup

Create a Discord application and bot in the Discord Developer Portal.

Enable these privileged intents for the bot:

- **Server Members Intent**
- **Message Content Intent**

The bot needs Server Members Intent because it must determine who belongs to configured staff roles and who has/has not answered.

Give the bot permissions appropriate to the channels you configure. At minimum it needs to view and send messages. If configured staff roles are not mentionable, the bot also needs permission to mention roles. If it should wake archived forum profile threads, it needs thread-management permission in that server/channel.

Invite it with both the `bot` and `applications.commands` scopes.

## Termux install

```bash
pkg update
pkg install python git

git clone https://github.com/Runmih/Venue-Helper.git
cd Venue-Helper

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set your token for the current Termux session:

```bash
export DISCORD_TOKEN='YOUR_TOKEN_HERE'
```

Optional during development, set your test server ID so `/setup` syncs there immediately:

```bash
export DEV_GUILD_ID='YOUR_SERVER_ID'
```

Run:

```bash
python bot.py
```

For a more permanent local setup, store those exports in a private shell file that is **not** committed to GitHub.

## First use

In the server that should own the configuration, run:

```text
/setup
```

The bot sends you a DM and asks one question at a time. Type `cancel` at any point to abandon setup.

It asks for:

1. Event name
2. Timezone
3. Event weekday/start/end
4. Shift configuration
5. Presence-check server/channel
6. Staff groups and role IDs
7. Moderator roles
8. Automatic presence-check schedules
9. Optional moderator-review schedule
10. Moderator decision channel
11. Optional announcement server/channel
12. Announcement template
13. Final confirmation

Nothing is saved until the final confirmation.

## Announcement template

Static text is written normally. Only changing values use placeholders.

Built-ins:

```text
{{event_name}}
{{event_start:F}}
{{event_start:t}}
{{event_end:F}}
{{event_end:t}}
{{present_staff}}
{{staff_count}}
```

Weekly values use:

```text
{{weekly:Syncshell ID}}
{{weekly:Password}}
```

When a moderator chooses **Announce Event**, Venue Helper DMs them and asks for each weekly value one at a time, shows a preview, and requires `POST` before anything is published.

## Notes / current limitations

This is the first functional version, not the finished bot.

- Weekly recurrence is currently the only recurrence type.
- A setup may contain up to 20 shifts and 10 automated checks.
- Setup is DM text based. Discord's channel/role picker components cannot be used cleanly across arbitrary servers from a DM, so the first version accepts IDs/role mentions and validates them.
- If the bot was offline when several scheduled checks were due, it sends only the most recent due check instead of spamming every missed reminder.
- Announcement messages must currently fit Discord's 2000-character message limit.
- Truly moderator-locked forum threads are not force-unlocked. Auto-archived threads are reopened when permissions allow it.
