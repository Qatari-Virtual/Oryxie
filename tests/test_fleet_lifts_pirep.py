import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from api.if_live_manager import (
    DEFAULT_SCOPES,
    IFLiveForbiddenError,
    IFOrganizationMember,
)
from api.live_fleet_state import (
    LiveFleetService,
    MAX_CACHE_EXPIRY_SECONDS,
    TTL_ORGANIZATION_MEMBERS,
)
from cogs.fleet_pirep_cog import FleetPirepCog, FleetPirepModal
from services.pirep_filing_service import (
    FLEET_LIFT_MULTIPLIER,
    FLEET_NON_LIFT_MULTIPLIER,
    PirepFilingService,
)


def _make_v3_manager(client):
    manager = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    manager.get_client = AsyncMock(return_value=ctx)
    return manager


class FleetLiftsCacheTests(unittest.IsolatedAsyncioTestCase):
    def test_default_scopes_include_live_members_read(self):
        self.assertIn("live:members.read", DEFAULT_SCOPES.split())

    async def test_members_lifts_15_min_cache_hit_refresh_and_1h_discard(self):
        client = AsyncMock()
        client.list_organization_members = AsyncMock(
            return_value=[
                IFOrganizationMember(
                    user_id="IF-USER-1",
                    username="CaptainOne",
                    role=0,
                    joined_at="2026-07-03T12:00:00Z",
                    active_lifts=2,
                ),
                IFOrganizationMember(
                    user_id="IF-USER-2",
                    username="CaptainTwo",
                    role=0,
                    joined_at="2026-07-03T12:00:00Z",
                    active_lifts=0,
                ),
            ]
        )
        v3_manager = _make_v3_manager(client)
        service = LiveFleetService(v3_manager)

        fake_now = [1000.0]
        with patch("api.live_fleet_state.time.monotonic", side_effect=lambda: fake_now[0]):
            # 1. Cold miss -> calls IF v3 API
            snap1 = await service.get_organization_members_lifts("org-1")
            self.assertTrue(snap1["success"])
            self.assertFalse(snap1["cache_hit"])
            self.assertEqual(snap1["ttl_seconds"], 900.0)
            self.assertEqual(snap1["total_members"], 2)
            self.assertEqual(snap1["members_with_lifts"], 1)
            self.assertEqual(snap1["by_user_id"]["if-user-1"]["active_lifts"], 2)
            self.assertEqual(snap1["by_user_id"]["if-user-2"]["active_lifts"], 0)
            client.list_organization_members.assert_awaited_once_with("org-1")

            # 2. Within 15 min (e.g. +899s) -> cache hit, no extra API call
            fake_now[0] += TTL_ORGANIZATION_MEMBERS - 1.0
            snap2 = await service.get_organization_members_lifts("org-1")
            self.assertTrue(snap2["success"])
            self.assertTrue(snap2["cache_hit"])
            self.assertEqual(client.list_organization_members.await_count, 1)

            # 3. Past 15 min (+2s more -> 901s) -> refreshes from API
            fake_now[0] += 2.0
            snap3 = await service.get_organization_members_lifts("org-1")
            self.assertTrue(snap3["success"])
            self.assertFalse(snap3["cache_hit"])
            self.assertEqual(client.list_organization_members.await_count, 2)

            # 4. Past 1 hour (>= 3600s) + API failure -> discards stale >1h data cleanly
            fake_now[0] += MAX_CACHE_EXPIRY_SECONDS + 5.0
            client.list_organization_members.side_effect = RuntimeError("timeout")
            snap4 = await service.get_organization_members_lifts("org-1")
            self.assertFalse(snap4["success"])
            self.assertEqual(snap4["by_user_id"], {})
            self.assertIn("timeout", snap4["error"])

    async def test_forbidden_403_does_not_serve_stale_cache(self):
        client = AsyncMock()
        client.list_organization_members = AsyncMock(
            return_value=[
                IFOrganizationMember(
                    user_id="if-1",
                    username="PilotOne",
                    role=0,
                    joined_at="2026-07-03T12:00:00Z",
                    active_lifts=1,
                )
            ]
        )
        v3_manager = _make_v3_manager(client)
        service = LiveFleetService(v3_manager)

        fake_now = [2000.0]
        with patch("api.live_fleet_state.time.monotonic", side_effect=lambda: fake_now[0]):
            await service.get_organization_members_lifts("org-1")
            fake_now[0] += TTL_ORGANIZATION_MEMBERS + 10.0
            client.list_organization_members.side_effect = IFLiveForbiddenError(
                403, 403, "Missing scope live:members.read"
            )
            res = await service.get_organization_members_lifts("org-1")
            self.assertFalse(res["success"])
            self.assertFalse(res["stale_fallback"])
            self.assertIn("live:members.read", res["error"])


class FleetPirepPerPilotMultiplierTests(unittest.IsolatedAsyncioTestCase):
    def _build_bot(self, members_list, pilot_rows, if_user_lookup=None):
        client = AsyncMock()
        client.list_organization_members = AsyncMock(return_value=members_list)
        v3_manager = _make_v3_manager(client)
        fleet_service = LiveFleetService(v3_manager)

        async def get_org_members_lifts(org_id=None, force_refresh=False, pilot_id=1):
            return await fleet_service.get_organization_members_lifts(
                org_id, force_refresh=force_refresh, pilot_id=pilot_id
            )

        if_live_manager = MagicMock()
        if_live_manager.fleet_service = fleet_service
        if_live_manager.get_organization_members_lifts = AsyncMock(
            side_effect=get_org_members_lifts
        )

        pilots_model = MagicMock()
        pilots_model.get_pilot_by_discord_id = AsyncMock(
            side_effect=lambda did: pilot_rows.get(str(did))
        )
        pilots_model.update_ifuserid_by_id = AsyncMock(return_value=1)
        pilots_model.update_ifuserid_by_ifc_username = AsyncMock(return_value=1)

        cc_api_manager = MagicMock()
        cc_api_manager.submit_pirep = AsyncMock(return_value={"status": 0, "result": None})

        if_api_manager = MagicMock()
        if_api_manager.get_user_by_ifc_username = AsyncMock(return_value=if_user_lookup)

        bot = SimpleNamespace(
            if_live_manager=if_live_manager,
            pilots_model=pilots_model,
            cc_api_manager=cc_api_manager,
            if_api_manager=if_api_manager,
        )
        return bot, client

    async def test_dual_pilot_gets_different_multipliers_and_uses_cache(self):
        members = [
            IFOrganizationMember("uid-p1", "LiftPilot", 0, "2026-07-03T12:00:00Z", active_lifts=2),
            IFOrganizationMember("uid-p2", "FreePilot", 0, "2026-07-03T12:00:00Z", active_lifts=0),
        ]
        pilots = {
            "111": {"id": 10, "callsign": "QRV101", "discordid": "111", "ifuserid": "UID-P1", "ifc": ""},
            "222": {"id": 20, "callsign": "QRV202", "discordid": "222", "ifuserid": "uid-p2", "ifc": ""},
        }
        bot, client = self._build_bot(members, pilots)
        service = PirepFilingService(bot)

        with patch.dict(os.environ, {"IF_ORGANIZATION_ID": "org-qrv"}):
            res = await service.submit_fleet_pirep(
                pilot1_discord_id=111,
                pilot2_discord_id=222,
                frame_data={"name": "A7-BAE"},
                dep_icao="OTHH",
                arr_icao="EGLL",
                total_duration_hours=6,
                total_duration_minutes=30,
                pilot1_name="Pilot One",
                pilot2_name="Pilot Two",
            )
            # Second submission within 15 minutes must reuse cached member lifts
            res2 = await service.submit_fleet_pirep(
                pilot1_discord_id=222,
                pilot2_discord_id=111,
                frame_data={"name": "A7-BCB"},
                dep_icao="EGLL",
                arr_icao="OTHH",
                total_duration_hours=6,
                total_duration_minutes=30,
                pilot1_name="Pilot Two",
                pilot2_name="Pilot One",
            )

        self.assertTrue(res["success"])
        self.assertIsNone(res["lift_lookup_warning"])
        self.assertEqual(res["pilot1_result"]["multiplier"], FLEET_LIFT_MULTIPLIER)
        self.assertEqual(res["pilot1_result"]["multiplier_label"], "2x")
        self.assertEqual(res["pilot1_result"]["active_lifts"], 2)
        self.assertEqual(res["pilot2_result"]["multiplier"], FLEET_NON_LIFT_MULTIPLIER)
        self.assertEqual(res["pilot2_result"]["multiplier_label"], "1.5x")
        self.assertEqual(res["pilot2_result"]["active_lifts"], 0)

        # Reverse order on second flight
        self.assertEqual(res2["pilot1_result"]["multiplier"], FLEET_NON_LIFT_MULTIPLIER)
        self.assertEqual(res2["pilot2_result"]["multiplier"], FLEET_LIFT_MULTIPLIER)

        # IF v3 members endpoint only hit ONCE across both dual-pilot PIREPs
        client.list_organization_members.assert_awaited_once_with("org-qrv")

        calls = bot.cc_api_manager.submit_pirep.await_args_list
        self.assertEqual(calls[0].kwargs["multiplier"], "200000")
        self.assertEqual(calls[1].kwargs["multiplier"], "150000")
        self.assertEqual(calls[2].kwargs["multiplier"], "150000")
        self.assertEqual(calls[3].kwargs["multiplier"], "200000")

    async def test_missing_ifuserid_resolves_via_ifc_username(self):
        members = [
            IFOrganizationMember("uid-resolved", "SomePilot", 0, "2026-07-03T12:00:00Z", active_lifts=1),
        ]
        pilots = {
            "333": {
                "id": 30,
                "callsign": "QRV303",
                "discordid": "333",
                "ifuserid": None,
                "ifc": "https://community.infiniteflight.com/u/SomePilot/summary",
            }
        }
        bot, _ = self._build_bot(
            members,
            pilots,
            if_user_lookup={"result": {"userId": "uid-resolved"}},
        )
        service = PirepFilingService(bot)

        with patch.dict(os.environ, {"IF_ORGANIZATION_ID": "org-qrv"}):
            res = await service.submit_fleet_pirep(
                pilot1_discord_id=333,
                pilot2_discord_id=None,
                frame_data={"name": "A7-APC"},
                dep_icao="OTHH",
                arr_icao="OMDB",
                total_duration_hours=1,
                total_duration_minutes=0,
                pilot1_name="SomePilot",
            )

        self.assertTrue(res["success"])
        self.assertEqual(res["pilot1_result"]["multiplier"], "200000")
        self.assertEqual(res["pilot1_result"]["active_lifts"], 1)
        bot.pilots_model.update_ifuserid_by_id.assert_awaited_once_with(30, "uid-resolved")

    async def test_lookup_failure_falls_back_to_15x_with_warning(self):
        pilots = {
            "444": {"id": 40, "callsign": "QRV404", "discordid": "444", "ifuserid": "uid-404", "ifc": ""},
        }
        bot, client = self._build_bot([], pilots)
        client.list_organization_members.side_effect = IFLiveForbiddenError(
            403, 403, "Missing scope live:members.read"
        )
        service = PirepFilingService(bot)

        with patch.dict(os.environ, {"IF_ORGANIZATION_ID": "org-qrv"}):
            res = await service.submit_fleet_pirep(
                pilot1_discord_id=444,
                pilot2_discord_id=None,
                frame_data={"name": "A7-APC"},
                dep_icao="OTHH",
                arr_icao="OMDB",
                total_duration_hours=1,
                total_duration_minutes=0,
                pilot1_name="PilotFour",
            )

        self.assertTrue(res["success"])
        self.assertIsNotNone(res["lift_lookup_warning"])
        self.assertIn("live:members.read", res["lift_lookup_warning"])
        self.assertEqual(res["pilot1_result"]["multiplier"], FLEET_NON_LIFT_MULTIPLIER)
        self.assertEqual(res["pilot1_result"]["lift_status"], "lookup_failed")


class FleetPirepCogTests(unittest.IsolatedAsyncioTestCase):
    def test_acars_live_deactivated_and_live_fleet_pirep_has_no_multiplier_param(self):
        bot = MagicMock()
        cog = FleetPirepCog(bot)

        self.assertFalse(hasattr(cog, "acars_live"))
        cmd_names = [cmd.name for cmd in cog.get_app_commands()]
        self.assertIn("live_fleet_pirep", cmd_names)
        self.assertNotIn("acars_live", cmd_names)

        live_cmd = next(cmd for cmd in cog.get_app_commands() if cmd.name == "live_fleet_pirep")
        param_names = [p.name for p in live_cmd.parameters]
        self.assertEqual(param_names, ["frame", "pilot_1", "pilot_2"])

    async def test_modal_submit_renders_split_multipliers_and_lift_warning(self):
        bot = MagicMock()
        cog = FleetPirepCog(bot)
        mock_service = MagicMock()
        mock_service.submit_fleet_pirep = AsyncMock(
            return_value={
                "success": True,
                "lift_lookup_warning": None,
                "pilot1_result": {
                    "success": True,
                    "pilot_name": "Pilot One",
                    "duration": "03:00",
                    "multiplier": "200000",
                    "multiplier_label": "2x",
                    "active_lifts": 1,
                    "has_active_lift": True,
                    "lift_status": "matched",
                    "lift_note": None,
                },
                "pilot2_result": {
                    "success": True,
                    "pilot_name": "Pilot Two",
                    "duration": "03:00",
                    "multiplier": "150000",
                    "multiplier_label": "1.5x",
                    "active_lifts": 0,
                    "has_active_lift": False,
                    "lift_status": "matched",
                    "lift_note": None,
                },
            }
        )
        cog._get_pirep_service = MagicMock(return_value=mock_service)

        modal = FleetPirepModal(
            cog=cog,
            frame_name="A7-BAE",
            frame_data={"icao": "B77W", "livery": "Qatar Airways"},
            pilot1_id=111,
            pilot2_id=222,
            pilot1_name="Pilot One",
            pilot2_name="Pilot Two",
        )
        modal.departure._value = "OTHH"
        modal.arrival._value = "EGLL"
        modal.hours._value = "06"
        modal.minutes._value = "00"

        interaction = MagicMock()
        interaction.user = SimpleNamespace(id=999, display_name="StaffUser")
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        await modal.on_submit(interaction)

        mock_service.submit_fleet_pirep.assert_awaited_once()
        self.assertIsNone(mock_service.submit_fleet_pirep.await_args.kwargs["multiplier"])

        sent_embed: discord.Embed = interaction.followup.send.await_args.kwargs["embed"]
        fields = {f.name: f.value for f in sent_embed.fields}
        self.assertEqual(fields["✨ Multiplier"], "P1: 2x · P2: 1.5x")
        self.assertIn("2x", fields["👤 Pilot 1"])
        self.assertIn("1 active lift", fields["👤 Pilot 1"])
        self.assertIn("1.5x", fields["👤 Pilot 2"])
        self.assertIn("0 active lifts", fields["👤 Pilot 2"])


if __name__ == "__main__":
    unittest.main()
