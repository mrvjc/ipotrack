from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import json
import logging
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, selectinload

from database import SessionLocal, init_db, IPO, GMPHistory, ListingPerformanceSnapshot
from quant_engine import calculate_ipo_analytics, AllotmentAndProfitReport
from scraper import IPODataAggregator
from sync_service import read_sync_status, run_pipeline


logger = logging.getLogger(__name__)


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            parsed = datetime.strptime(value.strip(), fmt).date()
            return parsed if parsed.year < 2030 else None
        except ValueError:
            continue
    return None


def _has_ipo_details(ipo: IPO) -> bool:
    """A name/category-only scraper record is not useful in the upcoming list."""
    return any((
        (ipo.price_band_high or 0) > 0,
        (ipo.lot_size or 0) > 0,
        _as_date(ipo.open_date) is not None,
        _as_date(ipo.close_date) is not None,
        _as_date(ipo.listing_date) is not None,
        ipo.listing_price is not None,
        ipo.listing_gain_pct is not None,
        any((item.qib_times or 0) > 0 or (item.nii_times or 0) > 0
            or (item.retail_times or 0) > 0 or (item.total_times or 0) > 0
            for item in ipo.sub_logs),
        any((item.gmp_price or 0) != 0 for item in ipo.gmp_logs),
    ))


def _latest_retail_subscription(ipo: IPO) -> float | None:
    """Return only an observed retail split; never substitute total subscription."""
    latest = ipo.sub_logs[-1] if ipo.sub_logs else None
    if latest is None or not latest.category_split_available or latest.retail_is_proxy:
        return None
    return latest.retail_times


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _gmp_change_24h(ipo: IPO) -> float | None:
    """Compare the latest GMP with the nearest observation at least 24 hours old."""
    if not ipo.gmp_logs:
        return None

    latest = ipo.gmp_logs[-1]
    target_at = _as_utc_naive(latest.recorded_at) - timedelta(hours=24)
    reference = max(
        (
            observation
            for observation in ipo.gmp_logs[:-1]
            if _as_utc_naive(observation.recorded_at) <= target_at
        ),
        key=lambda observation: _as_utc_naive(observation.recorded_at),
        default=None,
    )
    if reference is None:
        return None
    return round(latest.gmp_price - reference.gmp_price, 2)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Prepare the database. IPOJi is fetched on dashboard refresh or manual sync.
    init_db()
    yield

app = FastAPI(
    title="IPOTrack Intelligence API",
    description="Real-time Indian IPO GMP, Subscription & Allotment Probability Engine",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/api/ipos")
def get_all_ipos(category: str = None, section: str = None, db: Session = Depends(get_db)):
    """List IPOs, optionally filtered into upcoming, open, closed, or listed."""
    valid_sections = {"upcoming", "open", "closed", "listed"}
    section = section.lower() if section else None
    if section and section not in valid_sections:
        raise HTTPException(status_code=400, detail="section must be upcoming, open, closed, or listed")

    # Defense in depth: the public feed only includes non-SME records.
    query = db.query(IPO).options(
        selectinload(IPO.gmp_logs),
        selectinload(IPO.sub_logs),
        selectinload(IPO.listing_logs),
    ).filter(IPO.category.notilike("%sme%"))
    if category:
        query = query.filter(IPO.category.ilike(category))
    
    ipos = query.all()
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    filtered_ipos = []
    for ipo in ipos:
        listed = (ipo.source_status or "").strip().lower() == "listed"
        opens, closes = _as_date(ipo.open_date), _as_date(ipo.close_date)
        if listed:
            current_section = "listed"
        elif opens and today < opens:
            current_section = "upcoming"
        elif opens and closes and opens <= today <= closes:
            current_section = "open"
        elif closes and today > closes:
            current_section = "closed"
        else:
            current_section = "upcoming"
        if current_section == "upcoming" and not _has_ipo_details(ipo):
            continue
        if (section == current_section) or (section is None and not listed):
            filtered_ipos.append((ipo, current_section))
    results = []

    for ipo, current_section in filtered_ipos:
        latest_gmp = ipo.gmp_logs[-1] if ipo.gmp_logs else None
        latest_sub = ipo.sub_logs[-1] if ipo.sub_logs else None
        registrar_name, registrar_url = IPODataAggregator.canonicalize_registrar(
            ipo.registrar_name,
            ipo.registrar_url,
        )
        allotment_action = None
        if registrar_url and current_section == "closed":
            allotment_action = "Check Allotment Status"
        elif registrar_url and current_section == "listed":
            allotment_action = "View Allotment Result"

        gmp_val = latest_gmp.gmp_price if latest_gmp else 0.0
        gmp_change_24h = _gmp_change_24h(ipo)
        retail_sub = _latest_retail_subscription(ipo)
        total_sub = (
            latest_sub.total_times
            if latest_sub and latest_sub.total_subscription_available
            else None
        )

        analytics = calculate_ipo_analytics(
            company_name=ipo.company_name,
            issue_price=ipo.price_band_high,
            lot_size=ipo.lot_size,
            gmp_price=gmp_val,
            retail_sub=retail_sub,
            subscription_total=total_sub,
        )

        results.append({
            "id": ipo.id,
            "company_name": ipo.company_name,
            "status": "Listed" if current_section == "listed" else current_section.title(),
            "section": current_section,
            "category": ipo.category,
            "price_band_high": ipo.price_band_high,
            "lot_size": ipo.lot_size,
            "open_date": ipo.open_date if _as_date(ipo.open_date) else None,
            "close_date": ipo.close_date if _as_date(ipo.close_date) else None,
            "listing_date": ipo.listing_date if _as_date(ipo.listing_date) else None,
            "listing_price": ipo.listing_price,
            "listing_gain_pct": ipo.listing_gain_pct,
            "listing_data_updated_at": (
                ipo.listing_logs[-1].recorded_at.isoformat() + "Z" if ipo.listing_logs else None
            ),
            "registrar_name": registrar_name,
            "registrar_url": registrar_url,
            "allotment_action": allotment_action,
            "latest_gmp": gmp_val,
            "gmp_change_24h": gmp_change_24h,
            "estimated_listing_price": analytics.estimated_listing_price,
            "expected_gain_pct": round((gmp_val / ipo.price_band_high) * 100, 2) if ipo.price_band_high else 0,
            "subscription": {
                "retail": retail_sub,
                "retail_is_estimate": False,
                "nii": (
                    latest_sub.nii_times
                    if latest_sub and latest_sub.category_split_available
                    else None
                ),
                "qib": (
                    latest_sub.qib_times
                    if latest_sub and latest_sub.category_split_available
                    else None
                ),
                "total": total_sub,
            },
            "allotment_probability_pct": analytics.allotment_probability_pct,
            "probability_verdict": analytics.probability_verdict
        })

    return results


@app.get("/api/sync/status")
def get_sync_status():
    try:
        return read_sync_status()
    except Exception:
        logger.exception("Could not read saved sync status")
        return {"status": "cached", "last_synced_at": None, "message": None}


@app.get("/api/ipos/{ipo_id}/listing-history")
def get_ipo_listing_history(ipo_id: int, db: Session = Depends(get_db)):
    """Return IPOJi listing result observations captured by each explicit refresh."""
    ipo = db.query(IPO).filter(IPO.id == ipo_id).first()
    if not ipo:
        raise HTTPException(status_code=404, detail="IPO not found")

    observations = (
        db.query(ListingPerformanceSnapshot)
        .filter(ListingPerformanceSnapshot.ipo_id == ipo_id)
        .order_by(ListingPerformanceSnapshot.recorded_at.asc())
        .all()
    )
    return {
        "company_name": ipo.company_name,
        "listing_date": ipo.listing_date,
        "listing_price": ipo.listing_price,
        "listing_gain_pct": ipo.listing_gain_pct,
        "observations": [
            {
                "timestamp": item.recorded_at.replace(tzinfo=timezone.utc).isoformat(),
                "listing_date": item.listing_date,
                "listing_price": item.listing_price,
                "listing_gain_pct": item.listing_gain_pct,
            }
            for item in observations
        ],
    }

@app.get("/api/ipos/{ipo_id}/analytics", response_model=AllotmentAndProfitReport)
def get_ipo_deep_analytics(ipo_id: int, lots: int = 1, db: Session = Depends(get_db)):
    ipo = db.query(IPO).filter(IPO.id == ipo_id).first()
    if not ipo:
        raise HTTPException(status_code=404, detail="IPO not found")

    latest_gmp = ipo.gmp_logs[-1].gmp_price if ipo.gmp_logs else 0.0
    latest_sub = ipo.sub_logs[-1] if ipo.sub_logs else None
    latest_retail_sub = _latest_retail_subscription(ipo)
    total_sub = (
        latest_sub.total_times
        if latest_sub and latest_sub.total_subscription_available
        else None
    )

    return calculate_ipo_analytics(
        company_name=ipo.company_name,
        issue_price=ipo.price_band_high,
        lot_size=ipo.lot_size * lots,
        gmp_price=latest_gmp,
        retail_sub=latest_retail_sub,
        subscription_total=total_sub,
    )

@app.get("/api/ipos/{ipo_id}/history")
def get_ipo_gmp_history(
    ipo_id: int,
    hours: int = Query(default=5, ge=1, le=24),
    db: Session = Depends(get_db),
):
    ipo = db.query(IPO).filter(IPO.id == ipo_id).first()
    if not ipo:
        raise HTTPException(status_code=404, detail="IPO not found")

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    logs = (
        db.query(GMPHistory)
        .filter(GMPHistory.ipo_id == ipo_id, GMPHistory.recorded_at >= cutoff)
        .order_by(GMPHistory.recorded_at.asc())
        .all()
    )
    return {
        "company_name": ipo.company_name,
        "issue_price": ipo.price_band_high,
        "window_hours": hours,
        "gmp_history": [
            {
                "timestamp": log.recorded_at.replace(tzinfo=timezone.utc).isoformat(),
                "gmp_price": log.gmp_price,
                "estimated_listing_price": log.estimated_listing_price
            }
            for log in logs
        ]
    }


@app.get("/api/ipos/{ipo_id}/chart", response_class=HTMLResponse)
def get_ipo_gmp_chart(ipo_id: int, db: Session = Depends(get_db)):
    """Render the GMP observations saved during the latest site refresh."""
    ipo = db.query(IPO).filter(IPO.id == ipo_id).first()
    if not ipo:
        raise HTTPException(status_code=404, detail="IPO not found")

    logs = (
        db.query(GMPHistory)
        .filter(GMPHistory.ipo_id == ipo_id, GMPHistory.source == "ipoji")
        .order_by(GMPHistory.recorded_at.asc())
        .all()
    )
    source_label = "IPOJi recorded observations"
    if not logs:
        logs = (
            db.query(GMPHistory)
            .filter(GMPHistory.ipo_id == ipo_id, GMPHistory.source == "refresh")
            .order_by(GMPHistory.recorded_at.asc())
            .all()
        )
        source_label = "Saved site-refresh snapshots"

    observation_data = json.dumps([
        {
            "timestamp": log.recorded_at.replace(tzinfo=timezone.utc).isoformat(),
            "gmp_price": log.gmp_price,
        }
        for log in logs
    ]).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    ipo_name = json.dumps(ipo.company_name).replace("</", "<\\/")
    source_label_json = json.dumps(source_label)
    issue_price_json = json.dumps(ipo.price_band_high or 0)

    page = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GMP chart</title><style>
*{box-sizing:border-box}
html,body{width:100%;height:100%;margin:0;overflow:hidden;background:#121316}
body{padding:12px 16px 10px;color:#fff;font:13px system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.shell{display:flex;height:100%;min-height:0;flex-direction:column;gap:10px}
.topbar{display:flex;min-width:0;align-items:center;justify-content:space-between;gap:16px}
.heading{min-width:0}
h1{overflow:hidden;margin:0;color:#fff;font-size:13px;font-weight:500;text-overflow:ellipsis;white-space:nowrap}
#updated{overflow:hidden;margin-top:3px;color:#8b909a;font-size:10px;text-overflow:ellipsis;white-space:nowrap}
.quote{display:flex;flex:none;align-items:center;gap:10px}
#latest-price{color:#fff;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.04em;line-height:1}
.change{border-radius:999px;padding:4px 7px;font-size:10px;font-weight:600;font-variant-numeric:tabular-nums}
.change.positive{background:rgb(0 208 156 / 12%);color:#00d09c}
.change.negative{background:rgb(255 83 83 / 12%);color:#ff5353}
.toolbar{display:flex;min-height:28px;align-items:center;justify-content:space-between;gap:12px}
.ranges{display:flex;align-items:center;gap:3px}
.ranges button{border:0;border-radius:4px;background:transparent;padding:5px 9px;color:#8b909a;font:500 10px system-ui,sans-serif;cursor:pointer}
.ranges button:hover{color:#fff}
.ranges button[aria-pressed="true"]{background:#00d09c;color:#000}
#summary{color:#8b909a;font-size:10px}
.chart-wrap{position:relative;flex:1;min-height:0;overflow:hidden;background:#121316}
canvas{display:block;width:100%;height:100%;touch-action:none}
#tooltip{position:absolute;z-index:2;display:flex;align-items:center;gap:6px;max-width:calc(100% - 16px);overflow:hidden;transform:translate(-50%,-100%);pointer-events:none;border:1px solid rgb(255 255 255 / 8%);border-radius:5px;background:#08090a;padding:6px 8px;color:#fff;font:11px ui-monospace,SFMono-Regular,Menlo,monospace;text-overflow:ellipsis;white-space:nowrap;box-shadow:0 4px 18px rgb(0 0 0 / 30%)}
#tooltip-text{display:block;min-width:0;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}
#tooltip[hidden],#message[hidden]{display:none}
#message{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#8b909a;font-size:12px}
@media(max-width:420px){body{padding:7px 9px}.shell{gap:6px}.topbar{gap:8px}#latest-price{font-size:18px}.toolbar{gap:6px}.ranges{gap:1px}.ranges button{padding:4px 6px}#summary{font-size:9px}}
</style></head><body>
<main class="shell">
  <header class="topbar">
    <div class="heading">
      <h1 id="title">GMP history</h1>
      <p id="updated">Saved IPOJi observations</p>
    </div>
    <div class="quote">
      <strong id="latest-price">&#8377;—</strong>
      <span id="gain-badge" class="change positive">0.00%</span>
    </div>
  </header>
  <div class="toolbar">
    <div class="ranges" role="group" aria-label="Observation range">
      <button type="button" data-range="5h" aria-pressed="false">5h</button>
      <button type="button" data-range="1d" aria-pressed="false">1D</button>
      <button type="button" data-range="7d" aria-pressed="false">1W</button>
      <button type="button" data-range="all" aria-pressed="true">ALL</button>
    </div>
    <span id="summary"></span>
  </div>
  <div class="chart-wrap">
    <canvas id="chart" aria-label="GMP area chart"></canvas>
    <div id="tooltip" hidden><span id="tooltip-text"></span></div>
    <div id="message" hidden></div>
  </div>
</main>
<script>
const companyName=__IPO_NAME__,sourceLabel=__SOURCE_LABEL__,issuePrice=Number(__ISSUE_PRICE__),rows=__OBSERVATIONS__;
const points=rows.map(row=>({t:Date.parse(row.timestamp),v:Number(row.gmp_price)}))
  .filter(point=>Number.isFinite(point.t)&&Number.isFinite(point.v))
  .sort((a,b)=>a.t-b.t);
const canvas=document.getElementById('chart'),ctx=canvas.getContext('2d');
const tooltip=document.getElementById('tooltip'),tooltipText=document.getElementById('tooltip-text');
const message=document.getElementById('message');
const latestPrice=document.getElementById('latest-price'),gainBadge=document.getElementById('gain-badge');
const updated=document.getElementById('updated'),summary=document.getElementById('summary');
const ranges={'5h':5*60*60*1000,'1d':24*60*60*1000,'7d':7*24*60*60*1000,all:null};
let selectedRange='all',visiblePoints=[],chartCoords=[],activeIndex=-1;
function priceText(value){return String.fromCodePoint(0x20B9)+Number(value).toLocaleString('en-IN',{maximumFractionDigits:2})}
function dateLabel(timestamp,span){
  const options=span<24*60*60*1000
    ?{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',hour12:true}
    :{timeZone:'Asia/Kolkata',day:'2-digit',month:'short'};
  return new Date(timestamp).toLocaleString('en-IN',options);
}
function fmtFullDate(timestamp){
  return new Date(timestamp).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:true});
}
function draw(){
  const dpr=window.devicePixelRatio||1,w=canvas.clientWidth,h=canvas.clientHeight;
  if(!w||!h||!visiblePoints.length)return;
  canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);
  ctx.setTransform(dpr,0,0,dpr,0,0);
  const pad={l:w<360?52:62,r:12,t:10,b:28},cw=Math.max(1,w-pad.l-pad.r),ch=Math.max(1,h-pad.t-pad.b);
  const first=visiblePoints[0].t,last=visiblePoints[visiblePoints.length-1].t;
  const start=first===last?first-30*60*1000:first,end=first===last?last+30*60*1000:last,span=end-start;
  const values=visiblePoints.map(point=>point.v),low=Math.min(...values),high=Math.max(...values);
  const spread=high-low||Math.max(Math.abs(high),1),padding=spread*.08;
  const min=low<0?low-padding:0,max=high<0?0:high+padding;
  const range=max-min||1;
  ctx.clearRect(0,0,w,h);
  ctx.font='10px ui-monospace,SFMono-Regular,Menlo,monospace';
  ctx.textBaseline='middle';
  const ticks=min===0?[max,max*2/3,max/3,0]
    :max===0?[0,min/3,min*2/3,min]
    :[max,max/2,0,min];
  ticks.forEach(value=>{
    const y=pad.t+(max-value)/range*ch;
    ctx.beginPath();ctx.setLineDash(value===0?[3,4]:[]);
    ctx.strokeStyle=value===0?'rgba(255,255,255,.2)':'rgba(255,255,255,.05)';
    ctx.lineWidth=1;ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();
    ctx.setLineDash([]);ctx.fillStyle='#8b909a';ctx.textAlign='right';
    ctx.fillText(priceText(value),pad.l-8,y);
  });
  ctx.textBaseline='alphabetic';ctx.fillStyle='#8b909a';ctx.font='10px ui-monospace,SFMono-Regular,Menlo,monospace';
  for(let i=0;i<4;i++){
    const fraction=i/3,x=pad.l+cw*fraction,t=start+span*fraction;
    ctx.textAlign=i===0?'left':i===3?'right':'center';
    ctx.fillText(dateLabel(t,span),x,h-5);
  }
  chartCoords=visiblePoints.map(point=>({
    x:pad.l+Math.max(0,Math.min(1,(point.t-start)/span))*cw,
    y:pad.t+(max-point.v)/range*ch
  }));
  const baselineY=pad.t+(max/range)*ch;
  const areaFill=ctx.createLinearGradient(0,pad.t,0,pad.t+ch);
  areaFill.addColorStop(0,'rgba(0,208,156,.13)');areaFill.addColorStop(1,'rgba(0,208,156,0)');
  ctx.beginPath();ctx.moveTo(chartCoords[0].x,baselineY);ctx.lineTo(chartCoords[0].x,chartCoords[0].y);
  for(let i=1;i<chartCoords.length;i++){
    const previous=chartCoords[i-1],current=chartCoords[i],midX=(previous.x+current.x)/2,midY=(previous.y+current.y)/2;
    ctx.quadraticCurveTo(previous.x,previous.y,midX,midY);
  }
  const areaEnd=chartCoords[chartCoords.length-1];ctx.lineTo(areaEnd.x,areaEnd.y);ctx.lineTo(areaEnd.x,baselineY);
  ctx.closePath();ctx.fillStyle=areaFill;ctx.fill();
  ctx.beginPath();ctx.moveTo(chartCoords[0].x,chartCoords[0].y);
  for(let i=1;i<chartCoords.length;i++){
    const previous=chartCoords[i-1],current=chartCoords[i],midX=(previous.x+current.x)/2,midY=(previous.y+current.y)/2;
    ctx.quadraticCurveTo(previous.x,previous.y,midX,midY);
  }
  const lastCoord=chartCoords[chartCoords.length-1];
  ctx.lineTo(lastCoord.x,lastCoord.y);
  ctx.strokeStyle='#00d09c';ctx.lineWidth=1.5;ctx.lineCap='round';ctx.lineJoin='round';ctx.stroke();
  if(activeIndex>=0&&chartCoords[activeIndex]){
    const active=chartCoords[activeIndex];
    ctx.save();ctx.beginPath();ctx.setLineDash([3,4]);ctx.strokeStyle='rgba(255,255,255,.2)';
    ctx.lineWidth=1;ctx.moveTo(active.x,pad.t);ctx.lineTo(active.x,pad.t+ch);ctx.stroke();
    ctx.setLineDash([]);ctx.shadowColor='#00d09c';ctx.shadowBlur=10;ctx.beginPath();
    ctx.arc(active.x,active.y,6,0,Math.PI*2);ctx.strokeStyle='#00d09c';ctx.lineWidth=1.5;ctx.stroke();
    ctx.shadowBlur=0;ctx.beginPath();ctx.arc(active.x,active.y,2.5,0,Math.PI*2);
    ctx.fillStyle='#121316';ctx.fill();ctx.restore();
  }
}
function updateHeader(){
  if(!visiblePoints.length)return;
  const latest=visiblePoints[visiblePoints.length-1],pct=issuePrice>0?latest.v/issuePrice*100:0;
  latestPrice.textContent=priceText(latest.v);
  gainBadge.textContent=(pct>0?'+':'')+pct.toFixed(2)+'%';
  gainBadge.className='change '+(latest.v<0?'negative':'positive');
  updated.textContent=sourceLabel+' | Latest '+fmtFullDate(latest.t);
}
function render(){
  document.querySelectorAll('[data-range]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.range===selectedRange)));
  const duration=ranges[selectedRange],latestTime=points.length?points[points.length-1].t:0;
  visiblePoints=duration===null?points:points.filter(point=>point.t>=latestTime-duration);
  activeIndex=-1;tooltip.hidden=true;
  summary.textContent=visiblePoints.length+' / '+points.length+' observations';
  canvas.hidden=visiblePoints.length===0;message.hidden=visiblePoints.length>0;
  if(!visiblePoints.length){message.textContent='No GMP observations have been saved yet.';return}
  updateHeader();draw();
}
canvas.addEventListener('pointermove',event=>{
  if(!chartCoords.length)return;
  const rect=canvas.getBoundingClientRect(),x=event.clientX-rect.left;
  activeIndex=0;
  for(let i=1;i<chartCoords.length;i++){
    if(Math.abs(chartCoords[i].x-x)<Math.abs(chartCoords[activeIndex].x-x))activeIndex=i;
  }
  const point=visiblePoints[activeIndex],coord=chartCoords[activeIndex];
  const gmpPct=issuePrice>0?((point.v/issuePrice)*100).toFixed(2):null;
  const percentage=gmpPct===null?'':`${Number(gmpPct)>0?'+':''}${gmpPct}% `;
  tooltipText.textContent=`${percentage}(${String.fromCodePoint(0x20B9)}${point.v}) | ${fmtFullDate(point.t)}`;
  tooltip.hidden=false;
  const tooltipHalfWidth=tooltip.getBoundingClientRect().width/2;
  const left=Math.max(tooltipHalfWidth+8,Math.min(rect.width-tooltipHalfWidth-8,coord.x));
  tooltip.style.left=left+'px';tooltip.style.top=Math.max(38,coord.y-8)+'px';
  draw();
});
canvas.addEventListener('pointerleave',()=>{activeIndex=-1;tooltip.hidden=true;draw()});
window.addEventListener('keydown',event=>{
  if(event.key==='Escape'||event.key==='/'){
    window.parent.postMessage({type:'ipo-track-key',key:event.key},'*');
  }
});
document.getElementById('title').textContent=companyName+' · GMP';
document.querySelectorAll('[data-range]').forEach(button=>button.addEventListener('click',()=>{selectedRange=button.dataset.range;render()}));
window.addEventListener('resize',draw);
render();
</script></body></html>'''
    page = page.replace("__IPO_NAME__", ipo_name)
    page = page.replace("__SOURCE_LABEL__", source_label_json)
    page = page.replace("__ISSUE_PRICE__", issue_price_json)
    page = page.replace("__OBSERVATIONS__", observation_data)
    return HTMLResponse(page)

@app.post("/api/sync")
async def trigger_manual_sync():
    try:
        result = await run_pipeline()
    except Exception as exc:
        logger.warning("Sync request failed; serving cached data: %s", exc, exc_info=True)
        try:
            last_synced_at = read_sync_status()["last_synced_at"]
        except Exception:
            logger.exception("Could not read sync status after failed refresh")
            last_synced_at = None
        return {
            "status": "cached",
            "last_synced_at": last_synced_at,
            "synced": 0,
        }
    return {
        "status": "ok" if result["ok"] else "cached",
        "last_synced_at": result.get("last_synced_at"),
        "synced": result["synced"],
    }
