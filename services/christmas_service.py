"""Domain service for QRV Christmas Operations 2026."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from api.if_live_manager import (
    IFLiveNotFoundError,
    IFScheduleRequest,
    PersistentFlightCrewMemberRole,
    PersistentFlightType,
    ScheduledFlightStatus,
)
from cogs.airline_hubs import (
    CARGO_REGISTRATIONS,
    RETIRED_REGISTRATIONS,
    airline_name_for_registration,
    is_cargo_registration,
)

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.join("assets", "christmas_2026_config.json")
FLEET_FRAMES_PATH = os.path.join("assets", "fleet_frames.json")
DEFAULT_CHRISTMAS_MULTIPLIER = "300000"
DEFAULT_CHRISTMAS_MULTIPLIER_LABEL = "3x"
MULTIPLIER_LABELS = {
    "100000": "1x",
    "150000": "1.5x",
    "200000": "2x",
    "300000": "3x",
}
CHMS_FLIGHTNUM_RE = re.compile(
    r"^([A-Z0-9-]+)\s+CHMS\s+(XMAS-\d+)\s+L([12])$",
    re.IGNORECASE,
)
TERMINAL_SCHEDULE_STATUSES = {
    int(ScheduledFlightStatus.CANCELLED),
    int(ScheduledFlightStatus.ARRIVED),
}
AIRCRAFT_DISPLAY_NAMES = {
    "A388": "Airbus A380-800",
    "B77W": "Boeing 777-300ER",
    "B77L": "Boeing 777-200LR",
    "A35K": "Airbus A350-1000",
    "A359": "Airbus A350-900",
    "A333": "Airbus A330-300",
    "B788": "Boeing 787-8",
    "B787": "Boeing 787-8",
    "B789": "Boeing 787-9",
}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _strip_reg(registration: str) -> str:
    return str(registration or "").strip().upper().replace("-", "")


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_nm = 3440.065
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return 2.0 * radius_nm * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _schedule_status(schedule: Any) -> Optional[int]:
    try:
        raw = _get(schedule, "status")
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _crew_user_id(member: Any) -> str:
    for key in ("userId", "user_id", "ifuserid", "id"):
        val = _get(member, key)
        if val not in (None, ""):
            return str(val).strip()
    return ""


def _crew_role(member: Any) -> Optional[int]:
    try:
        raw = _get(member, "role")
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


class ChristmasService:
    """Handles dispatch eligibility, IF Live scheduling, `/cpsc` PIREPs, and Ticket rewards."""

    DEFAULT_EVENT_NAME = "christmas_2026"
    DEFAULT_TICKET_CURRENCY = "ChristmasTickets"
    DEFAULT_COOKIE_CURRENCY = "Cookies"
    TOKEN_OWNER_ID = 1

    def __init__(self, bot: Any, config_path: str = DEFAULT_CONFIG_PATH):
        self.bot = bot
        self.config_path = config_path
        self._config_cache: Optional[Dict[str, Any]] = None
        self._fleet_frames_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._dispatch_lock = asyncio.Lock()

    def load_config(self, force_reload: bool = False) -> Dict[str, Any]:
        """Load Christmas 2026 operational config from JSON."""
        if self._config_cache is not None and not force_reload:
            return self._config_cache
        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                self._config_cache = json.load(handle)
        except Exception as exc:
            log.error("Failed to load %s: %s", self.config_path, exc)
            self._config_cache = {}
        return self._config_cache

    def _load_fleet_frames(self) -> Dict[str, Dict[str, Any]]:
        if self._fleet_frames_cache is not None:
            return self._fleet_frames_cache
        try:
            if os.path.exists(FLEET_FRAMES_PATH):
                with open(FLEET_FRAMES_PATH, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                self._fleet_frames_cache = {
                    _strip_reg(reg): {**meta, "registration": str(reg).strip().upper()}
                    for reg, meta in (raw or {}).items()
                }
                return self._fleet_frames_cache
        except Exception as exc:
            log.warning("Could not load %s: %s", FLEET_FRAMES_PATH, exc)
        self._fleet_frames_cache = {}
        return self._fleet_frames_cache

    @staticmethod
    def format_flight_num(registration: str, dispatch_code: str, leg_number: int) -> str:
        """Build the CrewCenter flightnum string: `{reg} CHMS {dispatch_code} L{leg}`."""
        reg = str(registration or "").strip().upper()
        code = str(dispatch_code or "").strip().upper()
        return f"{reg} CHMS {code} L{int(leg_number)}"

    @staticmethod
    def parse_christmas_flightnum(flight_num: str) -> Optional[Tuple[str, str, int]]:
        """Parse `{reg} CHMS {dispatch_code} L{1|2}` into (registration, code, leg_number)."""
        match = CHMS_FLIGHTNUM_RE.match(str(flight_num or "").strip())
        if not match:
            return None
        reg, code, leg = match.groups()
        return reg.upper(), code.upper(), int(leg)

    @staticmethod
    def split_duration_minutes(total_minutes: int, dual_pilot: bool) -> Tuple[int, int]:
        """Split total minutes 50/50 for dual-pilot flights; odd minute goes to Pilot 1."""
        mins = max(0, int(total_minutes))
        if not dual_pilot:
            return mins, 0
        half = mins // 2
        p1 = half + (mins % 2)
        p2 = half
        return p1, p2

    @staticmethod
    def split_tickets(total_tickets: int, dual_pilot: bool) -> Tuple[int, int]:
        """Split ChristmasTickets 50/50; odd remainder goes to Pilot 1 (`ceil` / `floor`)."""
        total = max(0, int(total_tickets))
        if not dual_pilot:
            return total, 0
        p1 = (total + 1) // 2
        p2 = total // 2
        return p1, p2

    @staticmethod
    def format_hhmm(minutes: int) -> str:
        hrs, mins = divmod(max(0, int(minutes)), 60)
        return f"{hrs:02d}:{mins:02d}"

    @staticmethod
    def parse_hhmm_to_minutes(duration_str: str) -> Optional[int]:
        raw = str(duration_str or "").strip()
        if ":" not in raw:
            return None
        try:
            h_str, m_str = raw.split(":", 1)
            hours = int(h_str)
            minutes = int(m_str)
            if hours < 0 or minutes < 0 or minutes >= 60:
                return None
            return hours * 60 + minutes
        except ValueError:
            return None

    def _event_name(self) -> str:
        return str(self.load_config().get("event_name") or self.DEFAULT_EVENT_NAME)

    def _ticket_currency(self) -> str:
        return str(self.load_config().get("ticket_currency_name") or self.DEFAULT_TICKET_CURRENCY)

    def _cookie_currency(self) -> str:
        return str(self.load_config().get("cookie_currency_name") or self.DEFAULT_COOKIE_CURRENCY)

    def _hub_icao(self) -> str:
        return str(self.load_config().get("hub_icao") or "OTHH").strip().upper()

    def _org_id(self) -> str:
        return os.getenv("IF_ORGANIZATION_ID", "").strip()

    def _hub_coordinates(self) -> Tuple[float, float]:
        hub = self._hub_icao()
        flightdata = getattr(self.bot, "flightdata", None)
        if flightdata is not None:
            airport = flightdata.get_airport_data(hub)
            if airport:
                try:
                    return float(airport["latitude_deg"]), float(airport["longitude_deg"])
                except (KeyError, TypeError, ValueError):
                    pass
        coords = self.load_config().get("hub_coords") or {}
        return float(coords.get("latitude", 25.2731)), float(coords.get("longitude", 51.6081))

    def _canonical_icao(self, icao: str) -> str:
        cfg = self.load_config()
        caps = cfg.get("aircraft_capacities") or {}
        norm = str(icao or "").strip().upper()
        if norm in caps:
            return norm
        alias = str((cfg.get("icao_aliases") or {}).get(norm) or "").strip().upper()
        if alias in caps:
            return alias
        return norm

    def is_eligible_qr_passenger_frame(
        self,
        registration: str,
        icao: str = "",
        livery_name: str = "",
        model_name: str = "",
    ) -> Tuple[bool, Optional[str], int]:
        """Validate whether a frame is an eligible Qatar Airways passenger aircraft."""
        cfg = self.load_config()
        reg_upper = str(registration or "").strip().upper()
        reg_key = _strip_reg(reg_upper)
        if not reg_key:
            return False, None, 0

        excluded_keys = {_strip_reg(r) for r in (cfg.get("excluded_registrations") or ())}
        excluded_keys.update(_strip_reg(r) for r in CARGO_REGISTRATIONS)
        excluded_keys.update(_strip_reg(r) for r in RETIRED_REGISTRATIONS)
        if reg_key in excluded_keys or is_cargo_registration(reg_upper) or is_cargo_registration(reg_key):
            return False, None, 0

        if (
            airline_name_for_registration(reg_upper) != "Qatar Airways"
            or airline_name_for_registration(reg_key) != "Qatar Airways"
        ):
            return False, None, 0

        frames_meta = self._load_fleet_frames()
        frame_entry = frames_meta.get(reg_key)
        if frame_entry:
            frame_livery = str(frame_entry.get("livery") or "").strip().lower()
            if frame_livery and ("cargo" in frame_livery or "qatar" not in frame_livery):
                return False, None, 0

        livery_lower = str(livery_name or "").strip().lower()
        if livery_lower and livery_lower not in ("standard", "va fleet", "unknown"):
            if "cargo" in livery_lower or "qatar" not in livery_lower:
                return False, None, 0
        elif not frame_entry and not reg_key.startswith("A7"):
            return False, None, 0

        caps = cfg.get("aircraft_capacities") or {}
        candidate_icao = str(icao or (frame_entry or {}).get("icao") or "").strip().upper()
        if not candidate_icao or candidate_icao == "XXXX":
            candidate_icao = self._infer_icao_from_model_name(model_name)

        canonical = self._canonical_icao(candidate_icao)
        capacity = int(caps.get(canonical) or 0)
        if capacity <= 0:
            return False, None, 0

        return True, canonical, capacity

    @staticmethod
    def _infer_icao_from_model_name(model_name: str) -> str:
        text = str(model_name or "").upper()
        if "380" in text:
            return "A388"
        if "777-300" in text or "77W" in text:
            return "B77W"
        if "350-1000" in text or "A35K" in text:
            return "A35K"
        if "350-900" in text or "A359" in text:
            return "A359"
        if "330-300" in text or "A333" in text:
            return "A333"
        if "777-200LR" in text or "B77L" in text:
            return "B77L"
        if "787-8" in text or "B788" in text or "B787" in text:
            return "B788"
        if "321NEO" in text or "A21N" in text:
            return "A21N"
        if "321" in text:
            return "A321"
        if "320" in text:
            return "A320"
        return ""

    async def resolve_pilot_by_discord_id(
        self,
        discord_id: int,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Look up active pilot by Discord ID and resolve missing ifuserid from IFC if needed."""
        pilots_model = getattr(self.bot, "pilots_model", None)
        if pilots_model is None:
            return None, "Pilot database is unavailable."

        pilot = await pilots_model.get_pilot_by_discord_id(str(discord_id))
        if not pilot:
            return None, "No active CrewCenter pilot profile is linked to your Discord account."

        if_user_id = str(pilot.get("ifuserid") or "").strip()
        if not if_user_id:
            ifc_url = str(pilot.get("ifc") or "").strip()
            match = re.search(r"/u/([^/?#]+)", ifc_url, re.IGNORECASE)
            if_api = getattr(self.bot, "if_api_manager", None)
            if match and if_api is not None and hasattr(if_api, "get_user_by_ifc_username"):
                username = match.group(1).strip()
                try:
                    profile = await if_api.get_user_by_ifc_username(username)
                    result = (profile or {}).get("result") or {}
                    resolved = str(result.get("userId") or "").strip()
                    if resolved:
                        await pilots_model.update_ifuserid_by_id(pilot["id"], resolved)
                        pilot["ifuserid"] = resolved
                        if_user_id = resolved
                except Exception as exc:
                    log.warning("Failed resolving ifuserid for %s: %s", username, exc)

        if not if_user_id:
            return None, "Your pilot profile has no Infinite Flight User ID linked. Contact staff."

        return pilot, None

    async def resolve_copilot_by_callsign(
        self,
        callsign_input: str,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Resolve an optional co-pilot by callsign or numeric digits (e.g. 133 -> QRV133)."""
        pilots_model = getattr(self.bot, "pilots_model", None)
        if pilots_model is None:
            return None, "Pilot database is unavailable."

        raw = str(callsign_input or "").strip().upper()
        if not raw:
            return None, "Please provide a valid co-pilot callsign."

        callsign = f"QRV{raw.zfill(3)}" if raw.isdigit() else raw
        pilot = await pilots_model.get_pilot_by_callsign(callsign)
        if not pilot:
            return None, f"No active pilot found with callsign `{callsign}`."
        if not str(pilot.get("ifuserid") or "").strip():
            return None, f"Co-pilot `{callsign}` does not have an Infinite Flight User ID linked."
        return pilot, None

    async def get_pool_summary(self) -> Dict[str, Any]:
        """Return pool counters, board stats, and top Ticket holders."""
        cfg = self.load_config()
        target_pax = int(cfg.get("target_pax") or 5000)
        pool = await self.bot.christmas_model.get_pool_summary(target_pax)
        stats = await self.bot.christmas_model.get_board_stats()
        top_holders = await self.bot.event_transaction_model.get_strict_top_holders(
            limit=5,
            event_name=self._event_name(),
            currency_name=self._ticket_currency(),
        )
        return {
            **pool,
            **stats,
            "top_holders": top_holders,
            "west_min_ratio": float(cfg.get("west_min_pool_ratio") or 0.90),
        }

    async def get_dispatchable_frames_at_hub(self) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """List eligible QR passenger frames parked at OTHH without crewed schedules ahead."""
        manager = getattr(self.bot, "if_live_manager", None)
        org_id = self._org_id()
        if manager is None or not org_id:
            return [], "Infinite Flight Live is not configured or unavailable."

        snapshot = await manager.get_live_fleet(org_id)
        fleet_list = list(getattr(snapshot, "fleet_list", None) or snapshot or [])
        fleet_service = getattr(self.bot, "fleet_service", None)
        if fleet_service is not None and fleet_list:
            await fleet_service.enrich_fleet(fleet_list)

        active_legs = await self.bot.christmas_model.get_active_legs()
        locked_regs = {_strip_reg(leg.get("registration")) for leg in active_legs}

        hub_lat, hub_lon = self._hub_coordinates()
        radius_nm = float(self.load_config().get("hub_parked_radius_nm") or 10.0)

        eligible_frames: List[Dict[str, Any]] = []
        for ac in fleet_list:
            reg = str(_get(ac, "registration", "") or "").strip().upper()
            if not reg or _strip_reg(reg) in locked_regs:
                continue

            if int(_get(ac, "status", 0) or 0) != 0:
                continue
            if _get(ac, "is_stopped", False) or _get(ac, "is_actively_flying", False):
                continue
            if not (_get(ac, "is_on_ground", False) or _get(ac, "is_parked", False)):
                continue

            try:
                lat = float(_get(ac, "latitude"))
                lon = float(_get(ac, "longitude"))
            except (TypeError, ValueError):
                continue
            dist_nm = _haversine_nm(lat, lon, hub_lat, hub_lon)
            if dist_nm > radius_nm:
                continue

            schedules = list(_get(ac, "schedules", []) or [])
            non_terminal = [
                s for s in schedules if _schedule_status(s) not in TERMINAL_SCHEDULE_STATUSES
            ]
            if any(_get(s, "crew", []) for s in non_terminal):
                continue

            db_ac = _get(ac, "db_aircraft") or {}
            db_icao = str(db_ac.get("icao") or "")
            livery_name = str(_get(ac, "livery_name") or db_ac.get("liveryname") or "")
            model_name = str(_get(ac, "model") or db_ac.get("name") or "")

            ok, canonical_icao, capacity = self.is_eligible_qr_passenger_frame(
                registration=reg,
                icao=db_icao,
                livery_name=livery_name,
                model_name=model_name,
            )
            if not ok or not canonical_icao:
                continue

            eligible_frames.append(
                {
                    "if_org_aircraft_id": str(_get(ac, "aircraft_id") or _get(ac, "id") or ""),
                    "content_aircraft_id": str(_get(ac, "content_aircraft_id") or ""),
                    "registration": reg,
                    "icao": canonical_icao,
                    "model": model_name or canonical_icao,
                    "livery_name": livery_name or "Qatar Airways",
                    "capacity": capacity,
                    "distance_nm": round(dist_nm, 1),
                    "uncrewed_schedules_count": len(non_terminal),
                }
            )

        eligible_frames.sort(key=lambda item: item["registration"])
        return eligible_frames, None

    async def _resolve_matching_db_aircraft_ids(
        self,
        content_aircraft_id: str,
        canonical_icao: str,
        model_name: str = "",
    ) -> List[int]:
        """Resolve all CrewCenter aircraft.id values corresponding to a fleet frame."""
        matched_ids: Set[int] = set()
        fleet_service = getattr(self.bot, "fleet_service", None)
        if fleet_service is not None and content_aircraft_id:
            direct = await fleet_service.match_aircraft_by_if_id(content_aircraft_id)
            if direct and direct.get("id") is not None:
                matched_ids.add(int(direct["id"]))

        alias_icao = str((self.load_config().get("icao_aliases") or {}).get(canonical_icao) or "").upper()
        accepted_icaos = {canonical_icao.upper()}
        if alias_icao:
            accepted_icaos.add(alias_icao)

        aircraft_model = getattr(self.bot, "aircraft_model", None)
        if aircraft_model is not None:
            all_rows = await aircraft_model.get_all_aircraft()
            for row in all_rows or []:
                if row.get("id") is None:
                    continue
                livery = str(row.get("liveryname") or "").strip().lower()
                if "cargo" in livery:
                    continue
                if livery and "qatar" not in livery:
                    continue
                row_icao = str(row.get("icao") or "").strip().upper()
                row_name = str(row.get("name") or "").strip().lower()
                if row_icao in accepted_icaos:
                    matched_ids.add(int(row["id"]))
                elif canonical_icao == "A35K" and "350-1000" in row_name:
                    matched_ids.add(int(row["id"]))
                elif model_name and model_name.strip().lower() == row_name and "qatar" in livery:
                    matched_ids.add(int(row["id"]))

        return sorted(matched_ids)

    async def check_operation_and_routes(
        self,
        operation: str,
        frame: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Validate EAST/WEST pool eligibility (including 90% gate) and find matching routes."""
        op = str(operation or "").strip().upper()
        if op not in ("EAST", "WEST"):
            return {"ok": False, "error": f"Unsupported operation `{operation}`."}

        cfg = self.load_config()
        target_pax = int(cfg.get("target_pax") or 5000)
        ratio = float(cfg.get("west_min_pool_ratio") or 0.90)
        capacity = int(frame.get("capacity") or 0)
        pool = await self.bot.christmas_model.get_pool_summary(target_pax)

        if op == "WEST":
            min_required = math.ceil(ratio * capacity)
            othh_available = int(pool.get("othh_available") or 0)
            if othh_available < min_required:
                return {
                    "ok": False,
                    "error": (
                        f"⛔ **West Distribution Unavailable for {frame['registration']} ({frame['icao']})**\n"
                        f"West dispatches require at least **90%** of aircraft capacity available at `{self._hub_icao()}`.\n"
                        f"• Available at `{self._hub_icao()}`: **{othh_available:,} PAX**\n"
                        f"• Required for `{frame['icao']}` ({capacity} seats): **{min_required:,} PAX**\n\n"
                        f"Please select **🟢 East Collection** to bring more passengers into Doha first!"
                    ),
                    "pool": pool,
                }
            allocated_pax = min(capacity, othh_available)
            airports_map = cfg.get("west_airports") or {}
        else:
            east_available = int(pool.get("east_available") or 0)
            if east_available <= 0:
                return {
                    "ok": False,
                    "error": (
                        "🎯 **East Collection Target Reached**\n"
                        f"All **{target_pax:,} passengers** are already collected or currently dispatched inbound. "
                        "Switch to **🔵 West Distribution** to fly passengers onward from Doha!"
                    ),
                    "pool": pool,
                }
            allocated_pax = min(capacity, east_available)
            airports_map = cfg.get("east_airports") or {}

        aircraft_db_ids = await self._resolve_matching_db_aircraft_ids(
            content_aircraft_id=str(frame.get("content_aircraft_id") or ""),
            canonical_icao=str(frame.get("icao") or ""),
            model_name=str(frame.get("model") or ""),
        )
        route_rows = await self.bot.routes_model.find_routes_from_origin_to_destinations_for_aircraft(
            origin_icao=self._hub_icao(),
            destination_icaos=list(airports_map.keys()),
            aircraft_ids=aircraft_db_ids,
        )

        by_dest: Dict[str, Dict[str, Any]] = {}
        for row in route_rows:
            arr = str(row.get("arr") or "").strip().upper()
            if not arr or arr in by_dest:
                continue
            fltnums = [f.strip() for f in str(row.get("fltnum") or "").split(",") if f.strip()]
            outbound_flt = fltnums[0] if fltnums else ""
            return_flts = await self.bot.routes_model.find_flight_numbers_by_icao(arr, self._hub_icao())
            by_dest[arr] = {
                "icao": arr,
                "city": airports_map.get(arr, arr),
                "outbound_fltnum": outbound_flt,
                "return_fltnum": return_flts[0] if return_flts else outbound_flt,
                "duration_seconds": int(row.get("duration") or 0),
            }

        if not by_dest:
            region_word = "Eastern" if op == "EAST" else "Western"
            return {
                "ok": False,
                "error": (
                    f"❌ `{frame['registration']}` (`{frame['icao']}`) does not operate to any "
                    f"designated {region_word} airports for Christmas in our route database."
                ),
                "pool": pool,
            }

        destinations = sorted(by_dest.values(), key=lambda d: d["icao"])
        return {
            "ok": True,
            "operation": op,
            "allocated_pax": allocated_pax,
            "pool": pool,
            "destinations": destinations,
        }

    async def create_dispatch(
        self,
        pilot: Dict[str, Any],
        frame: Dict[str, Any],
        operation: str,
        destination: Dict[str, Any],
        copilot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Atomically reserve PAX, create 2 IF Live legs, and record the dispatch pair."""
        manager = getattr(self.bot, "if_live_manager", None)
        if manager is None:
            return {"ok": False, "error": "Infinite Flight Live is unavailable."}

        pilot_id = int(pilot["id"])
        pilot_if_id = str(pilot.get("ifuserid") or "").strip()
        pilot_callsign = str(pilot.get("callsign") or "PILOT")
        copilot_id = int(copilot["id"]) if copilot else None
        copilot_if_id = str(copilot.get("ifuserid") or "").strip() if copilot else None
        copilot_callsign = str(copilot.get("callsign") or "") if copilot else ""

        if copilot_id is not None and copilot_id == pilot_id:
            return {"ok": False, "error": "You cannot select yourself as your co-pilot."}
        if copilot_id is not None and not copilot_if_id:
            return {"ok": False, "error": "Selected co-pilot has no linked Infinite Flight User ID."}

        max_dispatches = int(self.load_config().get("max_active_dispatches_per_pilot") or 2)
        hub = self._hub_icao()
        dest_icao = str(destination["icao"]).strip().upper()
        reg = str(frame["registration"]).strip().upper()
        if_org_aircraft_id = str(frame["if_org_aircraft_id"])

        async with self._dispatch_lock:
            if await self.bot.christmas_model.count_active_dispatches_for_pilot(pilot_id) >= max_dispatches:
                return {
                    "ok": False,
                    "error": f"You already have the maximum of **{max_dispatches}** active Christmas dispatches.",
                }
            if (
                copilot_id is not None
                and await self.bot.christmas_model.count_active_dispatches_for_pilot(copilot_id) >= max_dispatches
            ):
                return {
                    "ok": False,
                    "error": f"Co-pilot `{copilot_callsign}` already has **{max_dispatches}** active Christmas dispatches.",
                }
            if await self.bot.christmas_model.get_active_legs_for_registration(reg):
                return {
                    "ok": False,
                    "error": f"`{reg}` was just claimed by another pilot. Please choose a different aircraft.",
                }

            recheck = await self.check_operation_and_routes(operation, frame)
            if not recheck.get("ok"):
                return {"ok": False, "error": recheck.get("error")}
            allocated_pax = int(recheck["allocated_pax"])

            dispatch_code = await self.bot.christmas_model.generate_next_dispatch_code()
            leg1_callsign = str(destination.get("outbound_fltnum") or dispatch_code).strip()
            leg2_callsign = str(destination.get("return_fltnum") or leg1_callsign).strip()

            created_ids: List[str] = []
            async with await manager.get_client(self.TOKEN_OWNER_ID) as client:
                live_schedules = await client.list_aircraft_schedules(if_org_aircraft_id)
                active_live = [
                    s for s in (live_schedules or [])
                    if _schedule_status(s) not in TERMINAL_SCHEDULE_STATUSES
                ]
                if any(_get(s, "crew", []) for s in active_live):
                    return {
                        "ok": False,
                        "error": f"`{reg}` now has an assigned crewed schedule on Infinite Flight Live.",
                    }

                try:
                    leg1_req = IFScheduleRequest(
                        callsign=leg1_callsign,
                        flight_type=PersistentFlightType.COMMERCIAL,
                        origin_icao=hub,
                        destination_icao=dest_icao,
                        is_timeless=True,
                        briefing=f"QRV Christmas Ops 2026 | {dispatch_code} | Leg 1 ({operation})",
                    )
                    leg1_sched = await client.create_schedule(if_org_aircraft_id, leg1_req)
                    created_ids.append(str(leg1_sched.id))

                    leg2_req = IFScheduleRequest(
                        callsign=leg2_callsign,
                        flight_type=PersistentFlightType.COMMERCIAL,
                        origin_icao=dest_icao,
                        destination_icao=hub,
                        is_timeless=True,
                        briefing=f"QRV Christmas Ops 2026 | {dispatch_code} | Leg 2 ({operation})",
                    )
                    leg2_sched = await client.create_schedule(if_org_aircraft_id, leg2_req)
                    created_ids.append(str(leg2_sched.id))

                    await client.reorder_schedules(if_org_aircraft_id, str(leg1_sched.id), after_id=None)
                    await client.reorder_schedules(
                        if_org_aircraft_id,
                        str(leg2_sched.id),
                        after_id=str(leg1_sched.id),
                    )

                    await client.assign_crew(
                        str(leg1_sched.id),
                        pilot_if_id,
                        int(PersistentFlightCrewMemberRole.CAPTAIN),
                    )
                    await client.assign_crew(
                        str(leg2_sched.id),
                        pilot_if_id,
                        int(PersistentFlightCrewMemberRole.CAPTAIN),
                    )
                    if copilot_if_id:
                        await client.assign_crew(
                            str(leg1_sched.id),
                            copilot_if_id,
                            int(PersistentFlightCrewMemberRole.FIRST_OFFICER),
                        )
                        await client.assign_crew(
                            str(leg2_sched.id),
                            copilot_if_id,
                            int(PersistentFlightCrewMemberRole.FIRST_OFFICER),
                        )

                    pair = await self.bot.christmas_model.create_dispatch_pair(
                        operation_type=operation,
                        registration=reg,
                        destination_icao=dest_icao,
                        allocated_pax=allocated_pax,
                        leg1_schedule_id=str(leg1_sched.id),
                        leg2_schedule_id=str(leg2_sched.id),
                        pilot_id=pilot_id,
                        copilot_id=copilot_id,
                        dispatch_code=dispatch_code,
                    )
                except Exception as exc:
                    log.exception("Failed creating Christmas dispatch %s on %s", dispatch_code, reg)
                    for sched_id in reversed(created_ids):
                        try:
                            await client.delete_schedule(sched_id)
                        except Exception:
                            log.warning("Cleanup delete_schedule failed for %s", sched_id)
                    return {"ok": False, "error": f"Could not create dispatch: {exc}"}

        ticket_per_pax = int(self.load_config().get("ticket_per_pax") or 1)
        ticket_reward = allocated_pax * ticket_per_pax

        simbrief_leg1 = self._build_simbrief_link(hub, dest_icao, frame["icao"], pilot_callsign, leg1_callsign)
        simbrief_leg2 = self._build_simbrief_link(dest_icao, hub, frame["icao"], pilot_callsign, leg2_callsign)

        return {
            "ok": True,
            "dispatch_code": pair["dispatch_code"],
            "operation": operation,
            "registration": reg,
            "aircraft_icao": frame["icao"],
            "aircraft_model": frame.get("model") or frame["icao"],
            "hub_icao": hub,
            "destination_icao": dest_icao,
            "destination_city": destination.get("city") or dest_icao,
            "allocated_pax": allocated_pax,
            "ticket_reward": ticket_reward,
            "leg1_schedule_id": str(leg1_sched.id),
            "leg2_schedule_id": str(leg2_sched.id),
            "leg1_callsign": leg1_callsign,
            "leg2_callsign": leg2_callsign,
            "simbrief_leg1": simbrief_leg1,
            "simbrief_leg2": simbrief_leg2,
        }

    def _build_simbrief_link(
        self,
        origin: str,
        destination: str,
        aircraft_icao: str,
        callsign: str,
        flight_number: Optional[str] = None,
    ) -> Optional[str]:
        simbrief = getattr(self.bot, "simbrief_service", None)
        if simbrief is None:
            return None
        sb_type = "B788" if aircraft_icao == "B787" else aircraft_icao
        return simbrief.generate_dispatch_link(
            origin=origin,
            destination=destination,
            aircraft_type=sb_type,
            callsign=callsign,
            flight_number=flight_number,
        )

    async def _render_boarding_pass(self, **kwargs: Any) -> Optional[bytes]:
        bp_service = getattr(self.bot, "boarding_pass_service", None)
        if bp_service is None:
            return None
        try:
            return await bp_service.render_boarding_pass(kwargs)
        except Exception as exc:
            log.warning("Boarding pass rendering failed: %s", exc)
            return None

    async def claim_open_return_leg(
        self,
        pilot: Dict[str, Any],
        schedule_id: str,
    ) -> Dict[str, Any]:
        """Claim an OPEN Leg 2 return flight for the given pilot."""
        manager = getattr(self.bot, "if_live_manager", None)
        if manager is None:
            return {"ok": False, "error": "Infinite Flight Live is unavailable."}

        pilot_id = int(pilot["id"])
        pilot_if_id = str(pilot.get("ifuserid") or "").strip()
        pilot_callsign = str(pilot.get("callsign") or "PILOT")
        max_dispatches = int(self.load_config().get("max_active_dispatches_per_pilot") or 2)

        async with self._dispatch_lock:
            if await self.bot.christmas_model.count_active_dispatches_for_pilot(pilot_id) >= max_dispatches:
                return {
                    "ok": False,
                    "error": f"You already have the maximum of **{max_dispatches}** active Christmas dispatches.",
                }

            leg = await self.bot.christmas_model.get_leg_by_schedule_id(schedule_id)
            if not leg or leg.get("status") != "OPEN":
                return {"ok": False, "error": "That open return leg is no longer available."}

            async with await manager.get_client(self.TOKEN_OWNER_ID) as client:
                await client.assign_crew(
                    str(schedule_id),
                    pilot_if_id,
                    int(PersistentFlightCrewMemberRole.CAPTAIN),
                )
                claimed = await self.bot.christmas_model.claim_open_leg(schedule_id, pilot_id)
                if not claimed:
                    try:
                        await client.remove_crew(str(schedule_id), pilot_if_id)
                    except Exception:
                        log.warning("Rollback remove_crew failed for open leg %s", schedule_id)
                    return {"ok": False, "error": "That open return leg was claimed by someone else."}

        cfg = self.load_config()
        origin = str(leg["origin_icao"]).upper()
        dest = str(leg["destination_icao"]).upper()
        frames_meta = self._load_fleet_frames()
        frame_entry = frames_meta.get(_strip_reg(leg["registration"])) or {}
        icao = self._canonical_icao(frame_entry.get("icao") or "A35K")
        allocated_pax = int(leg.get("allocated_pax") or 0)
        ticket_reward = allocated_pax * int(cfg.get("ticket_per_pax") or 1)
        return_flts = await self.bot.routes_model.find_flight_numbers_by_icao(origin, dest)
        flt = return_flts[0] if return_flts else leg["dispatch_code"]
        simbrief = self._build_simbrief_link(origin, dest, icao, pilot_callsign, flt)
        return {
            "ok": True,
            "leg": leg,
            "simbrief_link": simbrief,
            "ticket_reward": ticket_reward,
        }

    async def detect_leg_duration(
        self,
        pilot_discord_id: int,
        copilot_discord_id: Optional[int],
        origin_icao: str,
        destination_icao: str,
    ) -> Optional[Dict[str, Any]]:
        """Auto-detect flight duration from IF API v2 recent flights."""
        filing = getattr(self.bot, "pirep_filing_service", None)
        if filing is None:
            return None

        origin = str(origin_icao).upper()
        dest = str(destination_icao).upper()
        for uid in (pilot_discord_id, copilot_discord_id):
            if not uid:
                continue
            res = await filing.fetch_if_flights(int(uid), limit=6)
            if not res or not res.get("success"):
                continue
            for flight in res.get("flights") or []:
                f_dep = str(flight.get("departure") or "").upper()
                f_arr = str(flight.get("arrival") or "").upper()
                if f_dep == origin and f_arr == dest:
                    mins = self.parse_hhmm_to_minutes(str(flight.get("duration") or ""))
                    if mins is None and flight.get("duration_seconds"):
                        mins = int(flight["duration_seconds"]) // 60
                    if mins and mins > 0:
                        return {
                            "total_minutes": mins,
                            "duration_str": self.format_hhmm(mins),
                            "date": flight.get("date"),
                        }
        return None

    def get_christmas_multiplier(self) -> Dict[str, str]:
        """Return fixed CrewCenter multiplier for Christmas flights (3x / 300000)."""
        cfg = self.load_config()
        raw_override = cfg.get("multiplier_override")
        raw_configured = cfg.get("pirep_multiplier")
        if raw_override is not None and str(raw_override).strip():
            code = str(raw_override).strip()
            label = MULTIPLIER_LABELS.get(code, code)
            return {"multiplier": code, "multiplier_label": label}
        if raw_configured is not None and str(raw_configured).strip():
            code = str(raw_configured).strip()
            configured_label = str(cfg.get("pirep_multiplier_label") or "").strip()
            label = configured_label or MULTIPLIER_LABELS.get(code, code)
            return {"multiplier": code, "multiplier_label": label}
        return {
            "multiplier": DEFAULT_CHRISTMAS_MULTIPLIER,
            "multiplier_label": DEFAULT_CHRISTMAS_MULTIPLIER_LABEL,
        }

    async def submit_cpsc_pirep(
        self,
        caller_pilot: Dict[str, Any],
        caller_discord_id: int,
        leg: Dict[str, Any],
        total_minutes: int,
        copilot_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Submit Christmas PIREP(s) via CrewCenter and link Pilot 1's pirep_id (Option A)."""
        cc_api = getattr(self.bot, "cc_api_manager", None)
        if cc_api is None:
            return {"ok": False, "error": "CrewCenter API is unavailable."}
        if total_minutes <= 0:
            return {"ok": False, "error": "Flight duration must be greater than 0 minutes."}

        schedule_id = str(leg["schedule_id"])
        fresh_leg = await self.bot.christmas_model.get_leg_by_schedule_id(schedule_id)
        if not fresh_leg or fresh_leg.get("status") == "CANCELLED":
            return {"ok": False, "error": "This dispatch leg is no longer valid."}
        if fresh_leg.get("pirep_id") is not None:
            return {
                "ok": False,
                "error": f"A PIREP (`#{fresh_leg['pirep_id']}`) is already linked to this leg.",
            }

        caller_id = int(caller_pilot["id"])
        leg_pilot_id = fresh_leg.get("pilot_id")
        if leg_pilot_id is not None and int(leg_pilot_id) != caller_id:
            captain = fresh_leg.get("pilot_callsign") or f"Pilot #{leg_pilot_id}"
            return {
                "ok": False,
                "error": (
                    f"Only the Captain (`{captain}`) on `{fresh_leg['dispatch_code']}` "
                    "submits `/cpsc` for this leg (which files for both crew members)."
                ),
            }
        if leg_pilot_id is None:
            await self.bot.christmas_model.set_leg_pilot_if_missing(schedule_id, caller_id)
            fresh_leg["pilot_id"] = caller_id

        if copilot_override is not None:
            if int(copilot_override["id"]) == caller_id:
                return {"ok": False, "error": "You cannot set yourself as co-pilot."}
            await self.bot.christmas_model.update_leg_copilot(schedule_id, int(copilot_override["id"]))
            copilot_id: Optional[int] = int(copilot_override["id"])
            copilot_discord_id: Optional[int] = (
                int(copilot_override["discordid"])
                if str(copilot_override.get("discordid") or "").isdigit()
                else None
            )
            copilot_callsign = str(copilot_override.get("callsign") or f"Pilot #{copilot_id}")
        elif fresh_leg.get("copilot_id") is not None and int(fresh_leg["copilot_id"]) != caller_id:
            copilot_id = int(fresh_leg["copilot_id"])
            copilot_discord_id = (
                int(fresh_leg["copilot_discordid"])
                if str(fresh_leg.get("copilot_discordid") or "").isdigit()
                else None
            )
            copilot_callsign = str(fresh_leg.get("copilot_callsign") or f"Pilot #{copilot_id}")
        else:
            copilot_id = None
            copilot_discord_id = None
            copilot_callsign = ""

        dual_pilot = copilot_id is not None
        p1_mins, p2_mins = self.split_duration_minutes(total_minutes, dual_pilot=dual_pilot)
        p1_duration = self.format_hhmm(p1_mins)
        p2_duration = self.format_hhmm(p2_mins)

        cfg = self.load_config()
        aircraft_id = int(cfg.get("pirep_aircraft_id") or 11)
        flight_num = self.format_flight_num(
            fresh_leg["registration"],
            fresh_leg["dispatch_code"],
            int(fresh_leg["leg_number"]),
        )
        origin = str(fresh_leg["origin_icao"]).upper()
        dest = str(fresh_leg["destination_icao"]).upper()
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        mult_info = self.get_christmas_multiplier()
        mult_code = mult_info["multiplier"]
        mult_label = mult_info["multiplier_label"]

        p1_resp = await cc_api.submit_pirep(
            pilot_id=caller_id,
            flight_num=flight_num,
            departure=origin,
            arrival=dest,
            flight_time=p1_duration,
            date=date_str,
            aircraft_id=aircraft_id,
            fuel_used=0,
            multiplier=mult_code,
        )
        if not p1_resp or p1_resp.get("status") != 0:
            return {
                "ok": False,
                "error": f"CrewCenter rejected Pilot 1 PIREP submission: `{p1_resp}`",
            }

        linked_pirep_id = await self.bot.pireps_model.find_latest_pirep_id(
            pilot_id=caller_id,
            flight_num=flight_num,
            departure=origin,
            arrival=dest,
        )
        if linked_pirep_id is None:
            await asyncio.sleep(0.5)
            linked_pirep_id = await self.bot.pireps_model.find_latest_pirep_id(
                pilot_id=caller_id,
                flight_num=flight_num,
                departure=origin,
                arrival=dest,
            )
        if linked_pirep_id is None:
            return {
                "ok": False,
                "error": (
                    "Pilot 1 PIREP was posted to CrewCenter, but its `pirep_id` could not yet be found in MySQL. "
                    "Wait a moment and check CrewCenter before retrying."
                ),
            }

        await self.bot.christmas_model.link_pirep(schedule_id, linked_pirep_id)

        p2_result: Optional[Dict[str, Any]] = None
        if dual_pilot and copilot_id is not None:
            p2_resp = await cc_api.submit_pirep(
                pilot_id=copilot_id,
                flight_num=flight_num,
                departure=origin,
                arrival=dest,
                flight_time=p2_duration,
                date=date_str,
                aircraft_id=aircraft_id,
                fuel_used=0,
                multiplier=mult_code,
            )
            p2_ok = bool(p2_resp and p2_resp.get("status") == 0)
            p2_result = {
                "ok": p2_ok,
                "callsign": copilot_callsign,
                "duration": p2_duration,
                "multiplier_label": mult_label,
                "error": None if p2_ok else f"CrewCenter returned {p2_resp}",
            }

        return {
            "ok": True,
            "flight_num": flight_num,
            "dispatch_code": fresh_leg["dispatch_code"],
            "leg_number": int(fresh_leg["leg_number"]),
            "origin_icao": origin,
            "destination_icao": dest,
            "pirep_id": linked_pirep_id,
            "allocated_pax": int(fresh_leg.get("allocated_pax") or 0),
            "pilot1": {
                "callsign": caller_pilot.get("callsign") or f"Pilot #{caller_id}",
                "duration": p1_duration,
                "multiplier_label": mult_label,
            },
            "pilot2": p2_result,
        }

    async def run_sync_cycle(self) -> Dict[str, Any]:
        """Run one 2-minute sync cycle for IF Live status, recovery detection, and PIREP polling."""
        summary: Dict[str, Any] = {
            "arrived_updated": 0,
            "cancelled_dispatches": 0,
            "reopened_legs": 0,
            "approved_legs": 0,
            "rejected_legs": 0,
            "board_needs_refresh": False,
            "log_events": [],
            "dm_notifications": [],
        }

        await self._sync_if_live_legs(summary)
        await self._poll_pending_pireps(summary)
        return summary

    async def _sync_if_live_legs(self, summary: Dict[str, Any]) -> None:
        manager = getattr(self.bot, "if_live_manager", None)
        org_id = self._org_id()
        if manager is None or not org_id:
            return

        active_legs = await self.bot.christmas_model.get_active_legs()
        if not active_legs:
            return

        try:
            snapshot = await manager.get_live_fleet(org_id)
        except Exception as exc:
            log.warning("Christmas sync could not fetch live fleet: %s", exc)
            return

        schedules_by_id = {
            str(_get(s, "id")): s
            for s in (getattr(snapshot, "all_schedules", None) or [])
            if _get(s, "id")
        }
        cancelled_codes: Set[str] = set()

        for leg in active_legs:
            sched_id = str(leg["schedule_id"])
            dispatch_code = str(leg["dispatch_code"])
            if dispatch_code in cancelled_codes:
                continue

            sched = schedules_by_id.get(sched_id)
            if sched is None:
                continue

            status = _schedule_status(sched)
            if status == int(ScheduledFlightStatus.ARRIVED):
                pilot_id_override, copilot_id_override = await self._resolve_arrived_crew(leg, sched)
                if await self.bot.christmas_model.mark_leg_arrived(
                    sched_id,
                    pilot_id=pilot_id_override,
                    copilot_id=copilot_id_override,
                ):
                    summary["arrived_updated"] += 1
                    summary["board_needs_refresh"] = True
                continue

            crew = list(_get(sched, "crew", []) or [])
            actual_dep = _get(sched, "actual_departure_utc")
            lost_crew_before_takeoff = (not crew and not actual_dep) or (
                status == int(ScheduledFlightStatus.CANCELLED) and not actual_dep
            )
            if not lost_crew_before_takeoff:
                continue

            if int(leg["leg_number"]) == 1:
                await self._cancel_dispatch_with_if_live(dispatch_code)
                cancelled_codes.add(dispatch_code)
                summary["cancelled_dispatches"] += 1
                summary["board_needs_refresh"] = True
                summary["log_events"].append(
                    f"🚫 **Dispatch Cancelled (Unassigned before takeoff):** `{dispatch_code}` "
                    f"(`{leg['registration']}` {leg['origin_icao']} → {leg['destination_icao']})"
                )
            elif int(leg["leg_number"]) == 2 and leg.get("status") == "DISPATCHED":
                if await self.bot.christmas_model.reopen_leg(sched_id):
                    summary["reopened_legs"] += 1
                    summary["board_needs_refresh"] = True
                    summary["log_events"].append(
                        f"🔄 **Return Leg Reopened:** `{dispatch_code}` Leg 2 "
                        f"(`{leg['registration']}` {leg['origin_icao']} → {leg['destination_icao']}) "
                        "is now **OPEN** for any pilot to claim!"
                    )

    async def _resolve_arrived_crew(
        self,
        leg: Dict[str, Any],
        schedule: Any,
    ) -> Tuple[Optional[int], Optional[int]]:
        """Detect Captain/Co-pilot or StoppedFlightMonitor mid-flight recovery on arrived leg."""
        pilots_model = getattr(self.bot, "pilots_model", None)
        if pilots_model is None:
            return None, None

        crew = list(_get(schedule, "crew", []) or [])
        captain_if_id = ""
        fo_if_id = ""
        for member in crew:
            uid = _crew_user_id(member)
            role = _crew_role(member)
            if not uid:
                continue
            if role == int(PersistentFlightCrewMemberRole.CAPTAIN) and not captain_if_id:
                captain_if_id = uid
            elif role == int(PersistentFlightCrewMemberRole.FIRST_OFFICER) and not fo_if_id:
                fo_if_id = uid
            elif not captain_if_id:
                captain_if_id = uid

        recovery_if_id = ""
        stopped_model = getattr(self.bot, "stopped_flight_model", None)
        if stopped_model is not None and hasattr(stopped_model, "get_incident"):
            try:
                incident = await stopped_model.get_incident(str(leg["schedule_id"]))
                if incident and incident.get("claim_user_id"):
                    recovery_if_id = str(incident["claim_user_id"]).strip()
            except Exception as exc:
                log.debug("Could not check stopped flight incident for %s: %s", leg["schedule_id"], exc)

        existing_pilot_id = leg.get("pilot_id")
        existing_pilot_if = str(leg.get("pilot_ifuserid") or "").strip()

        async def _pilot_db_id(if_uid: str) -> Optional[int]:
            if not if_uid:
                return None
            row = await pilots_model.get_active_pilot_by_ifuserid(if_uid)
            return int(row["id"]) if row and row.get("id") is not None else None

        pilot_override: Optional[int] = None
        copilot_override: Optional[int] = None

        if recovery_if_id and recovery_if_id != existing_pilot_if:
            rec_id = await _pilot_db_id(recovery_if_id)
            if rec_id is not None and rec_id != existing_pilot_id:
                copilot_override = rec_id

        if copilot_override is None and fo_if_id:
            fo_id = await _pilot_db_id(fo_if_id)
            if fo_id is not None and fo_id != existing_pilot_id:
                copilot_override = fo_id

        if copilot_override is None and captain_if_id and captain_if_id != existing_pilot_if:
            cap_id = await _pilot_db_id(captain_if_id)
            if cap_id is not None:
                if existing_pilot_id is None:
                    pilot_override = cap_id
                elif cap_id != existing_pilot_id:
                    copilot_override = cap_id

        return pilot_override, copilot_override

    async def _cancel_dispatch_with_if_live(self, dispatch_code: str) -> int:
        legs = await self.bot.christmas_model.get_dispatch_legs(dispatch_code)
        manager = getattr(self.bot, "if_live_manager", None)
        if manager is not None and legs:
            try:
                async with await manager.get_client(self.TOKEN_OWNER_ID) as client:
                    for leg in legs:
                        if leg.get("status") in ("DISPATCHED", "OPEN"):
                            try:
                                await client.delete_schedule(str(leg["schedule_id"]))
                            except IFLiveNotFoundError:
                                pass
                            except Exception as exc:
                                log.warning(
                                    "Could not delete IF Live schedule %s: %s",
                                    leg["schedule_id"],
                                    exc,
                                )
            except Exception as exc:
                log.warning("Could not open IF Live client while cancelling %s: %s", dispatch_code, exc)
        return await self.bot.christmas_model.mark_dispatch_cancelled(dispatch_code)

    async def admin_cancel_dispatch(self, dispatch_code: str) -> Dict[str, Any]:
        """Cancel a dispatch cleanly on IF Live and release its reserved PAX."""
        code = str(dispatch_code or "").strip().upper()
        legs = await self.bot.christmas_model.get_dispatch_legs(code)
        if not legs:
            return {"ok": False, "error": f"No dispatch found with code `{code}`."}
        if any(int(leg.get("pax_credited") or 0) == 1 for leg in legs):
            return {
                "ok": False,
                "error": f"Dispatch `{code}` already has credited PAX and cannot be cancelled.",
            }
        affected = await self._cancel_dispatch_with_if_live(code)
        return {"ok": True, "dispatch_code": code, "cancelled_rows": affected}

    def _resolve_city_name(self, icao: str, cities_map: Dict[str, Any]) -> str:
        code = str(icao or "").strip().upper()
        if code in cities_map and cities_map[code]:
            return str(cities_map[code])
        if code == "OTHH":
            return "Doha"
        flightdata = getattr(self.bot, "flightdata", None)
        if flightdata is not None and hasattr(flightdata, "get_airport_data"):
            airport = flightdata.get_airport_data(code) or {}
            if airport.get("municipality"):
                return str(airport["municipality"])
        return code

    def _resolve_aircraft_display_name(self, registration: str) -> str:
        frames_meta = self._load_fleet_frames()
        entry = frames_meta.get(_strip_reg(registration)) or {}
        if entry.get("model"):
            return str(entry["model"])
        raw_icao = str(entry.get("icao") or "").strip().upper()
        canonical = self._canonical_icao(raw_icao) if raw_icao else ""
        return (
            AIRCRAFT_DISPLAY_NAMES.get(raw_icao)
            or AIRCRAFT_DISPLAY_NAMES.get(canonical)
            or canonical
            or raw_icao
            or "Qatar Passenger"
        )

    @staticmethod
    def _format_ticket_date(raw_date: Any) -> Optional[str]:
        if raw_date is None or raw_date == "":
            return None
        if hasattr(raw_date, "strftime"):
            return raw_date.strftime("%d %b %Y").upper()
        raw_str = str(raw_date).strip()
        if not raw_str:
            return None
        try:
            return datetime.fromisoformat(raw_str[:10]).strftime("%d %b %Y").upper()
        except ValueError:
            return raw_str.upper()

    async def _build_reward_ticket_dms(
        self,
        leg: Dict[str, Any],
        pirep_id: int,
        pirep_row: Dict[str, Any],
        dual_pilot: bool,
        p1_tickets: int,
        p2_tickets: int,
        cfg: Dict[str, Any],
        event_name: str,
        ticket_currency: str,
    ) -> List[Dict[str, Any]]:
        """Render per-pilot Option B v7 Reward Ticket PNGs and prepare DM payloads."""
        dispatch_code = str(leg["dispatch_code"]).strip().upper()
        leg_num = int(leg["leg_number"])
        origin_icao = str(leg["origin_icao"]).strip().upper()
        dest_icao = str(leg["destination_icao"]).strip().upper()
        cities_map = {**(cfg.get("east_airports") or {}), **(cfg.get("west_airports") or {})}
        origin_city = self._resolve_city_name(origin_icao, cities_map)
        dest_city = self._resolve_city_name(dest_icao, cities_map)

        routes_model = getattr(self.bot, "routes_model", None)
        flts: List[str] = []
        if routes_model is not None and hasattr(routes_model, "find_flight_numbers_by_icao"):
            try:
                flts = list(await routes_model.find_flight_numbers_by_icao(origin_icao, dest_icao) or [])
            except Exception as exc:
                log.debug("Could not resolve flight number for %s->%s: %s", origin_icao, dest_icao, exc)
        flight_num = str(flts[0]).strip().upper() if flts else dispatch_code

        reg = str(leg.get("registration") or "A7-XXX").strip().upper()
        aircraft_model = self._resolve_aircraft_display_name(reg)
        flight_date = self._format_ticket_date(pirep_row.get("date"))
        allocated_pax = int(leg.get("allocated_pax") or 0)

        pilot_id = leg.get("pilot_id")
        copilot_id = leg.get("copilot_id")
        p1_name = str(leg.get("pilot_name") or leg.get("pilot_callsign") or f"Pilot #{pilot_id}").strip()
        p1_callsign = str(leg.get("pilot_callsign") or p1_name).strip()
        p2_name_raw = leg.get("copilot_name") or leg.get("copilot_callsign") or (
            f"Pilot #{copilot_id}" if dual_pilot and copilot_id else ""
        )
        p2_name = str(p2_name_raw).strip() or None
        p2_callsign = str(leg.get("copilot_callsign") or p2_name or "").strip()

        recipients = [(pilot_id, p1_tickets, leg.get("pilot_discordid"), p1_name, p1_callsign)]
        if dual_pilot and copilot_id:
            recipients.append((copilot_id, p2_tickets, leg.get("copilot_discordid"), p2_name or p2_callsign, p2_callsign))

        notifications: List[Dict[str, Any]] = []
        pilots_model = getattr(self.bot, "pilots_model", None)
        for rec_pilot_id, rec_tickets, raw_did, rec_name, rec_callsign in recipients:
            if not rec_pilot_id or rec_tickets <= 0:
                continue

            if not raw_did and pilots_model is not None and hasattr(pilots_model, "get_pilot_by_id"):
                try:
                    p_row = await pilots_model.get_pilot_by_id(int(rec_pilot_id))
                    if p_row:
                        raw_did = p_row.get("discordid")
                except Exception as exc:
                    log.debug("Could not look up Discord ID for pilot %s: %s", rec_pilot_id, exc)

            did_str = str(raw_did or "").strip()
            if not did_str.isdigit():
                log.info(
                    "Skipping Reward Ticket DM for %s (%s): no numeric Discord ID.",
                    rec_callsign,
                    dispatch_code,
                )
                continue

            new_balance = await self.bot.event_transaction_model.get_strict_balance(
                int(rec_pilot_id),
                event_name=event_name,
                currency_name=ticket_currency,
            )
            png_bytes = await self._render_boarding_pass(
                dispatch_code=dispatch_code,
                flight_number=flight_num,
                origin=origin_icao,
                destination=dest_icao,
                origin_city=origin_city,
                destination_city=dest_city,
                registration=reg,
                aircraft_model=aircraft_model,
                pilot_name=p1_name,
                copilot_name=p2_name if dual_pilot else None,
                allocated_pax=allocated_pax,
                flight_date=flight_date,
                tickets_earned=rec_tickets,
            )
            notifications.append(
                {
                    "discord_id": int(did_str),
                    "pilot_id": int(rec_pilot_id),
                    "pilot_callsign": rec_callsign,
                    "pilot_name": rec_name,
                    "dispatch_code": dispatch_code,
                    "leg_number": leg_num,
                    "pirep_id": pirep_id,
                    "flight_number": flight_num,
                    "origin_icao": origin_icao,
                    "destination_icao": dest_icao,
                    "origin_city": origin_city,
                    "destination_city": dest_city,
                    "allocated_pax": allocated_pax,
                    "tickets_earned": int(rec_tickets),
                    "new_balance": int(new_balance),
                    "dual_pilot": dual_pilot,
                    "png_bytes": png_bytes,
                }
            )
        return notifications

    async def _poll_pending_pireps(self, summary: Dict[str, Any]) -> None:
        pending_legs = await self.bot.christmas_model.get_pending_pirep_legs()
        if not pending_legs:
            return

        pirep_ids = [int(leg["pirep_id"]) for leg in pending_legs if leg.get("pirep_id") is not None]
        pireps_by_id = await self.bot.pireps_model.get_pireps_statuses(pirep_ids)
        if not pireps_by_id:
            return

        cfg = self.load_config()
        ticket_per_pax = int(cfg.get("ticket_per_pax") or 1)
        event_name = self._event_name()
        ticket_currency = self._ticket_currency()

        for leg in pending_legs:
            pirep_id = int(leg["pirep_id"])
            pirep_row = pireps_by_id.get(pirep_id)
            if not pirep_row:
                continue

            status = pirep_row.get("status")
            sched_id = str(leg["schedule_id"])
            dispatch_code = str(leg["dispatch_code"])
            leg_num = int(leg["leg_number"])

            if status == 2:
                await self.bot.christmas_model.clear_pirep_link(sched_id)
                summary["rejected_legs"] += 1
                summary["log_events"].append(
                    f"❌ **PIREP Rejected:** `{dispatch_code}` Leg {leg_num} (`PIREP #{pirep_id}`) "
                    "was rejected — link cleared so the pilot can fix and re-run `/cpsc`."
                )
                continue

            if status != 1:
                continue

            if leg.get("status") in ("DISPATCHED", "OPEN"):
                await self.bot.christmas_model.mark_leg_arrived(sched_id)

            allocated_pax = int(leg.get("allocated_pax") or 0)
            if allocated_pax <= 0:
                if await self.bot.christmas_model.mark_empty_leg_processed(sched_id):
                    summary["approved_legs"] += 1
                    summary["board_needs_refresh"] = True
                    summary["log_events"].append(
                        f"✈️ **Empty Ferry Leg Approved:** `{dispatch_code}` Leg {leg_num} "
                        f"(`{leg['registration']}` {leg['origin_icao']} → {leg['destination_icao']}, PIREP #{pirep_id})"
                    )
                continue

            credited = await self.bot.christmas_model.claim_pax_credit(sched_id)
            if not credited:
                continue

            pilot_id = leg.get("pilot_id")
            copilot_id = leg.get("copilot_id")
            dual_pilot = bool(pilot_id and copilot_id and int(pilot_id) != int(copilot_id))
            total_tickets = allocated_pax * ticket_per_pax
            p1_tickets, p2_tickets = self.split_tickets(total_tickets, dual_pilot=dual_pilot)
            reason = f"Christmas Ops {dispatch_code} Leg {leg_num} (PIREP #{pirep_id})"

            if pilot_id and p1_tickets > 0:
                already_p1 = await self.bot.event_transaction_model.check_strict_duplicate(
                    int(pilot_id),
                    reason,
                    event_name=event_name,
                    currency_name=ticket_currency,
                )
                if not already_p1:
                    await self.bot.event_transaction_model.add_strict_transaction(
                        int(pilot_id),
                        p1_tickets,
                        reason,
                        event_name=event_name,
                        currency_name=ticket_currency,
                    )

            if dual_pilot and copilot_id and p2_tickets > 0:
                already_p2 = await self.bot.event_transaction_model.check_strict_duplicate(
                    int(copilot_id),
                    reason,
                    event_name=event_name,
                    currency_name=ticket_currency,
                )
                if not already_p2:
                    await self.bot.event_transaction_model.add_strict_transaction(
                        int(copilot_id),
                        p2_tickets,
                        reason,
                        event_name=event_name,
                        currency_name=ticket_currency,
                    )

            summary["approved_legs"] += 1
            summary["board_needs_refresh"] = True

            p1_label = leg.get("pilot_callsign") or f"Pilot #{pilot_id}"
            if dual_pilot:
                p2_label = leg.get("copilot_callsign") or f"Pilot #{copilot_id}"
                award_text = f"**+{p1_tickets:,}** to `{p1_label}` & **+{p2_tickets:,}** to `{p2_label}`"
            else:
                award_text = f"**+{p1_tickets:,}** to `{p1_label}`"

            summary["log_events"].append(
                f"🎟️ **Christmas Tickets Credited:** `{dispatch_code}` Leg {leg_num} "
                f"(`{leg['origin_icao']} → {leg['destination_icao']}`, **{allocated_pax:,} PAX**, PIREP #{pirep_id}) — "
                f"{award_text}"
            )
            dm_entries = await self._build_reward_ticket_dms(
                leg=leg,
                pirep_id=pirep_id,
                pirep_row=pirep_row,
                dual_pilot=dual_pilot,
                p1_tickets=p1_tickets,
                p2_tickets=p2_tickets,
                cfg=cfg,
                event_name=event_name,
                ticket_currency=ticket_currency,
            )
            summary["dm_notifications"].extend(dm_entries)

    async def get_pilot_ticket_summary(self, pilot_id: int) -> Dict[str, Any]:
        """Return strictly isolated christmas_2026 Ticket balance, leaderboard, and recent rows."""
        ev = self._event_name()
        curr = self._ticket_currency()
        balance = await self.bot.event_transaction_model.get_strict_balance(
            int(pilot_id),
            event_name=ev,
            currency_name=curr,
        )
        top_holders = await self.bot.event_transaction_model.get_strict_top_holders(
            limit=10,
            event_name=ev,
            currency_name=curr,
        )
        recent = await self.bot.event_transaction_model.get_strict_records(
            limit=5,
            pilot_id=int(pilot_id),
            event_name=ev,
            currency_name=curr,
        )
        return {
            "balance": balance,
            "top_holders": top_holders,
            "recent": recent,
        }

    async def enrich_leg_with_recovery_copilot(self, leg: Dict[str, Any]) -> Dict[str, Any]:
        """Populate co-pilot details from StoppedFlightModel if a leg was recovered before sync ran."""
        if leg.get("copilot_id"):
            return leg
        stopped_model = getattr(self.bot, "stopped_flight_model", None)
        pilots_model = getattr(self.bot, "pilots_model", None)
        if stopped_model is None or pilots_model is None or not hasattr(stopped_model, "get_incident"):
            return leg
        try:
            incident = await stopped_model.get_incident(str(leg["schedule_id"]))
            claim_uid = str((incident or {}).get("claim_user_id") or "").strip()
            if not claim_uid:
                return leg
            rec_pilot = await pilots_model.get_active_pilot_by_ifuserid(claim_uid)
            if rec_pilot and rec_pilot.get("id") and int(rec_pilot["id"]) != int(leg.get("pilot_id") or 0):
                enriched = dict(leg)
                enriched["copilot_id"] = int(rec_pilot["id"])
                enriched["copilot_callsign"] = rec_pilot.get("callsign")
                enriched["copilot_discordid"] = rec_pilot.get("discordid")
                enriched["copilot_ifuserid"] = rec_pilot.get("ifuserid")
                await self.bot.christmas_model.update_leg_copilot(str(leg["schedule_id"]), int(rec_pilot["id"]))
                return enriched
        except Exception as exc:
            log.debug("Recovery co-pilot check failed for %s: %s", leg.get("schedule_id"), exc)
        return leg

    async def admin_adjust_tickets(
        self,
        pilot_id: int,
        amount: int,
        reason: str,
    ) -> bool:
        """Apply an admin Ticket credit/debit strictly in `christmas_2026`."""
        return await self.bot.event_transaction_model.add_strict_transaction(
            int(pilot_id),
            int(amount),
            reason,
            event_name=self._event_name(),
            currency_name=self._ticket_currency(),
        )

    async def exchange_tickets_to_cookies(
        self,
        pilot_id: int,
        ticket_amount: int,
    ) -> Dict[str, Any]:
        """Phase 2 helper stub: atomically exchange ChristmasTickets into Cookies."""
        qty = int(ticket_amount)
        if qty <= 0:
            return {"ok": False, "error": "Ticket amount must be positive."}

        ev = self._event_name()
        ticket_curr = self._ticket_currency()
        cookie_curr = self._cookie_currency()
        rate = int(self.load_config().get("ticket_to_cookie_rate") or 1)

        balance = await self.bot.event_transaction_model.get_strict_balance(
            int(pilot_id),
            event_name=ev,
            currency_name=ticket_curr,
        )
        if balance < qty:
            return {
                "ok": False,
                "error": f"Insufficient ChristmasTickets (have {balance:,}, need {qty:,}).",
            }

        cookies = qty * rate
        debited = await self.bot.event_transaction_model.add_strict_transaction(
            int(pilot_id),
            -qty,
            f"Ticket Exchange -> {cookies} {cookie_curr}",
            event_name=ev,
            currency_name=ticket_curr,
        )
        if not debited:
            return {"ok": False, "error": "Failed to debit ChristmasTickets."}

        credited = await self.bot.event_transaction_model.add_strict_transaction(
            int(pilot_id),
            cookies,
            f"Ticket Exchange <- {qty} {ticket_curr}",
            event_name=ev,
            currency_name=cookie_curr,
        )
        if not credited:
            await self.bot.event_transaction_model.add_strict_transaction(
                int(pilot_id),
                qty,
                "Rollback failed Cookie exchange",
                event_name=ev,
                currency_name=ticket_curr,
            )
            return {"ok": False, "error": "Failed to credit Cookies."}

        return {
            "ok": True,
            "tickets_spent": qty,
            "cookies_awarded": cookies,
            "remaining_tickets": balance - qty,
        }
