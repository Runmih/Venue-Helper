import asyncio
import logging
import os
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks

from db import Database


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("venue-helper")

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_LOOKUP = {name.lower(): i for i, name in enumerate(DAY_NAMES)}
DAY_LOOKUP.update({name[:3].lower(): i for i, name in enumerate(DAY_NAMES)})
SNOWFLAKE_RE = re.compile(r"\d{15,22}")
WEEKLY_VAR_RE = re.compile(r"\{\{weekly:([^}]+)}}", re.IGNORECASE)
TOKEN_RE = re.compile(r"\{\{([a-zA-Z_]+)(?::([tTdDfFR]))?}}")
DISCORD_CHANNEL_RE = re.compile(r"discord(?:app)?\.com/channels/\d+/(\d+)")


def parse_time(value: str) -> str | None:
    try:
        return datetime.strptime(value.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def parse_day(value: str) -> int | None:
    return DAY_LOOKUP.get(value.strip().lower())


def parse_ids(value: str) -> list[int]:
    return [int(x) for x in SNOWFLAKE_RE.findall(value)]


def yes(value: str) -> bool | None:
    v = value.strip().lower()
    if v in {"yes", "y", "yeah", "yep"}:
        return True
    if v in {"no", "n", "nope"}:
        return False
    return None


def discord_ts(dt: datetime, style: str = "F") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def local_event_dt(config: dict, event_date: date, hhmm: str, *, rollover_after: str | None = None) -> datetime:
    tz = ZoneInfo(config["timezone"])
    h, m = map(int, hhmm.split(":"))
    result = datetime.combine(event_date, time(h, m), tzinfo=tz)
    if rollover_after and hhmm <= rollover_after:
        result += timedelta(days=1)
    return result


def next_event_start(config: dict, now: datetime) -> datetime:
    event_weekday = int(config["event_weekday"])
    days_ahead = (event_weekday - now.weekday()) % 7
    candidate_date = now.date() + timedelta(days=days_ahead)
    candidate = local_event_dt(config, candidate_date, config["event_start"])
    if candidate <= now:
        candidate_date += timedelta(days=7)
        candidate = local_event_dt(config, candidate_date, config["event_start"])
    return candidate


def schedule_before_event(config: dict, event_start: datetime, weekday: int, hhmm: str) -> datetime:
    event_date = event_start.date()
    days_back = (event_start.weekday() - weekday) % 7
    target_date = event_date - timedelta(days=days_back)
    h, m = map(int, hhmm.split(":"))
    target = datetime.combine(target_date, time(h, m), tzinfo=event_start.tzinfo)
    if target >= event_start:
        target -= timedelta(days=7)
    return target


class ConversationCancelled(Exception):
    pass


class VenueHelper(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database(os.getenv("VENUE_HELPER_DB", "venue_helper.db"))

    async def setup_hook(self) -> None:
        self.add_view(PresenceView(self))
        self.add_view(DecisionView(self))

        dev_guild = os.getenv("DEV_GUILD_ID")
        if dev_guild:
            guild = discord.Object(id=int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to DEV_GUILD_ID=%s", dev_guild)
        else:
            await self.tree.sync()
            log.info("Synced global commands")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        if not self.scheduler.is_running():
            self.scheduler.start()

    async def ask(self, user: discord.User | discord.Member, dm: discord.DMChannel, prompt: str) -> str:
        await dm.send(prompt)

        def check(message: discord.Message) -> bool:
            return message.author.id == user.id and message.channel.id == dm.id

        while True:
            try:
                msg = await self.wait_for("message", check=check, timeout=900)
            except asyncio.TimeoutError as exc:
                raise ConversationCancelled("Setup timed out after 15 minutes.") from exc
            text = msg.content.strip()
            if text.lower() == "cancel":
                raise ConversationCancelled("Cancelled.")
            if text:
                return text
            await dm.send("I didn't receive any text. Please try again, or type `cancel`.")

    async def ask_yes_no(self, user, dm, prompt: str) -> bool:
        while True:
            value = yes(await self.ask(user, dm, prompt + "\nReply `yes` or `no`."))
            if value is not None:
                return value
            await dm.send("Please answer `yes` or `no`.")

    async def ask_day(self, user, dm, prompt: str) -> int:
        while True:
            raw = await self.ask(user, dm, prompt + "\nExample: `Monday`.")
            parsed = parse_day(raw)
            if parsed is not None:
                return parsed
            await dm.send("I couldn't recognize that weekday. Try `Monday`, `Tue`, etc.")

    async def ask_time(self, user, dm, prompt: str) -> str:
        while True:
            raw = await self.ask(user, dm, prompt + "\nUse 24-hour time, for example `20:00`.")
            parsed = parse_time(raw)
            if parsed:
                return parsed
            await dm.send("That isn't a valid `HH:MM` time.")

    async def ask_int(self, user, dm, prompt: str, minimum: int, maximum: int) -> int:
        while True:
            raw = await self.ask(user, dm, prompt)
            try:
                n = int(raw)
            except ValueError:
                n = -1
            if minimum <= n <= maximum:
                return n
            await dm.send(f"Please enter a number from {minimum} to {maximum}.")

    async def resolve_target_channel(
        self,
        user: discord.User | discord.Member,
        dm: discord.DMChannel,
        home_guild: discord.Guild,
        purpose: str,
        allow_same: tuple[int, int] | None = None,
    ) -> tuple[int, int]:
        if allow_same:
            raw = await self.ask(
                user,
                dm,
                f"Where should {purpose} go?\nType `same` to use the previous channel, or send a server ID.",
            )
            if raw.lower() == "same":
                return allow_same
            server_ids = parse_ids(raw)
        else:
            raw = await self.ask(
                user,
                dm,
                f"Which server should I use for {purpose}?\nSend the server ID, or type `here` for **{home_guild.name}**.",
            )
            server_ids = [home_guild.id] if raw.lower() == "here" else parse_ids(raw)

        while not server_ids:
            raw = await self.ask(user, dm, "I need a server ID. Send it, or type `here`.")
            server_ids = [home_guild.id] if raw.lower() == "here" else parse_ids(raw)

        guild_id = server_ids[0]
        guild = self.get_guild(guild_id)
        if guild is None:
            await dm.send("I'm not in that server. Add me there first, then send another server ID.")
            return await self.resolve_target_channel(user, dm, home_guild, purpose, allow_same)

        try:
            member = guild.get_member(user.id) or await guild.fetch_member(user.id)
        except discord.HTTPException:
            member = None
        if member is None or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            await dm.send("You need `Manage Server` or Administrator in the destination server too.")
            return await self.resolve_target_channel(user, dm, home_guild, purpose, allow_same)

        while True:
            raw_channel = await self.ask(user, dm, f"Send the channel ID for {purpose} in **{guild.name}**.")
            ids = parse_ids(raw_channel)
            if not ids:
                await dm.send("I couldn't find a channel ID in that message.")
                continue
            channel = guild.get_channel(ids[0])
            if channel is None:
                try:
                    channel = await guild.fetch_channel(ids[0])
                except discord.HTTPException:
                    channel = None
            if channel is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await dm.send("I couldn't find a text channel/thread with that ID.")
                continue
            me = guild.me
            if me and isinstance(channel, discord.abc.GuildChannel):
                perms = channel.permissions_for(me)
                if not (perms.view_channel and perms.send_messages):
                    await dm.send("I can see that channel, but I don't have permission to post there.")
                    continue
            await dm.send(f"Found **#{channel.name}** in **{guild.name}**. ✅")
            return guild.id, channel.id

    async def run_setup(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Run `/setup` inside the server you want to configure.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not (
            interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild
        ):
            await interaction.response.send_message("You need `Manage Server` or Administrator to run setup.", ephemeral=True)
            return

        await interaction.response.send_message("I'll continue setup with you in DMs. Type `cancel` there at any time.", ephemeral=True)
        try:
            dm = interaction.user.dm_channel or await interaction.user.create_dm()
            await dm.send(
                f"## Venue Helper setup\nConfiguring **{interaction.guild.name}**.\n"
                "I'll ask one thing at a time. Nothing is saved until the final confirmation."
            )
        except discord.Forbidden:
            await interaction.followup.send("I couldn't DM you. Please allow DMs from server members and run `/setup` again.", ephemeral=True)
            return

        user = interaction.user
        home_guild = interaction.guild

        try:
            event_name = await self.ask(user, dm, "What should I call the recurring event?")

            while True:
                timezone_name = await self.ask(
                    user,
                    dm,
                    "What timezone should this event use?\nExample: `Europe/Zurich` or `Europe/Berlin`.",
                )
                try:
                    ZoneInfo(timezone_name)
                    break
                except ZoneInfoNotFoundError:
                    await dm.send("I don't recognize that timezone. Use an IANA timezone such as `Europe/Zurich`.")

            event_weekday = await self.ask_day(user, dm, "Which weekday does the event normally happen?")
            event_start = await self.ask_time(user, dm, "What time does the event start?")
            event_end = await self.ask_time(user, dm, "What time does the event end?")

            uses_shifts = await self.ask_yes_no(user, dm, "Do participants choose between separate shifts?")
            shifts: list[dict] = []
            if uses_shifts:
                count = await self.ask_int(user, dm, "How many shifts are there? (1-20)", 1, 20)
                for i in range(count):
                    label = await self.ask(user, dm, f"What should shift {i + 1} be called?")
                    start = await self.ask_time(user, dm, f"When does **{label}** start?")
                    end = await self.ask_time(user, dm, f"When does **{label}** end?")
                    shifts.append({"label": label, "start": start, "end": end})
            else:
                shifts.append({"label": "Available", "start": event_start, "end": event_end})

            check_guild_id, check_channel_id = await self.resolve_target_channel(
                user, dm, home_guild, "presence checks"
            )
            check_guild = self.get_guild(check_guild_id)
            assert check_guild is not None

            group_count = await self.ask_int(user, dm, "How many staff groups/roles should be included? (1-10)", 1, 10)
            staff_groups = []
            for i in range(group_count):
                label = await self.ask(user, dm, f"Display name for staff group {i + 1}?\nExample: `SFW`, `NSFW`, `Hosts`.")
                while True:
                    role_raw = await self.ask(user, dm, f"Mention the Discord role for **{label}**, or send its role ID.")
                    ids = parse_ids(role_raw)
                    role = check_guild.get_role(ids[0]) if ids else None
                    if role:
                        staff_groups.append({"label": label, "role_id": role.id})
                        break
                    await dm.send(f"I couldn't find that role in **{check_guild.name}**.")

            moderator_roles: list[int] = []
            mod_raw = await self.ask(
                user,
                dm,
                "Which roles may make moderator decisions?\nMention one or more roles from the setup server, or type `none`. Server administrators always qualify.",
            )
            if mod_raw.lower() != "none":
                for role_id in parse_ids(mod_raw):
                    if home_guild.get_role(role_id):
                        moderator_roles.append(role_id)
                await dm.send(f"Stored {len(moderator_roles)} moderator role(s).")

            check_count = await self.ask_int(user, dm, "How many automatic presence checks/reminders should happen before each event? (1-10)", 1, 10)
            checks = []
            for i in range(check_count):
                day = await self.ask_day(user, dm, f"Which weekday should presence check {i + 1} run?")
                at = await self.ask_time(user, dm, f"What time should presence check {i + 1} run?")
                while True:
                    audience = (await self.ask(
                        user,
                        dm,
                        "Who should this check ping?\n"
                        "`everyone` = all configured staff roles\n"
                        "`pending` = people with no response + Unsure\n"
                        "`unanswered` = only people with no response\n"
                        "`unsure` = only people who selected Unsure",
                    )).lower()
                    if audience in {"everyone", "pending", "unanswered", "unsure"}:
                        break
                    await dm.send("Choose `everyone`, `pending`, `unanswered`, or `unsure`.")
                checks.append({"weekday": day, "time": at, "audience": audience})

            scheduled_review = await self.ask_yes_no(
                user,
                dm,
                "Should I automatically post a moderator decision summary at a scheduled time?\n"
                "If you choose no, moderators can still press **Moderator Review** on any presence-check message.",
            )
            review = None
            if scheduled_review:
                review_day = await self.ask_day(user, dm, "Which weekday should the moderator review happen?")
                review_time = await self.ask_time(user, dm, "What time should the moderator review happen?")
                review = {"weekday": review_day, "time": review_time}

            moderation_guild_id, moderation_channel_id = await self.resolve_target_channel(
                user,
                dm,
                home_guild,
                "moderator decisions",
                allow_same=(check_guild_id, check_channel_id),
            )

            announcement_enabled = await self.ask_yes_no(user, dm, "Should this event support posting an announcement after moderator approval?")
            announcement = None
            if announcement_enabled:
                ann_guild_id, ann_channel_id = await self.resolve_target_channel(
                    user, dm, home_guild, "announcements"
                )
                await dm.send(
                    "Now send the announcement template as **one Discord message**.\n\n"
                    "Static text is left exactly as written. Available placeholders:\n"
                    "`{{event_name}}`\n"
                    "`{{event_start:F}}` / `{{event_start:t}}`\n"
                    "`{{event_end:F}}` / `{{event_end:t}}`\n"
                    "`{{present_staff}}`\n"
                    "`{{staff_count}}`\n"
                    "`{{weekly:Anything You Want}}` for values moderators enter each week."
                )
                template = await self.ask(user, dm, "Send the template now.")
                weekly_vars = list(dict.fromkeys(x.strip() for x in WEEKLY_VAR_RE.findall(template)))
                if weekly_vars:
                    await dm.send("Weekly values detected: " + ", ".join(f"`{x}`" for x in weekly_vars))
                announcement = {
                    "guild_id": ann_guild_id,
                    "channel_id": ann_channel_id,
                    "template": template,
                }

            config = {
                "event_name": event_name,
                "timezone": timezone_name,
                "event_weekday": event_weekday,
                "event_start": event_start,
                "event_end": event_end,
                "shifts": shifts,
                "check_guild_id": check_guild_id,
                "check_channel_id": check_channel_id,
                "staff_groups": staff_groups,
                "moderator_roles": moderator_roles,
                "checks": checks,
                "review": review,
                "moderation_guild_id": moderation_guild_id,
                "moderation_channel_id": moderation_channel_id,
                "announcement": announcement,
                "created_by": user.id,
            }

            summary = [
                "## Setup summary",
                f"**Event:** {event_name}",
                f"**Timezone:** `{timezone_name}`",
                f"**Event:** {DAY_NAMES[event_weekday]} {event_start}–{event_end}",
                f"**Shifts:** {len(shifts)}",
                f"**Staff groups:** {', '.join(g['label'] for g in staff_groups)}",
                f"**Automatic checks:** {len(checks)}",
                f"**Scheduled moderator review:** {'Yes' if review else 'No'}",
                f"**Announcements:** {'Yes' if announcement else 'No'}",
                "\nSave this configuration?",
            ]
            if await self.ask_yes_no(user, dm, "\n".join(summary)):
                self.db.save_config(home_guild.id, config)
                await dm.send("Saved. ✅ Venue Helper will use this configuration from now on.")
            else:
                await dm.send("Nothing was saved.")
        except ConversationCancelled as exc:
            await dm.send(str(exc))
        except Exception:
            log.exception("Setup failed")
            await dm.send("Setup hit an unexpected error. Check the bot console for the traceback.")

    async def is_staff(self, config: dict, user_id: int) -> bool:
        guild = self.get_guild(int(config["check_guild_id"]))
        if not guild:
            return False
        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.HTTPException:
            return False
        role_ids = {r.id for r in member.roles}
        return any(int(g["role_id"]) in role_ids for g in config["staff_groups"])

    async def is_moderator(self, home_guild_id: int, config: dict, user_id: int) -> bool:
        guild = self.get_guild(home_guild_id)
        if guild is None:
            return False
        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.HTTPException:
            return False
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        role_ids = {r.id for r in member.roles}
        return any(int(rid) in role_ids for rid in config.get("moderator_roles", []))

    async def get_staff_groups(self, config: dict) -> list[tuple[dict, list[discord.Member]]]:
        guild = self.get_guild(int(config["check_guild_id"]))
        if guild is None:
            return []
        try:
            if not guild.chunked:
                await guild.chunk(cache=True)
        except discord.HTTPException:
            pass
        result = []
        for group in config["staff_groups"]:
            role = guild.get_role(int(group["role_id"]))
            result.append((group, list(role.members) if role else []))
        return result

    def event_datetimes(self, config: dict, event_date_str: str) -> tuple[datetime, datetime]:
        event_date = date.fromisoformat(event_date_str)
        start = local_event_dt(config, event_date, config["event_start"])
        end = local_event_dt(config, event_date, config["event_end"], rollover_after=config["event_start"])
        return start, end

    async def build_presence_text(self, home_guild_id: int, config: dict, event_date: str) -> str:
        availability = self.db.get_availability(home_guild_id, event_date)
        start, end = self.event_datetimes(config, event_date)
        lines = [
            f"## {config['event_name']} · Presence Check",
            f"**Event:** {discord_ts(start, 'F')} → {discord_ts(end, 't')}",
            "",
            "Choose every shift you can attend, or select **Unsure** / **Can't make it**.",
            "",
        ]
        for i, shift in enumerate(config["shifts"]):
            s = local_event_dt(config, date.fromisoformat(event_date), shift["start"])
            e = local_event_dt(config, date.fromisoformat(event_date), shift["end"], rollover_after=shift["start"])
            lines.append(f"**{i + 1}. {shift['label']}** · {discord_ts(s, 't')}–{discord_ts(e, 't')}")
        lines.append("")

        groups = await self.get_staff_groups(config)
        for group, members in groups:
            member_ids = {m.id for m in members if not m.bot}
            lines.append(f"### {group['label']}")
            for i, _shift in enumerate(config["shifts"]):
                count = sum(
                    1 for uid in member_ids
                    if availability.get(uid, {}).get("status") == "shifts"
                    and i in availability.get(uid, {}).get("shifts", [])
                )
                lines.append(f"{i + 1}️⃣ **{count} available**")
            unsure = sum(1 for uid in member_ids if availability.get(uid, {}).get("status") == "unsure")
            cant = sum(1 for uid in member_ids if availability.get(uid, {}).get("status") == "cant")
            no_response = sum(1 for uid in member_ids if uid not in availability)
            lines.extend([
                f"🟠 Unsure: **{unsure}**",
                f"🔴 Can't make it: **{cant}**",
                f"⚪ No response: **{no_response}**",
                "",
            ])
        return "\n".join(lines)[:1950]

    async def ping_text(self, home_guild_id: int, config: dict, event_date: str, audience: str) -> str:
        if audience == "everyone":
            return " ".join(f"<@&{g['role_id']}>" for g in config["staff_groups"])

        availability = self.db.get_availability(home_guild_id, event_date)
        groups = await self.get_staff_groups(config)
        members: dict[int, discord.Member] = {}
        for _group, group_members in groups:
            for member in group_members:
                if not member.bot:
                    members[member.id] = member

        selected = []
        for uid in members:
            record = availability.get(uid)
            if audience == "pending" and (record is None or record.get("status") == "unsure"):
                selected.append(uid)
            elif audience == "unanswered" and record is None:
                selected.append(uid)
            elif audience == "unsure" and record and record.get("status") == "unsure":
                selected.append(uid)
        if not selected:
            return "*No one needs a reminder for this check.*"
        text = " ".join(f"<@{uid}>" for uid in selected)
        return text[:1200]

    async def post_presence_check(self, home_guild_id: int, config: dict, event_date: str, check: dict) -> None:
        guild = self.get_guild(int(config["check_guild_id"]))
        channel = guild.get_channel(int(config["check_channel_id"])) if guild else None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.warning("Presence channel missing for guild %s", home_guild_id)
            return
        ping = await self.ping_text(home_guild_id, config, event_date, check["audience"])
        body = await self.build_presence_text(home_guild_id, config, event_date)
        message = await channel.send(
            f"{ping}\n{body}",
            view=PresenceView(self),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
        )
        self.db.index_message(message.id, home_guild_id, event_date, "presence", channel.guild.id, channel.id)

    async def refresh_presence_messages(self, home_guild_id: int, config: dict, event_date: str) -> None:
        body = await self.build_presence_text(home_guild_id, config, event_date)
        for info in self.db.list_messages(home_guild_id, event_date, "presence"):
            guild = self.get_guild(int(info["guild_id"]))
            channel = guild.get_channel(int(info["channel_id"])) if guild else None
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            try:
                message = await channel.fetch_message(int(info["message_id"]))
                await message.edit(content=body, view=PresenceView(self))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue

    async def build_review_text(self, home_guild_id: int, config: dict, event_date: str) -> str:
        body = await self.build_presence_text(home_guild_id, config, event_date)
        return (
            "## Moderator Review\n"
            "Here is the current attendance information. Venue Helper does **not** decide whether the event should proceed.\n\n"
            + body
            + "\n**What do you want to do?**"
        )[:1950]

    async def post_moderator_review(self, home_guild_id: int, config: dict, event_date: str) -> None:
        guild = self.get_guild(int(config["moderation_guild_id"]))
        channel = guild.get_channel(int(config["moderation_channel_id"])) if guild else None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        text = await self.build_review_text(home_guild_id, config, event_date)
        message = await channel.send(text, view=DecisionView(self))
        self.db.index_message(message.id, home_guild_id, event_date, "decision", channel.guild.id, channel.id)
        self.db.set_event_status(home_guild_id, event_date, "awaiting_decision")

    async def profile_flow(self, home_guild_id: int, config: dict, user: discord.User | discord.Member) -> None:
        try:
            dm = user.dm_channel or await user.create_dm()
            current = self.db.get_profile(home_guild_id, user.id)
            intro = "## Staff profile\nI'll ask three short questions. Type `cancel` to stop."
            if current:
                intro += f"\nCurrent name: **{current['display_name']}**\nCurrent link: {current['url']}"
            await dm.send(intro)
            name = await self.ask(user, dm, "What display name should announcements use for you?")
            url = await self.ask(user, dm, "Send your profile link.")
            while True:
                link_type = (await self.ask(user, dm, "What kind of link is it? Reply `discord`, `website`, or `other`.")).lower()
                if link_type in {"discord", "website", "other"}:
                    break
                await dm.send("Choose `discord`, `website`, or `other`.")
            self.db.upsert_profile(home_guild_id, user.id, name, url, link_type)
            await dm.send("Profile saved. ✅")
        except ConversationCancelled:
            try:
                await user.send("Profile setup cancelled.")
            except discord.HTTPException:
                pass
        except discord.Forbidden:
            pass

    async def prepare_discord_profile(self, url: str) -> int | None:
        match = DISCORD_CHANNEL_RE.search(url)
        if not match:
            return None
        channel_id = int(match.group(1))
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                return channel_id
        if isinstance(channel, discord.Thread):
            if channel.locked:
                return channel_id
            if channel.archived:
                try:
                    await channel.edit(archived=False, reason="Venue Helper preparing staff profile for announcement")
                except discord.HTTPException:
                    pass
        return channel_id

    async def format_present_staff(self, home_guild_id: int, config: dict, event_date: str) -> tuple[str, int]:
        availability = self.db.get_availability(home_guild_id, event_date)
        groups = await self.get_staff_groups(config)
        lines = []
        unique_attendees: set[int] = set()
        for group, members in groups:
            attending = [m for m in members if availability.get(m.id, {}).get("status") == "shifts"]
            if not attending:
                continue
            lines.append(f"__{group['label']}__")
            for member in attending:
                unique_attendees.add(member.id)
                profile = self.db.get_profile(home_guild_id, member.id)
                if not profile:
                    lines.append(f"- {member.display_name} (profile not set)")
                    continue
                name = profile["display_name"]
                url = profile["url"]
                if profile["link_type"] == "discord":
                    channel_id = await self.prepare_discord_profile(url)
                    if channel_id:
                        lines.append(f"- <#{channel_id}>")
                    else:
                        lines.append(f"- [{name}](<{url}>)")
                else:
                    lines.append(f"- [{name}](<{url}>)")
            lines.append("")
        return "\n".join(lines).strip() or "*No attending staff have been selected.*", len(unique_attendees)

    async def render_announcement(
        self,
        home_guild_id: int,
        config: dict,
        event_date: str,
        weekly_values: dict[str, str],
    ) -> str:
        announcement = config.get("announcement")
        if not announcement:
            raise ValueError("Announcements are not configured.")
        template = announcement["template"]
        start, end = self.event_datetimes(config, event_date)
        present_staff, staff_count = await self.format_present_staff(home_guild_id, config, event_date)

        for key, value in weekly_values.items():
            template = re.sub(
                r"\{\{weekly:" + re.escape(key) + r"}}",
                lambda _m: value,
                template,
                flags=re.IGNORECASE,
            )

        values = {
            "event_name": config["event_name"],
            "present_staff": present_staff,
            "staff_count": str(staff_count),
        }

        def replace(match: re.Match) -> str:
            key = match.group(1).lower()
            style = match.group(2)
            if key == "event_start":
                return discord_ts(start, style or "F")
            if key == "event_end":
                return discord_ts(end, style or "F")
            return values.get(key, match.group(0))

        return TOKEN_RE.sub(replace, template)

    async def announcement_flow(self, home_guild_id: int, config: dict, event_date: str, user: discord.User | discord.Member) -> None:
        announcement = config.get("announcement")
        if not announcement:
            try:
                await user.send("Announcements are disabled for this event.")
            except discord.HTTPException:
                pass
            return
        try:
            dm = user.dm_channel or await user.create_dm()
            await dm.send("## Prepare announcement\nI'll collect any weekly values, show you a preview, then ask before posting.")
            names = list(dict.fromkeys(x.strip() for x in WEEKLY_VAR_RE.findall(announcement["template"])))
            weekly_values = {}
            for name in names:
                weekly_values[name] = await self.ask(user, dm, f"Enter this week's value for **{name}**.")

            rendered = await self.render_announcement(home_guild_id, config, event_date, weekly_values)
            if len(rendered) > 2000:
                await dm.send(f"The rendered announcement is {len(rendered)} characters, above Discord's 2000-character message limit. Nothing was posted.")
                return

            await dm.send("### Preview", allowed_mentions=discord.AllowedMentions.none())
            await dm.send(rendered, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
            confirmation = await self.ask(user, dm, "Type `POST` to publish this announcement, or `cancel`.")
            if confirmation.strip().upper() != "POST":
                await dm.send("Nothing was posted.")
                return

            guild = self.get_guild(int(announcement["guild_id"]))
            channel = guild.get_channel(int(announcement["channel_id"])) if guild else None
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await dm.send("I can't find the configured announcement channel anymore.")
                return
            message = await channel.send(
                rendered,
                suppress_embeds=True,
                allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
            )
            self.db.set_event_status(home_guild_id, event_date, "announced", user.id)
            await dm.send(f"Posted. ✅ {message.jump_url}")
        except ConversationCancelled:
            try:
                await user.send("Announcement cancelled.")
            except discord.HTTPException:
                pass
        except discord.Forbidden:
            pass
        except Exception:
            log.exception("Announcement flow failed")
            try:
                await user.send("Announcement preparation failed. Check the bot console for the traceback.")
            except discord.HTTPException:
                pass

    @tasks.loop(minutes=1)
    async def scheduler(self) -> None:
        for home_guild_id, config in self.db.list_configs():
            try:
                tz = ZoneInfo(config["timezone"])
                now = datetime.now(tz)
                event_start = next_event_start(config, now)
                event_date = event_start.date().isoformat()

                due_checks = []
                for idx, check in enumerate(config.get("checks", [])):
                    scheduled = schedule_before_event(config, event_start, int(check["weekday"]), check["time"])
                    key = f"check:{idx}"
                    if scheduled <= now and not self.db.was_dispatched(home_guild_id, event_date, key):
                        due_checks.append((scheduled, idx, check, key))

                if due_checks:
                    due_checks.sort(key=lambda x: x[0])
                    latest = due_checks[-1]
                    for _scheduled, _idx, _check, key in due_checks[:-1]:
                        self.db.mark_dispatched(home_guild_id, event_date, key)
                    await self.post_presence_check(home_guild_id, config, event_date, latest[2])
                    self.db.mark_dispatched(home_guild_id, event_date, latest[3])

                review = config.get("review")
                if review:
                    scheduled_review = schedule_before_event(
                        config, event_start, int(review["weekday"]), review["time"]
                    )
                    key = "review"
                    if scheduled_review <= now and not self.db.was_dispatched(home_guild_id, event_date, key):
                        await self.post_moderator_review(home_guild_id, config, event_date)
                        self.db.mark_dispatched(home_guild_id, event_date, key)
            except Exception:
                log.exception("Scheduler error for guild %s", home_guild_id)

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.wait_until_ready()


class AvailabilitySelect(discord.ui.Select):
    def __init__(self, bot: VenueHelper, context: dict, config: dict, current: dict | None):
        self.bot = bot
        self.context = context
        self.config = config
        options = []
        selected_shifts = set(current.get("shifts", [])) if current else set()
        current_status = current.get("status") if current else None
        for i, shift in enumerate(config["shifts"]):
            options.append(
                discord.SelectOption(
                    label=shift["label"][:100],
                    value=f"shift:{i}",
                    description=f"{shift['start']}–{shift['end']}",
                    default=current_status == "shifts" and i in selected_shifts,
                )
            )
        options.extend(
            [
                discord.SelectOption(label="Unsure / answer later", value="unsure", emoji="🟠", default=current_status == "unsure"),
                discord.SelectOption(label="Can't make it", value="cant", emoji="🔴", default=current_status == "cant"),
            ]
        )
        super().__init__(
            placeholder="Choose your availability",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        values = set(self.values)
        special = values & {"unsure", "cant"}
        shifts = [int(v.split(":")[1]) for v in values if v.startswith("shift:")]
        if len(special) > 1 or (special and shifts):
            await interaction.response.send_message(
                "Choose shifts **or** `Unsure` / `Can't make it`, not both.", ephemeral=True
            )
            return
        if "unsure" in values:
            status = "unsure"
            shifts = []
        elif "cant" in values:
            status = "cant"
            shifts = []
        else:
            status = "shifts"
        self.bot.db.set_availability(
            int(self.context["home_guild_id"]),
            self.context["event_date"],
            interaction.user.id,
            status,
            shifts,
        )
        await interaction.response.send_message("Availability saved. ✅", ephemeral=True)
        await self.bot.refresh_presence_messages(
            int(self.context["home_guild_id"]), self.config, self.context["event_date"]
        )


class AvailabilityView(discord.ui.View):
    def __init__(self, bot: VenueHelper, context: dict, config: dict, current: dict | None):
        super().__init__(timeout=300)
        self.add_item(AvailabilitySelect(bot, context, config, current))


class PresenceView(discord.ui.View):
    def __init__(self, bot: VenueHelper):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Set / Update Availability", style=discord.ButtonStyle.primary, custom_id="vh:availability")
    async def availability(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.message is None:
            return
        context = self.bot.db.get_message_context(interaction.message.id)
        if not context:
            await interaction.response.send_message("I can't match this message to an active check.", ephemeral=True)
            return
        config = self.bot.db.get_config(int(context["home_guild_id"]))
        if not config or not await self.bot.is_staff(config, interaction.user.id):
            await interaction.response.send_message("You aren't in one of the configured staff roles.", ephemeral=True)
            return
        current = self.bot.db.get_availability(int(context["home_guild_id"]), context["event_date"]).get(interaction.user.id)
        await interaction.response.send_message(
            "Select your availability. You can change it later.",
            view=AvailabilityView(self.bot, context, config, current),
            ephemeral=True,
        )

    @discord.ui.button(label="Set Profile", style=discord.ButtonStyle.secondary, custom_id="vh:profile")
    async def profile(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.message is None:
            return
        context = self.bot.db.get_message_context(interaction.message.id)
        if not context:
            await interaction.response.send_message("I can't match this message to an active check.", ephemeral=True)
            return
        config = self.bot.db.get_config(int(context["home_guild_id"]))
        if not config or not await self.bot.is_staff(config, interaction.user.id):
            await interaction.response.send_message("You aren't in one of the configured staff roles.", ephemeral=True)
            return
        await interaction.response.send_message("I'll DM you to set up your profile.", ephemeral=True)
        asyncio.create_task(self.bot.profile_flow(int(context["home_guild_id"]), config, interaction.user))

    @discord.ui.button(label="Moderator Review", style=discord.ButtonStyle.secondary, custom_id="vh:review")
    async def review(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.message is None:
            return
        context = self.bot.db.get_message_context(interaction.message.id)
        if not context:
            await interaction.response.send_message("I can't match this message to an active check.", ephemeral=True)
            return
        home_guild_id = int(context["home_guild_id"])
        config = self.bot.db.get_config(home_guild_id)
        if not config or not await self.bot.is_moderator(home_guild_id, config, interaction.user.id):
            await interaction.response.send_message("Only configured moderators/admins can do that.", ephemeral=True)
            return
        await interaction.response.send_message("Posting a fresh moderator summary. ✅", ephemeral=True)
        await self.bot.post_moderator_review(home_guild_id, config, context["event_date"])


class DecisionView(discord.ui.View):
    def __init__(self, bot: VenueHelper):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Announce Event", style=discord.ButtonStyle.success, custom_id="vh:announce")
    async def announce(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.message is None:
            return
        context = self.bot.db.get_message_context(interaction.message.id)
        if not context:
            await interaction.response.send_message("I can't match this review to an event.", ephemeral=True)
            return
        home_guild_id = int(context["home_guild_id"])
        config = self.bot.db.get_config(home_guild_id)
        if not config or not await self.bot.is_moderator(home_guild_id, config, interaction.user.id):
            await interaction.response.send_message("Only configured moderators/admins can do that.", ephemeral=True)
            return
        if not config.get("announcement"):
            await interaction.response.send_message("Announcements are disabled in this setup.", ephemeral=True)
            return
        await interaction.response.send_message("I'll DM you to prepare and preview the announcement.", ephemeral=True)
        asyncio.create_task(self.bot.announcement_flow(home_guild_id, config, context["event_date"], interaction.user))

    @discord.ui.button(label="Do Not Announce", style=discord.ButtonStyle.danger, custom_id="vh:no_announce")
    async def no_announce(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.message is None:
            return
        context = self.bot.db.get_message_context(interaction.message.id)
        if not context:
            await interaction.response.send_message("I can't match this review to an event.", ephemeral=True)
            return
        home_guild_id = int(context["home_guild_id"])
        config = self.bot.db.get_config(home_guild_id)
        if not config or not await self.bot.is_moderator(home_guild_id, config, interaction.user.id):
            await interaction.response.send_message("Only configured moderators/admins can do that.", ephemeral=True)
            return
        self.bot.db.set_event_status(home_guild_id, context["event_date"], "no_announcement", interaction.user.id)
        await interaction.response.edit_message(
            content=interaction.message.content + f"\n\n**Decision:** No announcement · <@{interaction.user.id}>",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )


bot = VenueHelper()


@bot.tree.command(name="setup", description="Configure Venue Helper for this server")
async def setup_command(interaction: discord.Interaction) -> None:
    await bot.run_setup(interaction)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")
    bot.run(token)


if __name__ == "__main__":
    main()
