import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("oryxie.cogs.potw_lotw_message")

LOTW_IMAGE_URL = (
    "https://cdn.discordapp.com/attachments/1207938859292434473/"
    "1571574325822230598/LOTW.png?ex=6afbe2e5&is=6afa9165&"
    "hm=5ad54fdd8c81c9e4a3da906a5ee3c78608a2f30e76a46a7d87c657a0fdea4162"
)
POTW_IMAGE_URL = (
    "https://cdn.discordapp.com/attachments/1197457653799059536/"
    "1571574549101809814/POTW.jpg?ex=6afbe31a&is=6afa919a&"
    "hm=ffd8f97f6f998bc6db358e995d1fc13e17b6a95cbffce812e40f377d0acf0b2f"
)
SUBMISSIONS_CHANNEL_NAME = "potw-and-lotw-submissions"
AUTHORIZED_ROLE_NAMES = {"manager", "executive"}


class PotwLotwMessageCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="potw-lotw-message",
        description="Post this week's LOTW or POTW announcement",
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
        channel="Where to post (defaults to the channel where you ran the command)",
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

        target_channel = channel or interaction.channel
        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "Choose a text channel, or run this command in a text channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        is_lotw = message_type.value == "lotw"
        month = datetime.now(timezone.utc).strftime("%B")
        kind = "LOTW" if is_lotw else "POTW"
        photo = "landings" if is_lotw else "pictures"
        restriction = "landing" if is_lotw else "picture"
        no_edit = "video" if is_lotw else "photo"
        image_url = LOTW_IMAGE_URL if is_lotw else POTW_IMAGE_URL

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

        content = (
            f"**--- Week of {month}**\n\n"
            f"This week's topic is **{topic}**\n\n"
            f"Use {submissions_reference} to submit a photo. "
            f"Only post {photo} from Infinite Flight.\n\n"
            f"Only one {restriction} from each user will qualify for the penultimate stage.\n\n"
            "Submissions close on saturday 0600Z\n\n"
            f"No edited {no_edit} is allowed\n\n"
            f"{entertainment_reference}"
        )
        embed = discord.Embed()
        embed.set_image(url=image_url)
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=[entertainment_role] if entertainment_role else False,
        )

        try:
            await target_channel.send(
                content,
                embed=embed,
                allowed_mentions=allowed_mentions,
            )
        except discord.Forbidden:
            logger.exception(
                "Missing permission to post %s announcement in channel %s",
                kind,
                target_channel.id,
            )
            await interaction.followup.send(
                f"I don't have permission to post in {target_channel.mention}.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logger.exception(
                "Failed to post %s announcement in channel %s",
                kind,
                target_channel.id,
            )
            await interaction.followup.send(
                "Discord couldn't deliver the announcement. Please try again.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Posted the {kind} announcement in {target_channel.mention}.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PotwLotwMessageCog(bot))
