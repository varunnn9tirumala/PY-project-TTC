"""National demo: intelligent railway traffic control center (Flask)."""

from __future__ import annotations

import copy
import csv
import io
import json
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

# Initialize Flask application
app = Flask(__name__) 
app.secret_key = "dev-section-controller-key"


@dataclass
class Train:
    """Data class representing a train entity and its operational parameters."""
    code: str
    name: str
    section: str
    category: str
    priority: int  # higher = more important
    planned_entry: datetime
    section_run_min: int
    delay_min: int = 0
    platform_need: int = 1
    source: str = ""
    destination: str = ""
    zone: str = ""
    state: str = ""
    avg_speed_kmph: int = 60
    avg_speed_kmph: int = 60
    delay_probability: float = 0.15
    stops: list[str] | None = None
    schedule: list[dict[str, Any]] | None = None

    def entry_effective(self) -> datetime:
        """Calculates actual entry time by adding delay to the planned entry."""
        return self.planned_entry + timedelta(minutes=self.delay_min)


# Define path for static data storage (JSON/CSV files)
DATA_DIR = Path(__file__).parent / "static" / "data"


def _load_json(name: str) -> dict[str, Any]:
    """Helper to load JSON data from the static data directory."""
    with (DATA_DIR / name).open("r", encoding="utf-8") as f:
        return json.load(f)


def _build_zone_rules(network: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Extracts headway and platform capacity rules for each route in the network."""
    rules: dict[str, dict[str, int]] = {}
    for route in network["routes"]:
        rules[route["id"]] = {
            "headway_min": int(route.get("headway_min", 3)),
            "platform_capacity": int(route.get("platform_capacity", 2)),
        }
    return rules


def _seed_trains(now: datetime, network: dict[str, Any]) -> list[Train]:
    """Generates initial dummy train data for the demo based on catalog templates."""
    base = now.replace(second=0, microsecond=0)
    routes = network["routes"]
    catalog = _load_json("train_catalog.json")["train_templates"]
    rng = random.Random(42)
    seeded: list[Train] = []
    # 220+ active trains for all-India demo mode.
    for i in range(220):
        tpl = catalog[i % len(catalog)]
        route = routes[i % len(routes)]
        delay_prob = float(tpl.get("delay_probability", 0.2))
        delay = rng.randint(2, 24) if rng.random() < delay_prob else 0
        seeded.append(
            Train(
                code=str(int(tpl["base_no"]) + i),
                name=f"{tpl['name']} {i % 7 + 1}",
                section=route["id"],
                category=tpl["category"],
                priority=int(tpl["priority"]),
                planned_entry=base + timedelta(minutes=(i * 2) % 240),
                section_run_min=int(route["run_min"]),
                delay_min=delay,
                platform_need=int(tpl["platform_need"]),
                source=route["from"],
                destination=route["to"],
                zone=route["zone"],
                state=route["state"],
                avg_speed_kmph=int(tpl.get("avg_speed_kmph", 60)),
                delay_probability=delay_prob,
                stops=route.get("stops", []),
            )
        )
    return seeded


# Global In-Memory State to store application data during runtime
STATE: dict[str, Any] = {
    "trains": [],
    "last_plan": [],
    "audit": [],
    "section_name": "India Rail Intelligent Traffic Control Center",
    "section_rules": {},
    "network": {},
    "history": [],
}


def _log(event: str, detail: str) -> None:
    """Adds a timestamped entry to the audit log for tracking system actions."""
    STATE["audit"].insert(
        0,
        {
            "id": str(uuid.uuid4())[:8],
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "detail": detail,
        },
    )
    STATE["audit"] = STATE["audit"][:200]


def _serialize_train(t: Train, rec_entry: datetime | None = None, rec_exit: datetime | None = None) -> dict[str, Any]:
    """Converts Train object to a dictionary for JSON/Frontend consumption."""
    entry = rec_entry or t.entry_effective()
    exit_ = rec_exit or (entry + timedelta(minutes=t.section_run_min))
    hold_min = max(0, int((entry - t.entry_effective()).total_seconds() // 60))
    decision = "PROCEED" if hold_min == 0 else "HOLD"
    return {
        "code": t.code,
        "name": t.name,
        "section": t.section,
        "category": t.category,
        "priority": t.priority,
        "planned_entry": t.planned_entry.strftime("%H:%M"),
        "delay_min": t.delay_min,
        "section_run_min": t.section_run_min,
        "entry_effective": t.entry_effective().strftime("%H:%M"),
        "recommended_entry": entry.strftime("%H:%M"),
        "recommended_exit": exit_.strftime("%H:%M"),
        "hold_min": hold_min,
        "platform_need": t.platform_need,
        "decision": decision,
        "source": t.source,
        "destination": t.destination,
        "zone": t.zone,
        "state": t.state,
        "avg_speed_kmph": t.avg_speed_kmph,
        "rerouted_to": "",
        "action_note": (
            "Clear signal and dispatch."
            if decision == "PROCEED"
            else f"Hold at control point for {hold_min} min, then dispatch."
        ),
        "schedule": getattr(t, "schedule", None),
    }


def _route_meta(section_id: str) -> dict[str, Any] | None:
    """Fetches route configuration data for a specific section ID."""
    for route in STATE["network"].get("routes", []):
        if route["id"] == section_id:
            return route
    return None


def _suggest_alternative_route(train: Train, section_load: dict[str, int]) -> str | None:
    """Suggests a lighter route based on current section load and compatibility."""
    current = _route_meta(train.section)
    if not current:
        return None
    candidates = []
    for route in STATE["network"].get("routes", []):
        if route["id"] == train.section:
            continue
        # Keep reroute realistic: similar runtime and at least same broad region.
        if route["state"] != current["state"] and route["zone"] != current["zone"]:
            continue
        if abs(int(route["run_min"]) - int(current["run_min"])) > 8:
            continue
        candidates.append(route)
    if not candidates:
        return None
    best = min(candidates, key=lambda r: section_load.get(r["id"], 0))
    if section_load.get(best["id"], 0) < section_load.get(train.section, 0):
        return best["id"]
    return None


def optimize_plan(trains: list[Train]) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Main Scheduling Engine:
    1. Reroutes low-priority trains if a section is congested.
    2. Sorts trains by priority and entry time.
    3. Resolves headway and platform conflicts using a greedy timeline approach.
    """
    planning_trains = copy.deepcopy(trains)
    section_load: dict[str, int] = {}
    for t in planning_trains:
        section_load[t.section] = section_load.get(t.section, 0) + 1

    # Smart rerouting pass for lower-priority services under heavy congestion.
    rerouted: dict[str, tuple[str, str]] = {}
    for t in planning_trains:
        if section_load.get(t.section, 0) < 10:
            continue
        if t.priority >= 78:
            continue
        if t.category in {"Vande Bharat", "Rajdhani", "Shatabdi", "Duronto"}:
            continue
        alt = _suggest_alternative_route(t, section_load)
        if alt:
            section_load[t.section] -= 1
            section_load[alt] = section_load.get(alt, 0) + 1
            rerouted[t.code] = (t.section, alt)
            t.section = alt
            alt_meta = _route_meta(alt)
            if alt_meta:
                t.source = alt_meta["from"]
                t.destination = alt_meta["to"]
                t.zone = alt_meta["zone"]
                t.state = alt_meta["state"]
                t.section_run_min = int(alt_meta["run_min"])

    # Sorting logic: Section ID first, then highest priority, then earliest effective entry
    ordered = sorted(planning_trains, key=lambda x: (x.section, -x.priority, x.entry_effective()))
    explanations: list[str] = []
    
    # Track segment timeline: segment_id -> list of (start_time, end_time, train_priority, train_code)
    segment_occupancy: dict[str, list[tuple[datetime, datetime, int, str]]] = {}
    timeline_end_by_section: dict[str, datetime] = {}
    platform_occupancy: dict[str, list[tuple[datetime, datetime, int]]] = {}
    results: list[dict[str, Any]] = []

    for t in ordered:
        route_meta = _route_meta(t.section)
        rule = STATE["section_rules"].get(t.section, {"headway_min": 3, "platform_capacity": 2})
        headway_min = int(rule["headway_min"])
        platform_capacity = int(rule["platform_capacity"])
        
        start = t.entry_effective()
        t.schedule = []
        
        if route_meta and "segments" in route_meta:
            current_time = start
            for seg in route_meta["segments"]:
                seg_id = f"{t.section}_{seg['from']}_{seg['to']}"
                km = float(seg["km"])
                
                # Base dynamic speed
                target_speed = t.avg_speed_kmph
                if t.delay_min > 0:
                    # Make up time if delayed
                    target_speed = min(130, int(t.avg_speed_kmph * 1.15))
                    
                min_speed = max(30, int(t.avg_speed_kmph * 0.5))
                run_min = max(2, int((km / target_speed) * 60))
                
                occupancy = segment_occupancy.setdefault(seg_id, [])
                
                signal = "GREEN"
                actual_speed = target_speed
                hold_at_start = 0
                
                # Check for conflicts on this pin-to-pin segment
                while True:
                    conflict = False
                    for occ_start, occ_end, occ_pri, occ_code in occupancy:
                        # Headway: must be headway_min clear of any existing train
                        if not (current_time + timedelta(minutes=run_min) + timedelta(minutes=headway_min) <= occ_start or 
                                current_time >= occ_end + timedelta(minutes=headway_min)):
                            conflict = True
                            wait_until = occ_end + timedelta(minutes=headway_min)
                            
                            # Attempt pacing (YELLOW) if we are arriving too early behind another train
                            if wait_until > current_time:
                                pacing_run_min = int((wait_until - current_time).total_seconds() / 60)
                                if pacing_run_min > run_min:
                                    pacing_speed = int((km / pacing_run_min) * 60)
                                    if pacing_speed >= min_speed:
                                        # Pacing successful, we don't need to hold
                                        run_min = pacing_run_min
                                        actual_speed = pacing_speed
                                        signal = "YELLOW"
                                        conflict = False
                                        explanations.append(f"Train {t.code} pacing at {actual_speed} km/h (YELLOW) behind {occ_code} to {seg['to']}.")
                                        break
                                
                            if conflict and wait_until > current_time:
                                hold_time = int((wait_until - current_time).total_seconds() / 60)
                                if hold_time > 0:
                                    hold_at_start += hold_time
                                    current_time = wait_until
                                    signal = "RED"
                                    # Revert to target_speed after holding
                                    actual_speed = target_speed
                                    run_min = max(2, int((km / target_speed) * 60))
                            break
                    if not conflict:
                        break
                
                if hold_at_start > 0:
                    explanations.append(f"Train {t.code} held {hold_at_start} min at {seg['from']} siding (RED) to clear route.")
                
                end_time = current_time + timedelta(minutes=run_min)
                occupancy.append((current_time, end_time, t.priority, t.code))
                
                t.schedule.append({
                    "from": seg["from"],
                    "to": seg["to"],
                    "km": km,
                    "start": current_time.strftime("%H:%M:%S"),
                    "end": end_time.strftime("%H:%M:%S"),
                    "start_dt": current_time.isoformat(),
                    "end_dt": end_time.isoformat(),
                    "speed": actual_speed,
                    "run_min": run_min,
                    "signal": signal
                })
                current_time = end_time
            
            end = current_time
            t.section_run_min = int((end - start).total_seconds() / 60)
            
            # Simple platform check at destination
            if t.platform_need > 0:
                platform_occupancy.setdefault(t.section, []).append((start, end, t.platform_need))
                
        else:
            # Fallback for routes without segments
            last_end = timeline_end_by_section.get(t.section)
            if last_end is not None and start < (last_end + timedelta(minutes=headway_min)):
                blocked_until = last_end + timedelta(minutes=headway_min)
                hold = int((blocked_until - start).total_seconds() // 60)
                start = blocked_until
                explanations.append(
                    f"Train {t.code} held {hold} min to clear express route in {t.section}."
                )

            if t.platform_need > 0:
                while True:
                    end = start + timedelta(minutes=t.section_run_min)
                    active_platforms = 0
                    for p_start, p_end, need in platform_occupancy.get(t.section, []):
                        if p_start < end and p_end > start:
                            active_platforms += need
                    if active_platforms + t.platform_need <= platform_capacity:
                        break
                    start += timedelta(minutes=1)
                platform_hold = int((start - t.entry_effective()).total_seconds() // 60)
                if platform_hold > 0:
                    explanations.append(
                        f"Platform reassigned for {t.code}; delayed to reduce congestion in {t.section}."
                    )

            end = start + timedelta(minutes=t.section_run_min)
            timeline_end_by_section[t.section] = end
            if t.platform_need > 0:
                platform_occupancy.setdefault(t.section, []).append((start, end, t.platform_need))
            
            t.schedule = [{
                "from": t.source,
                "to": t.destination,
                "km": 0,
                "start": start.strftime("%H:%M:%S"),
                "end": end.strftime("%H:%M:%S"),
                "start_dt": start.isoformat(),
                "end_dt": end.isoformat(),
                "speed": t.avg_speed_kmph,
                "run_min": t.section_run_min,
                "signal": "GREEN"
            }]

        row = _serialize_train(t, start, end)
        if t.code in rerouted:
            from_section, to_section = rerouted[t.code]
            row["rerouted_to"] = to_section
            row["action_note"] = (
                f"Rerouted from {from_section} to {to_section} for congestion balancing."
            )
            explanations.append(
                f"Train {t.code} dynamically rerouted from {from_section} to {to_section} to reduce corridor congestion."
            )
        results.append(row)

    # Present in timetable order for the UI
    by_code = {r["code"]: r for r in results}
    table_rows = [by_code[t.code] for t in sorted(planning_trains, key=lambda x: x.planned_entry)]
    return table_rows, explanations


def init_state() -> None:
    """Resets and initializes the application state from source files."""
    now = datetime.now()
    network = _load_json("india_network.json")
    STATE["network"] = network
    STATE["section_rules"] = _build_zone_rules(network)
    STATE["trains"] = _seed_trains(now, network)
    rows, expl = optimize_plan(STATE["trains"])
    STATE["last_plan"] = rows
    STATE["audit"] = []
    STATE["history"] = []
    _log(
        "INIT",
        f"Loaded India-wide demo with {len(STATE['trains'])} active trains across {len(STATE['section_rules'])} routes.",
    )


def _parse_csv_trains(file_content: str, now: datetime) -> list[Train]:
    """Validates and parses uploaded CSV data into Train objects."""
    reader = csv.DictReader(io.StringIO(file_content))
    required = {
        "code",
        "name",
        "section",
        "category",
        "priority",
        "entry_offset_min",
        "section_run_min",
        "delay_min",
        "platform_need",
    }
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        missing = sorted(required.difference(set(reader.fieldnames or [])))
        raise ValueError(f"CSV missing columns: {', '.join(missing)}")

    parsed: list[Train] = []
    seen_codes: set[str] = set()
    for idx, row in enumerate(reader, start=2):
        code = (row.get("code") or "").strip().upper()
        if not code:
            raise ValueError(f"Row {idx}: code is required")
        if code in seen_codes:
            raise ValueError(f"Row {idx}: duplicate code {code}")
        seen_codes.add(code)
        section = (row.get("section") or "").strip().upper()
        if section not in STATE["section_rules"]:
            raise ValueError(f"Row {idx}: unknown section {section}")
        parsed.append(
            Train(
                code=code,
                name=(row.get("name") or "").strip() or f"Train {code}",
                section=section,
                category=(row.get("category") or "").strip() or "Express",
                priority=max(0, min(100, int(row.get("priority") or 50))),
                planned_entry=now.replace(second=0, microsecond=0) + timedelta(minutes=int(row.get("entry_offset_min") or 0)),
                section_run_min=max(5, int(row.get("section_run_min") or 20)),
                delay_min=max(0, int(row.get("delay_min") or 0)),
                platform_need=max(0, int(row.get("platform_need") or 1)),
            )
        )
    if not parsed:
        raise ValueError("CSV has no train rows")
    return parsed


@app.before_request
def ensure_state() -> None:
    """Ensures state is initialized before any request is processed."""
    if not STATE["trains"]:
        init_state()


@app.route("/")
def dashboard():
    """Main landing page displaying metrics, network map, and current plan."""
    rows, explanations = optimize_plan(STATE["trains"])
    STATE["last_plan"] = rows
    kpis = compute_kpis(rows)
    STATE["history"].append(
        {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "throughput": kpis["throughput"],
            "avg_delay": kpis["avg_hold"],
            "conflicts": kpis["conflicts_prevented"],
            "utilization": kpis["utilization_pct"],
        }
    )
    STATE["history"] = STATE["history"][-60:]
    return render_template(
        "index.html",
        page="dashboard",
        section=STATE["section_name"],
        trains=rows,
        explanations=explanations,
        kpis=kpis,
        sections=sorted(STATE["section_rules"].items()),
        network=STATE["network"],
    )


@app.route("/control-room")
def control_room():
    """Operational view focusing on real-time train movements and dispatch instructions."""
    rows, explanations = optimize_plan(STATE["trains"])
    kpis = compute_kpis(rows)
    return render_template(
        "control_room.html",
        page="controlroom",
        section=STATE["section_name"],
        trains=rows[:80],
        explanations=explanations[:60],
        kpis=kpis,
    )


@app.route("/what-if", methods=["GET", "POST"])
def what_if():
    """Simulation engine to test network resilience under various scenarios."""
    message = ""
    preview_rows: list[dict[str, Any]] | None = None
    preview_expl: list[str] | None = None
    base_rows, _ = optimize_plan(STATE["trains"])
    base_kpis = compute_kpis(base_rows)
    if request.method == "POST":
        scenario = (request.form.get("scenario") or "custom").strip()
        code = (request.form.get("train_code") or "").strip().upper()
        extra = int(request.form.get("extra_delay") or 0)
        tmp = copy.deepcopy(STATE["trains"])
        if scenario == "rain":
            for t in tmp:
                t.delay_min += 5
        elif scenario == "signal_failure":
            for t in tmp[:40]:
                t.delay_min += 12
        elif scenario == "breakdown":
            for t in tmp[:10]:
                t.delay_min += 20
        elif scenario == "platform_blocked":
            for key in STATE["section_rules"]:
                STATE["section_rules"][key]["platform_capacity"] = max(1, STATE["section_rules"][key]["platform_capacity"] - 1)
        elif scenario == "peak_hour":
            for t in tmp:
                t.delay_min += 4
        elif scenario == "special_train_added":
            first_route = next(iter(STATE["section_rules"]))
            tmp.append(
                Train(
                    code="09999",
                    name="Special Relief Express",
                    section=first_route,
                    category="Special",
                    priority=97,
                    planned_entry=datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=15),
                    section_run_min=24,
                    delay_min=0,
                    platform_need=1,
                    source="New Delhi",
                    destination="Howrah",
                    zone="NR",
                    state="Delhi",
                )
            )
        found = False
        if code:
            for t in tmp:
                if t.code == code:
                    t.delay_min += extra
                    found = True
                    break
        if code and not found:
            message = f"No train with code {code}."
        else:
            preview_rows, preview_expl = optimize_plan(tmp)
            preview_kpis = compute_kpis(preview_rows)
            _log("WHAT-IF", f"Scenario {scenario} simulated. Delta avg delay {preview_kpis['avg_hold'] - base_kpis['avg_hold']:+.1f}.")
            message = "Scenario simulation complete."
    return render_template(
        "whatif.html",
        page="whatif",
        section=STATE["section_name"],
        message=message,
        preview=preview_rows,
        preview_expl=preview_expl or [],
        trains=STATE["last_plan"],
        before=base_kpis,
        after=compute_kpis(preview_rows) if preview_rows else None,
    )


@app.route("/override", methods=["GET", "POST"])
def override():
    """Manual controller interface to adjust train priorities dynamically."""
    message = ""
    if request.method == "POST":
        code = (request.form.get("train_code") or "").strip().upper()
        bump = int(request.form.get("priority_bump") or 0)
        found = False
        for t in STATE["trains"]:
            if t.code == code:
                t.priority = max(0, min(100, t.priority + bump))
                found = True
                break
        if not found:
            message = f"No train with code {code}."
        else:
            _log("OVERRIDE", f"Controller adjusted priority of {code} by {bump:+d}.")
            message = f"Updated operational priority for {code}."
    rows, _ = optimize_plan(STATE["trains"])
    STATE["last_plan"] = rows
    return render_template(
        "override.html",
        page="override",
        section=STATE["section_name"],
        message=message,
        trains=rows,
    )


@app.route("/reoptimize", methods=["POST"])
def reoptimize():
    """Manual trigger to rerun the scheduling algorithm."""
    _log("REOPTIMIZE", "Controller triggered full section re-optimization.")
    return redirect(url_for("dashboard"))


@app.route("/reset-demo", methods=["POST"])
def reset_demo():
    """Resets state to baseline demo data."""
    init_state()
    _log("RESET", "Demo data restored to baseline.")
    return redirect(url_for("dashboard"))


@app.route("/upload-csv", methods=["POST"])
def upload_csv():
    """Handles bulk train data upload via CSV."""
    uploaded = request.files.get("train_csv")
    if not uploaded:
        _log("UPLOAD_FAIL", "CSV upload attempted without a file.")
        return redirect(url_for("dashboard"))
    try:
        text = uploaded.read().decode("utf-8")
        STATE["trains"] = _parse_csv_trains(text, datetime.now())
        _log("UPLOAD_OK", f"Loaded {len(STATE['trains'])} trains from CSV upload.")
    except Exception as exc:
        _log("UPLOAD_FAIL", f"CSV rejected: {exc}")
    return redirect(url_for("dashboard"))


@app.route("/manage", methods=["GET", "POST"])
def manage():
    """Full CRUD interface for managing trains and network sections."""
    message = ""
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        code = (request.form.get("code") or "").strip().upper()

        if action == "add":
            try:
                if not code:
                    raise ValueError("Train code is required.")
                if any(t.code == code for t in STATE["trains"]):
                    raise ValueError(f"Train {code} already exists.")
                section = (request.form.get("section") or "").strip().upper()
                if section not in STATE["section_rules"]:
                    raise ValueError(f"Unknown section {section}.")
                base = datetime.now().replace(second=0, microsecond=0)
                offset = int(request.form.get("entry_offset_min") or 0)
                STATE["trains"].append(
                    Train(
                        code=code,
                        name=(request.form.get("name") or "").strip() or f"Train {code}",
                        section=section,
                        category=(request.form.get("category") or "").strip() or "Express",
                        priority=max(0, min(100, int(request.form.get("priority") or 50))),
                        planned_entry=base + timedelta(minutes=offset),
                        section_run_min=max(5, int(request.form.get("section_run_min") or 20)),
                        delay_min=max(0, int(request.form.get("delay_min") or 0)),
                        platform_need=max(0, int(request.form.get("platform_need") or 1)),
                    )
                )
                _log("MANAGE_ADD", f"Added train {code} via control UI.")
                message = f"Added train {code}."
            except Exception as exc:
                message = f"Add failed: {exc}"

        elif action == "update":
            found = False
            for t in STATE["trains"]:
                if t.code == code:
                    found = True
                    t.priority = max(0, min(100, int(request.form.get("priority") or t.priority)))
                    t.delay_min = max(0, int(request.form.get("delay_min") or t.delay_min))
                    t.section_run_min = max(5, int(request.form.get("section_run_min") or t.section_run_min))
                    t.platform_need = max(0, int(request.form.get("platform_need") or t.platform_need))
                    section = (request.form.get("section") or t.section).strip().upper()
                    if section in STATE["section_rules"]:
                        t.section = section
                    break
            if found:
                _log("MANAGE_UPDATE", f"Updated train {code} parameters.")
                message = f"Updated train {code}."
            else:
                message = f"No train with code {code}."

        elif action == "delete":
            before = len(STATE["trains"])
            STATE["trains"] = [t for t in STATE["trains"] if t.code != code]
            if len(STATE["trains"]) < before:
                _log("MANAGE_DELETE", f"Removed train {code} from active list.")
                message = f"Removed train {code}."
            else:
                message = f"No train with code {code}."

        elif action == "auto_manage":
            rows, _ = optimize_plan(STATE["trains"])
            holds = [r for r in rows if r["hold_min"] > 0]
            _log("AI_AUTOMANAGE", f"Auto-managed {len(rows)} trains; {len(holds)} holds assigned.")
            message = f"AI recommendations refreshed: {len(rows)} trains planned, {len(holds)} holds assigned."

        elif action == "add_section":
            section_id = (request.form.get("section_id") or "").strip().upper()
            try:
                if not section_id:
                    raise ValueError("Section name is required.")
                if section_id in STATE["section_rules"]:
                    raise ValueError(f"Section {section_id} already exists.")
                headway_min = max(1, int(request.form.get("headway_min") or 3))
                platform_capacity = max(1, int(request.form.get("platform_capacity") or 1))
                STATE["section_rules"][section_id] = {
                    "headway_min": headway_min,
                    "platform_capacity": platform_capacity,
                }
                _log("SECTION_ADD", f"Added section {section_id} with headway {headway_min} and capacity {platform_capacity}.")
                message = f"Added section {section_id}."
            except Exception as exc:
                message = f"Add section failed: {exc}"

        elif action == "update_section":
            section_id = (request.form.get("section_id") or "").strip().upper()
            if section_id not in STATE["section_rules"]:
                message = f"Section {section_id} not found."
            else:
                headway_min = max(1, int(request.form.get("headway_min") or STATE["section_rules"][section_id]["headway_min"]))
                platform_capacity = max(1, int(request.form.get("platform_capacity") or STATE["section_rules"][section_id]["platform_capacity"]))
                STATE["section_rules"][section_id]["headway_min"] = headway_min
                STATE["section_rules"][section_id]["platform_capacity"] = platform_capacity
                _log("SECTION_UPDATE", f"Updated section {section_id}: headway {headway_min}, capacity {platform_capacity}.")
                message = f"Updated section {section_id}."

        elif action == "delete_section":
            section_id = (request.form.get("section_id") or "").strip().upper()
            if section_id not in STATE["section_rules"]:
                message = f"Section {section_id} not found."
            else:
                assigned_count = sum(1 for t in STATE["trains"] if t.section == section_id)
                if assigned_count > 0:
                    message = f"Cannot delete {section_id}; {assigned_count} trains are still assigned."
                elif len(STATE["section_rules"]) == 1:
                    message = "At least one section must remain."
                else:
                    STATE["section_rules"].pop(section_id, None)
                    _log("SECTION_DELETE", f"Deleted section {section_id}.")
                    message = f"Deleted section {section_id}."

    rows, explanations = optimize_plan(STATE["trains"])
    STATE["last_plan"] = rows
    return render_template(
        "manage.html",
        page="manage",
        section=STATE["section_name"],
        message=message,
        trains=rows,
        explanations=explanations,
        sections=sorted(STATE["section_rules"].keys()),
        section_rules=sorted(STATE["section_rules"].items()),
    )


@app.route("/audit")
def audit():
    """Displays the system audit trail with search and filter capabilities."""
    filtered = list(STATE["audit"])
    q = (request.args.get("q") or "").strip().lower()
    event_filter = (request.args.get("event") or "").strip().upper()
    if q:
        filtered = [e for e in filtered if q in e["detail"].lower() or q in e["event"].lower()]
    if event_filter:
        filtered = [e for e in filtered if e["event"] == event_filter]
    return render_template(
        "audit.html",
        page="audit",
        section=STATE["section_name"],
        entries=filtered,
    )


@app.route("/audit/export")
def audit_export():
    """Exports the audit log as a CSV file download."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["time", "event", "detail"])
    for e in STATE["audit"]:
        writer.writerow([e["ts"], e["event"], e["detail"]])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-log.csv"},
    )


@app.route("/api/live-map")
def api_live_map():
    """API endpoint providing coordinates for live train tracking on the dashboard map."""
    rows = STATE["last_plan"]  # Use the calculated plan
    station_lookup = {s["name"]: s for s in STATE["network"].get("stations", [])}
    now = datetime.now()
    train_points = []
    route_load: dict[str, int] = {}
    
    for r in rows:
        route_load[r["section"]] = route_load.get(r["section"], 0) + 1
        
        schedule = r.get("schedule")
        if not schedule:
            continue
            
        current_segment = None
        # Determine the current segment based on time
        for seg in schedule:
            start_dt = datetime.fromisoformat(seg["start_dt"])
            end_dt = datetime.fromisoformat(seg["end_dt"])
            if start_dt <= now <= end_dt:
                current_segment = seg
                break
        
        if current_segment:
            start_dt = datetime.fromisoformat(current_segment["start_dt"])
            end_dt = datetime.fromisoformat(current_segment["end_dt"])
            src = station_lookup.get(current_segment["from"])
            dst = station_lookup.get(current_segment["to"])
            
            if src and dst:
                total_seconds = (end_dt - start_dt).total_seconds()
                elapsed = (now - start_dt).total_seconds()
                progress = elapsed / total_seconds if total_seconds > 0 else 1.0
                
                x = src["x"] + (dst["x"] - src["x"]) * progress
                y = src["y"] + (dst["y"] - src["y"]) * progress
                
                train_points.append(
                    {
                        "code": r["code"],
                        "name": r["name"],
                        "category": r["category"],
                        "priority": r["priority"],
                        "x": x,
                        "y": y,
                        "section": r["section"],
                        "decision": current_segment.get("signal", "GREEN"),
                        "current_segment": current_segment,
                    }
                )
        else:
            first_seg = schedule[0]
            last_seg = schedule[-1]
            if now < datetime.fromisoformat(first_seg["start_dt"]):
                src = station_lookup.get(first_seg["from"])
                if src:
                    train_points.append({
                        "code": r["code"],
                        "name": r["name"],
                        "category": r["category"],
                        "priority": r["priority"],
                        "x": src["x"],
                        "y": src["y"],
                        "section": r["section"],
                        "decision": "RED",
                        "current_segment": first_seg,
                    })
            elif now > datetime.fromisoformat(last_seg["end_dt"]):
                dst = station_lookup.get(last_seg["to"])
                if dst:
                    train_points.append({
                        "code": r["code"],
                        "name": r["name"],
                        "category": r["category"],
                        "priority": r["priority"],
                        "x": dst["x"],
                        "y": dst["y"],
                        "section": r["section"],
                        "decision": "FINISHED",
                        "current_segment": last_seg,
                    })

    conflicts = [k for k, v in route_load.items() if v > 18]
    return jsonify(
        {
            "stations": list(station_lookup.values()),
            "routes": STATE["network"].get("routes", []),
            "trains": train_points,
            "occupied": list(route_load.keys()),
            "conflicts": conflicts,
            "timestamp": now.isoformat(),
        }
    )


@app.route("/api/analytics")
def api_analytics():
    """API endpoint providing historical performance data for frontend charts."""
    rows = STATE["history"][-20:]
    return jsonify(
        {
            "labels": [r["ts"] for r in rows],
            "throughput": [r["throughput"] for r in rows],
            "delay": [r["avg_delay"] for r in rows],
            "conflicts": [r["conflicts"] for r in rows],
            "utilization": [r["utilization"] for r in rows],
        }
    )


def compute_kpis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculates Key Performance Indicators (KPIs) like throughput and average hold time."""
    holds = [r["hold_min"] for r in rows]
    total_hold = sum(holds)
    avg_hold = round(sum(holds) / len(holds), 1) if holds else 0
    max_hold = max(holds) if holds else 0
    throughput = len(rows)
    active_trains = sum(1 for r in rows if r["decision"] in {"PROCEED", "HOLD"})
    conflicts_prevented = sum(1 for r in rows if r["hold_min"] > 0)
    utilization_pct = round(min(100.0, (active_trains / max(1, len(STATE["section_rules"]) * 20)) * 100), 1)
    return {
        "throughput": throughput,
        "total_hold": total_hold,
        "avg_hold": avg_hold,
        "max_hold": max_hold,
        "active_trains": active_trains,
        "conflicts_prevented": conflicts_prevented,
        "utilization_pct": utilization_pct,
        "on_time_pct": max(0, 100 - min(70, avg_hold * 2.8)),
        "punctuality_proxy": max(0, 100 - min(60, avg_hold * 3)),
    }


if __name__ == "__main__":
    # Entry point for the application
    init_state()
    app.run(debug=True, port=5050)