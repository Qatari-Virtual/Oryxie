"""
Checkride Notification & Tracking Cog

Monitors the CrewCenter 'replay' table and posts a daily report at 1800Z (18:00 UTC)
to the training channel detailing pending checkrides, who claimed them,
and how long they have been awaiting evaluation.
"""

import asyncio
from datetime import datetime, timezone, time
import logging
import os
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger("oryxie.checkride_tracker")

# --- CONFIGURATION (Hardcoded with env fallback) ---
TRAINING_CHANNEL_ID = int(os.getenv("CHECKRIDE_NOTIFICATION_CHANNEL_ID", "1124058252280348702"))
FI_ROLE_ID = int(os.getenv("CHECKRIDE_FI_ROLE_ID", "1153341364545794089"))
CC_TRAINING_URL_TEMPLATE = "https://crew.qatarivirtual.com/admin/users/training/{pilot_id}"

# Scheduled at 1800Z (18:00 UTC) daily
REPORT_TIME = time(hour=18, minute=0, second=0, tzinfo=timezone.utc)

# Colors
QATAR_MAROON = 0x8A1538
GREEN_ALL_CLEAR = 0x2ECC71
WARNING_ORANGE = 0xF39C12
ALERT_RED = 0xE74C3C


def format_pending_duration(upload_date: datetime, now_utc: datetime) -> Tuple[int, int, str]:
    """
    Calculates the pending time elapsed since the checkride was uploaded.

    Args:
        upload_date: The timestamp the checkride was uploaded.
        now_utc: The current UTC datetime.

    Returns:
        Tuple[int, int, str]: (total_hours, days, formatted_string)
            e.g. (24, 1, "24h (1d)"), (48, 2, "48h (2d)"), (3, 0, "3h")
    """
    if upload_date.tzinfo is None:
        upload_date = upload_date.replace(tzinfo=timezone.utc)

    diff = now_utc - upload_date
    total_hours = max(0, int(diff.total_seconds() // 3600))
    days = total_hours // 24
    rem_hours = total_hours % 24

    if total_hours < 1:
        formatted = "< 1h"
    elif total_hours < 24:
        formatted = f"{total_hours}h"
    else:
        if rem_hours == 0:
            formatted = f"{total_hours}h ({days}d)"
        else:
            formatted = f"{total_hours}h ({days}d {rem_hours}h)"

    return total_hours, days, formatted


def build_checkride_report(
    records: List[Dict],
    now_utc: Optional[datetime] = None,
) -> Tuple[discord.Embed, Optional[str]]:
    """
    Constructs the Discord embed and notification ping content for pending checkrides.

    Args:
        records: List of checkride row dictionaries from CheckrideModel.
        now_utc: Optional UTC datetime override (defaults to current time).

    Returns:
        Tuple[discord.Embed, Optional[str]]: (embed, ping_content)
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # State 1: No pending checkrides (Queue Clear)
    if not records:
        embed = discord.Embed(
            title="📋 Checkride Review Queue — 1800Z Daily Report",
            description=(
                "🎉 **The checkride queue is completely clear!**\n\n"
                "There are currently **0** pending checkrides awaiting instructor evaluation.\n"
                "Great job to our Flight Instructors for keeping evaluations up to date! ✈️"
            ),
            color=GREEN_ALL_CLEAR,
            timestamp=now_utc,
        )
        embed.set_footer(text="Qatari Virtual Training Department • 1800Z Daily Digest")
        return embed, None

    # State 2: Checkrides Pending
    unclaimed: List[Dict] = []
    claimed: List[Dict] = []
    max_hours = 0
    claimed_fi_pings: set[str] = set()

    for item in records:
        upload_dt = item.get("upload_date")
        if isinstance(upload_dt, str):
            try:
                upload_dt = datetime.fromisoformat(upload_dt)
            except Exception:
                upload_dt = now_utc
        elif not isinstance(upload_dt, datetime):
            upload_dt = now_utc

        hours, days, duration_str = format_pending_duration(upload_dt, now_utc)
        item["_hours"] = hours
        item["_days"] = days
        item["_duration_str"] = duration_str
        item["_upload_dt"] = upload_dt

        if hours > max_hours:
            max_hours = hours

        approved_by = item.get("approved_by") or 0
        if approved_by > 0:
            claimed.append(item)
            # If claimed for >= 24h, queue the claimed instructor for reminder ping
            if hours >= 24:
                fi_discord = item.get("fi_discord_id")
                if fi_discord and str(fi_discord).isdigit():
                    claimed_fi_pings.add(str(fi_discord))
        else:
            unclaimed.append(item)

    # Determine embed color based on oldest pending submission
    if max_hours >= 48:
        embed_color = ALERT_RED
    elif max_hours >= 24:
        embed_color = WARNING_ORANGE
    else:
        embed_color = QATAR_MAROON

    embed = discord.Embed(
        title="📋 Checkride Review Queue — 1800Z Daily Report",
        description=(
            f"**Total Pending:** `{len(records)}`  |  "
            f"**Unclaimed:** `{len(unclaimed)}`  |  "
            f"**Claimed:** `{len(claimed)}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=embed_color,
        timestamp=now_utc,
    )

    # Format Unclaimed Checkrides Field
    if unclaimed:
        unclaimed_lines = []
        for cr in unclaimed:
            cr_id = cr.get("id")
            pilot_id = cr.get("pilotid")
            cs = cr.get("trainee_callsign") or "Unknown"
            name = cr.get("trainee_name") or "Unknown"
            cr_type = "🎙️ ATC" if cr.get("checkride_type") == 1 else "🎯 UNICOM"
            dur = cr.get("_duration_str", "N/A")
            review_url = CC_TRAINING_URL_TEMPLATE.format(pilot_id=pilot_id)

            unclaimed_lines.append(
                f"• **CR #{cr_id}** — **{cs}** ({name}) | `{cr_type}`\n"
                f"  └ ⏱️ Pending: `{dur}` | ⚠️ **Unclaimed**\n"
                f"  └ 🔗 [Open in CrewCenter]({review_url})"
            )

        # Discord field limit is 1024 characters; split if necessary
        unclaimed_text = "\n\n".join(unclaimed_lines)
        if len(unclaimed_text) <= 1024:
            embed.add_field(
                name=f"⏳ Unclaimed Checkrides ({len(unclaimed)})",
                value=unclaimed_text,
                inline=False,
            )
        else:
            # Chunking into multiple fields
            chunks = []
            curr_chunk = ""
            for line in unclaimed_lines:
                if len(curr_chunk) + len(line) + 2 > 1000:
                    chunks.append(curr_chunk)
                    curr_chunk = line
                else:
                    curr_chunk = f"{curr_chunk}\n\n{line}" if curr_chunk else line
            if curr_chunk:
                chunks.append(curr_chunk)

            for idx, chunk in enumerate(chunks):
                field_name = f"⏳ Unclaimed Checkrides ({len(unclaimed)})" if idx == 0 else f"⏳ Unclaimed Checkrides (Cont. {idx+1})"
                embed.add_field(name=field_name, value=chunk, inline=False)

    # Format Claimed Checkrides Field
    if claimed:
        claimed_lines = []
        for cr in claimed:
            cr_id = cr.get("id")
            pilot_id = cr.get("pilotid")
            cs = cr.get("trainee_callsign") or "Unknown"
            name = cr.get("trainee_name") or "Unknown"
            cr_type = "🎙️ ATC" if cr.get("checkride_type") == 1 else "🎯 UNICOM"
            dur = cr.get("_duration_str", "N/A")
            hours = cr.get("_hours", 0)
            review_url = CC_TRAINING_URL_TEMPLATE.format(pilot_id=pilot_id)

            fi_cs = cr.get("fi_callsign") or "FI"
            fi_name = cr.get("fi_name") or "Instructor"
            fi_discord = cr.get("fi_discord_id")
            fi_mention = f"<@{fi_discord}>" if (fi_discord and str(fi_discord).isdigit()) else f"**{fi_cs}**"

            urgent_tag = " 🚨 *Overdue for grading!*" if hours >= 48 else (" ⚠️ *Pending > 24h*" if hours >= 24 else "")

            claimed_lines.append(
                f"• **CR #{cr_id}** — **{cs}** ({name}) | `{cr_type}`\n"
                f"  └ ⏱️ Pending: `{dur}`{urgent_tag}\n"
                f"  └ 👤 Claimed by: {fi_mention} ({fi_name})\n"
                f"  └ 🔗 [Open in CrewCenter]({review_url})"
            )

        claimed_text = "\n\n".join(claimed_lines)
        if len(claimed_text) <= 1024:
            embed.add_field(
                name=f"🔒 Claimed Checkrides ({len(claimed)})",
                value=claimed_text,
                inline=False,
            )
        else:
            chunks = []
            curr_chunk = ""
            for line in claimed_lines:
                if len(curr_chunk) + len(line) + 2 > 1000:
                    chunks.append(curr_chunk)
                    curr_chunk = line
                else:
                    curr_chunk = f"{curr_chunk}\n\n{line}" if curr_chunk else line
            if curr_chunk:
                chunks.append(curr_chunk)

            for idx, chunk in enumerate(chunks):
                field_name = f"🔒 Claimed Checkrides ({len(claimed)})" if idx == 0 else f"🔒 Claimed Checkrides (Cont. {idx+1})"
                embed.add_field(name=field_name, value=chunk, inline=False)

    embed.set_footer(text="Qatari Virtual Training Department • 1800Z Daily Digest")

    # Construct Pings
    pings: List[str] = []
    if unclaimed:
        pings.append(f"<@&{FI_ROLE_ID}>")

    for discord_id in sorted(claimed_fi_pings):
        pings.append(f"<@{discord_id}>")

    if pings:
        notification_content = f"🔔 **Checkride Daily Notice:** {' '.join(pings)}"
    else:
        notification_content = None

    return embed, notification_content


class CheckrideTracker(commands.Cog):
    """
    Cog responsible for tracking pending checkride evaluations
    and posting daily 1800Z digests to the training channel.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.daily_checkride_report.start()
        logger.info("CheckrideTracker cog initialized. Daily 1800Z loop scheduled.")

    def cog_unload(self):
        """Cancel background loop cleanly on cog unload."""
        self.daily_checkride_report.cancel()
        logger.info("CheckrideTracker cog unloaded. Loop cancelled.")

    async def get_checkride_model(self):
        """Helper to safely retrieve the CheckrideModel from bot instance."""
        model = getattr(self.bot, "checkride_model", None)
        if not model:
            logger.error("CheckrideModel is not initialized on bot!")
        return model

    async def build_report_payload(self) -> Tuple[discord.Embed, Optional[str]]:
        """Queries database and builds embed and ping content."""
        model = await self.get_checkride_model()
        if not model:
            err_embed = discord.Embed(
                title="❌ Checkride System Error",
                description="Unable to connect to the Checkride database model.",
                color=ALERT_RED,
            )
            return err_embed, None

        records = await model.get_pending_checkrides()
        return build_checkride_report(records)

    @tasks.loop(time=REPORT_TIME)
    async def daily_checkride_report(self):
        """
        Background task running every day at 18:00 UTC (1800Z).
        Posts the checkride queue digest to the training channel.
        """
        await self.bot.wait_until_ready()
        logger.info("Executing daily 1800Z checkride report...")

        try:
            channel = self.bot.get_channel(TRAINING_CHANNEL_ID)
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(TRAINING_CHANNEL_ID)
                except Exception as e:
                    logger.error("Could not fetch training channel %s: %s", TRAINING_CHANNEL_ID, e)
                    return

            embed, content = await self.build_report_payload()
            await channel.send(content=content, embed=embed)
            logger.info("Daily 1800Z checkride report successfully posted.")

        except Exception:
            logger.exception("Unexpected error in daily_checkride_report task loop:")

    @daily_checkride_report.before_loop
    async def before_daily_checkride_report(self):
        """Ensure bot is ready before starting the task loop."""
        await self.bot.wait_until_ready()

    # =========================================================================
    # SLASH COMMANDS
    # =========================================================================

    @app_commands.command(
        name="cr_queue",
        description="View the live pending checkride queue awaiting evaluation.",
    )
    async def cr_queue_command(self, interaction: discord.Interaction):
        """Displays the pending checkride review queue on demand."""
        # Respect 3-second rule from AGENTS.md
        await interaction.response.defer(ephemeral=True)

        try:
            embed, _ = await self.build_report_payload()
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.exception("Error executing /cr_queue command: %s", e)
            await interaction.followup.send(
                "❌ An error occurred while fetching the checkride queue.",
                ephemeral=True,
            )

    @app_commands.command(
        name="cr_test_report",
        description="Trigger the 1800Z checkride report in the training channel (Staff/Admin only).",
    )
    @app_commands.default_permissions(administrator=True)
    async def cr_test_report_command(self, interaction: discord.Interaction):
        """Allows staff to manually trigger and test the 1800Z digest."""
        await interaction.response.defer(ephemeral=True)

        try:
            channel = self.bot.get_channel(TRAINING_CHANNEL_ID)
            if not channel:
                channel = await self.bot.fetch_channel(TRAINING_CHANNEL_ID)

            embed, content = await self.build_report_payload()
            await channel.send(content=content, embed=embed)
            await interaction.followup.send(
                f"✅ Checkride report successfully sent to {channel.mention}!",
                ephemeral=True,
            )
        except Exception as e:
            logger.exception("Error triggering manual checkride test report: %s", e)
            await interaction.followup.send(
                f"❌ Failed to send checkride report: {e}",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    """Extension entry point for Oryxie."""
    await bot.add_cog(CheckrideTracker(bot))
