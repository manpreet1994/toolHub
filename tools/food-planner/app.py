"""What to eat tomorrow: log meals, get suggestions from recency + frequency (no LLM).

Upload this single file to ToolHub (or run it standalone: `python food_planner.py`).
Data lives in <data_dir>/meals.json as a flat list of {id, date, meal, dish}.

Scoring (per meal slot): score = min(days_since_last, 90) * (1 + ln(times_eaten))
  - the longer since you last had it, the higher it ranks (2 weeks beats 1 week)
  - dishes you eat more often get a boost, so favourites come back sooner than one-offs
  - dishes eaten today or yesterday are skipped unless nothing else is available
"""
import json
import math
import os
import threading
import uuid
from datetime import date, timedelta

from flask import Flask, jsonify, request

MEALS = ["breakfast", "lunch", "dinner"]
COOLDOWN_DAYS = 2   # skip dishes eaten within this many days of the target day
MAX_DAYS = 90       # cap so a one-off from months ago doesn't beat everything forever
TOP_N = 5


def create_app(data_dir):
    app = Flask(__name__)
    path = os.path.join(data_dir, "meals.json")
    lock = threading.Lock()

    def load():
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return json.load(f)

    def save(entries):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(entries, f, indent=1)
        os.replace(tmp, path)  # atomic

    def key(dish):
        return " ".join(dish.lower().split())

    def suggest(entries, meal, today, planned):
        """Rank dishes for `meal` on the day after `today`."""
        target = today + timedelta(days=1)
        stats = {}
        for e in entries:
            if e["meal"] != meal or date.fromisoformat(e["date"]) > today:
                continue  # only real history, not plans for future days
            s = stats.setdefault(key(e["dish"]), {"dish": e["dish"], "dates": set()})
            s["dates"].add(e["date"])
        ranked = []
        for k, s in stats.items():
            if k in planned:
                continue
            last = max(date.fromisoformat(d) for d in s["dates"])
            days, count = (target - last).days, len(s["dates"])
            ranked.append({
                "dish": s["dish"], "days": days, "count": count,
                "score": min(days, MAX_DAYS) * (1 + math.log(count)),
                "recent": days <= COOLDOWN_DAYS + 1,
            })
        fresh = [r for r in ranked if not r["recent"]]
        pool = fresh or ranked  # relax the cooldown if history is tiny
        pool.sort(key=lambda r: (-r["score"], r["dish"].lower()))
        for r in pool:
            r["score"] = round(r["score"], 1)
        return pool[:TOP_N]

    @app.get("/api/overview")
    def overview():
        today = date.fromisoformat(request.args["today"])
        tomorrow = (today + timedelta(days=1)).isoformat()
        entries = load()
        day = lambda d: [e for e in entries if e["date"] == d]
        planned = {m: {key(e["dish"]) for e in day(tomorrow) if e["meal"] == m} for m in MEALS}
        return jsonify(
            tomorrow=tomorrow,
            today_entries=day(today.isoformat()),
            tomorrow_entries=day(tomorrow),
            suggestions={m: suggest(entries, m, today, planned[m]) for m in MEALS},
            known={m: sorted({e["dish"] for e in entries if e["meal"] == m}) for m in MEALS},
            total=len(entries),
        )

    @app.post("/api/log")
    def log():  # used both for "I ate this" and for accepting a suggestion
        b = request.get_json()
        dish, meal = " ".join(str(b.get("dish", "")).split()), b.get("meal")
        d = date.fromisoformat(b["date"]).isoformat()
        if not dish or meal not in MEALS:
            return jsonify(error="dish and a valid meal are required"), 400
        with lock:
            entries = load()
            if not any(e["date"] == d and e["meal"] == meal and key(e["dish"]) == key(dish) for e in entries):
                # reuse the first-seen spelling so "Dosa" and "dosa" stay one dish
                canon = next((e["dish"] for e in entries if key(e["dish"]) == key(dish)), dish)
                entries.append({"id": uuid.uuid4().hex[:10], "date": d, "meal": meal, "dish": canon})
                save(entries)
        return jsonify(ok=True)

    @app.delete("/api/log/<eid>")
    def remove(eid):
        with lock:
            save([e for e in load() if e["id"] != eid])
        return jsonify(ok=True)

    @app.get("/")
    def index():
        return PAGE

    return app


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>What to eat</title>
<style>
:root{--bg:#fafafa;--fg:#1a1a1a;--card:#fff;--bd:#ddd;--ac:#2563eb;--mut:#6b7280}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--card:#222;--bd:#333;--ac:#60a5fa;--mut:#9ca3af}}
*{box-sizing:border-box}body{font:16px system-ui;background:var(--bg);color:var(--fg);max-width:880px;margin:1.5rem auto;padding:0 1rem}
h1{margin:0 0 4px}h2{margin:28px 0 10px;font-size:1.1rem}h3{margin:0 0 8px;text-transform:capitalize}
.row{display:flex;gap:8px;flex-wrap:wrap}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px}
input,select,button{font:inherit;padding:8px 10px;border-radius:6px;border:1px solid var(--bd);background:var(--card);color:var(--fg)}
input[name=dish]{flex:1;min-width:160px}button{cursor:pointer}button.p{background:var(--ac);border-color:var(--ac);color:#fff}
.item{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 0;border-top:1px solid var(--bd)}
.item:first-of-type{border-top:0}small,.mut{color:var(--mut)}.x{border:0;background:none;color:var(--mut);padding:2px 6px}
.chip{display:inline-block;background:var(--ac);color:#fff;border-radius:99px;padding:2px 10px;margin:2px 4px 2px 0;font-size:.9rem}
</style>
<h1>What to eat</h1><div class=mut id=sub></div>

<h2>Log a meal</h2>
<form id=f class="row card">
  <input type=date name=date required>
  <select name=meal><option>breakfast<option>lunch<option>dinner</select>
  <input name=dish placeholder="What did you eat?" list=known required autocomplete=off>
  <datalist id=known></datalist><button class=p>Add</button>
</form>
<div id=today class=mut style="margin-top:8px"></div>

<h2>Tomorrow <span class=mut id=tdate></span></h2>
<div id=empty class="card mut" hidden>No history yet. Log what you ate and suggestions will appear here.</div>
<div class=grid id=plan></div>

<script>
const MEALS=['breakfast','lunch','dinner'], $=s=>document.querySelector(s);
const today=new Date().toLocaleDateString('en-CA'); let S=null;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api=(p,o)=>fetch('api/'+p,o).then(r=>r.json());
const post=(p,b)=>api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
$('[name=date]').value=today;

async function load(){
  S=await api('overview?today='+today);
  $('#sub').textContent=S.total+' meals logged';
  $('#tdate').textContent=new Date(S.tomorrow+'T00:00').toDateString();
  const meal=$('[name=meal]').value;
  $('#known').innerHTML=(S.known[meal]||[]).map(d=>`<option value="${esc(d)}">`).join('');
  $('#today').innerHTML=S.today_entries.length?'Today: '+S.today_entries.map(e=>
    `<span class=chip>${esc(e.meal)}: ${esc(e.dish)} <button class=x data-del="${e.id}" style="color:#fff">×</button></span>`).join(''):'Nothing logged for today yet.';
  $('#empty').hidden=S.total>0;
  $('#plan').innerHTML=MEALS.map(m=>{
    const planned=S.tomorrow_entries.filter(e=>e.meal===m);
    const sug=S.suggestions[m];
    return `<div class=card><h3>${m}</h3>`+
      planned.map(e=>`<div class=item><b>✓ ${esc(e.dish)}</b><button class=x data-del="${e.id}" title=remove>×</button></div>`).join('')+
      (sug.length?`<small>Suggestions</small>`:'')+
      sug.map(s=>`<div class=item><div>${esc(s.dish)}<br><small>last ${s.days} day${s.days==1?'':'s'} ago · eaten ${s.count}×</small></div>
        <button data-acc="${m}" data-dish="${esc(s.dish)}">Accept</button></div>`).join('')+
      (!sug.length&&!planned.length?'<small>No suggestions yet.</small>':'')+`</div>`;}).join('');
}
$('#f').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);
  await post('log',{date:f.get('date'),meal:f.get('meal'),dish:f.get('dish')});
  e.target.dish.value='';load();};
$('[name=meal]').onchange=load;
document.body.onclick=async e=>{const t=e.target;
  if(t.dataset.del){await api('log/'+t.dataset.del,{method:'DELETE'});load();}
  if(t.dataset.acc){await post('log',{date:S.tomorrow,meal:t.dataset.acc,dish:t.dataset.dish});load();}};
load();
</script>"""


if __name__ == "__main__":
    os.makedirs("food_data", exist_ok=True)
    create_app("food_data").run(port=8001)
