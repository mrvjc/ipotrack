from pydantic import BaseModel

class AllotmentAndProfitReport(BaseModel):
    ipo_name: str
    issue_price: float
    lot_size: int
    total_investment: float
    retail_subscription_times: float | None
    retail_subscription_is_estimate: bool
    allotment_probability_pct: float | None
    probability_verdict: str
    gmp_price: float
    estimated_listing_price: float
    gross_profit_per_lot: float
    estimated_taxes_and_charges: float
    net_profit_per_lot: float
    net_roi_pct: float
    breakeven_selling_price: float

def calculate_ipo_analytics(
    company_name: str,
    issue_price: float,
    lot_size: int,
    gmp_price: float,
    retail_sub: float | None,
    subscription_total: float | None = None,
) -> AllotmentAndProfitReport:
    # 1. Capital Required for 1 Retail Lot
    total_investment = issue_price * lot_size
    est_listing_price = issue_price + gmp_price
    total_sell_value = est_listing_price * lot_size

    # 2. Indicative retail allotment odds. Total subscription is never used here.
    if retail_sub is None:
        prob_pct = None
        verdict = (
            "Bidding not started"
            if subscription_total is not None and subscription_total <= 0
            else "Retail subscription data unavailable"
        )
    elif retail_sub <= 1.0:
        prob_pct = 100.0
        verdict = "Retail category is undersubscribed"
    else:
        prob_pct = round((1.0 / retail_sub) * 100, 2)
        verdict = f"Approx. 1 in {round(retail_sub, 1)} retail applicants"

    # 3. Indian Exchange Statutory Charges & Break-Even Calculation (Delivery Sale on Listing Day)
    # STT: 0.1% on sell value | Exchange Txn: 0.00325% | SEBI: 0.0001% | DP Charge: ~Rs 15.93
    stt = total_sell_value * 0.001
    exchange_txn = total_sell_value * 0.0000325
    sebi_fees = total_sell_value * 0.000001
    dp_charge_base = 13.50 # Standard CDSL/Broker DP charge before GST
    gst = 0.18 * (exchange_txn + sebi_fees + dp_charge_base)

    total_charges = round(stt + exchange_txn + sebi_fees + dp_charge_base + gst, 2)

    # 4. Gross vs Net Profit
    gross_profit = round(gmp_price * lot_size, 2)
    net_profit = round(gross_profit - total_charges, 2)
    net_roi = round((net_profit / total_investment) * 100, 2) if total_investment > 0 else 0.0

    # Minimum selling price per share needed just to cover taxes & DP charges
    breakeven_price = round(issue_price + (total_charges / lot_size), 2) if lot_size > 0 else 0.0

    return AllotmentAndProfitReport(
        ipo_name=company_name,
        issue_price=issue_price,
        lot_size=lot_size,
        total_investment=round(total_investment, 2),
        retail_subscription_times=retail_sub,
        retail_subscription_is_estimate=False,
        allotment_probability_pct=prob_pct,
        probability_verdict=verdict,
        gmp_price=gmp_price,
        estimated_listing_price=est_listing_price,
        gross_profit_per_lot=gross_profit,
        estimated_taxes_and_charges=total_charges,
        net_profit_per_lot=net_profit,
        net_roi_pct=net_roi,
        breakeven_selling_price=breakeven_price
    )
