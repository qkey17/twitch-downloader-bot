import asyncio
import unittest
import test_pagination_repair as f
from src import pagination_diagnostics as d
from src.twitch_api import pagination_repair as repair


class RightBoundaryTests(unittest.IsolatedAsyncioTestCase):
    collect = f.RepairTests.collect
    failure = f.RepairTests.failure
    run_worker = f.RepairTests.run_worker

    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)

    async def captured(self, session, env):
        journal=f.Capture();token=d.CURRENT.set(journal)
        try:rows=await self.collect(session,env=env)
        finally:d.CURRENT.reset(token)
        event=next(e for e in journal.records if e['event']=='repair_window_verified')
        return rows,event

    async def test_second_live_geometry_62_ids_control_and_main_cross_end(self):
        parent=[f.edge(100+n,20343+(n*125)//61,n) for n in range(62)]
        ordered=sorted([*parent,f.edge(999,20400)],key=lambda e:(e['node']['contentOffsetSeconds'],f.datetime.fromisoformat(e['node']['createdAt'])))
        session=f.Session({('offset',0):f.page([f.edge(1,0),f.edge(2,20342)],'P'),
                           ('cursor','P'):f.page(parent,'C1'),
                           ('offset',20330):f.page([f.edge(2,20342),*ordered,f.edge(3,20469)],'B1'),
                           ('cursor','B1'):f.page([f.edge(4,20470.8),f.edge(5,20472)],'B2'),
                           ('cursor','C1'):f.page([f.edge(3,20469),f.edge(4,20470.8)],'C1b'),
                           ('cursor','C1b'):f.page([f.edge(5,20472),f.edge(6,20500)])})
        rows,event=await self.captured(session,{**f.ENV,'TCD_PAGINATION_REPAIR_START':'20343',
                                              'TCD_PAGINATION_REPAIR_END':'20470',
                                              'TCD_PAGINATION_REPAIR_CONTROL_OFFSET':'20330'})
        self.assertEqual((62,63,1),(event['original_ids'],event['merged_ids'],event['added_ids']))
        self.assertEqual((20468,20470),(event['repair_high'],event['verification_high']))
        self.assertGreater(event['main_last_second'],20470)
        self.assertGreater(event['control_last_second'],20470)
        self.assertEqual((3,3),(event['tail_ids'],event['tail_confirmed_ids']))
        self.assertEqual(69,len(rows))
        self.assertEqual(1,sum(r[3]=='m999' for r in rows))
        self.assertEqual(len(rows),len({r[3] for r in rows}))
        self.assertEqual(sorted(r[0] for r in rows),[r[0] for r in rows])
        self.assertEqual([('offset',0),('cursor','P'),('offset',20330),('cursor','B1'),('cursor','C1'),('cursor','C1b')],[k for k,_ in session.calls])

    async def test_control_passes_h_but_terminal_before_v_refuses(self):
        session=f.simple()
        session.routes[('offset',5)]=f.control([f.edge(4,11),f.edge(6,13)],None)
        await self.failure(session,'control_no_right_barrier',env={**f.ENV,'TCD_PAGINATION_REPAIR_END':'30'})

    async def test_control_v_uses_same_four_attempt_budget(self):
        session=f.simple()
        session.routes[('offset',5)]=f.control([f.edge(6,13)],'B1')
        for n,offset in ((1,14),(2,15),(3,15.9)):
            session.routes[('cursor',f'B{n}')]=f.page([f.edge(10+n,offset)],f'B{n+1}')
        await self.failure(session,'request_limit',env={**f.ENV,'TCD_PAGINATION_REPAIR_END':'15'})
        self.assertEqual(6,len(session.calls))
        self.assertNotIn(('cursor','B4'),[k for k,_ in session.calls])

    async def test_main_must_cross_v_within_two_pages(self):
        session=f.simple()
        session.routes[('offset',5)]=f.control([f.edge(4,11),f.edge(90,16)],'B')
        session.routes[('cursor','C1')]=f.page([f.edge(6,13)],'C1b')
        session.routes[('cursor','C1b')]=f.page([f.edge(7,14)],'C1c')
        await self.failure(session,'main_barrier_or_tail_limit',env={**f.ENV,'TCD_PAGINATION_REPAIR_END':'15'})
        self.assertNotIn(('cursor','C1c'),[k for k,_ in session.calls])

    async def test_unknown_id_between_h_and_end_is_never_repaired(self):
        session=f.simple()
        session.routes[('offset',5)]=f.control([f.edge(4,11),f.edge(90,13),f.edge(6,16)],'B')
        session.routes[('cursor','C1')]=f.page([f.edge(6,16),f.edge(7,20)])
        await self.failure(session,'control_tail_missing',env={**f.ENV,'TCD_PAGINATION_REPAIR_END':'15'})

    async def test_h_group_is_retained_and_deduplicated_before_v(self):
        session=f.simple()
        session.routes[('offset',5)]=f.control([f.edge(4,11),f.edge(6,16)],'B')
        session.routes[('cursor','C1')]=f.page([f.edge(5,12),f.edge(9,12,1)],'C1b')
        session.routes[('cursor','C1b')]=f.page([f.edge(6,16)])
        rows,event=await self.captured(session,{**f.ENV,'TCD_PAGINATION_REPAIR_END':'15'})
        self.assertEqual(['m1','m2','m3','m4','m5','m9','m6'],[r[3] for r in rows])
        self.assertEqual(12,event['repair_high'])
        self.assertEqual(15,event['verification_high'])

    async def test_fractional_end_requires_crossing_whole_second(self):
        for passes in (False,True):
            session=f.simple()
            session.routes[('offset',5)]=f.control([f.edge(4,11),f.edge(6,13.9)],'B1' if passes else None)
            session.routes[('cursor','B1')]=f.page([f.edge(7,14)])
            session.routes[('cursor','C1')]=f.page([f.edge(6,13.9),f.edge(7,14)])
            env={**f.ENV,'TCD_PAGINATION_REPAIR_END':'13.5'}
            if passes:
                rows,event=await self.captured(session,env)
                self.assertEqual(13,event['verification_high'])
                self.assertEqual(14,event['main_last_second'])
                self.assertEqual(14,event['control_last_second'])
            else:await self.failure(session,'control_no_right_barrier',env=env)

    async def test_end_equal_h_retains_previous_semantics(self):
        rows,event=await self.captured(f.simple(),f.ENV)
        self.assertEqual((12,12),(event['repair_high'],event['verification_high']))
        self.assertEqual(8,len(rows))

    async def test_v_failure_never_publishes_and_cleans_spool(self):
        session=f.simple()
        session.routes[('cursor','P')]=f.page([f.edge(3,10),f.edge(5,11)],'C1')
        session.routes[('offset',5)]=f.control([f.edge(4,10.5),f.edge(5,11),f.edge(6,13)],'B')
        session.routes[('cursor','C1')]=f.page([f.edge(7,11.9)],'C1b')
        session.routes[('cursor','C1b')]=f.page([f.edge(8,12)],'C1c')
        with f.tempfile.TemporaryDirectory() as root:
            old=f.Path(root)/'old.html';old.write_text('previous generation')
            with self.assertRaisesRegex(repair.RepairError,'main_barrier_or_tail_limit'):
                await self.run_worker(session,root)
            self.last_build.assert_not_awaited()
            self.assertEqual('previous generation',old.read_text())
            self.assertEqual([],list((f.Path(root)/'spool').iterdir()))
