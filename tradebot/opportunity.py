"""What the same money would have done in a plain index fund.

Every other comparison in this package asks whether a strategy beat holding *the same
asset*. That is the right test of the rules, and it is not the decision anyone actually
faces. The real question is what else the money could have been doing, and for almost
everyone the honest alternative is a low-cost index fund bought once and left alone.

That comparison is deliberately unflattering, and it is here because it keeps winning.
Over the ten years this repository tests on, ten thousand pounds held in SPY finished at
about forty-one thousand. The best strategy built here finished at twenty-seven. The
fourteen thousand pound difference is not a rounding error or a bad decade - it is the
measured result, and it is the largest single number in the project.

So this is not a benchmark in the usual sense. It is the cost of choosing to do
something rather than nothing, priced in pounds, and printed next to every result that
might tempt you into doing something.

A note on fairness
------------------
Comparing a Bitcoin strategy to an equity index is not like-for-like, and nothing here
pretends otherwise: different assets, different risks, different everything. It is not
offered as a like-for-like measure of skill. It is offered as the opportunity cost of
the capital, which is the number that decides whether an activity was worth doing.
Judging the rules is what the same-asset benchmark is for, and both are always shown.

The index also loses money, sometimes badly. It fell 33.7% inside the window tested
here. "Boring" does not mean "safe", and this module will say so.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import Candle

#: A broad, cheap, widely held equity index fund. Not a recommendation - a yardstick,
#: and the one most people's alternative actually resembles.
DEFAULT_INDEX = "SPY"

#: Typical all-in annual cost of a mainstream index tracker, as a fraction. Small, and
#: charged here anyway: a comparison that gave the index a free ride would be doing the
#: exact thing this package exists to complain about.
INDEX_ANNUAL_FEE = 0.0007

MS_PER_YEAR = 365.25 * 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class Opportunity:
    """What holding the index over the same window would have come to."""

    symbol: str
    start_price: float
    end_price: float
    years: float
    starting_cash: float

    @property
    def gross_return_pct(self) -> float:
        if self.start_price <= 0:
            return 0.0
        return (self.end_price / self.start_price - 1.0) * 100.0

    @property
    def return_pct(self) -> float:
        """After the tracker's own annual fee, so the yardstick is not flattered either."""
        gross = self.end_price / self.start_price if self.start_price > 0 else 1.0
        return (gross * (1.0 - INDEX_ANNUAL_FEE) ** max(self.years, 0.0) - 1.0) * 100.0

    @property
    def ending_cash(self) -> float:
        return self.starting_cash * (1.0 + self.return_pct / 100.0)

    def shortfall(self, strategy_ending: float) -> float:
        """Pounds left on the table by trading instead of holding the index.

        Negative means the strategy won. Reported in money rather than percent on
        purpose: percentages are easy to wave away, and a four-figure number is not.
        """
        return self.ending_cash - strategy_ending


def measure(candles: list[Candle], starting_cash: float) -> Opportunity | None:
    """Price the do-nothing alternative over exactly the window ``candles`` covers."""
    if len(candles) < 2:
        return None
    first, last = candles[0], candles[-1]
    if first.close <= 0:
        return None
    years = (last.ts - first.ts) / MS_PER_YEAR
    return Opportunity(
        symbol=DEFAULT_INDEX,
        start_price=first.close,
        end_price=last.close,
        years=years,
        starting_cash=starting_cash,
    )


def render(opportunity: Opportunity | None, strategy_ending: float,
           strategy_label: str = "this strategy", currency: str = "£") -> str:
    """One block, printed under a result, saying what the alternative was worth."""
    if opportunity is None:
        return ""

    gap = opportunity.shortfall(strategy_ending)
    lines = [
        "",
        f"  The do-nothing alternative ({opportunity.symbol}, held)",
        "  " + "-" * 46,
        f"  {'index fund, held':<26}{currency}{opportunity.ending_cash:>12,.2f}"
        f"{opportunity.return_pct:>10.1f}%",
        f"  {strategy_label:<26}{currency}{strategy_ending:>12,.2f}"
        f"{(strategy_ending / opportunity.starting_cash - 1.0) * 100.0:>10.1f}%",
    ]

    if gap > 0:
        lines += [
            "",
            f"  Trading cost you {currency}{gap:,.2f} against buying the index and",
            "  leaving it alone. That is the price of the activity, and it is the",
            "  number to beat before any of this is worth your time.",
        ]
    else:
        lines += [
            "",
            f"  This beat the index by {currency}{abs(gap):,.2f} over this window.",
            "  One window is not evidence. Check it out of sample before believing it.",
        ]

    if opportunity.years >= 0.5:
        lines.append("")
        lines.append("  The index is not a safe asset - it fell hard inside this window too.")
        lines.append("  Cheaper and better are different claims; this is the cheaper one.")
    lines.append("")
    return "\n".join(lines)
