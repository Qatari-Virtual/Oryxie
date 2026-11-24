"""
Unit tests for CheckrideModel and CheckrideTracker Cog.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from database.checkride_model import CheckrideModel
from cogs.checkride_tracker import (
    format_pending_duration,
    build_checkride_report,
    FI_ROLE_ID,
    GREEN_ALL_CLEAR,
    WARNING_ORANGE,
    ALERT_RED,
    QATAR_MAROON,
)


class TestDurationFormatting:
    """Tests for format_pending_duration helper function."""

    def test_less_than_one_hour(self):
        now = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)
        upload = now - timedelta(minutes=30)
        hours, days, text = format_pending_duration(upload, now)
        assert hours == 0
        assert days == 0
        assert text == "< 1h"

    def test_under_twenty_four_hours(self):
        now = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)
        upload = now - timedelta(hours=7)
        hours, days, text = format_pending_duration(upload, now)
        assert hours == 7
        assert days == 0
        assert text == "7h"

    def test_exact_twenty_four_hours(self):
        now = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)
        upload = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)
        hours, days, text = format_pending_duration(upload, now)
        assert hours == 24
        assert days == 1
        assert text == "24h (1d)"

    def test_over_twenty_four_hours_with_remainder(self):
        now = datetime(2026, 11, 25, 22, 0, tzinfo=timezone.utc)
        upload = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)
        hours, days, text = format_pending_duration(upload, now)
        assert hours == 28
        assert days == 1
        assert text == "28h (1d 4h)"

    def test_forty_eight_hours(self):
        now = datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc)
        upload = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)
        hours, days, text = format_pending_duration(upload, now)
        assert hours == 48
        assert days == 2
        assert text == "48h (2d)"

    def test_naive_datetime_handling(self):
        now = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)
        upload = datetime(2026, 11, 24, 18, 0)  # Naive
        hours, days, text = format_pending_duration(upload, now)
        assert hours == 24
        assert days == 1
        assert text == "24h (1d)"


class TestReportBuilder:
    """Tests for build_checkride_report."""

    def test_empty_queue_all_clear(self):
        now = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)
        embed, ping_content = build_checkride_report([], now_utc=now)

        assert ping_content is None
        assert embed.color.value == GREEN_ALL_CLEAR
        assert "clear" in embed.description.lower()
        assert len(embed.fields) == 0

    def test_unclaimed_checkrides_report(self):
        now = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)
        records = [
            {
                "id": 101,
                "pilotid": 1543,
                "checkride_type": 1,  # ATC
                "upload_date": datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc),
                "approved": 0,
                "approved_by": 0,
                "trainee_callsign": "QRV230",
                "trainee_name": "John Doe",
                "trainee_discord_id": "999888777",
                "fi_callsign": None,
                "fi_name": None,
                "fi_discord_id": None,
            }
        ]

        embed, ping_content = build_checkride_report(records, now_utc=now)

        assert embed.color.value == WARNING_ORANGE  # 24h is warning
        assert f"<@&{FI_ROLE_ID}>" in ping_content
        assert len(embed.fields) == 1

        unclaimed_field = embed.fields[0]
        assert "Unclaimed Checkrides (1)" in unclaimed_field.name
        assert "CR #101" in unclaimed_field.value
        assert "QRV230" in unclaimed_field.value
        assert "John Doe" in unclaimed_field.value
        assert "🎙️ ATC" in unclaimed_field.value
        assert "24h (1d)" in unclaimed_field.value
        assert "https://crew.qatarivirtual.com/admin/users/training/1543" in unclaimed_field.value

    def test_claimed_checkride_under_24h(self):
        now = datetime(2026, 11, 24, 22, 0, tzinfo=timezone.utc)
        records = [
            {
                "id": 102,
                "pilotid": 1544,
                "checkride_type": 0,  # UNICOM
                "upload_date": datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc),
                "approved": 0,
                "approved_by": 42,
                "trainee_callsign": "QRV412",
                "trainee_name": "Jane Smith",
                "trainee_discord_id": "888777666",
                "fi_callsign": "QRV005",
                "fi_name": "Chief Instructor",
                "fi_discord_id": "111222333444",
            }
        ]

        embed, ping_content = build_checkride_report(records, now_utc=now)

        assert embed.color.value == QATAR_MAROON  # < 24h
        # Claimed under 24h and 0 unclaimed -> no ping content
        assert ping_content is None
        assert len(embed.fields) == 1

        claimed_field = embed.fields[0]
        assert "Claimed Checkrides (1)" in claimed_field.name
        assert "CR #102" in claimed_field.value
        assert "QRV412" in claimed_field.value
        assert "🎯 UNICOM" in claimed_field.value
        assert "<@111222333444>" in claimed_field.value
        assert "https://crew.qatarivirtual.com/admin/users/training/1544" in claimed_field.value

    def test_claimed_checkride_over_48h_urgent_alert(self):
        now = datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc)
        records = [
            {
                "id": 103,
                "pilotid": 1545,
                "checkride_type": 1,
                "upload_date": datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc),
                "approved": 0,
                "approved_by": 42,
                "trainee_callsign": "QRV999",
                "trainee_name": "Slow Candidate",
                "trainee_discord_id": "777666555",
                "fi_callsign": "QRV005",
                "fi_name": "Instructor Bob",
                "fi_discord_id": "111222333444",
            }
        ]

        embed, ping_content = build_checkride_report(records, now_utc=now)

        assert embed.color.value == ALERT_RED  # >= 48h
        # Over 24h/48h -> pings the assigned FI directly
        assert "<@111222333444>" in ping_content
        assert "Overdue for grading!" in embed.fields[0].value


@pytest.mark.asyncio
class TestCheckrideModel:
    """Tests for database/checkride_model.py."""

    async def test_get_pending_checkrides_queries_approved_zero(self):
        mock_db = MagicMock()
        mock_db.fetch_all = AsyncMock(return_value=[{"id": 1, "approved": 0}])

        model = CheckrideModel(mock_db)
        results = await model.get_pending_checkrides()

        assert len(results) == 1
        assert results[0]["id"] == 1
        mock_db.fetch_all.assert_called_once()
        query = mock_db.fetch_all.call_args[0][0]
        assert "FROM replay r" in query
        assert "WHERE r.approved = 0" in query
        assert "ORDER BY r.upload_date ASC" in query

    async def test_get_checkride_by_id(self):
        mock_db = MagicMock()
        mock_db.fetch_one = AsyncMock(return_value={"id": 105, "pilotid": 1543})

        model = CheckrideModel(mock_db)
        record = await model.get_checkride_by_id(105)

        assert record is not None
        assert record["id"] == 105
        mock_db.fetch_one.assert_called_once()
        args = mock_db.fetch_one.call_args[0][1]
        assert args == (105,)
