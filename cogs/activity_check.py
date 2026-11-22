import discord
from discord.ext import commands
from discord import app_commands
import re
from datetime import datetime, timedelta

MANAGER_ROLE_ID = 1150344935095152731

def is_manager_or_admin():
    def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if any(role.id == MANAGER_ROLE_ID for role in interaction.user.roles):
            return True
        if interaction.user.guild_permissions.administrator:
            return True
        raise app_commands.MissingRole(MANAGER_ROLE_ID)
    return app_commands.check(predicate)

class ActivityCheckCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.INACTIVE_ROLE_ID = 1224371905012961342
        self.TRAINEE_ROLE_ID = 1137998383630524477
        self.LOA_ROLE_ID = 1193185998859935755
        self.CODESHARE_REP_ROLE_ID = 1120952921299894354
        self.FLEET_ROLE_ID = 1484645309983490119
        self.MANAGER_ROLE_ID = MANAGER_ROLE_ID
        self.GRADUATED_RANK_ROLE_IDS = {
            1090755316624408696,  # Cadet
            1090755686104825977,  # Second Officer
            1090755990569357312,  # First Officer
            1090756112158044230,  # Captain
            1090756368631341056,  # Senior Captain
            1090757434617888889,  # Ruby
            1090757602230673448,  # Sapphire
            1090757780085940375,  # Emerald
            1171821033154691182,  # OneWorld
            1308739377739993088,  # Oryx
            1484645309983490119,  # Fleet Role
        }

    @app_commands.command(name="activity_check", description="Manage pilot activity checking")
    @app_commands.describe(
        action="What to do",
        callsign="Pilot callsign (for check_pilot)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Activity Check(Everyone)", value="check_everyone"),
        app_commands.Choice(name="Activity Check(Fleet)", value="check_fleet"),
        app_commands.Choice(name="Check Specific Pilot", value="check_pilot"),
        app_commands.Choice(name="Audit Active Pilots Missing Fleet Role", value="audit_fleet"),
        app_commands.Choice(name="Activity Check (Trainees)", value="check_trainees")
    ])
    @is_manager_or_admin()
    async def activity_check(self, interaction: discord.Interaction, action: str, callsign: str = None):
        if action in ("check_everyone", "run_check"):
            await self._run_activity_check(interaction, scope="everyone")
        elif action == "check_fleet":
            await self._run_activity_check(interaction, scope="fleet")
        elif action == "check_pilot":
            if not callsign:
                await interaction.response.send_message("Callsign is required for pilot check.", ephemeral=True)
                return
            await self._check_specific_pilot(interaction, callsign)
        elif action == "audit_fleet":
            await self._run_audit_active_missing_fleet(interaction)
        elif action == "check_trainees":
            await self._run_trainee_activity_check(interaction)

    async def _run_activity_check(self, interaction: discord.Interaction, scope: str = "everyone"):
        await interaction.response.defer(ephemeral=True)
        
        # Validate dependencies
        if not hasattr(self.bot, 'pilots_model') or not hasattr(self.bot, 'db_manager'):
            await interaction.followup.send("❌ Database services not available!", ephemeral=True)
            return
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ Guild not found!", ephemeral=True)
            return

        # Target members based on scope
        scope_title = "Everyone"
        if scope == "fleet":
            fleet_role = guild.get_role(self.FLEET_ROLE_ID)
            if not fleet_role:
                await interaction.followup.send(f"❌ Fleet role (ID: {self.FLEET_ROLE_ID}) not found!", ephemeral=True)
                return
            members = [member for member in guild.members if fleet_role in member.roles and not member.bot]
            scope_title = "Fleet"
        else:
            members = [member for member in guild.members if not member.bot]
            scope_title = "Everyone"
        
        # Get roles
        inactive_role = guild.get_role(self.INACTIVE_ROLE_ID)
        trainee_role = guild.get_role(self.TRAINEE_ROLE_ID)
        loa_role = guild.get_role(self.LOA_ROLE_ID)
        codeshare_rep_role = guild.get_role(self.CODESHARE_REP_ROLE_ID)
        
        if not inactive_role:
            await interaction.followup.send("❌ Inactive role not found!", ephemeral=True)
            return

        status_message = await interaction.followup.send(
            f"🚀 Starting activity check ({scope_title}) for **{len(members)}** members... This may take a moment."
        )
        
        # Scan members for callsigns
        pilots_found = []
        no_callsign = []
        
        for member in members:
            if member.bot:
                continue
                
            callsign_match = re.search(r'QRV\d{3,}', member.display_name, re.IGNORECASE)
            if callsign_match:
                callsign = callsign_match.group(0).upper()
                pilots_found.append({
                    'member': member,
                    'callsign': callsign,
                    'display_name': member.display_name
                })
            else:
                try:
                    pilot_data = await self.bot.pilots_model.get_pilot_by_discord_id(str(member.id))
                    if pilot_data and pilot_data.get('callsign'):
                        pilots_found.append({
                            'member': member,
                            'callsign': pilot_data['callsign'],
                            'display_name': member.display_name
                        })
                    else:
                        no_callsign.append(member)
                except Exception:
                    no_callsign.append(member)
        
        # Process pilots for activity check
        processed = 0
        removed_inactive = 0
        skipped_protected = 0
        skipped_trainee = 0
        skipped_loa = 0
        skipped_already_inactive = 0
        skipped_veteran = 0
        skipped_active = 0
        skipped_codeshare_rep = 0

        newly_inactive_info = []
        already_inactive_info = []
        reactivated_info = []
        no_callsign_info = []
        
        for pilot_info in pilots_found:
            member = pilot_info['member']
            callsign = pilot_info['callsign']
            has_inactive_role = inactive_role in member.roles
            
            # Determine if pilot should be inactive based on conditions
            should_be_inactive = await self._should_pilot_be_inactive(callsign, member, trainee_role, loa_role, codeshare_rep_role)
            
            if should_be_inactive is None:
                # Database error or pilot not found
                no_callsign_info.append(f"{member.mention} ({callsign}) : pilot record not found in DB")
                continue
            elif should_be_inactive == "protected":
                skipped_protected += 1
                if has_inactive_role:
                    await member.remove_roles(inactive_role, reason="Protected callsign")
                    removed_inactive += 1
                    reactivated_info.append(f"{member.mention} ({callsign}) : reactivated (Protected Callsign)")
            elif should_be_inactive == "trainee":
                skipped_trainee += 1
                if has_inactive_role:
                    await member.remove_roles(inactive_role, reason="Trainee role")
                    removed_inactive += 1
                    reactivated_info.append(f"{member.mention} ({callsign}) : reactivated (Trainee Role)")
            elif should_be_inactive == "loa":
                skipped_loa += 1
                if has_inactive_role:
                    await member.remove_roles(inactive_role, reason="LOA role")
                    removed_inactive += 1
                    reactivated_info.append(f"{member.mention} ({callsign}) : reactivated (LOA Role)")
            elif should_be_inactive == "codeshare_rep":
                skipped_codeshare_rep += 1
                if has_inactive_role:
                    await member.remove_roles(inactive_role, reason="Codeshare representative")
                    removed_inactive += 1
                    reactivated_info.append(f"{member.mention} ({callsign}) : reactivated (Codeshare Rep)")
            elif should_be_inactive == "veteran":
                skipped_veteran += 1
                if has_inactive_role:
                    await member.remove_roles(inactive_role, reason="Veteran status")
                    removed_inactive += 1
                    reactivated_info.append(f"{member.mention} ({callsign}) : reactivated (Veteran 5000+ hrs)")
            elif should_be_inactive == "active":
                skipped_active += 1
                if has_inactive_role:
                    await member.remove_roles(inactive_role, reason="Recent activity")
                    removed_inactive += 1
                    reactivated_info.append(f"{member.mention} ({callsign}) : reactivated (Recent Flight Activity)")
            elif should_be_inactive is True:
                # Should be inactive
                pilot_data = await self.bot.pilots_model.get_pilot_by_callsign(callsign)
                last_pirep_date = await self._get_pilot_last_pirep_date(pilot_data['id']) if pilot_data else None
                days_since = (datetime.now().date() - last_pirep_date).days if last_pirep_date else 'Never'
                last_pirep_str = last_pirep_date.strftime('%Y-%m-%d') if last_pirep_date else 'Never'
                days_desc = f"{days_since} days" if days_since != 'Never' else 'no PIREPs'

                if not has_inactive_role:
                    try:
                        await member.add_roles(inactive_role, reason="No PIREP in 30+ days")
                        newly_inactive_info.append(f"{member.mention} ({callsign}) : marked inactive : no PIREP in {days_desc} (last: {last_pirep_str})")
                        processed += 1
                    except Exception as e:
                        print(f"Error adding inactive role to {callsign}: {e}")
                        newly_inactive_info.append(f"{member.mention} ({callsign}) : failed to add role : {e}")
                else:
                    skipped_already_inactive += 1
                    already_inactive_info.append(f"{member.mention} ({callsign}) : already inactive : no PIREP in {days_desc} (last: {last_pirep_str})")

        for member in no_callsign:
            no_callsign_info.append(f"{member.mention} : no QRV callsign found : {member.display_name}")

        total_skipped = (
            skipped_protected + skipped_trainee + skipped_loa + 
            skipped_already_inactive + skipped_veteran + skipped_active + skipped_codeshare_rep
        )

        embed = discord.Embed(
            title=f"📋 Activity Check Report ({scope_title})",
            description="The activity check process has completed.",
            color=discord.Color.blue()
        )
        embed.add_field(name="🔴 Newly Inactive", value=f"`{processed}` pilots marked inactive.", inline=False)
        embed.add_field(name="⚠️ Already Inactive", value=f"`{skipped_already_inactive}` pilots already had the inactive role.", inline=False)
        embed.add_field(name="🟢 Reactivated", value=f"`{removed_inactive}` pilots had the inactive role removed.", inline=False)
        embed.add_field(name="⏭️ Skipped (Exempt/Active)", value=f"`{total_skipped}` pilots skipped (Protected: {skipped_protected}, Trainees: {skipped_trainee}, LOA: {skipped_loa}, Codeshare: {skipped_codeshare_rep}, Veterans: {skipped_veteran}, Active: {skipped_active}).", inline=False)
        embed.add_field(name="❓ No Callsign", value=f"`{len(no_callsign_info)}` members without a valid callsign.", inline=False)
        embed.set_footer(text=f"Total members scanned: {len(members)} | Pilots found: {len(pilots_found)}")

        await status_message.edit(content="Activity check complete!", embed=embed)

        # Send member info in smaller batches grouped by category to avoid 2000 char limit (same as sync_discord_ids)
        categories = [
            ("🔴 Newly Inactive Pilots", newly_inactive_info),
            ("⚠️ Already Inactive Pilots", already_inactive_info),
            ("🟢 Reactivated Pilots", reactivated_info),
            ("❓ Missing Callsign", no_callsign_info)
        ]

        for category_name, info_list in categories:
            if info_list:
                for i in range(0, len(info_list), 15):
                    batch = info_list[i:i+15]
                    batch_text = "\n".join(batch)
                    batch_num = (i // 15) + 1
                    total_batches = (len(info_list) + 14) // 15
                    try:
                        await interaction.followup.send(
                            f"**{category_name} (Batch {batch_num}/{total_batches}):**\n{batch_text}",
                            ephemeral=True
                        )
                    except discord.HTTPException:
                        truncated_text = batch_text[:1800] + "...\n[Truncated]"
                        await interaction.followup.send(
                            f"**{category_name} (Batch {batch_num}/{total_batches}):**\n{truncated_text}",
                            ephemeral=True
                        )

    async def _run_audit_active_missing_fleet(self, interaction: discord.Interaction):
        """Audits server members to find active pilots (PIREP in last 30 days) who lack the Fleet role."""
        await interaction.response.defer(ephemeral=True)

        if not hasattr(self.bot, 'pilots_model') or not hasattr(self.bot, 'db_manager'):
            await interaction.followup.send("❌ Database services not available!", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ Guild not found!", ephemeral=True)
            return

        fleet_role = guild.get_role(self.FLEET_ROLE_ID)
        if not fleet_role:
            await interaction.followup.send(f"❌ Fleet role (ID: {self.FLEET_ROLE_ID}) not found!", ephemeral=True)
            return

        # Target non-bot members who DO NOT have the Fleet role
        members_without_fleet = [m for m in guild.members if fleet_role not in m.roles and not m.bot]

        status_message = await interaction.followup.send(
            f"🚀 Scanning **{len(members_without_fleet)}** members without the Fleet role for recent flight activity... This may take a moment."
        )

        active_missing_fleet_info = []
        scanned_pilots = 0

        for member in members_without_fleet:
            callsign = None
            callsign_match = re.search(r'QRV\d{3,}', member.display_name, re.IGNORECASE)
            if callsign_match:
                callsign = callsign_match.group(0).upper()
            else:
                try:
                    pilot_by_discord = await self.bot.pilots_model.get_pilot_by_discord_id(str(member.id))
                    if pilot_by_discord and pilot_by_discord.get('callsign'):
                        callsign = pilot_by_discord['callsign']
                except Exception:
                    pass

            if not callsign:
                continue

            scanned_pilots += 1
            pilot_data = await self.bot.pilots_model.get_pilot_by_callsign(callsign)
            if not pilot_data:
                continue

            last_pirep_date = await self._get_pilot_last_pirep_date(pilot_data['id'])
            if last_pirep_date:
                days_since = (datetime.now().date() - last_pirep_date).days
                if days_since < 30:
                    last_pirep_str = last_pirep_date.strftime('%Y-%m-%d')
                    active_missing_fleet_info.append(
                        f"{member.mention} ({callsign}) : Active ({days_since}d ago, last PIREP: {last_pirep_str})"
                    )

        embed = discord.Embed(
            title="📋 Active Pilots Missing Fleet Role",
            description="The following pilots have filed an approved PIREP within the last 30 days but do NOT have the Fleet role.",
            color=discord.Color.gold()
        )
        embed.add_field(
            name="✈️ Active Pilots Without Fleet Role",
            value=f"`{len(active_missing_fleet_info)}` active pilots found.",
            inline=False
        )
        embed.set_footer(
            text=f"Total scanned without Fleet role: {len(members_without_fleet)} | QRV pilots matched: {scanned_pilots}"
        )

        await status_message.edit(content="Audit complete!", embed=embed)

        # Batch send member lines (15 per message to stay safely under 2000 characters)
        if active_missing_fleet_info:
            total_batches = (len(active_missing_fleet_info) + 14) // 15
            for i in range(0, len(active_missing_fleet_info), 15):
                batch = active_missing_fleet_info[i:i + 15]
                batch_text = "\n".join(batch)
                batch_num = (i // 15) + 1
                try:
                    await interaction.followup.send(
                        f"**Active Pilots Missing Fleet Role (Batch {batch_num}/{total_batches}):**\n{batch_text}",
                        ephemeral=True
                    )
                except discord.HTTPException:
                    truncated = batch_text[:1800] + "...\n[Truncated]"
                    await interaction.followup.send(
                        f"**Active Pilots Missing Fleet Role (Batch {batch_num}/{total_batches}):**\n{truncated}",
                        ephemeral=True
                    )

    async def _run_trainee_activity_check(self, interaction: discord.Interaction):
        """Audits members with the Trainee role to identify graduated, inactive, or unregistered members."""
        await interaction.response.defer(ephemeral=True)

        if not hasattr(self.bot, 'pilots_model') or not hasattr(self.bot, 'db_manager'):
            await interaction.followup.send("❌ Database services not available!", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ Guild not found!", ephemeral=True)
            return

        trainee_role = guild.get_role(self.TRAINEE_ROLE_ID)
        if not trainee_role:
            await interaction.followup.send(f"❌ Trainee role (ID: {self.TRAINEE_ROLE_ID}) not found!", ephemeral=True)
            return

        trainee_members = [m for m in guild.members if trainee_role in m.roles and not m.bot]

        status_message = await interaction.followup.send(
            f"🚀 Starting Trainee Activity Check for **{len(trainee_members)}** members with the Trainee role... This may take a moment."
        )

        graduated_info = []
        inactive_info = []
        unregistered_info = []

        for member in trainee_members:
            # 1. Check if member already has higher pilot rank roles or Fleet role
            has_rank_or_fleet = any(role.id in self.GRADUATED_RANK_ROLE_IDS for role in member.roles)

            # 2. Check callsign / DB profile
            callsign = None
            callsign_match = re.search(r'QRV\d{3,}', member.display_name, re.IGNORECASE)
            if callsign_match:
                callsign = callsign_match.group(0).upper()
            else:
                try:
                    pilot_by_discord = await self.bot.pilots_model.get_pilot_by_discord_id(str(member.id))
                    if pilot_by_discord and pilot_by_discord.get('callsign'):
                        callsign = pilot_by_discord['callsign']
                except Exception:
                    pass

            if not callsign:
                # No QRV callsign or DB profile
                if has_rank_or_fleet:
                    graduated_info.append(f"{member.mention} : Graduated (Has pilot rank/fleet role, no callsign)")
                else:
                    unregistered_info.append(f"{member.mention} : No QRV callsign found : {member.display_name}")
                continue

            pilot_data = await self.bot.pilots_model.get_pilot_by_callsign(callsign)
            if not pilot_data:
                if has_rank_or_fleet:
                    graduated_info.append(f"{member.mention} ({callsign}) : Graduated (Has pilot rank/fleet role, no DB record)")
                else:
                    unregistered_info.append(f"{member.mention} ({callsign}) : Pilot record not found in DB")
                continue

            last_pirep_date = await self._get_pilot_last_pirep_date(pilot_data['id'])
            days_since = (datetime.now().date() - last_pirep_date).days if last_pirep_date else None
            last_pirep_str = last_pirep_date.strftime('%Y-%m-%d') if last_pirep_date else 'Never'

            # If member has a higher rank role, or flew recently (< 30 days)
            if has_rank_or_fleet:
                graduated_info.append(
                    f"{member.mention} ({callsign}) : Graduated (Has pilot rank/fleet role, last PIREP: {last_pirep_str})"
                )
            elif days_since is not None and days_since < 30:
                graduated_info.append(
                    f"{member.mention} ({callsign}) : Active Pilot (Last PIREP: {last_pirep_str}, {days_since}d ago - ready for role removal)"
                )
            else:
                days_desc = f"{days_since}d ago" if days_since is not None else "no PIREPs"
                inactive_info.append(
                    f"{member.mention} ({callsign}) : Inactive Trainee (Last PIREP: {last_pirep_str}, {days_desc})"
                )

        embed = discord.Embed(
            title="📋 Trainee Role Audit & Activity Report",
            description=f"Audited **{len(trainee_members)}** members currently assigned the Trainee role.",
            color=discord.Color.orange()
        )
        embed.add_field(
            name="🎓 Graduated / Active (Trainee Role Can Be Removed)",
            value=f"`{len(graduated_info)}` members hold pilot rank/fleet roles or recent flight logs.",
            inline=False
        )
        embed.add_field(
            name="💤 Inactive Trainees",
            value=f"`{len(inactive_info)}` trainees have no PIREPs in 30+ days or never flew.",
            inline=False
        )
        embed.add_field(
            name="❓ Unregistered / No Callsign",
            value=f"`{len(unregistered_info)}` members without valid QRV callsigns or database profiles.",
            inline=False
        )
        embed.set_footer(text=f"Total Trainees scanned: {len(trainee_members)}")

        await status_message.edit(content="Trainee activity check complete!", embed=embed)

        categories = [
            ("🎓 Graduated / Active Trainees (Ready For Trainee Role Removal)", graduated_info),
            ("💤 Inactive Trainees", inactive_info),
            ("❓ Unregistered Trainees", unregistered_info)
        ]

        for category_name, info_list in categories:
            if info_list:
                total_batches = (len(info_list) + 14) // 15
                for i in range(0, len(info_list), 15):
                    batch = info_list[i:i + 15]
                    batch_text = "\n".join(batch)
                    batch_num = (i // 15) + 1
                    try:
                        await interaction.followup.send(
                            f"**{category_name} (Batch {batch_num}/{total_batches}):**\n{batch_text}",
                            ephemeral=True
                        )
                    except discord.HTTPException:
                        truncated = batch_text[:1800] + "...\n[Truncated]"
                        await interaction.followup.send(
                            f"**{category_name} (Batch {batch_num}/{total_batches}):**\n{truncated}",
                            ephemeral=True
                        )
    
    async def _should_pilot_be_inactive(self, callsign: str, member, trainee_role, loa_role, codeshare_rep_role):
        """Determine if pilot should be inactive. Returns True/False/reason string or None for error"""
        try:
            # Check protected callsigns (QRV001-QRV019)
            callsign_num = int(callsign[3:])
            if 1 <= callsign_num <= 19:
                return "protected"
            
            # Check trainee role
            if trainee_role and trainee_role in member.roles:
                return "trainee"
            
            # Check LOA role
            if loa_role and loa_role in member.roles:
                return "loa"
            
            # Check Codeshare representative role
            if codeshare_rep_role and codeshare_rep_role in member.roles:
                return "codeshare_rep"
            
            # Check database conditions
            pilot_data = await self.bot.pilots_model.get_pilot_by_callsign(callsign)
            if not pilot_data:
                return None
            
            # Check if veteran (5000+ hours)
            flight_hours = await self._get_pilot_flight_hours(pilot_data['id'])
            if flight_hours and flight_hours >= 5000:
                return "veteran"
            
            # Check activity (last PIREP within 30 days)
            last_pirep_date = await self._get_pilot_last_pirep_date(pilot_data['id'])
            if last_pirep_date:
                days_since = (datetime.now().date() - last_pirep_date).days
                if days_since < 30:
                    return "active"
            
            # Should be inactive
            return True
            
        except Exception as e:
            print(f"Error checking pilot status for {callsign}: {e}")
            return None

    async def _check_specific_pilot(self, interaction: discord.Interaction, callsign: str):
        await interaction.response.defer(ephemeral=True)
        
        # Format callsign
        if not callsign.upper().startswith('QRV'):
            callsign = f"QRV{callsign}"
        
        # Get pilot data
        pilot = await self.bot.pilots_model.get_pilot_by_callsign(callsign.upper())
        if not pilot:
            await interaction.followup.send(f"❌ Pilot {callsign} not found", ephemeral=True)
            return
        
        # Get activity data
        last_pirep_date = await self._get_pilot_last_pirep_date(pilot['id'])
        flight_hours = await self._get_pilot_flight_hours(pilot['id'])
        
        report = f"Activity Report: {callsign}\n\n"
        report += f"Last PIREP: {last_pirep_date.strftime('%Y-%m-%d') if last_pirep_date else 'Never'}\n"
        report += f"Total Flight Hours: {flight_hours:.1f}\n"
        
        if last_pirep_date:
            days_ago = (datetime.now().date() - last_pirep_date).days
            status = "🟢 Active" if days_ago <= 30 else "🔴 Inactive"
            report += f"Days Since Last PIREP: {days_ago}\n"
            report += f"Status: {status}"
        else:
            report += "Status: 🔴 No PIREPs found"
        
        await interaction.followup.send(report, ephemeral=True)

    async def _get_pilot_flight_hours(self, pilot_id):
        """Get total flight hours for pilot from PIREPs"""
        query = """
        SELECT SUM(flighttime) as total_seconds 
        FROM pireps 
        WHERE pilotid = %s AND status = 1
        """
        result = await self.bot.db_manager.fetch_one(query, (pilot_id,))
        total_seconds = result['total_seconds'] if result and result['total_seconds'] else 0
        return total_seconds / 3600  # Convert seconds to hours

    async def _get_pilot_last_pirep_date(self, pilot_id):
        """Get pilot's most recent PIREP date"""
        query = """
        SELECT DATE(date) as pirep_date 
        FROM pireps 
        WHERE pilotid = %s AND status = 1 
        ORDER BY date DESC 
        LIMIT 1
        """
        result = await self.bot.db_manager.fetch_one(query, (pilot_id,))
        return result['pirep_date'] if result else None

    @activity_check.error
    async def on_activity_check_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, (app_commands.MissingRole, app_commands.MissingPermissions)):
            msg = "You need the Manager role to use this command."
        else:
            msg = f"An error occurred: {error}"
            
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

async def setup(bot):
    await bot.add_cog(ActivityCheckCog(bot))
