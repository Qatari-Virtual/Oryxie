import calendar
import logging
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("oryxie.cogs.potw_lotw_message")

IMAGE_FILES = {
    "lotw": Path(__file__).resolve().parents[1] / "assets" / "lotw.png",
    "potw": Path(__file__).resolve().parents[1] / "assets" / "potw.jpg",
}
SUBMISSIONS_CHANNEL_NAME = "potw-and-lotw-submissions"
AUTHORIZED_ROLE_NAMES = {"manager", "executive"}
DEFAULT_POST_CHANNEL_ID = 1405962701242634270


def ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def calendar_week_options(now: datetime) -> list[discord.SelectOption]:
    weeks = calendar.Calendar(firstweekday=calendar.MONDAY).monthdayscalendar(
        now.year, now.month
    )
    month_abbreviation = calendar.month_abbr[now.month]
    options = []

    for week_number, days in enumerate(weeks, start=1):
        days_in_month = [day for day in days if day]
        options.append(
            discord.SelectOption(
                label=(
                    f"{ordinal(week_number)} Week "
                    f"({month_abbreviation} {days_in_month[0]}-{days_in_month[-1]})"
                ),
                value=str(week_number),
            )
        )

    return options


def announcement_content(
    message_type: str,
    topic: str,
    week_heading: str,
    submissions_reference: str,
    entertainment_reference: str,
) -> str:
    is_lotw = message_type == "lotw"
    photo = "landings" if is_lotw else "pictures"
    restriction = "landing" if is_lotw else "picture"
    no_edit = "video" if is_lotw else "photo"

    return (
        f"**{week_heading}**\n\n"
        f"This week's topic is **{topic}**\n\n"
        f"Use {submissions_reference} to submit a photo. "
        f"Only post {photo} from Infinite Flight.\n\n"
        f"Only one {restriction} from each user will qualify for the penultimate stage.\n\n"
        "Submissions close on saturday 0600Z\n\n"
        f"No edited {no_edit} is allowed\n\n"
        f"{entertainment_reference}"
    )


class CalendarWeekSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Choose a week from this month's calendar",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, WeeklyAnnouncementView):
            raise RuntimeError("Calendar week selector is not attached to its view")

        self.view.week_number = int(self.values[0])
        self.view.confirm_button.disabled = False
        await interaction.response.edit_message(
            content=self.view.preview_content(),
            view=self.view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class WeeklyAnnouncementView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        target_channel: discord.TextChannel | discord.Thread,
        message_type: str,
        topic: str,
        month: str,
        calendar_options: list[discord.SelectOption],
        submissions_reference: str,
        entertainment_reference: str,
        entertainment_role: discord.Role | None,
    ):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.target_channel = target_channel
        self.message_type = message_type
        self.topic = topic
        self.month = month
        self.submissions_reference = submissions_reference
        self.entertainment_reference = entertainment_reference
        self.entertainment_role = entertainment_role
        self.week_number: int | None = None

        self.add_item(CalendarWeekSelect(calendar_options))
        self.confirm_button.disabled = True

    def preview_content(self) -> str:
        week_heading = (
            f"{ordinal(self.week_number)} Week of {self.month}"
            if self.week_number is not None
            else "Select a calendar week below"
        )
        return announcement_content(
            self.message_type,
            self.topic,
            week_heading,
            self.submissions_reference,
            self.entertainment_reference,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who started this announcement can confirm or cancel it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm and post", style=discord.ButtonStyle.success)
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if self.week_number is None:
            await interaction.response.send_message(
                "Choose a week from the calendar before confirming.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        kind = "LOTW" if self.message_type == "lotw" else "POTW"
        image_path = IMAGE_FILES[self.message_type]
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=[self.entertainment_role] if self.entertainment_role else False,
        )

        try:
            with image_path.open("rb") as image_file:
                await self.target_channel.send(
                    announcement_content(
                        self.message_type,
                        self.topic,
                        f"{ordinal(self.week_number)} Week of {self.month}",
                        self.submissions_reference,
                        self.entertainment_reference,
                    ),
                    file=discord.File(image_file, filename=image_path.name),
                    allowed_mentions=allowed_mentions,
                )
        except OSError:
            logger.exception("Could not read the %s announcement image", kind)
            await interaction.followup.send(
                "The announcement image file is missing or unreadable.",
                ephemeral=True,
            )
            return
        except discord.Forbidden:
            logger.exception(
                "Missing permission to post %s announcement in channel %s",
                kind,
                self.target_channel.id,
            )
            await interaction.followup.send(
                f"I don't have permission to post in {self.target_channel.mention}.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logger.exception(
                "Failed to post %s announcement in channel %s",
                kind,
                self.target_channel.id,
            )
            await interaction.followup.send(
                "Discord couldn't deliver the announcement. Please try again.",
                ephemeral=True,
            )
            return

        await interaction.edit_original_response(
            content=f"Posted the {kind} announcement in {self.target_channel.mention}.",
            view=None,
            attachments=[],
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content="Announcement cancelled.",
            view=None,
            attachments=[],
        )


class PotwLotwMessageCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="potw-lotw-message",
        description="Prepare and confirm this week's LOTW or POTW announcement",
    )
    @app_commands.guild_only()
    @app_commands.choices(
        message_type=[
            app_commands.Choice(name="Landing of the Week (LOTW)", value="lotw"),
            app_commands.Choice(name="Picture of the Week (POTW)", value="potw"),
        ]
    )
    @app_commands.describe(
        message_type="Choose which weekly photo announcement to post",
        topic="This week's LOTW or POTW topic",
        channel="Optional destination override (defaults to the weekly announcement channel)",
    )
    async def weekly_photo(
        self,
        interaction: discord.Interaction,
        message_type: app_commands.Choice[str],
        topic: str,
        channel: discord.TextChannel | None = None,
    ):
        if not isinstance(interaction.user, discord.Member) or not any(
            role.name.casefold() in AUTHORIZED_ROLE_NAMES
            for role in interaction.user.roles
        ):
            await interaction.response.send_message(
                "Only members with the Manager or Executive role can post this announcement.",
                ephemeral=True,
            )
            return

        topic = topic.strip()
        if not topic:
            await interaction.response.send_message(
                "Please provide a topic for the announcement.",
                ephemeral=True,
            )
            return
        if len(topic) > 100:
            await interaction.response.send_message(
                "The topic must be 100 characters or fewer.",
                ephemeral=True,
            )
            return

        target_channel = channel or interaction.guild.get_channel_or_thread(
            DEFAULT_POST_CHANNEL_ID
        )
        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "I can't access the configured weekly announcement channel. "
                "Choose a destination channel or ask an administrator to check my access.",
                ephemeral=True,
            )
            return

        now = datetime.now(timezone.utc)
        guild = interaction.guild
        submission_channel = (
            discord.utils.get(guild.text_channels, name=SUBMISSIONS_CHANNEL_NAME)
            if guild
            else None
        )
        entertainment_role = (
            discord.utils.get(guild.roles, name="Entertainment") if guild else None
        )
        submissions_reference = (
            submission_channel.mention
            if submission_channel
            else f"#{SUBMISSIONS_CHANNEL_NAME}"
        )
        entertainment_reference = (
            entertainment_role.mention if entertainment_role else "@Entertainment"
        )
        view = WeeklyAnnouncementView(
            author_id=interaction.user.id,
            target_channel=target_channel,
            message_type=message_type.value,
            topic=topic,
            month=now.strftime("%B"),
            calendar_options=calendar_week_options(now),
            submissions_reference=submissions_reference,
            entertainment_reference=entertainment_reference,
            entertainment_role=entertainment_role,
        )
        image_path = IMAGE_FILES[message_type.value]

        try:
            with image_path.open("rb") as image_file:
                await interaction.response.send_message(
                    view.preview_content(),
                    file=discord.File(image_file, filename=image_path.name),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                    ephemeral=True,
                )
        except OSError:
            logger.exception(
                "Could not read the %s announcement preview image",
                message_type.value.upper(),
            )
            await interaction.response.send_message(
                "The announcement image file is missing or unreadable.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(PotwLotwMessageCog(bot))
