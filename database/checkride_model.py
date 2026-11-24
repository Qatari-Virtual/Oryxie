"""
Checkride Database Model

This module handles database queries related to checkride flight replays
from the 'replay' table in the CrewCenter database.
"""

from typing import Optional, Dict, List
import logging
from .manager import DatabaseManager

logger = logging.getLogger('oryxie.checkride_model')


class CheckrideModel:
    """
    Handles operations related to the 'replay' table for checkride evaluations.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def get_pending_checkrides(self) -> List[Dict]:
        """
        Retrieves all pending checkrides (approved = 0) with trainee and
        claimed instructor details joined from the 'pilots' table.

        Returns:
            List[Dict]: List of pending checkride dictionaries sorted oldest first.
        """
        query = """
            SELECT 
                r.id,
                r.pilotid,
                r.filename,
                r.checkride_type,
                r.upload_date,
                r.approved,
                r.approved_by,
                t.callsign AS trainee_callsign,
                t.name AS trainee_name,
                t.discordid AS trainee_discord_id,
                fi.callsign AS fi_callsign,
                fi.name AS fi_name,
                fi.discordid AS fi_discord_id
            FROM replay r
            JOIN pilots t ON r.pilotid = t.id
            LEFT JOIN pilots fi ON r.approved_by = fi.id
            WHERE r.approved = 0
            ORDER BY r.upload_date ASC
        """
        try:
            results = await self.db.fetch_all(query)
            return results if results else []
        except Exception as e:
            logger.exception("Failed to fetch pending checkrides: %s", e)
            return []

    async def get_checkride_by_id(self, replay_id: int) -> Optional[Dict]:
        """
        Retrieves a single checkride record by its primary key ID.

        Args:
            replay_id: Checkride record ID.

        Returns:
            Optional[Dict]: Checkride record or None if not found.
        """
        query = """
            SELECT 
                r.id,
                r.pilotid,
                r.filename,
                r.checkride_type,
                r.upload_date,
                r.approved,
                r.approved_by,
                t.callsign AS trainee_callsign,
                t.name AS trainee_name,
                t.discordid AS trainee_discord_id,
                fi.callsign AS fi_callsign,
                fi.name AS fi_name,
                fi.discordid AS fi_discord_id
            FROM replay r
            JOIN pilots t ON r.pilotid = t.id
            LEFT JOIN pilots fi ON r.approved_by = fi.id
            WHERE r.id = %s
        """
        try:
            return await self.db.fetch_one(query, (replay_id,))
        except Exception as e:
            logger.exception("Failed to fetch checkride with id %s: %s", replay_id, e)
            return None
