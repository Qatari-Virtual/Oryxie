"""Discord presentation layer for QRV Christmas Operations 2026."""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

VIEW_TIMEOUT_SECONDS = 300


def _progress_bar(pct: float, length: int = 12) -> str:
    clamped = min(100.0, max(0.0, float(pct)))
    filled = int(round((clamped / 100.0) * length))
    return "█" * filled + "░" * (length - filled)


def _parse_hex_color(value: str, fallback: int = 0x8A1538) -> int:
    raw = str(value or "").strip().lstrip("#")
    if len(raw) == 6:
        try:
            return int(raw, 16)
        except ValueError:
            pass
    return fallback


class DispatchCopilotModal(discord.ui.Modal, title="Set Christmas Co-pilot"):
    callsign = discord.ui.TextInput(
        label="Co-pilot Callsign",
        placeholder="e.g. QRV133 or 133",
        required=True,
        max_length=32,
    )

    def __init__(self, parent_view: "DispatchConfirmView"):
        super().__init__(timeout=180)
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.owner_discord_id:
            await interaction.response.send_message("Only the requesting pilot can edit this dispatch.", ephemeral=True)
            return

        copilot, error = await self.parent_view.cog.bot.christmas_service.resolve_copilot_by_callsign(
            self.callsign.value
        )
        if error or not copilot:
            await interaction.response.send_message(f"❌ {error or 'Co-pilot lookup failed.'}", ephemeral=True)
            return
        if int(copilot["id"]) == int(self.parent_view.pilot["id"]):
            await interaction.response.send_message("❌ You cannot assign yourself as co-pilot.", ephemeral=True)
            return

        self.parent_view.copilot = copilot
        self.parent_view._refresh_buttons()
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(),
            view=self.parent_view,
        )


class CpscDurationModal(discord.ui.Modal, title="Enter Flight Duration"):
    hours = discord.ui.TextInput(
        label="Flight Hours (HH)",
        placeholder="e.g. 06",
        required=True,
        max_length=2,
    )
    minutes = discord.ui.TextInput(
        label="Flight Minutes (MM)",
        placeholder="e.g. 45",
        required=True,
        max_length=2,
    )

    def __init__(self, parent_view: "CpscConfirmView"):
        super().__init__(timeout=180)
        self.parent_view = parent_view
        if parent_view.total_minutes and parent_view.total_minutes > 0:
            hrs, mins = divmod(parent_view.total_minutes, 60)
            self.hours.default = f"{hrs:02d}"
            self.minutes.default = f"{mins:02d}"

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.owner_discord_id:
            await interaction.response.send_message("Only the filing pilot can edit this PIREP.", ephemeral=True)
            return
        try:
            hrs = int(self.hours.value)
            mins = int(self.minutes.value)
            if not (0 <= hrs <= 99 and 0 <= mins <= 59) or (hrs == 0 and mins == 0):
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("❌ Enter valid Hours (0-99) and Minutes (0-59), > 00:00.", ephemeral=True)
            return

        self.parent_view.total_minutes = (hrs * 60) + mins
        self.parent_view.duration_source = "Manual Entry"
        self.parent_view._refresh_buttons()
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(),
            view=self.parent_view,
        )


class FrameSelectView(discord.ui.View):
    def __init__(
        self,
        cog: "ChristmasOperationsCog",
        pilot: Dict[str, Any],
        owner_discord_id: int,
        frames: List[Dict[str, Any]],
        page: int = 0,
    ):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.cog = cog
        self.pilot = pilot
        self.owner_discord_id = owner_discord_id
        self.frames = frames
        self.page = page
        self._build_items()

    def _build_items(self) -> None:
        self.clear_items()
        page_items = self.frames[self.page * 25 : (self.page + 1) * 25]
        options = [
            discord.SelectOption(
                label=f"{f['registration']} · {f['icao']} · {f['capacity']} PAX"[:100],
                description=f"{f['model']} · {f['distance_nm']} NM from OTHH"[:100],
                value=f["if_org_aircraft_id"],
            )
            for f in page_items
        ]
        select = discord.ui.Select(
            placeholder=f"Select parked QR passenger frame (page {self.page + 1})…",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

        if self.page > 0:
            prev_btn = discord.ui.Button(label="◀️ Prev", style=discord.ButtonStyle.secondary, row=1)
            prev_btn.callback = self._prev_page
            self.add_item(prev_btn)
        if (self.page + 1) * 25 < len(self.frames):
            next_btn = discord.ui.Button(label="Next ▶️", style=discord.ButtonStyle.secondary, row=1)
            next_btn.callback = self._next_page
            self.add_item(next_btn)

    async def _prev_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the requesting pilot can use this menu.", ephemeral=True)
            return
        self.page = max(0, self.page - 1)
        self._build_items()
        await interaction.response.edit_message(view=self)

    async def _next_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the requesting pilot can use this menu.", ephemeral=True)
            return
        max_page = max(0, (len(self.frames) - 1) // 25)
        self.page = min(max_page, self.page + 1)
        self._build_items()
        await interaction.response.edit_message(view=self)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the requesting pilot can use this menu.", ephemeral=True)
            return
        selected_id = interaction.data["values"][0]
        frame = next((f for f in self.frames if f["if_org_aircraft_id"] == selected_id), None)
        if not frame:
            await interaction.response.send_message("❌ Frame not found. Please start dispatch again.", ephemeral=True)
            return

        view = OperationSelectView(self.cog, self.pilot, self.owner_discord_id, frame)
        embed = discord.Embed(
            title="🛫 Step 2: Select Christmas Operation",
            description=(
                f"**Frame:** `{frame['registration']}` · {frame['model']} (**{frame['capacity']:,} PAX**)\n\n"
                "Choose whether to collect passengers from the East into Doha (`OTHH`), or distribute "
                "passengers from Doha (`OTHH`) to the West (requires **90%** capacity available in the OTHH pool)."
            ),
            color=0x8A1538,
        )
        await interaction.response.edit_message(content=None, embed=embed, view=view)


class OperationSelectView(discord.ui.View):
    def __init__(
        self,
        cog: "ChristmasOperationsCog",
        pilot: Dict[str, Any],
        owner_discord_id: int,
        frame: Dict[str, Any],
    ):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.cog = cog
        self.pilot = pilot
        self.owner_discord_id = owner_discord_id
        self.frame = frame

    @discord.ui.button(label="🟢 East Collection", style=discord.ButtonStyle.success)
    async def east_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_operation(interaction, "EAST")

    @discord.ui.button(label="🔵 West Distribution", style=discord.ButtonStyle.primary)
    async def west_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_operation(interaction, "WEST")

    async def _handle_operation(self, interaction: discord.Interaction, operation: str):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the requesting pilot can use this menu.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        res = await self.cog.bot.christmas_service.check_operation_and_routes(operation, self.frame)
        if not res.get("ok"):
            await interaction.edit_original_response(
                content=res.get("error") or "❌ No eligible routes available.",
                embed=None,
                view=self,
            )
            return

        view = DestinationSelectView(
            self.cog,
            self.pilot,
            self.owner_discord_id,
            self.frame,
            operation,
            int(res["allocated_pax"]),
            res["destinations"],
        )
        op_title = "🟢 East Collection" if operation == "EAST" else "🔵 West Distribution"
        embed = discord.Embed(
            title=f"🗺️ Step 3: Select Destination ({op_title})",
            description=(
                f"**Frame:** `{self.frame['registration']}` (`{self.frame['icao']}`)\n"
                f"**Allocated Payload:** **{res['allocated_pax']:,} PAX**\n"
                f"**Eligible Destinations:** {len(res['destinations'])} route(s) matched\n\n"
                "Choose your destination airport below:"
            ),
            color=0x1E7E34 if operation == "EAST" else 0x1F6FEB,
        )
        await interaction.edit_original_response(content=None, embed=embed, view=view)


class DestinationSelectView(discord.ui.View):
    def __init__(
        self,
        cog: "ChristmasOperationsCog",
        pilot: Dict[str, Any],
        owner_discord_id: int,
        frame: Dict[str, Any],
        operation: str,
        allocated_pax: int,
        destinations: List[Dict[str, Any]],
    ):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.cog = cog
        self.pilot = pilot
        self.owner_discord_id = owner_discord_id
        self.frame = frame
        self.operation = operation
        self.allocated_pax = allocated_pax
        self.destinations = destinations

        options = [
            discord.SelectOption(
                label=f"{d['icao']} · {d['city']}"[:100],
                description=f"Outbound: {d['outbound_fltnum'] or 'Charter'} · Return: {d['return_fltnum'] or 'Charter'}"[:100],
                value=d["icao"],
            )
            for d in destinations[:25]
        ]
        select = discord.ui.Select(
            placeholder="Select destination airport…",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the requesting pilot can use this menu.", ephemeral=True)
            return

        dest_icao = interaction.data["values"][0]
        destination = next((d for d in self.destinations if d["icao"] == dest_icao), None)
        if not destination:
            await interaction.response.send_message("❌ Destination not found.", ephemeral=True)
            return

        confirm_view = DispatchConfirmView(
            self.cog,
            self.pilot,
            self.owner_discord_id,
            self.frame,
            self.operation,
            self.allocated_pax,
            destination,
            back_destinations=self.destinations,
        )
        await interaction.response.edit_message(
            content=None,
            embed=confirm_view.build_embed(),
            view=confirm_view,
        )


class DispatchConfirmView(discord.ui.View):
    def __init__(
        self,
        cog: "ChristmasOperationsCog",
        pilot: Dict[str, Any],
        owner_discord_id: int,
        frame: Dict[str, Any],
        operation: str,
        allocated_pax: int,
        destination: Dict[str, Any],
        back_destinations: List[Dict[str, Any]],
    ):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.cog = cog
        self.pilot = pilot
        self.owner_discord_id = owner_discord_id
        self.frame = frame
        self.operation = operation
        self.allocated_pax = allocated_pax
        self.destination = destination
        self.back_destinations = back_destinations
        self.copilot: Optional[Dict[str, Any]] = None
        self.submitted = False
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self.clear_cop_btn.disabled = self.copilot is None or self.submitted
        self.confirm_btn.disabled = self.submitted
        self.set_cop_btn.disabled = self.submitted
        self.back_btn.disabled = self.submitted

    def build_embed(self) -> discord.Embed:
        op_label = "🟢 East Collection" if self.operation == "EAST" else "🔵 West Distribution"
        dest_icao = self.destination["icao"]
        dest_city = self.destination["city"]
        carrying = f"Leg 2 (`{dest_icao} → OTHH`)" if self.operation == "EAST" else f"Leg 1 (`OTHH → {dest_icao}`)"
        crew_str = (
            f"**{self.pilot.get('callsign')}** + **{self.copilot.get('callsign')}** (50/50 Tickets & Hours)"
            if self.copilot
            else f"**{self.pilot.get('callsign')}** (Solo)"
        )
        embed = discord.Embed(
            title="🎫 Step 4: Confirm Christmas Dispatch",
            description=(
                f"**Operation:** {op_label}\n"
                f"**Frame:** `{self.frame['registration']}` · {self.frame['model']}\n"
                f"**2-Leg Route:** `OTHH` → `{dest_icao}` ({dest_city}) → `OTHH`\n"
                f"**Payload:** **{self.allocated_pax:,} PAX** on {carrying}\n"
                f"**Ticket Reward:** **{self.allocated_pax:,} ChristmasTickets** *(credited on `/cpsc` approval)*\n"
                f"**Crew:** {crew_str}"
            ),
            color=0x1E7E34 if self.operation == "EAST" else 0x1F6FEB,
        )
        embed.set_footer(text="Takeoff windows: 6h at OTHH, 12h at outstations • File via /cpsc after landing")
        return embed

    @discord.ui.button(label="✅ Confirm & Dispatch", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the requesting pilot can confirm this dispatch.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        self.submitted = True
        self._refresh_buttons()

        res = await self.cog.bot.christmas_service.create_dispatch(
            pilot=self.pilot,
            frame=self.frame,
            operation=self.operation,
            destination=self.destination,
            copilot=self.copilot,
        )
        if not res.get("ok"):
            self.submitted = False
            self._refresh_buttons()
            await interaction.edit_original_response(
                content=f"❌ {res.get('error') or 'Dispatch failed.'}",
                embed=self.build_embed(),
                view=self,
            )
            return

        links = []
        if res.get("simbrief_leg1"):
            links.append(f"[SimBrief Leg 1 ({res['hub_icao']} → {res['destination_icao']})]({res['simbrief_leg1']})")
        if res.get("simbrief_leg2"):
            links.append(f"[SimBrief Leg 2 ({res['destination_icao']} → {res['hub_icao']})]({res['simbrief_leg2']})")
        links_text = " • ".join(links) if links else "SimBrief links unavailable"

        embed = discord.Embed(
            title=f"✅ Dispatch Confirmed · {res['dispatch_code']}",
            description=(
                f"**Airframe:** `{res['registration']}` ({res['aircraft_model']})\n"
                f"**Route:** `{res['hub_icao']} → {res['destination_icao']} → {res['hub_icao']}`\n"
                f"**Payload:** **{res['allocated_pax']:,} PAX** · **{res['ticket_reward']:,} ChristmasTickets** on approval\n"
                f"**Flight Planning:** {links_text}\n\n"
                "Use **`/cpsc`** after landing each leg to file your CrewCenter PIREP — your **Reward Ticket & ChristmasTickets** will arrive via DM upon approval!"
            ),
            color=0x2ECC71,
        )

        await interaction.edit_original_response(content=None, embed=embed, view=None)
        await self.cog.post_log_message(
            f"🛫 **Christmas Dispatch Created:** `{res['dispatch_code']}` ({res['operation']}) — "
            f"`{self.pilot.get('callsign')}` on `{res['registration']}` "
            f"(`{res['hub_icao']} ↔ {res['destination_icao']}`, **{res['allocated_pax']:,} PAX**)"
        )
        await self.cog.refresh_live_board()

    @discord.ui.button(label="👥 Set Co-pilot", style=discord.ButtonStyle.secondary)
    async def set_cop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the requesting pilot can edit this dispatch.", ephemeral=True)
            return
        await interaction.response.send_modal(DispatchCopilotModal(self))

    @discord.ui.button(label="🧹 Clear Co-pilot", style=discord.ButtonStyle.secondary)
    async def clear_cop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the requesting pilot can edit this dispatch.", ephemeral=True)
            return
        self.copilot = None
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="⬅️ Back", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the requesting pilot can use this button.", ephemeral=True)
            return
        view = DestinationSelectView(
            self.cog,
            self.pilot,
            self.owner_discord_id,
            self.frame,
            self.operation,
            self.allocated_pax,
            self.back_destinations,
        )
        op_title = "🟢 East Collection" if self.operation == "EAST" else "🔵 West Distribution"
        embed = discord.Embed(
            title=f"🗺️ Step 3: Select Destination ({op_title})",
            description=(
                f"**Frame:** `{self.frame['registration']}` (`{self.frame['icao']}`)\n"
                f"**Allocated Payload:** **{self.allocated_pax:,} PAX**\n"
                f"**Eligible Destinations:** {len(self.back_destinations)} route(s) matched\n\n"
                "Choose your destination airport below:"
            ),
            color=0x1E7E34 if self.operation == "EAST" else 0x1F6FEB,
        )
        await interaction.response.edit_message(content=None, embed=embed, view=view)


class OpenReturnSelectView(discord.ui.View):
    def __init__(
        self,
        cog: "ChristmasOperationsCog",
        pilot: Dict[str, Any],
        owner_discord_id: int,
        open_legs: List[Dict[str, Any]],
    ):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.cog = cog
        self.pilot = pilot
        self.owner_discord_id = owner_discord_id
        self.open_legs = open_legs

        options = [
            discord.SelectOption(
                label=f"{leg['dispatch_code']} · {leg['registration']} ({leg['origin_icao']} → {leg['destination_icao']})"[:100],
                description=f"PAX: {int(leg.get('allocated_pax') or 0):,} · Schedule {str(leg['schedule_id'])[:12]}"[:100],
                value=str(leg["schedule_id"]),
            )
            for leg in open_legs[:25]
        ]
        select = discord.ui.Select(
            placeholder="Select an open return leg to claim…",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the requesting pilot can use this menu.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        sched_id = interaction.data["values"][0]
        res = await self.cog.bot.christmas_service.claim_open_return_leg(self.pilot, sched_id)
        if not res.get("ok"):
            await interaction.edit_original_response(content=f"❌ {res.get('error')}", embed=None, view=None)
            return

        leg = res["leg"]
        sb_text = f"[SimBrief Return FPL]({res['simbrief_link']})" if res.get("simbrief_link") else "SimBrief unavailable"
        embed = discord.Embed(
            title=f"🔄 Return Leg Claimed · {leg['dispatch_code']}",
            description=(
                f"**Airframe:** `{leg['registration']}`\n"
                f"**Route:** `{leg['origin_icao']} → {leg['destination_icao']}` (Leg 2)\n"
                f"**Payload:** **{int(leg.get('allocated_pax') or 0):,} PAX** · **{res['ticket_reward']:,} Tickets** on approval\n"
                f"**Flight Planning:** {sb_text}\n\n"
                "File via **`/cpsc`** after landing — your **Reward Ticket & ChristmasTickets** will arrive via DM upon approval!"
            ),
            color=0x2ECC71,
        )

        await interaction.edit_original_response(content=None, embed=embed, view=None)
        await self.cog.post_log_message(
            f"🔄 **Open Return Leg Claimed:** `{leg['dispatch_code']}` Leg 2 by `{self.pilot.get('callsign')}` "
            f"(`{leg['registration']}` {leg['origin_icao']} → {leg['destination_icao']})"
        )
        await self.cog.refresh_live_board()


class CpscLegSelectView(discord.ui.View):
    def __init__(
        self,
        cog: "ChristmasOperationsCog",
        caller_pilot: Dict[str, Any],
        owner_discord_id: int,
        legs: List[Dict[str, Any]],
        copilot_override: Optional[Dict[str, Any]],
    ):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.cog = cog
        self.caller_pilot = caller_pilot
        self.owner_discord_id = owner_discord_id
        self.legs = legs
        self.copilot_override = copilot_override

        options = [
            discord.SelectOption(
                label=f"{l['dispatch_code']} L{l['leg_number']} · {l['registration']} ({l['origin_icao']}→{l['destination_icao']})"[:100],
                description=f"PAX: {int(l.get('allocated_pax') or 0):,} · Status: {l['status']}"[:100],
                value=str(l["schedule_id"]),
            )
            for l in legs[:25]
        ]
        select = discord.ui.Select(
            placeholder="Select your Christmas leg to file…",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the filing pilot can use this menu.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        sched_id = interaction.data["values"][0]
        leg = next((l for l in self.legs if str(l["schedule_id"]) == sched_id), None)
        if not leg:
            await interaction.edit_original_response(content="❌ Leg not found.", embed=None, view=None)
            return

        await self.cog._open_cpsc_confirm(
            interaction=interaction,
            caller_pilot=self.caller_pilot,
            leg=leg,
            copilot_override=self.copilot_override,
            edit_original=True,
        )


class CpscConfirmView(discord.ui.View):
    def __init__(
        self,
        cog: "ChristmasOperationsCog",
        caller_pilot: Dict[str, Any],
        owner_discord_id: int,
        leg: Dict[str, Any],
        total_minutes: Optional[int],
        duration_source: str,
        copilot_override: Optional[Dict[str, Any]],
    ):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.cog = cog
        self.caller_pilot = caller_pilot
        self.owner_discord_id = owner_discord_id
        self.leg = leg
        self.total_minutes = total_minutes
        self.duration_source = duration_source
        self.copilot_override = copilot_override
        self.submitted = False
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self.submit_btn.disabled = self.submitted or not self.total_minutes or self.total_minutes <= 0
        self.edit_btn.disabled = self.submitted

    def build_embed(self) -> discord.Embed:
        service = self.cog.bot.christmas_service
        flight_num = service.format_flight_num(
            self.leg["registration"],
            self.leg["dispatch_code"],
            int(self.leg["leg_number"]),
        )
        cop_callsign = (
            self.copilot_override.get("callsign")
            if self.copilot_override
            else self.leg.get("copilot_callsign")
        )
        dual_pilot = bool(cop_callsign)
        if self.total_minutes and self.total_minutes > 0:
            p1_m, p2_m = service.split_duration_minutes(self.total_minutes, dual_pilot=dual_pilot)
            dur_text = f"**{service.format_hhmm(self.total_minutes)}** *({self.duration_source})*"
            if dual_pilot:
                dur_text += f"\n└ Split 50/50: `{self.caller_pilot.get('callsign')}` **{service.format_hhmm(p1_m)}** · `{cop_callsign}` **{service.format_hhmm(p2_m)}**"
        else:
            dur_text = "⚠️ **Not auto-detected** — click **✏️ Edit Duration** to enter HH:MM."

        pax = int(self.leg.get("allocated_pax") or 0)
        tickets_note = (
            f"**{pax:,} ChristmasTickets** on approval"
            + (" *(50/50 split)*" if dual_pilot else "")
            if pax > 0
            else "**0 Tickets** *(empty ferry leg — still earns VA hours)*"
        )
        mult_label = service.get_christmas_multiplier()["multiplier_label"]
        embed = discord.Embed(
            title="🎄 Confirm `/cpsc` Christmas PIREP",
            description=(
                f"**Flight Number:** `{flight_num}`\n"
                f"**Route:** `{self.leg['origin_icao']} → {self.leg['destination_icao']}` (Leg {self.leg['leg_number']})\n"
                f"**Duration:** {dur_text}\n"
                f"**CrewCenter Aircraft ID:** `11` • **Multiplier:** **{mult_label}**\n"
                f"**Tickets:** {tickets_note}"
            ),
            color=0x8A1538,
        )
        return embed

    @discord.ui.button(label="✅ Submit PIREP", style=discord.ButtonStyle.success)
    async def submit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the filing pilot can submit this PIREP.", ephemeral=True)
            return
        if not self.total_minutes or self.total_minutes <= 0:
            await interaction.response.send_message("⚠️ Enter flight duration first.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        self.submitted = True
        self._refresh_buttons()

        res = await self.cog.bot.christmas_service.submit_cpsc_pirep(
            caller_pilot=self.caller_pilot,
            caller_discord_id=self.owner_discord_id,
            leg=self.leg,
            total_minutes=int(self.total_minutes),
            copilot_override=self.copilot_override,
        )
        if not res.get("ok"):
            self.submitted = False
            self._refresh_buttons()
            await interaction.edit_original_response(
                content=f"❌ {res.get('error') or 'Submission failed.'}",
                embed=self.build_embed(),
                view=self,
            )
            return

        p1 = res["pilot1"]
        lines = [
            f"**Flight:** `{res['flight_num']}` (`{res['origin_icao']} → {res['destination_icao']}`)",
            f"**Linked CrewCenter PIREP:** `#{res['pirep_id']}` *(Pending Staff Review)*",
            f"**Pilot 1:** `{p1['callsign']}` — **{p1['duration']}** · **{p1['multiplier_label']}**",
        ]
        if res.get("pilot2"):
            p2 = res["pilot2"]
            p2_status = "✅" if p2.get("ok") else f"⚠️ ({p2.get('error')})"
            lines.append(f"**Pilot 2:** `{p2['callsign']}` — **{p2['duration']}** · **{p2['multiplier_label']}** {p2_status}")

        lines.append("\nYour **Reward Ticket PNG & ChristmasTickets** will be DMed to you once staff approve the PIREP!")
        embed = discord.Embed(
            title="✅ Christmas PIREP Submitted!",
            description="\n".join(lines),
            color=0x2ECC71,
        )
        await interaction.edit_original_response(content=None, embed=embed, view=None)
        await self.cog.post_log_message(
            f"📝 **`/cpsc` PIREP Filed:** `{res['flight_num']}` (`{res['origin_icao']} → {res['destination_icao']}`) "
            f"— `PIREP #{res['pirep_id']}` by `{p1['callsign']}`"
        )

    @discord.ui.button(label="✏️ Edit Duration", style=discord.ButtonStyle.secondary)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("Only the filing pilot can edit this PIREP.", ephemeral=True)
            return
        await interaction.response.send_modal(CpscDurationModal(self))


class ChristmasLiveBoardView(discord.ui.View):
    """Persistent Live Operations Board buttons."""

    def __init__(self, cog: "ChristmasOperationsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="🛫 Dispatch Flight",
        style=discord.ButtonStyle.success,
        custom_id="xmas_board:dispatch",
    )
    async def dispatch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.handle_dispatch_click(interaction)

    @discord.ui.button(
        label="🔄 Claim Open Return Leg",
        style=discord.ButtonStyle.primary,
        custom_id="xmas_board:claim_open",
    )
    async def claim_open_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.handle_claim_open_click(interaction)

    @discord.ui.button(
        label="🎟️ My Tickets",
        style=discord.ButtonStyle.secondary,
        custom_id="xmas_board:my_tickets",
    )
    async def my_tickets_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.handle_my_tickets_click(interaction)

    @discord.ui.button(
        label="📋 My Dispatches",
        style=discord.ButtonStyle.secondary,
        custom_id="xmas_board:my_dispatches",
    )
    async def my_dispatches_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.handle_my_dispatches_click(interaction)


class ChristmasOperationsCog(commands.Cog):
    """Live Board, Dispatch Flow, `/cpsc`, `/xmas-admin`, and 2-min sync loop."""

    xmas_admin = app_commands.Group(
        name="xmas-admin",
        description="Admin controls for QRV Christmas Operations 2026",
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._table_ensured = False
        self.board_message: Optional[discord.Message] = None
        try:
            self.log_channel_id: Optional[int] = int(os.getenv("CANDY_LOGS_CHANNEL_ID", "0") or 0) or None
        except ValueError:
            self.log_channel_id = None

        self.persistent_view = ChristmasLiveBoardView(self)
        self.bot.add_view(self.persistent_view)
        self.sync_loop.start()

    def cog_unload(self):
        self.sync_loop.cancel()

    async def _ensure_table_once(self) -> None:
        if self._table_ensured:
            return
        db = getattr(self.bot, "db_manager", None)
        if db is None or getattr(db, "_pool", None) is None:
            return
        await self.bot.christmas_model.ensure_table()
        self._table_ensured = True

    async def post_log_message(self, content: str) -> None:
        if not self.log_channel_id:
            return
        channel = self.bot.get_channel(self.log_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.log_channel_id)
            except Exception:
                return
        try:
            await channel.send(content)
        except Exception as exc:
            log.warning("Failed posting Christmas log message: %s", exc)

    async def _send_reward_dms(self, dm_notifications: List[Dict[str, Any]]) -> None:
        """Send PIREP-Approved v7 Reward Ticket PNG + Ticket balance DM to credited pilots."""
        for note in dm_notifications or []:
            discord_id = note.get("discord_id")
            if not discord_id:
                continue
            try:
                user = self.bot.get_user(int(discord_id))
                if user is None:
                    user = await self.bot.fetch_user(int(discord_id))
                if user is None:
                    continue

                dispatch_code = note["dispatch_code"]
                leg_num = int(note["leg_number"])
                tickets = int(note["tickets_earned"])
                balance = int(note["new_balance"])
                split_note = " *(50/50 dual-pilot split)*" if note.get("dual_pilot") else ""

                embed = discord.Embed(
                    title=f"🎄 PIREP Approved · {dispatch_code} Leg {leg_num}",
                    description=(
                        f"Thanks for flying Qatar Virtual Christmas Operations 2026!\n\n"
                        f"**Route:** `{note['origin_icao']}` ({note['origin_city']}) → "
                        f"`{note['destination_icao']}` ({note['destination_city']})\n"
                        f"**Flight:** `{note['flight_number']}` • **PIREP:** `#{note['pirep_id']}`\n"
                        f"**Reward Credited:** **+{tickets:,} ChristmasTickets**{split_note}\n"
                        f"**Total Balance:** **{balance:,} ChristmasTickets**\n\n"
                        "*Can be exchanged for Cookies once the Christmas Shop deploys!*"
                    ),
                    color=0x1E7E34,
                )

                files: List[discord.File] = []
                png_bytes = note.get("png_bytes")
                if png_bytes:
                    fname = f"christmas_ticket_{dispatch_code}_L{leg_num}.png"
                    files.append(discord.File(io.BytesIO(png_bytes), filename=fname))
                    embed.set_image(url=f"attachment://{fname}")

                await user.send(
                    content=(
                        f"🎟️ Thanks for flying! Here are **+{tickets:,} ChristmasTickets** "
                        "*(exchangeable in the Shop for Cookies once deployed)*."
                    ),
                    embed=embed,
                    files=files,
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.info(
                    "Could not DM Christmas Reward Ticket to Discord ID %s (%s): %s",
                    discord_id,
                    note.get("dispatch_code"),
                    exc,
                )
            except Exception:
                log.exception(
                    "Unexpected error sending Christmas Reward Ticket DM to Discord ID %s",
                    discord_id,
                )

    def _build_board_embed(self, summary: Dict[str, Any]) -> discord.Embed:
        cfg = self.bot.christmas_service.load_config()
        branding = cfg.get("ui_branding") or {}
        title = str(branding.get("board_title") or "🎄 QRV CHRISTMAS OPERATIONS 2026")
        color = _parse_hex_color(branding.get("board_color_hex"), 0x8A1538)

        target = int(summary.get("target_pax") or 5000)
        east_collected = int(summary.get("east_collected") or 0)
        east_reserved = int(summary.get("east_reserved") or 0)
        east_available = int(summary.get("east_available") or 0)
        othh_pool = int(summary.get("othh_pool") or 0)
        othh_reserved = int(summary.get("othh_reserved") or 0)
        othh_available = int(summary.get("othh_available") or 0)
        west_delivered = int(summary.get("west_delivered") or 0)

        overall_pct = (west_delivered / target * 100.0) if target > 0 else 0.0
        east_pct = (east_collected / target * 100.0) if target > 0 else 0.0

        embed = discord.Embed(
            title=title,
            description=(
                "18-Day Holiday Airlift • **East Collection → Doha (`OTHH`) → West Distribution**\n"
                "Click **🛫 Dispatch Flight** to book a 2-leg charter from `OTHH`, and file via **`/cpsc`** after landing."
            ),
            color=color,
        )
        if branding.get("banner_url") or branding.get("board_banner_url"):
            embed.set_image(url=branding.get("board_banner_url") or branding.get("banner_url"))
        if branding.get("board_thumbnail_url"):
            embed.set_thumbnail(url=branding["board_thumbnail_url"])

        embed.add_field(
            name="🎯 Overall Airlift Goal (Delivered West)",
            value=f"`{_progress_bar(overall_pct)}` **{overall_pct:.1f}%**\n**{west_delivered:,} / {target:,} PAX** delivered to West",
            inline=False,
        )
        embed.add_field(
            name="🟢 East Collection (→ OTHH)",
            value=(
                f"`{_progress_bar(east_pct)}` **{east_pct:.1f}%**\n"
                f"Collected: **{east_collected:,}** • In Transit: **{east_reserved:,}** • Remaining: **{east_available:,}**"
            ),
            inline=False,
        )
        embed.add_field(
            name="🇶🇦 OTHH Hub Transit Pool",
            value=(
                f"Available to Dispatch West: **{othh_available:,} PAX**\n"
                f"Reserved Outbound: **{othh_reserved:,} PAX** • Total at Hub: **{othh_pool:,} PAX**"
            ),
            inline=False,
        )
        embed.add_field(
            name="✈️ Live Fleet Activity",
            value=(
                f"Active Dispatches: **{summary.get('active_dispatches', 0)}** "
                f"({summary.get('active_legs', 0)} active legs)\n"
                f"Open Return Legs: **{summary.get('open_return_legs', 0)}** • "
                f"Pending PIREPs: **{summary.get('pending_pireps', 0)}**"
            ),
            inline=False,
        )

        top_holders = summary.get("top_holders") or []
        if top_holders:
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            lb_lines = [
                f"{medals[idx]} **{row['callsign']}** — {int(row['total_balance']):,} Tickets"
                for idx, row in enumerate(top_holders[:5])
            ]
            embed.add_field(name="🎟️ Top Ticket Holders", value="\n".join(lb_lines), inline=False)

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        embed.set_footer(text=f"Updates upon CrewCenter PIREP approval • Last synced {now_utc}")
        return embed

    async def refresh_live_board(self) -> Optional[discord.Message]:
        """Find or create the pinned Live Operations Board and update its embed."""
        cfg = self.bot.christmas_service.load_config()
        channel_id = int(cfg.get("live_board_channel_id") or 0)
        if not channel_id:
            return None

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception as exc:
                log.warning("Could not resolve Christmas board channel %s: %s", channel_id, exc)
                return None

        board_title = str((cfg.get("ui_branding") or {}).get("board_title") or "🎄 QRV CHRISTMAS OPERATIONS 2026")
        if self.board_message is None:
            try:
                for msg in await channel.pins():
                    if msg.author == self.bot.user and msg.embeds and msg.embeds[0].title == board_title:
                        self.board_message = msg
                        break
            except Exception as exc:
                log.warning("Could not scan pins in channel %s: %s", channel_id, exc)

        summary = await self.bot.christmas_service.get_pool_summary()
        embed = self._build_board_embed(summary)

        if self.board_message is not None:
            try:
                await self.board_message.edit(embed=embed, view=self.persistent_view)
                return self.board_message
            except discord.NotFound:
                self.board_message = None
            except Exception as exc:
                log.warning("Failed editing Christmas Live Board: %s", exc)
                return self.board_message

        msg = await channel.send(embed=embed, view=self.persistent_view)
        try:
            await msg.pin()
        except Exception as exc:
            log.warning("Could not pin Christmas Live Board message: %s", exc)
        self.board_message = msg
        return msg

    async def _dispatch_sync_outputs(
        self,
        summary: Dict[str, Any],
        *,
        refresh_if_unpinned: bool = False,
    ) -> None:
        """Deliver channel logs, Reward Ticket DMs, and Live Board refreshes."""
        for msg in summary.get("log_events") or []:
            await self.post_log_message(msg)
        dm_notes = summary.get("dm_notifications") or []
        if dm_notes:
            await self._send_reward_dms(dm_notes)
        if summary.get("board_needs_refresh") or (refresh_if_unpinned and self.board_message is None):
            await self.refresh_live_board()

    @tasks.loop(minutes=2)
    async def sync_loop(self):
        await self.bot.wait_until_ready()
        try:
            await self._ensure_table_once()
            if not self._table_ensured:
                return
            summary = await self.bot.christmas_service.run_sync_cycle()
            await self._dispatch_sync_outputs(summary, refresh_if_unpinned=True)
        except Exception:
            log.exception("Unhandled error in ChristmasOperationsCog sync_loop")

    async def handle_dispatch_click(self, interaction: discord.Interaction) -> None:
        pilot, error = await self.bot.christmas_service.resolve_pilot_by_discord_id(interaction.user.id)
        if error or not pilot:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        max_dispatches = int(self.bot.christmas_service.load_config().get("max_active_dispatches_per_pilot") or 2)
        active_count = await self.bot.christmas_model.count_active_dispatches_for_pilot(int(pilot["id"]))
        if active_count >= max_dispatches:
            await interaction.followup.send(
                f"⛔ You already have **{active_count}/{max_dispatches}** active Christmas dispatches.",
                ephemeral=True,
            )
            return

        frames, f_err = await self.bot.christmas_service.get_dispatchable_frames_at_hub()
        if f_err:
            await interaction.followup.send(f"❌ {f_err}", ephemeral=True)
            return
        if not frames:
            await interaction.followup.send(
                "No eligible Qatar Airways passenger frames are currently parked & uncrewed at `OTHH`.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="✈️ Step 1: Select Available Aircraft at OTHH",
            description=f"Found **{len(frames)}** eligible Qatar Airways passenger frame(s) parked at `OTHH`.",
            color=0x8A1538,
        )
        view = FrameSelectView(self, pilot, interaction.user.id, frames)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def handle_claim_open_click(self, interaction: discord.Interaction) -> None:
        pilot, error = await self.bot.christmas_service.resolve_pilot_by_discord_id(interaction.user.id)
        if error or not pilot:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        max_dispatches = int(self.bot.christmas_service.load_config().get("max_active_dispatches_per_pilot") or 2)
        active_count = await self.bot.christmas_model.count_active_dispatches_for_pilot(int(pilot["id"]))
        if active_count >= max_dispatches:
            await interaction.followup.send(
                f"⛔ You already have **{active_count}/{max_dispatches}** active Christmas dispatches.",
                ephemeral=True,
            )
            return

        open_legs = await self.bot.christmas_model.get_open_return_legs()
        if not open_legs:
            await interaction.followup.send("There are currently no open return legs awaiting a pilot.", ephemeral=True)
            return

        view = OpenReturnSelectView(self, pilot, interaction.user.id, open_legs)
        embed = discord.Embed(
            title="🔄 Claim an Open Christmas Return Leg",
            description="Select an open Leg 2 return flight to `OTHH` below:",
            color=0x1F6FEB,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def handle_my_tickets_click(self, interaction: discord.Interaction) -> None:
        pilot, error = await self.bot.christmas_service.resolve_pilot_by_discord_id(interaction.user.id)
        if error or not pilot:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        info = await self.bot.christmas_service.get_pilot_ticket_summary(int(pilot["id"]))
        lb_lines = [
            f"`#{i}` **{r['callsign']}** — {int(r['total_balance']):,} Tickets"
            for i, r in enumerate(info["top_holders"], 1)
        ] or ["No Tickets earned yet."]
        recent_lines = [
            f"• `{int(r['amount']):+}` — {r['reason']}"
            for r in info["recent"]
        ] or ["No Ticket transactions yet."]

        embed = discord.Embed(
            title=f"🎟️ ChristmasTickets · {pilot.get('callsign')}",
            description=f"**Current Balance:** **{info['balance']:,} ChristmasTickets**",
            color=0x8A1538,
        )
        embed.add_field(name="🏆 Leaderboard (Top 10)", value="\n".join(lb_lines), inline=False)
        embed.add_field(name="🧾 Your Recent Activity", value="\n".join(recent_lines), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def handle_my_dispatches_click(self, interaction: discord.Interaction) -> None:
        pilot, error = await self.bot.christmas_service.resolve_pilot_by_discord_id(interaction.user.id)
        if error or not pilot:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        legs = await self.bot.christmas_model.get_active_dispatches_for_pilot(int(pilot["id"]))
        if not legs:
            await interaction.followup.send("You have no active Christmas dispatches.", ephemeral=True)
            return

        lines = []
        for leg in legs[:15]:
            pirep_label = f"PIREP #{leg['pirep_id']}" if leg.get("pirep_id") else "Not filed (`/cpsc`)"
            lines.append(
                f"• **{leg['dispatch_code']} L{leg['leg_number']}** (`{leg['registration']}`) — "
                f"`{leg['origin_icao']} → {leg['destination_icao']}` · "
                f"**{int(leg.get('allocated_pax') or 0):,} PAX** · {leg['status']} · {pirep_label}"
            )
        embed = discord.Embed(
            title=f"📋 Active Christmas Dispatches · {pilot.get('callsign')}",
            description="\n".join(lines),
            color=0x8A1538,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _open_cpsc_confirm(
        self,
        interaction: discord.Interaction,
        caller_pilot: Dict[str, Any],
        leg: Dict[str, Any],
        copilot_override: Optional[Dict[str, Any]],
        edit_original: bool = False,
    ) -> None:
        if copilot_override is None:
            leg = await self.bot.christmas_service.enrich_leg_with_recovery_copilot(leg)
        cop_discord = None
        if copilot_override and str(copilot_override.get("discordid") or "").isdigit():
            cop_discord = int(copilot_override["discordid"])
        elif leg.get("copilot_discordid") and str(leg.get("copilot_discordid")).isdigit():
            cop_discord = int(leg["copilot_discordid"])

        detected = await self.bot.christmas_service.detect_leg_duration(
            pilot_discord_id=interaction.user.id,
            copilot_discord_id=cop_discord,
            origin_icao=leg["origin_icao"],
            destination_icao=leg["destination_icao"],
        )
        total_minutes = detected["total_minutes"] if detected else None
        source = "Auto-detected from IF Logbook" if detected else "Not Detected"
        view = CpscConfirmView(
            self,
            caller_pilot,
            interaction.user.id,
            leg,
            total_minutes,
            source,
            copilot_override,
        )
        if edit_original:
            await interaction.edit_original_response(content=None, embed=view.build_embed(), view=view)
        else:
            await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)

    @app_commands.command(name="cpsc", description="File your Christmas Operations 2026 PIREP")
    @app_commands.describe(copilot="Optional co-pilot if you flew this leg together")
    async def cpsc_command(
        self,
        interaction: discord.Interaction,
        copilot: Optional[discord.Member] = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        caller_pilot, err = await self.bot.christmas_service.resolve_pilot_by_discord_id(interaction.user.id)
        if err or not caller_pilot:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return

        copilot_override: Optional[Dict[str, Any]] = None
        if copilot is not None:
            if copilot.id == interaction.user.id:
                await interaction.followup.send("❌ You cannot select yourself as co-pilot.", ephemeral=True)
                return
            copilot_override, cop_err = await self.bot.christmas_service.resolve_pilot_by_discord_id(copilot.id)
            if cop_err or not copilot_override:
                await interaction.followup.send(f"❌ Co-pilot error: {cop_err}", ephemeral=True)
                return

        unfiled = await self.bot.christmas_model.get_unfiled_legs_for_pilot(int(caller_pilot["id"]))
        if not unfiled:
            await interaction.followup.send(
                "You have no unfiled Christmas dispatch legs awaiting `/cpsc`.",
                ephemeral=True,
            )
            return

        caller_id = int(caller_pilot["id"])
        captain_legs = [
            l for l in unfiled if l.get("pilot_id") is None or int(l["pilot_id"]) == caller_id
        ]
        if not captain_legs:
            sample = unfiled[0]
            captain_label = sample.get("pilot_callsign") or f"Pilot #{sample.get('pilot_id')}"
            await interaction.followup.send(
                f"ℹ️ You are listed as Co-pilot on `{sample['dispatch_code']}` Leg {sample['leg_number']}. "
                f"Only the Captain (`{captain_label}`) files `/cpsc` for both crew members.",
                ephemeral=True,
            )
            return

        if len(captain_legs) == 1:
            await self._open_cpsc_confirm(
                interaction=interaction,
                caller_pilot=caller_pilot,
                leg=captain_legs[0],
                copilot_override=copilot_override,
                edit_original=False,
            )
            return

        view = CpscLegSelectView(self, caller_pilot, interaction.user.id, captain_legs, copilot_override)
        embed = discord.Embed(
            title="🎄 Select Christmas Leg for `/cpsc`",
            description="Select which completed/active Christmas leg you want to file a PIREP for:",
            color=0x8A1538,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @xmas_admin.command(name="refresh_board", description="Create or refresh the Live Operations Board")
    async def admin_refresh_board(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._ensure_table_once()
        msg = await self.refresh_live_board()
        if msg:
            await interaction.followup.send(f"✅ Live Board updated: {msg.jump_url}", ephemeral=True)
        else:
            await interaction.followup.send("❌ Could not locate Live Board channel.", ephemeral=True)

    @xmas_admin.command(name="status", description="Show Christmas Ops pool and dispatch summary")
    async def admin_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._ensure_table_once()
        summary = await self.bot.christmas_service.get_pool_summary()
        embed = self._build_board_embed(summary)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @xmas_admin.command(name="sync_now", description="Run the Christmas Ops sync & PIREP poller immediately")
    async def admin_sync_now(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._ensure_table_once()
        summary = await self.bot.christmas_service.run_sync_cycle()
        await self._dispatch_sync_outputs(summary, refresh_if_unpinned=False)
        dm_count = len(summary.get("dm_notifications") or [])
        await interaction.followup.send(
            f"✅ Sync complete — Arrived: {summary['arrived_updated']}, "
            f"Cancelled: {summary['cancelled_dispatches']}, "
            f"Reopened: {summary['reopened_legs']}, "
            f"PIREPs Approved: {summary['approved_legs']}, "
            f"PIREPs Rejected: {summary['rejected_legs']}, "
            f"Reward DMs: {dm_count}.",
            ephemeral=True,
        )

    @xmas_admin.command(name="cancel_dispatch", description="Cancel a Christmas dispatch and free reserved PAX")
    @app_commands.describe(dispatch_code="Dispatch code e.g. XMAS-0001")
    async def admin_cancel_dispatch(self, interaction: discord.Interaction, dispatch_code: str):
        await interaction.response.defer(ephemeral=True)
        res = await self.bot.christmas_service.admin_cancel_dispatch(dispatch_code)
        if not res.get("ok"):
            await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True)
            return
        await self.post_log_message(
            f"🛑 **Admin Cancelled Dispatch:** `{res['dispatch_code']}` by {interaction.user.mention}"
        )
        await self.refresh_live_board()
        await interaction.followup.send(
            f"✅ Cancelled `{res['dispatch_code']}` ({res['cancelled_rows']} row(s) updated).",
            ephemeral=True,
        )

    @xmas_admin.command(name="adjust_tickets", description="Add or remove ChristmasTickets for a pilot")
    @app_commands.describe(
        pilot="Target Discord member",
        amount="Positive to add, negative to deduct",
        reason="Reason for adjustment",
    )
    async def admin_adjust_tickets(
        self,
        interaction: discord.Interaction,
        pilot: discord.Member,
        amount: int,
        reason: str,
    ):
        await interaction.response.defer(ephemeral=True)
        if amount == 0:
            await interaction.followup.send("❌ Amount cannot be 0.", ephemeral=True)
            return
        pilot_row, err = await self.bot.christmas_service.resolve_pilot_by_discord_id(pilot.id)
        if err or not pilot_row:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return
        ok = await self.bot.christmas_service.admin_adjust_tickets(
            int(pilot_row["id"]),
            amount,
            f"Admin ({interaction.user.display_name}): {reason}",
        )
        if not ok:
            await interaction.followup.send("❌ Failed saving Ticket adjustment.", ephemeral=True)
            return
        await self.post_log_message(
            f"🎟️ **Admin Ticket Adjustment:** `{pilot_row['callsign']}` `{amount:+}` Tickets — {reason} (by {interaction.user.mention})"
        )
        await self.refresh_live_board()
        await interaction.followup.send(
            f"✅ Applied `{amount:+}` ChristmasTickets to `{pilot_row['callsign']}`.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ChristmasOperationsCog(bot))
