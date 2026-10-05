"""Раздел расхода между кампаниями по кликам. Главное свойство — деньги не теряются на округлении.

Сумма долей сверяется в ЦЕЛЫХ микро-единицах, а не через float-сравнение (иначе тест сам бы
врал: 66.67*3 == 200.01).
"""
import pytest

from adops.tracker import cost

MICRO = cost.MICRO


def micro(v):
    return int(round(float(v) * MICRO))


def assert_exact(total, shares, why=""):
    got, want = sum(micro(s) for s in shares), micro(total)
    assert got == want, f"{why}: сумма долей {got} мкр != {want} мкр (доли {shares})"


def test_even_split():
    sh = cost.split_by_clicks(200, [500, 500])
    assert sh == [100.0, 100.0]
    assert_exact(200, sh)


def test_uneven_split():
    sh = cost.split_by_clicks(200, [750, 250])
    assert sh == [150.0, 50.0]
    assert_exact(200, sh)


def test_indivisible_total_keeps_sum():
    sh = cost.split_by_clicks(200, [10, 10, 10])
    assert_exact(200, sh)
    assert max(sh) - min(sh) <= 1.0 / MICRO + 1e-12
    naive = [round(200 * 10 / 30, 2)] * 3
    assert sum(naive) != 200, "контроль: наивное округление до центов и правда врёт"


@pytest.mark.parametrize("total,ws", [(0.07, [1, 1, 1]), (271.83, [333, 667]), (1, [1, 2, 3, 4, 5, 6, 7]),
                                      (999.999999, [17, 5, 3]), (0.000003, [1, 1, 1]),
                                      (12345.67, [1, 999999])])
def test_nasty_fractions(total, ws):
    assert_exact(total, cost.split_by_clicks(total, ws), f"total={total} ws={ws}")


def test_zero_weight_gets_nothing():
    sh = cost.split_by_clicks(100, [10, 0, 10])
    assert sh[1] == 0.0
    assert_exact(100, sh)


def test_degenerate_inputs_give_zeros_not_all_to_first():
    assert cost.split_by_clicks(100, [0, 0]) == [0.0, 0.0]
    assert cost.split_by_clicks(0, [5, 5]) == [0.0, 0.0]
    assert cost.split_by_clicks(100, []) == []
    assert cost.split_by_clicks(-5, [1, 1]) == [0.0, 0.0]


def test_plan_single_campaign_gets_everything():
    plan = cost.plan_writes("t", {"t": [(11, "T", 500, 7.0)]}, 200)
    assert len(plan) == 1 and plan[0][0] == 11 and plan[0][4] == 200


def test_plan_two_campaigns_split_by_clicks():
    plan = cost.plan_writes("t", {"t": [(11, "T", 500, 7.0), (22, "T", 500, 3.0)]}, 200)
    assert [p[4] for p in plan] == [100.0, 100.0]
    assert_exact(200, [p[4] for p in plan])


def test_plan_ignores_zero_click_campaigns():
    tmap = {"k": [(11, "T", 500, 0.0), (22, "T", 0, 0.0), (33, "T", 500, 0.0)]}
    plan = cost.plan_writes("k", tmap, 200)
    assert {p[0] for p in plan} == {11, 33}


def test_plan_order_is_deterministic():
    a, b = (11, "T", 300, 0.0), (22, "T", 700, 0.0)
    p1 = cost.plan_writes("k", {"k": [a, b]}, 100)
    p2 = cost.plan_writes("k", {"k": [b, a]}, 100)
    assert p1 == p2
    assert p1[0][0] == 22, "первой идёт кампания с БОЛЬШИМ числом кликов"


def test_plan_tie_break_by_campaign_id():
    x, y = (5, "T", 10, 0.0), (9, "T", 10, 0.0)
    assert cost.plan_writes("k", {"k": [x, y]}, 1) == cost.plan_writes("k", {"k": [y, x]}, 1)


def test_plan_unknown_tag_is_empty():
    assert cost.plan_writes("нет-такой", {"k": [(1, "T", 5, 0.0)]}, 100) == []
