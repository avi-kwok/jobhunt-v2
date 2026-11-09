"""Priority-dict inversion."""

from jobhunt.config import invert_priorities


def test_invert_priorities_basic():
    grouped = {1: ["Google", "Stripe"], 2: ["Brex"], 3: ["RBC"]}
    mapping = invert_priorities(grouped)
    assert mapping == {"Google": 1, "Stripe": 1, "Brex": 2, "RBC": 3}


def test_invert_priorities_lowest_tier_wins_on_duplicate():
    grouped = {2: ["Stripe"], 1: ["Stripe"]}
    mapping = invert_priorities(grouped)
    assert mapping["Stripe"] == 1

