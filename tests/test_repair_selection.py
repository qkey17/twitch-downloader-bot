import asyncio
import unittest
from unittest.mock import patch
import test_pagination_repair as f
from src import pagination_diagnostics as d
from src.twitch_api import pagination_repair as r


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    collect = f.RepairTests.collect

    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)

    async def test_shift_one_and_two_seconds_preserves_cursor_rows_and_spool(self):
        for start in ('9','8'):
            session=f.simple()
            session.routes[('offset',0)]=f.page([f.edge(1,0),f.edge(2,7)],'P')
            session.routes[('offset',5)]=f.page([f.edge(2,7),f.edge(3,10),f.edge(4,11),f.edge(5,12),f.edge(6,13)],'B')
            journal=f.Capture(); token=d.CURRENT.set(journal)
            try:
                rows=await self.collect(session,env={**f.ENV,'TCD_PAGINATION_REPAIR_START':start})
            finally:d.CURRENT.reset(token)
            self.assertEqual([f'm{i}' for i in range(1,9)],[v[3] for v in rows])
            self.assertEqual(('cursor','C1'),session.calls[-1][0])
            verified=next(e for e in journal.records if e['event']=='repair_window_verified')
            self.assertEqual(10-int(start),verified['start_shift'])
            self.assertEqual(int(start),verified['configured_start'])
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                spool=f.ChatSpool(tmp);spool.open()
                try:
                    spool.append_batch([f.encode_row({'user':v[2],'text':v[3],'t':str(v[0])}) for v in rows])
                    self.assertEqual(8,spool.messages)
                    self.assertEqual([v[3] for v in rows],[v['text'] for v in spool.iter_rows()])
                finally:spool.close()

    async def test_gap_unknown_id_and_late_left_barrier_refuse_before_window(self):
        for entries,reason in (([f.edge(2,8),f.edge(90,9),f.edge(4,11),f.edge(6,13)],'unverified_prefix'),
                               ([f.edge(90,9),f.edge(4,11),f.edge(6,13)],'control_no_left_barrier')):
            session=f.simple();session.routes[('offset',5)]=f.page(entries,'B');rows=[]
            with self.assertRaisesRegex(r.RepairError,reason):
                await self.collect(session,env={**f.ENV,'TCD_PAGINATION_REPAIR_START':'9'},rows=rows)
            self.assertTrue(all(v[0]<9 for v in rows))

    async def test_live_observed_geometry_with_66_synthetic_ids(self):
        parent=[f.edge(100+n,20344+(n*132)//65,n) for n in range(66)]
        controlled=sorted([*parent,f.edge(999,20400)],key=lambda e:(e['node']['contentOffsetSeconds'],f.datetime.fromisoformat(e['node']['createdAt'])))
        session=f.Session({('offset',0):f.page([f.edge(1,0),f.edge(2,20342)],'P'),
                           ('cursor','P'):f.page(parent,'C1'),
                           ('offset',20330):f.page([f.edge(2,20342),*controlled,f.edge(3,20477)],'B'),
                           ('cursor','C1'):f.page([f.edge(3,20477),f.edge(4,20500)])})
        rows=await self.collect(session,env={**f.ENV,'TCD_PAGINATION_REPAIR_START':'20343',
                                          'TCD_PAGINATION_REPAIR_END':'20470',
                                          'TCD_PAGINATION_REPAIR_CONTROL_OFFSET':'20330'})
        self.assertEqual(71,len(rows))
        self.assertEqual(1,sum(v[3]=='m999' for v in rows))
        self.assertEqual(('cursor','C1'),session.calls[-1][0])

    async def test_excess_shift_and_end_before_first_refuse_without_control(self):
        for start,end in (('7','12'),('9','9.5')):
            session=f.simple();session.routes[('offset',0)]=f.page([f.edge(1,0)],'P')
            with self.assertRaisesRegex(r.RepairError,'window_not_one_page'):
                await self.collect(session,env={**f.ENV,'TCD_PAGINATION_REPAIR_START':start,'TCD_PAGINATION_REPAIR_END':end})
            self.assertEqual(2,len(session.calls))

    async def test_already_committed_boundary_second_still_refused(self):
        session=f.simple();session.routes[('offset',0)]=f.page([f.edge(1,9.1)],'P')
        session.routes[('cursor','P')]=f.page([f.edge(3,9.5),f.edge(5,12)],'C1')
        with self.assertRaisesRegex(r.RepairError,'left_boundary_committed'):
            await self.collect(session)
