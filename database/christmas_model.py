"""Database model for QRV Christmas Operations 2026 dispatches."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .manager import DatabaseManager


class ChristmasModel:
    """Manages two-leg Christmas dispatch records in `christmas_dispatches`."""

    DEFAULT_EVENT_NAME = "christmas_2026"
    HUB_ICAO = "OTHH"

    STATUS_DISPATCHED = "DISPATCHED"
    STATUS_OPEN = "OPEN"
    STATUS_ARRIVED = "ARRIVED"
    STATUS_CANCELLED = "CANCELLED"
    ACTIVE_STATUSES = (STATUS_DISPATCHED, STATUS_OPEN)

    _TABLE_DDL = """
        CREATE TABLE IF NOT EXISTS christmas_dispatches (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            dispatch_code VARCHAR(24) NOT NULL,
            event_name VARCHAR(50) NOT NULL DEFAULT 'christmas_2026',
            leg_number TINYINT NOT NULL,
            registration VARCHAR(32) NOT NULL,
            origin_icao VARCHAR(8) NOT NULL,
            destination_icao VARCHAR(8) NOT NULL,
            schedule_id VARCHAR(64) NOT NULL UNIQUE,
            pilot_id INT NULL,
            copilot_id INT NULL,
            allocated_pax INT NOT NULL DEFAULT 0,
            status VARCHAR(24) NOT NULL DEFAULT 'DISPATCHED',
            pax_credited TINYINT(1) NOT NULL DEFAULT 0,
            pirep_id INT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_xmas_dispatch_leg (dispatch_code, leg_number),
            INDEX idx_xmas_event_status (event_name, status),
            INDEX idx_xmas_pilot (pilot_id, status),
            INDEX idx_xmas_copilot (copilot_id, status),
            INDEX idx_xmas_reg (registration, status),
            INDEX idx_xmas_route_pax (origin_icao, destination_icao, pax_credited)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """

    _LEG_SELECT = """
        SELECT
            cd.*,
            p1.callsign AS pilot_callsign,
            p1.name AS pilot_name,
            p1.discordid AS pilot_discordid,
            p1.ifuserid AS pilot_ifuserid,
            p2.callsign AS copilot_callsign,
            p2.name AS copilot_name,
            p2.discordid AS copilot_discordid,
            p2.ifuserid AS copilot_ifuserid
        FROM christmas_dispatches cd
        LEFT JOIN pilots p1 ON cd.pilot_id = p1.id
        LEFT JOIN pilots p2 ON cd.copilot_id = p2.id
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        event_name: str = DEFAULT_EVENT_NAME,
    ):
        self.db = db_manager
        self.event_name = event_name

    @staticmethod
    def _normalize_icao(icao: str) -> str:
        return str(icao or "").strip().upper()

    @staticmethod
    def _normalize_registration(registration: str) -> str:
        return str(registration or "").strip().upper()

    async def ensure_table(self) -> None:
        """Verify `christmas_dispatches` exists, creating it if permitted."""
        if await self.db.fetch_one("SHOW TABLES LIKE %s", ("christmas_dispatches",)):
            return

        await self.db.execute(self._TABLE_DDL)
        if not await self.db.fetch_one("SHOW TABLES LIKE %s", ("christmas_dispatches",)):
            raise RuntimeError(
                "Required table christmas_dispatches is missing and could not "
                "be created. Grant CREATE TABLE permissions or create it manually."
            )

    async def generate_next_dispatch_code(self) -> str:
        """Generate the next sequential dispatch code (e.g., XMAS-0001)."""
        row = await self.db.fetch_one(
            """
            SELECT MAX(CAST(SUBSTRING(dispatch_code, 6) AS UNSIGNED)) AS max_seq
            FROM christmas_dispatches
            WHERE event_name = %s
              AND dispatch_code REGEXP '^XMAS-[0-9]+$'
            """,
            (self.event_name,),
        )
        next_seq = int((row or {}).get("max_seq") or 0) + 1
        return f"XMAS-{next_seq:04d}"

    async def create_leg(
        self,
        dispatch_code: str,
        leg_number: int,
        registration: str,
        origin_icao: str,
        destination_icao: str,
        schedule_id: str,
        pilot_id: Optional[int],
        copilot_id: Optional[int] = None,
        allocated_pax: int = 0,
        status: str = STATUS_DISPATCHED,
    ) -> Optional[int]:
        """Insert a single leg row into `christmas_dispatches`."""
        query = """
            INSERT INTO christmas_dispatches (
                dispatch_code, event_name, leg_number, registration,
                origin_icao, destination_icao, schedule_id,
                pilot_id, copilot_id, allocated_pax, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        args = (
            str(dispatch_code).strip().upper(),
            self.event_name,
            int(leg_number),
            self._normalize_registration(registration),
            self._normalize_icao(origin_icao),
            self._normalize_icao(destination_icao),
            str(schedule_id).strip(),
            pilot_id,
            copilot_id,
            int(allocated_pax),
            status,
        )
        return await self.db.insert(query, args)

    async def create_dispatch_pair(
        self,
        operation_type: str,
        registration: str,
        destination_icao: str,
        allocated_pax: int,
        leg1_schedule_id: str,
        leg2_schedule_id: str,
        pilot_id: int,
        copilot_id: Optional[int] = None,
        dispatch_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create both scheduled legs for an EAST or WEST round-trip dispatch."""
        op = str(operation_type or "").strip().upper()
        if op not in ("EAST", "WEST"):
            raise ValueError(f"Unsupported operation_type: {operation_type!r}")

        dest = self._normalize_icao(destination_icao)
        if not dest or dest == self.HUB_ICAO:
            raise ValueError(f"Invalid dispatch destination: {destination_icao!r}")

        pax = int(allocated_pax)
        if pax <= 0:
            raise ValueError(f"allocated_pax must be positive, got {allocated_pax!r}")

        code = (
            str(dispatch_code).strip().upper()
            if dispatch_code
            else await self.generate_next_dispatch_code()
        )
        leg1_pax = pax if op == "WEST" else 0
        leg2_pax = pax if op == "EAST" else 0

        leg1_id = await self.create_leg(
            dispatch_code=code,
            leg_number=1,
            registration=registration,
            origin_icao=self.HUB_ICAO,
            destination_icao=dest,
            schedule_id=leg1_schedule_id,
            pilot_id=pilot_id,
            copilot_id=copilot_id,
            allocated_pax=leg1_pax,
        )
        if not leg1_id:
            raise RuntimeError(f"Failed to insert Leg 1 for dispatch {code}")

        try:
            leg2_id = await self.create_leg(
                dispatch_code=code,
                leg_number=2,
                registration=registration,
                origin_icao=dest,
                destination_icao=self.HUB_ICAO,
                schedule_id=leg2_schedule_id,
                pilot_id=pilot_id,
                copilot_id=copilot_id,
                allocated_pax=leg2_pax,
            )
            if not leg2_id:
                raise RuntimeError(f"Failed to insert Leg 2 for dispatch {code}")
        except Exception:
            await self.db.execute(
                "DELETE FROM christmas_dispatches WHERE dispatch_code = %s AND event_name = %s",
                (code, self.event_name),
            )
            raise

        return {
            "dispatch_code": code,
            "leg1_id": leg1_id,
            "leg2_id": leg2_id,
            "allocated_pax": pax,
            "operation_type": op,
        }

    async def get_pool_summary(self, target_pax: int = 5000) -> Dict[str, int]:
        """Compute live passenger counters and active reservation totals."""
        row = await self.db.fetch_one(
            """
            SELECT
                COALESCE(SUM(CASE
                    WHEN destination_icao = %s AND pax_credited = 1
                    THEN allocated_pax ELSE 0
                END), 0) AS east_collected,
                COALESCE(SUM(CASE
                    WHEN destination_icao = %s AND status != 'CANCELLED' AND pax_credited = 0
                    THEN allocated_pax ELSE 0
                END), 0) AS east_reserved,
                COALESCE(SUM(CASE
                    WHEN origin_icao = %s AND pax_credited = 1
                    THEN allocated_pax ELSE 0
                END), 0) AS west_delivered,
                COALESCE(SUM(CASE
                    WHEN origin_icao = %s AND status != 'CANCELLED' AND pax_credited = 0
                    THEN allocated_pax ELSE 0
                END), 0) AS west_reserved
            FROM christmas_dispatches
            WHERE event_name = %s
              AND allocated_pax > 0
              AND status != 'CANCELLED'
            """,
            (
                self.HUB_ICAO,
                self.HUB_ICAO,
                self.HUB_ICAO,
                self.HUB_ICAO,
                self.event_name,
            ),
        ) or {}

        east_collected = int(row.get("east_collected") or 0)
        east_reserved = int(row.get("east_reserved") or 0)
        west_delivered = int(row.get("west_delivered") or 0)
        west_reserved = int(row.get("west_reserved") or 0)

        othh_pool = max(0, east_collected - west_delivered)
        return {
            "target_pax": int(target_pax),
            "east_collected": east_collected,
            "east_remaining": max(0, int(target_pax) - east_collected),
            "east_reserved": east_reserved,
            "east_available": max(0, int(target_pax) - east_collected - east_reserved),
            "othh_pool": othh_pool,
            "othh_reserved": west_reserved,
            "othh_available": max(0, othh_pool - west_reserved),
            "west_delivered": west_delivered,
        }

    async def get_leg_by_schedule_id(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single leg row (with pilot/copilot details) by IF Live schedule ID."""
        query = f"{self._LEG_SELECT} WHERE cd.schedule_id = %s"
        return await self.db.fetch_one(query, (str(schedule_id).strip(),))

    async def get_dispatch_legs(self, dispatch_code: str) -> List[Dict[str, Any]]:
        """Fetch both leg rows for a dispatch code ordered by leg number."""
        query = (
            f"{self._LEG_SELECT} "
            "WHERE cd.dispatch_code = %s AND cd.event_name = %s "
            "ORDER BY cd.leg_number ASC"
        )
        return await self.db.fetch_all(
            query,
            (str(dispatch_code).strip().upper(), self.event_name),
        ) or []

    async def get_active_legs(self) -> List[Dict[str, Any]]:
        """Fetch all DISPATCHED or OPEN legs for the active event."""
        query = (
            f"{self._LEG_SELECT} "
            "WHERE cd.event_name = %s AND cd.status IN ('DISPATCHED', 'OPEN') "
            "ORDER BY cd.created_at ASC, cd.leg_number ASC"
        )
        return await self.db.fetch_all(query, (self.event_name,)) or []

    async def get_active_legs_for_pilot(self, pilot_id: int) -> List[Dict[str, Any]]:
        """Fetch all active DISPATCHED legs assigned to a pilot or co-pilot."""
        query = (
            f"{self._LEG_SELECT} "
            "WHERE cd.event_name = %s "
            "AND cd.status = 'DISPATCHED' "
            "AND (cd.pilot_id = %s OR cd.copilot_id = %s) "
            "ORDER BY cd.created_at ASC, cd.leg_number ASC"
        )
        return await self.db.fetch_all(
            query,
            (self.event_name, pilot_id, pilot_id),
        ) or []

    async def get_active_legs_for_registration(self, registration: str) -> List[Dict[str, Any]]:
        """Fetch any active legs currently holding an aircraft registration."""
        query = (
            f"{self._LEG_SELECT} "
            "WHERE cd.event_name = %s "
            "AND cd.registration = %s "
            "AND cd.status IN ('DISPATCHED', 'OPEN') "
            "ORDER BY cd.created_at ASC, cd.leg_number ASC"
        )
        return await self.db.fetch_all(
            query,
            (self.event_name, self._normalize_registration(registration)),
        ) or []

    async def get_open_return_legs(self) -> List[Dict[str, Any]]:
        """Fetch Leg 2 return flights awaiting a pilot to claim them."""
        query = (
            f"{self._LEG_SELECT} "
            "WHERE cd.event_name = %s "
            "AND cd.leg_number = 2 "
            "AND cd.status = 'OPEN' "
            "ORDER BY cd.created_at ASC"
        )
        return await self.db.fetch_all(query, (self.event_name,)) or []

    async def mark_leg_arrived(
        self,
        schedule_id: str,
        pilot_id: Optional[int] = None,
        copilot_id: Optional[int] = None,
    ) -> bool:
        """Transition an active leg to ARRIVED, optionally updating final crew."""
        affected = await self.db.execute(
            """
            UPDATE christmas_dispatches
            SET status = %s,
                pilot_id = COALESCE(%s, pilot_id),
                copilot_id = COALESCE(%s, copilot_id)
            WHERE schedule_id = %s
              AND status IN ('DISPATCHED', 'OPEN')
            """,
            (self.STATUS_ARRIVED, pilot_id, copilot_id, str(schedule_id).strip()),
        )
        return bool(affected)

    async def claim_pax_credit(self, schedule_id: str) -> bool:
        """Atomically mark a passenger-carrying leg as credited (returns True once)."""
        affected = await self.db.execute(
            """
            UPDATE christmas_dispatches
            SET pax_credited = 1
            WHERE schedule_id = %s
              AND pax_credited = 0
              AND allocated_pax > 0
              AND status != 'CANCELLED'
            """,
            (str(schedule_id).strip(),),
        )
        return bool(affected)

    async def mark_dispatch_cancelled(self, dispatch_code: str) -> int:
        """Cancel un-credited legs of a dispatch and release reserved PAX."""
        return await self.db.execute(
            """
            UPDATE christmas_dispatches
            SET status = %s
            WHERE dispatch_code = %s
              AND event_name = %s
              AND status IN ('DISPATCHED', 'OPEN')
              AND pax_credited = 0
            """,
            (
                self.STATUS_CANCELLED,
                str(dispatch_code).strip().upper(),
                self.event_name,
            ),
        )

    async def reopen_leg(self, schedule_id: str) -> bool:
        """Clear crew and mark a leg OPEN so another pilot can claim it."""
        affected = await self.db.execute(
            """
            UPDATE christmas_dispatches
            SET status = %s,
                pilot_id = NULL,
                copilot_id = NULL
            WHERE schedule_id = %s
              AND status = 'DISPATCHED'
              AND pax_credited = 0
            """,
            (self.STATUS_OPEN, str(schedule_id).strip()),
        )
        return bool(affected)

    async def claim_open_leg(
        self,
        schedule_id: str,
        pilot_id: int,
        copilot_id: Optional[int] = None,
    ) -> bool:
        """Atomically assign crew to an OPEN leg."""
        affected = await self.db.execute(
            """
            UPDATE christmas_dispatches
            SET status = %s,
                pilot_id = %s,
                copilot_id = %s
            WHERE schedule_id = %s
              AND status = 'OPEN'
              AND pilot_id IS NULL
            """,
            (self.STATUS_DISPATCHED, pilot_id, copilot_id, str(schedule_id).strip()),
        )
        return bool(affected)

    async def update_leg_crew(
        self,
        schedule_id: str,
        pilot_id: Optional[int],
        copilot_id: Optional[int] = None,
    ) -> int:
        """Update pilot/copilot IDs on an active leg (e.g., mid-flight recovery)."""
        return await self.db.execute(
            """
            UPDATE christmas_dispatches
            SET pilot_id = %s,
                copilot_id = %s
            WHERE schedule_id = %s
              AND status IN ('DISPATCHED', 'OPEN')
            """,
            (pilot_id, copilot_id, str(schedule_id).strip()),
        )

    async def link_pirep(self, schedule_id: str, pirep_id: int) -> int:
        """Associate a CrewCenter PIREP ID with a dispatched leg."""
        return await self.db.execute(
            """
            UPDATE christmas_dispatches
            SET pirep_id = %s
            WHERE schedule_id = %s
            """,
            (int(pirep_id), str(schedule_id).strip()),
        )

    async def get_recent_dispatches(self, limit: int = 25) -> List[Dict[str, Any]]:
        """Fetch recent dispatch legs ordered newest first."""
        query = (
            f"{self._LEG_SELECT} "
            "WHERE cd.event_name = %s "
            "ORDER BY cd.id DESC "
            "LIMIT %s"
        )
        return await self.db.fetch_all(query, (self.event_name, int(limit))) or []

    async def count_active_dispatches_for_pilot(self, pilot_id: int) -> int:
        """Count distinct active Christmas dispatch codes assigned to a pilot."""
        row = await self.db.fetch_one(
            """
            SELECT COUNT(DISTINCT dispatch_code) AS active_count
            FROM christmas_dispatches
            WHERE event_name = %s
              AND status IN ('DISPATCHED', 'OPEN')
              AND (pilot_id = %s OR copilot_id = %s)
            """,
            (self.event_name, int(pilot_id), int(pilot_id)),
        ) or {}
        return int(row.get("active_count") or 0)

    async def get_active_dispatches_for_pilot(self, pilot_id: int) -> List[Dict[str, Any]]:
        """Fetch active DISPATCHED/OPEN legs assigned to a pilot or co-pilot."""
        query = (
            f"{self._LEG_SELECT} "
            "WHERE cd.event_name = %s "
            "AND cd.status IN ('DISPATCHED', 'OPEN') "
            "AND (cd.pilot_id = %s OR cd.copilot_id = %s) "
            "ORDER BY cd.created_at ASC, cd.leg_number ASC"
        )
        return await self.db.fetch_all(
            query,
            (self.event_name, int(pilot_id), int(pilot_id)),
        ) or []

    async def get_unfiled_legs_for_pilot(self, pilot_id: int) -> List[Dict[str, Any]]:
        """Fetch non-cancelled legs lacking a CrewCenter PIREP link for a pilot."""
        query = (
            f"{self._LEG_SELECT} "
            "WHERE cd.event_name = %s "
            "AND cd.status IN ('DISPATCHED', 'ARRIVED') "
            "AND cd.pirep_id IS NULL "
            "AND (cd.pilot_id = %s OR cd.copilot_id = %s) "
            "ORDER BY cd.created_at DESC, cd.leg_number ASC"
        )
        return await self.db.fetch_all(
            query,
            (self.event_name, int(pilot_id), int(pilot_id)),
        ) or []

    async def get_pending_pirep_legs(self) -> List[Dict[str, Any]]:
        """Fetch legs with a linked CrewCenter PIREP still awaiting approval processing."""
        query = (
            f"{self._LEG_SELECT} "
            "WHERE cd.event_name = %s "
            "AND cd.pirep_id IS NOT NULL "
            "AND cd.pax_credited = 0 "
            "AND cd.status != 'CANCELLED' "
            "ORDER BY cd.id ASC"
        )
        return await self.db.fetch_all(query, (self.event_name,)) or []

    async def mark_empty_leg_processed(self, schedule_id: str) -> bool:
        """Close out an empty ferry/return leg once its PIREP is approved."""
        affected = await self.db.execute(
            """
            UPDATE christmas_dispatches
            SET pax_credited = 1
            WHERE schedule_id = %s
              AND allocated_pax = 0
              AND pax_credited = 0
              AND status != 'CANCELLED'
            """,
            (str(schedule_id).strip(),),
        )
        return bool(affected)

    async def clear_pirep_link(self, schedule_id: str) -> int:
        """Clear a rejected PIREP link so the pilot can re-submit via /cpsc."""
        return await self.db.execute(
            """
            UPDATE christmas_dispatches
            SET pirep_id = NULL
            WHERE schedule_id = %s
            """,
            (str(schedule_id).strip(),),
        )

    async def update_leg_copilot(
        self,
        schedule_id: str,
        copilot_id: Optional[int],
    ) -> int:
        """Set or update the co-pilot ID on an uncredited, non-cancelled leg."""
        return await self.db.execute(
            """
            UPDATE christmas_dispatches
            SET copilot_id = %s
            WHERE schedule_id = %s
              AND pax_credited = 0
              AND status != 'CANCELLED'
            """,
            (copilot_id, str(schedule_id).strip()),
        )

    async def set_leg_pilot_if_missing(self, schedule_id: str, pilot_id: int) -> int:
        """Populate pilot_id on a leg only if it is currently NULL."""
        return await self.db.execute(
            """
            UPDATE christmas_dispatches
            SET pilot_id = %s
            WHERE schedule_id = %s
              AND pilot_id IS NULL
            """,
            (int(pilot_id), str(schedule_id).strip()),
        )

    async def get_leg_by_dispatch_and_number(
        self,
        dispatch_code: str,
        leg_number: int,
    ) -> Optional[Dict[str, Any]]:
        """Fetch a single leg by dispatch code and leg number."""
        query = (
            f"{self._LEG_SELECT} "
            "WHERE cd.dispatch_code = %s "
            "AND cd.leg_number = %s "
            "AND cd.event_name = %s"
        )
        return await self.db.fetch_one(
            query,
            (str(dispatch_code).strip().upper(), int(leg_number), self.event_name),
        )

    async def get_board_stats(self) -> Dict[str, int]:
        """Fetch operational counts for the Live Operations Board."""
        row = await self.db.fetch_one(
            """
            SELECT
                COUNT(DISTINCT CASE
                    WHEN status IN ('DISPATCHED', 'OPEN') THEN dispatch_code
                END) AS active_dispatches,
                COALESCE(SUM(CASE
                    WHEN status = 'DISPATCHED' THEN 1 ELSE 0
                END), 0) AS active_legs,
                COALESCE(SUM(CASE
                    WHEN leg_number = 2 AND status = 'OPEN' THEN 1 ELSE 0
                END), 0) AS open_return_legs,
                COALESCE(SUM(CASE
                    WHEN pirep_id IS NOT NULL AND pax_credited = 0 AND status != 'CANCELLED'
                    THEN 1 ELSE 0
                END), 0) AS pending_pireps,
                COALESCE(SUM(CASE
                    WHEN pax_credited = 1 AND status != 'CANCELLED' THEN 1 ELSE 0
                END), 0) AS completed_legs
            FROM christmas_dispatches
            WHERE event_name = %s
            """,
            (self.event_name,),
        ) or {}
        return {
            "active_dispatches": int(row.get("active_dispatches") or 0),
            "active_legs": int(row.get("active_legs") or 0),
            "open_return_legs": int(row.get("open_return_legs") or 0),
            "pending_pireps": int(row.get("pending_pireps") or 0),
            "completed_legs": int(row.get("completed_legs") or 0),
        }
