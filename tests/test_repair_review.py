import asyncio
import json
import unittest
from unittest.mock import patch

import test_pagination_repair as fixtures
from test_pagination_repair import Capture, simple, edge, page
from src import pagination_diagnostics as d


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    collect = fixtures.RepairTests.collect

    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=asyncio.SelectorEventLoop)

    async def test_exact_repeat_counts_include_prefix_window_and_tail(self):
        # Four shared IDs: prefix 2, window 3/5, tail 6. Old formula gave 3.
        for entries, expected in (([edge(2,8),edge(3,10),edge(4,11),edge(5,12),edge(6,13)],4),
                                  ([edge(2,8),edge(4,11),edge(6,13),edge(6,13)],3)):
            session = simple()
            session.routes[('offset',5)] = page(entries,'B')
            baseline = await self.collect(session)
            session = simple(); session.routes[('offset',5)] = page(entries,'B')
            journal = Capture(); token = d.CURRENT.set(journal)
            try:
                rows = await self.collect(session)
            finally:
                d.CURRENT.reset(token)
            event = next(r for r in journal.records if r['event']=='repair_window_verified')
            self.assertEqual(expected,event['repeated_ids'])
            self.assertGreaterEqual(event['repeated_ids'],0)
            self.assertEqual((1,3),(event['added_ids'],event['merged_ids']))
            self.assertEqual(baseline,rows)
            self.assertEqual(4,len(session.calls))

    async def test_no_repeats(self):
        # Disjoint merge inputs have zero repeats; valid pilot coverage normally
        # requires shared prefix/tail IDs, so test the union boundary directly.
        from src.twitch_api.pagination_repair import Record, ordered_union
        sequences = [[Record.from_edge(edge(n,n),'main',1,0)] for n in (1,2,3)]
        merged = ordered_union(sequences)
        self.assertEqual(0,sum(map(len,sequences))-len(merged))

    async def test_hashes_change_with_internal_composition_and_order(self):
        journal = Capture()
        sets = [[edge(n,n) for n in numbers] for numbers in ((1,2,3,4),(1,5,3,4),(1,3,2,4))]
        for entries in sets:
            journal.repair_page('main',2,3,entries,'private-in','private-out',True)
        records = journal.records
        self.assertEqual(3,len({r['ids_sha256'] for r in records}))
        for entries,record in zip(sets,records):
            self.assertEqual(d.ids_hash(entries),record['ids_sha256'])
            self.assertEqual(d._cursor_hash('private-in'),record['input_cursor_sha256'])
            self.assertEqual(d._cursor_hash('private-out'),record['output_cursor_sha256'])
        text = json.dumps(records)
        for private in ('private-in','private-out','synthetic viewer','m1','Client-Id'):
            self.assertNotIn(private,text)

    async def test_branch_attribution_and_no_hash_work_without_active_journal(self):
        baseline = await self.collect(simple())
        with patch.object(d,'ids_hash',side_effect=AssertionError('unexpected hash')), patch.object(d,'_cursor_hash',side_effect=AssertionError('unexpected cursor hash')):
            self.assertEqual(baseline,await self.collect(simple()))
            stopped = Capture(); stopped.stopped=True
            token=d.CURRENT.set(stopped)
            try:
                self.assertEqual(baseline,await self.collect(simple()))
            finally:
                d.CURRENT.reset(token)
        journal=Capture(); token=d.CURRENT.set(journal); session=simple()
        try:
            self.assertEqual(baseline,await self.collect(session))
        finally:
            d.CURRENT.reset(token)
        records=[r for r in journal.records if r['event']=='repair_page_received']
        self.assertEqual([('main',1),('main',2),('control',1),('main',3)],[(r['branch'],r['page']) for r in records])
        self.assertEqual([None,d._cursor_hash('P'),None,d._cursor_hash('C1')],[r['input_cursor_sha256'] for r in records])
        self.assertEqual(4,len(session.calls))
