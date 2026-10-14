"""版本账本测试（B6）：回合边界切换、租约钉版本、兼容判定、回收。"""

import pytest

from app.harness.composer import GraphVersion
from app.harness.versions import VersionRegistry

pytestmark = pytest.mark.anyio


def make_version(version_id: int, nodes=('a', 'b')) -> GraphVersion:
    return GraphVersion(
        id=version_id,
        structure_hash=f'hash-{version_id}',
        spec={'nodes': list(nodes)},
        impls={name: (lambda state: {}) for name in nodes},
        graph=None,
    )


async def test_swap_happens_at_lease_boundary():
    registry = VersionRegistry()
    registry.stage(make_version(1))
    async with registry.lease('t1') as version:
        assert version.id == 1

    registry.stage(make_version(2))
    assert registry.current.id == 1  # 还没到回合边界：运行图不变
    assert registry.pending.id == 2

    async with registry.lease('t1') as version:
        assert version.id == 2
    assert registry.pending is None


async def test_inflight_lease_pins_its_version():
    registry = VersionRegistry()
    registry.stage(make_version(1))
    async with registry.lease('t1') as first:
        registry.stage(make_version(2))
        async with registry.lease('t2') as second:
            assert second.id == 2
        assert first.id == 1  # 进行中的回合钉在旧版本上
        assert registry.lease_count(1) == 1
    assert registry.lease_count(1) == 0


async def test_lease_without_any_version_raises():
    registry = VersionRegistry()
    with pytest.raises(RuntimeError):
        async with registry.lease('t1'):
            pass


async def test_missing_nodes_is_the_resume_check():
    registry = VersionRegistry()
    registry.stage(make_version(1, nodes=('agent', 'tools')))
    registry.apply_pending()
    assert registry.missing_nodes(['tools']) == []
    assert registry.missing_nodes(['tools', 'gone']) == ['gone']


async def test_recycle_keeps_newest_and_spares_leases():
    registry = VersionRegistry(keep=1)
    # 第 1 版上有一个"进行中的回合"：它的租约一直握着
    registry.stage(make_version(1))
    in_flight = registry.lease('t1')
    assert (await in_flight.__aenter__()).id == 1

    for version_id in (2, 3):
        registry.stage(make_version(version_id))
        async with registry.lease('t2'):
            pass
    assert [version.id for version in registry.known()] == [1, 2, 3]

    # keep=1：只保最新；2 被回收，1 有在飞租约、3 是 current，都留着
    assert registry.recycle() == [2]
    assert [version.id for version in registry.known()] == [1, 3]

    await in_flight.__aexit__(None, None, None)
    assert registry.recycle() == [1]  # 租约退出后 1 也没人用了


async def test_recycle_spares_protected_by_predicate():
    registry = VersionRegistry(keep=1)
    for version_id in (1, 2, 3):
        registry.stage(make_version(version_id))
        async with registry.lease(f't{version_id}'):
            pass

    # 会话 t1 的版本 1 还压着待确认中断 → protect 说不能丢
    dropped = registry.recycle(protect=lambda threads: 't1' in threads)
    assert dropped == [2]
    assert 1 in [version.id for version in registry.known()]
    assert 3 in [version.id for version in registry.known()]
