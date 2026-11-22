import unittest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta
import discord
from cogs.activity_check import ActivityCheckCog

class TestActivityCheckCog(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.pilots_model = MagicMock()
        self.bot.db_manager = MagicMock()
        self.cog = ActivityCheckCog(self.bot)

    def test_command_choices_and_role_id(self):
        self.assertEqual(self.cog.FLEET_ROLE_ID, 1484645309983490119)
        self.assertEqual(self.cog.INACTIVE_ROLE_ID, 1224371905012961342)
        self.assertEqual(self.cog.MANAGER_ROLE_ID, 1150344935095152731)
        
        # Verify app command choices
        command = self.cog.activity_check
        action_param = next(p for p in command.parameters if p.name == "action")
        choice_names = [c.name for c in action_param.choices]
        choice_values = [c.value for c in action_param.choices]
        
        self.assertIn("Activity Check(Everyone)", choice_names)
        self.assertIn("Activity Check(Fleet)", choice_names)
        self.assertIn("Check Specific Pilot", choice_names)
        
        self.assertIn("check_everyone", choice_values)
        self.assertIn("check_fleet", choice_values)
        self.assertIn("check_pilot", choice_values)

    async def test_should_pilot_be_inactive_exemptions(self):
        trainee_role = MagicMock()
        loa_role = MagicMock()
        codeshare_rep_role = MagicMock()
        
        member = MagicMock()
        member.roles = []

        # 1. Protected callsign QRV001-019
        result = await self.cog._should_pilot_be_inactive("QRV005", member, trainee_role, loa_role, codeshare_rep_role)
        self.assertEqual(result, "protected")

        result = await self.cog._should_pilot_be_inactive("QRV019", member, trainee_role, loa_role, codeshare_rep_role)
        self.assertEqual(result, "protected")

        # 2. Trainee role
        member.roles = [trainee_role]
        result = await self.cog._should_pilot_be_inactive("QRV050", member, trainee_role, loa_role, codeshare_rep_role)
        self.assertEqual(result, "trainee")

        # 3. LOA role
        member.roles = [loa_role]
        result = await self.cog._should_pilot_be_inactive("QRV050", member, trainee_role, loa_role, codeshare_rep_role)
        self.assertEqual(result, "loa")

        # 4. Codeshare representative
        member.roles = [codeshare_rep_role]
        result = await self.cog._should_pilot_be_inactive("QRV050", member, trainee_role, loa_role, codeshare_rep_role)
        self.assertEqual(result, "codeshare_rep")

    async def test_should_pilot_be_inactive_veteran_and_active(self):
        member = MagicMock()
        member.roles = []
        
        self.bot.pilots_model.get_pilot_by_callsign = AsyncMock(return_value={'id': 42, 'callsign': 'QRV100'})
        
        # Veteran (5000+ hours)
        self.cog._get_pilot_flight_hours = AsyncMock(return_value=5500.0)
        result = await self.cog._should_pilot_be_inactive("QRV100", member, None, None, None)
        self.assertEqual(result, "veteran")

        # Active (within 30 days)
        self.cog._get_pilot_flight_hours = AsyncMock(return_value=120.0)
        self.cog._get_pilot_last_pirep_date = AsyncMock(return_value=datetime.now().date() - timedelta(days=10))
        result = await self.cog._should_pilot_be_inactive("QRV100", member, None, None, None)
        self.assertEqual(result, "active")

        # Inactive (> 30 days)
        self.cog._get_pilot_last_pirep_date = AsyncMock(return_value=datetime.now().date() - timedelta(days=45))
        result = await self.cog._should_pilot_be_inactive("QRV100", member, None, None, None)
        self.assertEqual(result, True)

    async def test_run_activity_check_fleet_scope_filters_correctly(self):
        interaction = AsyncMock()
        guild = MagicMock()
        interaction.guild = guild
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        fleet_role = MagicMock()
        fleet_role.id = self.cog.FLEET_ROLE_ID
        
        inactive_role = MagicMock()
        inactive_role.id = self.cog.INACTIVE_ROLE_ID
        
        guild.get_role.side_effect = lambda r_id: fleet_role if r_id == self.cog.FLEET_ROLE_ID else inactive_role

        # Member 1: in fleet, QRV101
        m1 = MagicMock(spec=discord.Member)
        m1.bot = False
        m1.display_name = "QRV101 Pilot One"
        m1.roles = [fleet_role]
        m1.remove_roles = AsyncMock()
        m1.add_roles = AsyncMock()

        # Member 2: NOT in fleet, QRV102
        m2 = MagicMock(spec=discord.Member)
        m2.bot = False
        m2.display_name = "QRV102 Pilot Two"
        m2.roles = []
        m2.remove_roles = AsyncMock()
        m2.add_roles = AsyncMock()

        guild.members = [m1, m2]

        self.bot.pilots_model.get_pilot_by_callsign = AsyncMock(return_value={'id': 1, 'callsign': 'QRV101'})
        self.cog._get_pilot_flight_hours = AsyncMock(return_value=10.0)
        self.cog._get_pilot_last_pirep_date = AsyncMock(return_value=datetime.now().date() - timedelta(days=5)) # Active

        status_msg_mock = AsyncMock()
        interaction.followup.send = AsyncMock(return_value=status_msg_mock)

        await self.cog._run_activity_check(interaction, scope="fleet")

        # Verify initial status message was sent for 1 member
        interaction.followup.send.assert_called()
        self.assertIn("1", interaction.followup.send.call_args_list[0][0][0])
        
        # Verify status message was edited with the summary Embed
        status_msg_mock.edit.assert_called()
        edit_kwargs = status_msg_mock.edit.call_args[1]
        embed = edit_kwargs['embed']
        self.assertIn("Fleet", embed.title)
        self.assertEqual(embed.footer.text, "Total members scanned: 1 | Pilots found: 1")

    async def test_run_activity_check_batch_logging(self):
        interaction = AsyncMock()
        guild = MagicMock()
        interaction.guild = guild
        interaction.response.defer = AsyncMock()
        status_msg_mock = AsyncMock()
        interaction.followup.send = AsyncMock(return_value=status_msg_mock)

        inactive_role = MagicMock()
        inactive_role.id = self.cog.INACTIVE_ROLE_ID
        guild.get_role.side_effect = lambda r_id: inactive_role if r_id == self.cog.INACTIVE_ROLE_ID else None

        # Pilot 1: Newly inactive (does not have inactive role, last PIREP > 30 days)
        m1 = MagicMock(spec=discord.Member)
        m1.bot = False
        m1.mention = "<@111>"
        m1.display_name = "QRV201 Newly Inactive"
        m1.roles = []
        m1.add_roles = AsyncMock()
        m1.remove_roles = AsyncMock()

        # Pilot 2: Already inactive (already has inactive role, last PIREP > 30 days)
        m2 = MagicMock(spec=discord.Member)
        m2.bot = False
        m2.mention = "<@222>"
        m2.display_name = "QRV202 Already Inactive"
        m2.roles = [inactive_role]
        m2.add_roles = AsyncMock()
        m2.remove_roles = AsyncMock()

        # Pilot 3: Reactivated (has inactive role, but recent PIREP)
        m3 = MagicMock(spec=discord.Member)
        m3.bot = False
        m3.mention = "<@333>"
        m3.display_name = "QRV203 Reactivated"
        m3.roles = [inactive_role]
        m3.add_roles = AsyncMock()
        m3.remove_roles = AsyncMock()

        # Member 4: Missing callsign
        m4 = MagicMock(spec=discord.Member)
        m4.bot = False
        m4.mention = "<@444>"
        m4.display_name = "JustUser"
        m4.roles = []

        guild.members = [m1, m2, m3, m4]

        async def mock_get_pilot(callsign):
            if callsign == "QRV201":
                return {'id': 201, 'callsign': 'QRV201'}
            elif callsign == "QRV202":
                return {'id': 202, 'callsign': 'QRV202'}
            elif callsign == "QRV203":
                return {'id': 203, 'callsign': 'QRV203'}
            return None

        self.bot.pilots_model.get_pilot_by_callsign = AsyncMock(side_effect=mock_get_pilot)
        self.bot.pilots_model.get_pilot_by_discord_id = AsyncMock(return_value=None)
        self.cog._get_pilot_flight_hours = AsyncMock(return_value=10.0)

        async def mock_last_pirep(pilot_id):
            if pilot_id == 201:
                return datetime.now().date() - timedelta(days=45)
            elif pilot_id == 202:
                return datetime.now().date() - timedelta(days=60)
            elif pilot_id == 203:
                return datetime.now().date() - timedelta(days=5)
            return None

        self.cog._get_pilot_last_pirep_date = AsyncMock(side_effect=mock_last_pirep)

        await self.cog._run_activity_check(interaction, scope="everyone")

        # Verify role modifications
        m1.add_roles.assert_called_with(inactive_role, reason="No PIREP in 30+ days")
        m3.remove_roles.assert_called_with(inactive_role, reason="Recent activity")

        # Verify followup batch calls:
        # call 0: initial status message
        # call 1: Newly inactive batch
        # call 2: Already inactive batch
        # call 3: Reactivated batch
        # call 4: Missing callsign batch
        followup_calls = [c[0][0] for c in interaction.followup.send.call_args_list]
        
        self.assertTrue(any("🔴 Newly Inactive Pilots" in call and "<@111>" in call for call in followup_calls))
        self.assertTrue(any("⚠️ Already Inactive Pilots" in call and "<@222>" in call for call in followup_calls))
        self.assertTrue(any("🟢 Reactivated Pilots" in call and "<@333>" in call for call in followup_calls))
        self.assertTrue(any("❓ Missing Callsign" in call and "<@444>" in call for call in followup_calls))

    async def test_run_activity_check_missing_fleet_role(self):
        interaction = AsyncMock()
        guild = MagicMock()
        interaction.guild = guild
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        guild.get_role.return_value = None  # Role not found

        await self.cog._run_activity_check(interaction, scope="fleet")

        interaction.followup.send.assert_called_with(
            f"❌ Fleet role (ID: {self.cog.FLEET_ROLE_ID}) not found!",
            ephemeral=True
        )

    def test_manager_role_permission(self):
        check_func = self.cog.activity_check.checks[0]

        # Member with manager role
        manager_role = MagicMock()
        manager_role.id = 1150344935095152731

        member = MagicMock(spec=discord.Member)
        member.roles = [manager_role]
        member.guild_permissions.administrator = False

        interaction = MagicMock(spec=discord.Interaction)
        interaction.guild = MagicMock()
        interaction.user = member

        self.assertTrue(check_func(interaction))

        # Member with admin perm
        admin_member = MagicMock(spec=discord.Member)
        admin_member.roles = []
        admin_member.guild_permissions.administrator = True
        interaction.user = admin_member

        self.assertTrue(check_func(interaction))

        # Member without manager role or admin
        regular_member = MagicMock(spec=discord.Member)
        regular_member.roles = []
        regular_member.guild_permissions.administrator = False
        interaction.user = regular_member

        with self.assertRaises(discord.app_commands.MissingRole):
            check_func(interaction)

if __name__ == "__main__":
    unittest.main()
