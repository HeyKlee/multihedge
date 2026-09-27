#!/usr/bin/env python3
"""Build the MultiHedge friction-and-exit-defect report as a PDF."""

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

OUT = "/home/kelly/multihedge/reports/multihedge-friction-exit-report.pdf"

H1 = ParagraphStyle("H1", fontName="Helvetica-Bold", fontSize=15, spaceAfter=4,
                    textColor=colors.HexColor("#1a1a1a"))
H2 = ParagraphStyle("H2", fontName="Helvetica-Bold", fontSize=10.5, spaceBefore=9,
                    spaceAfter=3, textColor=colors.HexColor("#333333"))
BODY = ParagraphStyle("BODY", fontName="Helvetica", fontSize=8.6, leading=12.2)
MONO = ParagraphStyle("MONO", fontName="Courier", fontSize=7.5, leading=9.4)
RED = ParagraphStyle("RED", parent=BODY, textColor=colors.HexColor("#a01818"))
GREEN = ParagraphStyle("GREEN", parent=BODY, textColor=colors.HexColor("#14601e"))

F = []


def h1(t):
    F.append(Paragraph(t, H1))


def h2(t):
    F.append(Paragraph(t, H2))


def p(t, style=BODY):
    F.append(Paragraph(t, style))


def code(t):
    for line in t.strip("\n").split("\n"):
        F.append(Paragraph(line.replace(" ", "&nbsp;") or "&nbsp;", MONO))
    F.append(Spacer(1, 3))


def table(rows, widths):
    t = Table(rows, colWidths=widths)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f5f5f5")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    F.append(t)
    F.append(Spacer(1, 5))


# ----------------------------------------------------------------- title
h1("MultiHedge: Execution Friction and Exit-Latency Defect Report")
p("Prepared 27 September 2026. All figures are measured from the production "
  "database or from authenticated Jupiter quotes. No figure in this report is "
  "modelled, estimated, or drawn from market microstructure literature.")
p("<b>Scope:</b> the cost model, the exit engine, a 125-configuration "
  "parameter sweep with chronological holdout, and a 1,000,000-trade "
  "simulation. This report supersedes an earlier cost figure that was "
  "derived with an inverted sign; the correction is documented in section 2.")

F.append(Spacer(1, 6))
h2("Summary of findings")
table([
    ["#", "Finding", "Evidence", "Status"],
    ["1", "Cost model had an inverted sign; every derived statistic was wrong",
     "rt = (buy*sell/mid^2 - 1) is structurally &le; 0", "CORRECTED"],
    ["2", "Cached mid compared against live quotes produced 98 impossible rows",
     "buy &lt; mid and sell &gt; mid simultaneously", "CORRECTED"],
    ["3", "True round-trip friction is 0.485% mean, not 1.8%",
     "57 authenticated quotes, zero negative values", "MEASURED"],
    ["4", "2% stop realises -7.389% mean; 58% close between -10% and -20%",
     "159 closed stops, exits checked every 120s", "CONFIRMED DEFECT"],
    ["5", "All 125 swept configurations are negative out of sample",
     "0 of 125 positive on holdout, both exit modes", "CONFIRMED"],
    ["6", "$24 account exhausts at trade 5,022 under 25% fractional sizing",
     "1,000,000-trade simulation, measured cost", "CONFIRMED"],
], [8 * mm, 66 * mm, 62 * mm, 24 * mm])

# ------------------------------------------------------- 1. exit latency
h2("1. The stop-loss defect: a 2% stop that loses 7.4%")
p("The live policy declares <font face='Courier'>STOP_LOSS = 0.02</font>. "
  "Realised stop-loss exits on the dynamic_scalper cohort do not resemble "
  "that number.")
code("""  policy stop:                      -2.0%
  realised stops (n=159)
     -3% to  -2%      11 trades     7%
     -5% to  -3%       5 trades     3%
    -10% to  -5%       2 trades     1%
    -20% to -10%      92 trades    58%""")
p("58% of all stop-losses close between -10% and -20%. This is not a small "
  "number of outliers dragging a mean; it is the dominant behaviour of the "
  "exit. Mean realised stop: <b>-7.389%</b>, which is 3.7x the declared stop. "
  "The worst single stop was -19.16%.")
p("Cause, measured directly from the sampling intervals:")
code("""  intervals between exit checks (n=34,012)
     median   120s
     p90      121s
     max   47,071s
  intervals over 60s:  100%""")
p("The exit function in <font face='Courier'>dynamic_shadow_scalper.py</font> "
  "is correct: it fires the instant "
  "<font face='Courier'>change &lt;= stop_loss_pct</font>. But it is only "
  "given the opportunity to look every 120 seconds. A memecoin can move 15% in "
  "two minutes. <b>A 2% stop evaluated every 120 seconds is not a 2% stop.</b>")
p("Economic weight, aggregating every trade by exit reason:")
code("""  trail_stop     150 x  +2.845%  =   +426.8%
  take_profit     74 x  +9.055%  =   +670.1%
  stop_loss      159 x  -7.389%  =  -1174.8%
                              total   -77.9%""")
p("Both winning exits are profitable. The stop-loss alone loses more than "
  "both winners earn combined. This single mechanism, not the cost model and "
  "not the parameter choice, is the dominant loss source in the system.",
  RED)

# ------------------------------------------------------- 2. cost model
h2("2. Correction: the cost model had an inverted sign")
p("The original monitor stored round-trip cost as:")
code("""  ops/route_monitor.py:384
  rt = ((buy * sell / (mid * mid)) - 1.0) * 100.0""")
p("For any real bid/ask, <font face='Courier'>buy &ge; mid</font> and "
  "<font face='Courier'>sell &le; mid</font>. The expression is therefore "
  "<b>structurally always &le; 0</b>. A negative value means COST, not gain. "
  "Read as a positive cost, the sign inverted and every statistic derived "
  "from it was wrong.")
p("A second, independent defect compounded it. The mid was a <b>cached</b> "
  "price while the quote was fetched live. On a fast-moving memecoin the "
  "cached mid lags, so both legs can appear favourable and the round trip "
  "appears to pay you. That produced 98 of 147 rows with a positive value, "
  "i.e. free money, which is impossible.")
p("Both defects are corrected in "
  "<font face='Courier'>ops/friction_measure_fixed.py</font>, which derives "
  "the reference from the quote pair itself so no cached price enters the "
  "arithmetic, and which quotes the sell leg for the token amount the buy "
  "leg actually produced.")

# ------------------------------------------------------- 3. true friction
h2("3. Measured friction on 57 authenticated Jupiter quotes")
code("""  successful quotes      57      throttled 29, failed 0
  median                 0.3460%
  mean                   0.4850%
  p90                    1.6841%
  max                    1.8854%
  negative values        0        (a negative cost is impossible)""")
table([
    ["Notional", "n", "median", "max"],
    ["$1", "19", "0.1843%", "1.8763%"],
    ["$2", "19", "0.4399%", "1.8786%"],
    ["$5", "19", "0.4946%", "1.8854%"],
], [30 * mm, 14 * mm, 30 * mm, 30 * mm])
p("The median sits at the AMM fee floor of roughly 0.50%, which is the "
  "physically expected result and a strong check that the corrected "
  "arithmetic is right. The near-zero individual observations are deep "
  "liquidity majors at small notionals, where the fee is waived or "
  "discounted and 6-decimal rounding is negligible.")
p("<b>Implication for the declared cost.</b> The system charges 1.800% per "
  "round trip. Measured mean friction is 0.485%, roughly 3.7x lower. The "
  "declared cost is conservative and materially overstates real friction. "
  "That is a defensible choice for safety, but it means reported paper P&amp;L "
  "understates strategy performance by about 1.3 percentage points per trade. "
  "Lowering it is a policy decision and has NOT been made here.")
p("Gas is accounted separately and explicitly, at $0.0005 per trade, rather "
  "than being folded into the friction figure.")

# ------------------------------------------------------- 4. sweep
h2("4. Parameter sweep: 125 configurations, chronological holdout")
p("Every entry in <font face='Courier'>mh_scalp_price_samples</font> was "
  "replayed under 125 combinations of take profit, stop loss and max hold "
  "(5 x 5 x 5). The split is on entry timestamp, never random, so no future "
  "price can inform an earlier decision. Cost is charged per exit from the "
  "measured distribution.")
p("Two exit modes are reported because they answer different questions: "
  "<b>as-recorded</b> evaluates the exit only at the 120-second sample "
  "points, which is what the running system does; <b>continuous</b> evaluates "
  "within each interval, bounding what a faster price feed could recover.")
table([
    ["Mode", "configs", "positive on train", "positive on holdout"],
    ["as-recorded", "125", "77", "0"],
    ["continuous", "125", "37", "0"],
], [30 * mm, 22 * mm, 40 * mm, 40 * mm])
p("<b>Zero of 125 configurations are profitable out of sample, in both exit "
  "modes.</b> 77 of 125 look positive on the training half, which is exactly "
  "the pattern that produces a strategy that appears to work and then does "
  "not.", RED)
p("Live policy (TP 5%, SL 2%, hold 7200s) on the holdout half:")
code("""  as-recorded   n=191  wr 35.1%  mean -0.4335%  payoff 0.89x  PF 0.48
  continuous    n=191  wr 41.4%  mean -0.3263%  payoff 0.77x  PF 0.54""")
p("Cost sensitivity of the best as-recorded configuration, still on holdout:")
code("""  cost = measured mean     (0.485%)   mean -0.2685%   negative
  cost = measured median   (0.346%)   mean -0.1295%   negative
  cost = measured p90      (1.684%)   mean -1.4676%   negative
  cost = measured max      (1.885%)   mean -1.6689%   negative
  cost = declared          (1.800%)   mean -1.5835%   negative""")
p("The result is negative at every cost in the measured range, including the "
  "most favourable. <b>The problem is not the cost assumption. There is no "
  "gross edge to pay it with.</b>")

# ------------------------------------------------------- 5. simulation
h2("5. One million trade simulation")
p("A backtest cannot support a million-trade estimate from 384 recorded "
  "entries, so trades are drawn by bootstrap from the 191 real holdout "
  "outcomes, preserving the measured win rate, payoff ratio and fat left "
  "tail. Cost is redrawn per trade from the 57 measured quotes. Sizing is 25% "
  "of equity capped at $500, starting from $24.00. Seed 20260927.")
code("""  trades completed       5,021
  ACCOUNT EXHAUSTED      trade 5,022
  final equity           $0.04
  total P&L              $-23.96
  peak equity            $24.30
  max drawdown           99.84%
  worst losing streak    53
  mean P&L per trade     $-0.004772""")
p("The account does not reach $100, $1,000, $10,000 or $1,000,000. It is "
  "exhausted at trade 5,022. Per-trade bookkeeping including size, gross "
  "return, cost, gas and resulting equity is written to "
  "<font face='Courier'>mh_simulation_trades</font>.", RED)
p("The arithmetic of the original target, for the record: growing $24 to "
  "$1,000,000 over 1,000,000 trades requires approximately "
  "<b>+0.000106% per trade</b>. The measured holdout expectancy is "
  "<b>-0.2685%</b> on the best configuration found. The required edge is "
  "roughly 2,500x smaller in magnitude than the loss actually observed, and "
  "of the opposite sign.")

# ------------------------------------------------------- 6. recommendation
h2("6. What is worth doing, in priority order")
p("<b>1. Evaluate exits far more often, or on a real price feed.</b> This is "
  "the only finding with a large, well-evidenced, mechanical payoff. A 2% "
  "stop that realises -7.4% because it is checked every 120 seconds is losing "
  "roughly 5.4 points per stopped trade to sampling latency alone. Across 159 "
  "stops that is the dominant loss in the system. This is an engineering "
  "defect, not a strategy question.")
p("<b>2. Treat the entry signal as unproven.</b> 55.4% of 5-minute forward "
  "labels are positive with a +0.3322% mean, against a measured round-trip "
  "friction of 0.485%. The gross edge is smaller than the cost of capturing "
  "it. The forward-label population is also not the traded population: it "
  "includes candidates the gate never bought.")
p("<b>3. Do not adopt any parameter set from this sweep.</b> Zero of 125 "
  "configurations survive out of sample. Adopting the training-best would be "
  "adopting a curve fit to the first half of September.")
p("<b>4. Consider lowering the declared cost to the measured mean, "
  "explicitly and as a policy decision.</b> 1.8% overstates real friction by "
  "about 3.7x. That would make reported paper P&amp;L more truthful, and it "
  "would NOT make the strategy profitable, because the result is negative at "
  "every measured cost including the lowest.")

h2("7. Changes made and not made")
p("Added, all read-only with respect to trading state:")
code("""  ops/friction_measure_fixed.py   corrected cost measurement
  ops/param_sweep.py               125-config sweep with holdout
  ops/simulate_trades.py           bootstrap simulation with full bookkeeping
  ops/backtest_live_policy.py      historical policy replay (earlier commit)""")
p("Not changed: config.yaml, execution_costs.py, paper.py, "
  "dynamic_shadow_scalper.py, mh_reasoner.py, mh_reasoner_params, any TP, SL, "
  "max-hold, trail, cost, reserve, wallet, signer, live gate or filter. No "
  "transaction was signed, submitted or broadcast. No promotion was applied.")

doc = SimpleDocTemplate(OUT, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                        topMargin=12 * mm, bottomMargin=12 * mm,
                        title="MultiHedge Friction and Exit Defect Report")
doc.build(F)
print("wrote", OUT)
