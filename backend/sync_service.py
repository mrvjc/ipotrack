import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from database import (
    SessionLocal, init_db, IPO, GMPHistory, SubscriptionSnapshot,
    ListingPerformanceSnapshot, SyncStatus,
)
from scraper import IPODataAggregator, IPOSnapshot, _clean_date


GMP_HISTORY_RETENTION_DAYS = 30
GMP_SNAPSHOT_INTERVAL = timedelta(hours=6)
SYNC_STATUS_ID = 1
logger = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _serialize_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc_naive(value).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _set_sync_status(
    status: str,
    *,
    last_synced_at: datetime | None = None,
    message: str | None = None,
) -> None:
    with SessionLocal() as db:
        row = db.get(SyncStatus, SYNC_STATUS_ID)
        if row is None:
            row = SyncStatus(id=SYNC_STATUS_ID, status=status)
            db.add(row)
        row.status = status
        row.last_attempt_at = _utc_now_naive()
        if last_synced_at is not None:
            row.last_synced_at = last_synced_at
        row.message = message
        db.commit()


def read_sync_status() -> dict[str, str | None]:
    with SessionLocal() as db:
        row = db.get(SyncStatus, SYNC_STATUS_ID)
        if row is None:
            return {"status": "cached", "last_synced_at": None, "message": None}
        return {
            "status": row.status,
            "last_synced_at": _serialize_utc(row.last_synced_at),
            "message": row.message,
        }


def _company_key(name: str) -> str:
    """Normalize punctuation/spacing differences between IPOJi lifecycle pages."""
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def sync_ipo_to_db(db: Session, snapshots: list[IPOSnapshot]):
    existing_records = db.query(IPO).filter(IPO.category.notilike("%sme%")).all()
    existing_by_name = {row.company_name: row for row in existing_records}
    existing_by_key = {_company_key(row.company_name): row for row in existing_records}
    gmp_observations_by_ipo: dict[int, dict[datetime, GMPHistory]] = {}
    latest_gmp_by_ipo: dict[int, GMPHistory] = {}
    existing_ids = [row.id for row in existing_records]
    if existing_ids:
        history_records = db.query(GMPHistory).filter(
            GMPHistory.ipo_id.in_(existing_ids),
        ).all()
        for item in history_records:
            if item.source == "ipoji":
                gmp_observations_by_ipo.setdefault(item.ipo_id, {})[item.recorded_at] = item
            latest = latest_gmp_by_ipo.get(item.ipo_id)
            if latest is None or _as_utc_naive(item.recorded_at) > _as_utc_naive(latest.recorded_at):
                latest_gmp_by_ipo[item.ipo_id] = item

    sync_time = _utc_now_naive()

    for snapshot in snapshots:
        if snapshot.category.strip().casefold() != "mainboard":
            print(f"[SKIP IPO] Ignoring non-mainboard IPO: {snapshot.company_name}")
            continue

        key = _company_key(snapshot.company_name)
        ipo_record = existing_by_name.get(snapshot.company_name) or existing_by_key.get(key)

        if not ipo_record:
            print(f"[NEW IPO] Inserting IPO: {snapshot.company_name} ({snapshot.category})")
            ipo_record = IPO(
                company_name=snapshot.company_name,
                category=snapshot.category,
                price_band_high=snapshot.price_band_high,
                lot_size=snapshot.lot_size,
                open_date=_clean_date(snapshot.open_date),
                close_date=_clean_date(snapshot.close_date),
                listing_date=_clean_date(snapshot.listing_date),
                listing_price=snapshot.listing_price,
                listing_gain_pct=snapshot.listing_gain_pct,
                ipoji_url=snapshot.ipoji_url,
                registrar_name=snapshot.registrar_name,
                registrar_url=snapshot.registrar_url,
                source_status=snapshot.status,
            )
            db.add(ipo_record)
            db.flush()
            existing_by_name[ipo_record.company_name] = ipo_record
            existing_by_key[key] = ipo_record
        else:
            if snapshot.price_band_high > 0:
                ipo_record.price_band_high = snapshot.price_band_high
            if snapshot.lot_size > 0:
                ipo_record.lot_size = snapshot.lot_size
            ipo_record.category = snapshot.category
            ipo_record.open_date = _clean_date(snapshot.open_date) or _clean_date(ipo_record.open_date)
            ipo_record.close_date = _clean_date(snapshot.close_date) or _clean_date(ipo_record.close_date)
            ipo_record.listing_date = _clean_date(snapshot.listing_date) or _clean_date(ipo_record.listing_date)
            if snapshot.listing_price is not None:
                ipo_record.listing_price = snapshot.listing_price
            if snapshot.listing_gain_pct is not None:
                ipo_record.listing_gain_pct = snapshot.listing_gain_pct
            if snapshot.ipoji_url:
                ipo_record.ipoji_url = snapshot.ipoji_url
            if snapshot.registrar_name:
                ipo_record.registrar_name = snapshot.registrar_name
                ipo_record.registrar_url = snapshot.registrar_url
            elif snapshot.registrar_url:
                ipo_record.registrar_url = snapshot.registrar_url
            ipo_record.source_status = snapshot.status

        # Keep each source observation so a stale/missing listing result is visible
        # and can be compared with the latest value returned by /api/ipos.
        if snapshot.listing_price is not None or snapshot.listing_gain_pct is not None:
            db.add(ListingPerformanceSnapshot(
                ipo_id=ipo_record.id,
                listing_date=snapshot.listing_date or ipo_record.listing_date,
                listing_price=snapshot.listing_price,
                listing_gain_pct=snapshot.listing_gain_pct,
            ))

        # Avoid writing an unchanged refresh point more often than every six hours.
        # Timestamped IPOJi chart observations remain separately deduplicated by timestamp.
        if snapshot.status.strip().lower() != "listed":
            latest_snapshot = latest_gmp_by_ipo.get(ipo_record.id)
            snapshot_changed = (
                latest_snapshot is None
                or latest_snapshot.gmp_price != snapshot.gmp_price
            )
            snapshot_is_stale = (
                latest_snapshot is None
                or sync_time - _as_utc_naive(latest_snapshot.recorded_at) >= GMP_SNAPSHOT_INTERVAL
            )
            if snapshot_changed or snapshot_is_stale:
                refresh_record = GMPHistory(
                    ipo_id=ipo_record.id,
                    gmp_price=snapshot.gmp_price,
                    estimated_listing_price=snapshot.estimated_listing_price,
                    recorded_at=sync_time,
                    source="refresh",
                )
                db.add(refresh_record)
                latest_gmp_by_ipo[ipo_record.id] = refresh_record

            db.add(SubscriptionSnapshot(
                ipo_id=ipo_record.id,
                qib_times=snapshot.qib_sub,
                nii_times=snapshot.nii_sub,
                retail_times=snapshot.retail_sub,
                total_times=snapshot.subscription_total,
                retail_is_proxy=snapshot.retail_sub_is_proxy,
                category_split_available=snapshot.category_split_available,
                total_subscription_available=snapshot.total_subscription_available,
            ))

        # IPOJi's graph data carries the actual observation timestamps. Keep those
        # points separately from the one-per-refresh snapshot used as a fallback.
        existing_observations = gmp_observations_by_ipo.setdefault(ipo_record.id, {})
        for observation in snapshot.gmp_observations:
            try:
                timestamp = datetime.fromisoformat(
                    str(observation["timestamp"]).replace("Z", "+00:00")
                )
                if timestamp.tzinfo is not None:
                    timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
                gmp_price = float(observation["gmp_price"])
            except (KeyError, TypeError, ValueError):
                continue

            existing = existing_observations.get(timestamp)
            if existing:
                if existing.gmp_price != gmp_price:
                    existing.gmp_price = gmp_price
                    existing.estimated_listing_price = (
                        ipo_record.price_band_high + gmp_price
                    )
                continue

            observation_record = GMPHistory(
                ipo_id=ipo_record.id,
                gmp_price=gmp_price,
                estimated_listing_price=ipo_record.price_band_high + gmp_price,
                recorded_at=timestamp,
                source="ipoji",
            )
            db.add(observation_record)
            existing_observations[timestamp] = observation_record
            latest_observation = latest_gmp_by_ipo.get(ipo_record.id)
            if (
                latest_observation is None
                or timestamp > _as_utc_naive(latest_observation.recorded_at)
            ):
                latest_gmp_by_ipo[ipo_record.id] = observation_record

    # Bound fallback snapshots while preserving IPOJi's timestamped source history.
    retention_cutoff = datetime.utcnow() - timedelta(days=GMP_HISTORY_RETENTION_DAYS)
    db.query(GMPHistory).filter(
        GMPHistory.source == "refresh",
        GMPHistory.recorded_at < retention_cutoff,
    ).delete(synchronize_session=False)
    db.commit()
    print(f"[Sync Complete] Successfully processed {len(snapshots)} IPOs")


async def run_pipeline():
    async with _sync_lock:
        try:
            init_db()
            _set_sync_status("syncing")
            aggregator = IPODataAggregator()
            snapshots = await aggregator.fetch_live_gmp_data()
            db = SessionLocal()
            try:
                sync_ipo_to_db(db=db, snapshots=snapshots)
            finally:
                db.close()

            synced_at = _utc_now_naive()
            _set_sync_status("ok", last_synced_at=synced_at)
            return {
                "ok": True,
                "synced": len(snapshots),
                "error": None,
                "last_synced_at": _serialize_utc(synced_at),
            }
        except Exception as exc:
            logger.warning("IPOJi sync failed; keeping cached database data: %s", exc, exc_info=True)
            try:
                _set_sync_status("cached", message="Live data unavailable; showing the last saved snapshot.")
                last_synced_at = read_sync_status()["last_synced_at"]
            except Exception:
                logger.exception("Could not persist or read cached sync status")
                last_synced_at = None
            return {
                "ok": False,
                "synced": 0,
                "error": str(exc),
                "last_synced_at": last_synced_at,
            }

if __name__ == "__main__":
    asyncio.run(run_pipeline())
