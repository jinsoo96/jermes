"""SpineMemoryStore - a host's spine as jermes's memory backend instead of the
local JSONL file. Mirrors SpineSkillLedger's existing tests one level down."""

from jermes.host import InMemorySpineStore, SpineMemoryStore
from jermes.memory import MemoryItem


def test_a_written_item_survives_a_fresh_store_over_the_same_spine():
    """한 프로세스가 쓰고 다른 프로세스(또는 다른 시점)가 같은 spine 으로 새로
    store 를 열어도 마지막 상태를 봐야 한다 - 원장이 이미 그렇게 동작한다."""
    spine = InMemorySpineStore()
    SpineMemoryStore(spine).save([MemoryItem(item_id="a", text="cp949 실패엔 utf-8")])

    reopened = SpineMemoryStore(spine)
    got = reopened.load()
    assert len(got) == 1
    assert got[0].item_id == "a"
    assert got[0].text == "cp949 실패엔 utf-8"


def test_the_latest_write_for_an_id_wins_on_replay():
    """append-only 라 옛 값이 지워지지 않는다 - 그러나 읽을 때는 마지막 것만."""
    spine = InMemorySpineStore()
    store = SpineMemoryStore(spine)
    store.save([MemoryItem(item_id="a", text="first")])
    store.save([MemoryItem(item_id="a", text="second", trust=0.8)])

    got = {i.item_id: i for i in SpineMemoryStore(spine).load()}
    assert len(got) == 1
    assert got["a"].text == "second"
    assert got["a"].trust == 0.8


def test_measurement_history_round_trips():
    """trust 숫자만이 아니라 왜 그 숫자인지(evidence·history)도 같이 산다 -
    to_dict/from_dict 가 이미 그렇게 하므로 spine 경로에서도 그래야 한다."""
    spine = InMemorySpineStore()
    item = MemoryItem(item_id="a", text="사실")
    item.evidence["measurements"] = [{"cases": 5, "gain": 0.2, "verdict": "helpful"}]
    item.history.append("measure: helpful gain=+0.200 trust 0.50->0.65 (n=5)")
    SpineMemoryStore(spine).save([item])

    back = SpineMemoryStore(spine).load()[0]
    assert back.evidence["measurements"][0]["verdict"] == "helpful"
    assert back.history == item.history


def test_two_items_do_not_clobber_each_other():
    spine = InMemorySpineStore()
    store = SpineMemoryStore(spine)
    store.save([MemoryItem(item_id="a", text="첫째"), MemoryItem(item_id="b", text="둘째")])

    got = {i.item_id: i.text for i in store.load()}
    assert got == {"a": "첫째", "b": "둘째"}
