from datetime import date, datetime, timedelta
from typing import Literal, Optional
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

app = FastAPI(title="FlowCare Smart Scheduling", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SERVICES = {
    "Consultation": 20,
    "Follow-up": 15,
    "Dental Cleaning": 40,
    "Deep Cleaning": 60,
    "X-Ray": 15,
    "Procedure": 45,
}
PROVIDERS = ["Dr. Smith", "Dr. Patel"]
URGENCY_WEIGHT = {"urgent": 0, "priority": 1, "normal": 2}
clients: list[WebSocket] = []
notifications = []

class AppointmentIn(BaseModel):
    patient: str
    phone: str = ""
    email: str = ""
    provider: str
    service: str
    appointment_date: str
    scheduled_time: Optional[str] = None
    source: Literal["online", "walk_in"] = "online"
    urgency: Literal["normal", "priority", "urgent"] = "normal"

class RescheduleIn(BaseModel):
    appointment_date: str
    scheduled_time: str

# Demo data. Replace with PostgreSQL in production.
today = date.today().isoformat()
appointments = [
    {"id": 1, "patient": "Sarah", "phone": "+1 555 0101", "email": "sarah@example.com", "provider": "Dr. Smith", "service": "Dental Cleaning", "appointment_date": today, "scheduled_time": "09:00", "source": "online", "urgency": "normal", "status": "completed", "checked_in_at": "08:52", "started_at": "09:03", "completed_at": "09:39", "actual_minutes": 36},
    {"id": 2, "patient": "Emma", "phone": "+1 555 0102", "email": "emma@example.com", "provider": "Dr. Smith", "service": "Consultation", "appointment_date": today, "scheduled_time": "09:45", "source": "online", "urgency": "normal", "status": "checked_in", "checked_in_at": "09:34", "started_at": None, "completed_at": None, "actual_minutes": None},
    {"id": 3, "patient": "David", "phone": "+1 555 0103", "email": "david@example.com", "provider": "Dr. Smith", "service": "Follow-up", "appointment_date": today, "scheduled_time": "10:15", "source": "online", "urgency": "normal", "status": "scheduled", "checked_in_at": None, "started_at": None, "completed_at": None, "actual_minutes": None},
]


def parse_dt(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00")


def service_minutes(a):
    history = [x["actual_minutes"] for x in appointments if x["service"] == a["service"] and x["provider"] == a["provider"] and x.get("actual_minutes")]
    return round(sum(history) / len(history)) if history else SERVICES.get(a["service"], 30)


def active_for(day, provider):
    rows = [a for a in appointments if a["appointment_date"] == day and a["provider"] == provider and a["status"] not in {"completed", "cancelled", "no_show"}]
    def key(a):
        if a["source"] == "walk_in":
            return (URGENCY_WEIGHT[a["urgency"]], a.get("created_at", "23:59"))
        return (1, a["scheduled_time"] or "23:59")
    return sorted(rows, key=key)


def queue_projection(day, provider):
    rows = active_for(day, provider)
    now = datetime.now()
    cursor = now
    result = []
    for pos, a in enumerate(rows, 1):
        duration = service_minutes(a)
        if a["source"] == "online" and a.get("scheduled_time"):
            scheduled = parse_dt(day, a["scheduled_time"])
            start = max(cursor, scheduled)
        else:
            start = cursor
        if a["status"] == "in_service" and a.get("started_at"):
            start = parse_dt(day, a["started_at"])
        predicted_end = start + timedelta(minutes=duration)
        result.append({"id": a["id"], "position": pos, "predicted_start": start.strftime("%H:%M"), "predicted_end": predicted_end.strftime("%H:%M"), "predicted_service_minutes": duration, "people_ahead": pos - 1})
        cursor = predicted_end
    return {x["id"]: x for x in result}


def notification(a, event, message):
    item = {"id": len(notifications)+1, "appointment_id": a["id"], "patient": a["patient"], "phone": a.get("phone", ""), "email": a.get("email", ""), "event": event, "message": message, "created_at": datetime.now().isoformat(timespec="seconds"), "delivery": "simulated"}
    notifications.insert(0, item)
    return item

async def broadcast(event="queue_updated"):
    dead = []
    for ws in clients:
        try: await ws.send_json({"type": event})
        except Exception: dead.append(ws)
    for ws in dead:
        if ws in clients: clients.remove(ws)

@app.get("/api/health")
def health(): return {"status": "ok"}

@app.get("/api/config")
def config(): return {"services": SERVICES, "providers": PROVIDERS, "today": today}

@app.get("/api/appointments")
def get_appointments(day: str = today, provider: Optional[str] = None):
    rows = [a for a in appointments if a["appointment_date"] == day and (not provider or a["provider"] == provider)]
    projections = {}
    for p in PROVIDERS:
        projections.update(queue_projection(day, p))
    return [{**a, **projections.get(a["id"], {"position": None, "predicted_start": None, "predicted_end": None, "predicted_service_minutes": service_minutes(a), "people_ahead": None})} for a in sorted(rows, key=lambda x: x.get("scheduled_time") or x.get("created_at", "23:59"))]

@app.post("/api/appointments")
async def create_appointment(data: AppointmentIn):
    if data.service not in SERVICES: raise HTTPException(400, "Unknown service")
    if data.provider not in PROVIDERS: raise HTTPException(400, "Unknown provider")
    if data.source == "online" and not data.scheduled_time: raise HTTPException(400, "Online appointment requires a time")
    if data.source == "online":
        for a in appointments:
            if a["appointment_date"] == data.appointment_date and a["provider"] == data.provider and a.get("scheduled_time") == data.scheduled_time and a["status"] not in {"cancelled", "no_show"}:
                raise HTTPException(409, "That slot is already booked")
    a = {"id": max([x["id"] for x in appointments], default=0)+1, **data.model_dump(), "status": "waiting" if data.source == "walk_in" else "scheduled", "created_at": datetime.now().strftime("%H:%M"), "checked_in_at": datetime.now().strftime("%H:%M") if data.source == "walk_in" else None, "started_at": None, "completed_at": None, "actual_minutes": None}
    appointments.append(a)
    notification(a, "created", f"FlowCare: {a['service']} {'walk-in added' if a['source']=='walk_in' else 'appointment booked for '+a['appointment_date']+' at '+a['scheduled_time']} with {a['provider']}.")
    await broadcast(); return a

@app.get("/api/availability")
def availability(day: str, provider: str):
    start = datetime.fromisoformat(f"{day}T09:00:00"); end = datetime.fromisoformat(f"{day}T17:00:00")
    booked = {(a.get("scheduled_time"), a["id"]) for a in appointments if a["appointment_date"] == day and a["provider"] == provider and a["source"] == "online" and a["status"] not in {"cancelled", "no_show"}}
    slots=[]; cur=start
    while cur < end:
        t=cur.strftime("%H:%M"); match=next((i for bt,i in booked if bt==t),None)
        slots.append({"time":t,"status":"booked" if match else "available","appointment_id":match})
        cur += timedelta(minutes=15)
    return slots

@app.post("/api/appointments/{appointment_id}/check-in")
async def check_in(appointment_id: int):
    a = next((x for x in appointments if x["id"] == appointment_id), None)
    if not a: raise HTTPException(404, "Appointment not found")
    if a["source"] != "online": raise HTTPException(400, "Walk-ins are checked in when added")
    scheduled = parse_dt(a["appointment_date"], a["scheduled_time"])
    now = datetime.now()
    if now < scheduled - timedelta(minutes=15):
        raise HTTPException(400, f"Check-in opens at {(scheduled-timedelta(minutes=15)).strftime('%H:%M')}")
    a["status"]="checked_in"; a["checked_in_at"]=now.strftime("%H:%M")
    notification(a,"check_in",f"FlowCare: You are checked in. Your live service estimate will update automatically.")
    await broadcast(); return a

@app.post("/api/appointments/{appointment_id}/status/{status}")
async def update_status(appointment_id:int,status:str):
    allowed={"scheduled","checked_in","waiting","in_service","completed","no_show","cancelled"}
    if status not in allowed: raise HTTPException(400,"Invalid status")
    a=next((x for x in appointments if x["id"]==appointment_id),None)
    if not a: raise HTTPException(404,"Appointment not found")
    now=datetime.now()
    if status=="in_service":
        a["started_at"]=now.strftime("%H:%M")
    if status=="completed":
        a["completed_at"]=now.strftime("%H:%M")
        if a.get("started_at"):
            start=parse_dt(a["appointment_date"],a["started_at"])
            a["actual_minutes"]=max(1,round((now-start).total_seconds()/60))
        else: a["actual_minutes"]=service_minutes(a)
    a["status"]=status
    if status=="no_show":
        candidates=[x for x in active_for(a["appointment_date"],a["provider"]) if x["id"]!=a["id"] and x["status"] in {"checked_in","waiting"}]
        if candidates:
            nxt=candidates[0]
            notification(nxt,"slot_opened",f"FlowCare: An earlier slot opened. You are now next in queue for {nxt['service']}.")
    await broadcast(); return a

@app.put("/api/appointments/{appointment_id}/reschedule")
async def reschedule(appointment_id:int,data:RescheduleIn):
    a=next((x for x in appointments if x["id"]==appointment_id),None)
    if not a: raise HTTPException(404,"Appointment not found")
    for x in appointments:
        if x["id"]!=a["id"] and x["appointment_date"]==data.appointment_date and x["provider"]==a["provider"] and x.get("scheduled_time")==data.scheduled_time and x["status"] not in {"cancelled","no_show"}:
            raise HTTPException(409,"That slot is already booked")
    a["appointment_date"]=data.appointment_date; a["scheduled_time"]=data.scheduled_time; a["status"]="scheduled"; a["checked_in_at"]=None
    notification(a,"rescheduled",f"FlowCare: Your appointment was rescheduled to {data.appointment_date} at {data.scheduled_time}.")
    await broadcast(); return a

@app.get("/api/notifications")
def get_notifications(): return notifications[:20]

@app.get("/api/analytics")
def analytics(day: str = today):
    rows=[a for a in appointments if a["appointment_date"]==day]
    return {"appointments":len([a for a in rows if a["source"]=="online"]),"walk_ins":len([a for a in rows if a["source"]=="walk_in"]),"waiting":len([a for a in rows if a["status"] in {"checked_in","waiting","scheduled"}]),"completed":len([a for a in rows if a["status"]=="completed"])}

@app.websocket("/ws")
async def websocket_endpoint(ws:WebSocket):
    await ws.accept(); clients.append(ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect:
        if ws in clients: clients.remove(ws)
