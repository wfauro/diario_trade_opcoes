import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.tmp.name) / 'test.sqlite3'
        app.DB_PATH.parent.mkdir(exist_ok=True)
        app.init_db()
        self.op = app.create_operation({
            'start_date':'2026-01-05','underlying':'TEST3','strategy':'Spread de calls',
            'capital_base':100,'legs':[
                {'code':'TESTA','option_type':'CALL','strike':10,'expiry':'2026-02-20','side':'LONG','quantity':200,'entry_premium':1,'multiplier':1,'entry_cost':10},
                {'code':'TESTB','option_type':'CALL','strike':12,'expiry':'2026-02-20','side':'SHORT','quantity':200,'entry_premium':0.5,'multiplier':1,'entry_cost':10}
            ]})
    def tearDown(self): self.tmp.cleanup()
    def test_count_uses_smallest_leg(self):
        self.assertEqual(self.op['initial_structures'],200)
        self.assertEqual(self.op['structure_count'],200)
        self.assertEqual(sum(l['quantity'] for l in self.op['legs']),400)
        self.assertEqual(app.dashboard()['open_structures'],200)
    def test_partial_close_costs_and_remaining(self):
        leg=self.op['legs'][0]
        op=app.add_closeout({'leg_id':leg['id'],'close_date':'2026-01-06','quantity':50,'premium':1.2,'costs':0.5})
        self.assertEqual(op['status'],'PARCIAL')
        self.assertEqual(op['structure_count'],150)
        self.assertAlmostEqual(op['realized'],7.0,places=2)
        self.assertEqual(op['closed'],False)
        self.assertEqual(op['closed_structure_count'],0)
        self.assertEqual(app.dashboard()['open_structures'],150)
    def test_cannot_close_more_than_remaining(self):
        leg=self.op['legs'][0]
        app.add_closeout({'leg_id':leg['id'],'close_date':'2026-01-06','quantity':150,'premium':1.1,'costs':0})
        with self.assertRaises(ValueError):
            app.add_closeout({'leg_id':leg['id'],'close_date':'2026-01-07','quantity':51,'premium':1.1,'costs':0})
    def test_closed_and_profit_summary(self):
        legs=self.op['legs']
        app.add_closeout({'leg_id':legs[0]['id'],'close_date':'2026-01-06','quantity':200,'premium':1.2,'costs':1})
        op=app.add_closeout({'leg_id':legs[1]['id'],'close_date':'2026-01-06','quantity':200,'premium':0.3,'costs':1})
        self.assertTrue(op['closed'])
        self.assertEqual(op['status'],'ENCERRADA')
        self.assertEqual(op['structure_count'],0)
        d=app.dashboard()
        self.assertEqual(d['closed_count'],1)
        self.assertEqual(d['closed_structures'],200)
        self.assertEqual(d['open_structures'],0)
        self.assertEqual(d['groups'][0]['structures'],200)
        self.assertEqual(d['groups'][0]['closed_structures'],200)
        self.assertEqual(d['win_count'],1)
        self.assertEqual(d['win_rate'],100)
        self.assertAlmostEqual(d['realized_total'],58.0,places=2)
        self.assertAlmostEqual(op['return_pct'],58.0,places=2)
        self.assertAlmostEqual(d['financial_return_pct'],58.0,places=2)
    def test_add_roll_leg_preserves_initial_count_and_history(self):
        legs=self.op['legs']
        app.add_closeout({'leg_id':legs[0]['id'],'close_date':'2026-01-06','quantity':200,'premium':1.2,'costs':0})
        op=app.add_closeout({'leg_id':legs[1]['id'],'close_date':'2026-01-06','quantity':200,'premium':0.3,'costs':0})
        self.assertTrue(op['closed'])
        reopened=app.add_leg({'operation_id':op['id'],'entry_date':'2026-01-10','code':'TESTC','option_type':'CALL','strike':11,'expiry':'2026-03-20','side':'LONG','quantity':150,'entry_premium':0.4,'multiplier':1,'entry_cost':2})
        self.assertEqual(reopened['status'],'PARCIAL')
        self.assertEqual(reopened['initial_structures'],200)
        self.assertEqual(reopened['structure_count'],150)
        self.assertEqual(reopened['legs'][-1]['entry_date'],'2026-01-10')
        self.assertAlmostEqual(reopened['realized'],60.0,places=2)

    def test_update_capital_base_recalculates_return(self):
        legs=self.op['legs']
        app.add_closeout({'leg_id':legs[0]['id'],'close_date':'2026-01-06','quantity':200,'premium':1.2,'costs':1})
        op=app.add_closeout({'leg_id':legs[1]['id'],'close_date':'2026-01-06','quantity':200,'premium':0.3,'costs':1})
        op=app.update_capital_base({'operation_id':op['id'],'capital_base':200})
        self.assertAlmostEqual(op['return_pct'],29.0,places=2)
        self.assertAlmostEqual(app.dashboard()['financial_return_pct'],29.0,places=2)

    def test_reference_is_separate(self):
        legs=self.op['legs']
        app.update_mark({'leg_id':legs[0]['id'],'reference_premium':1.25,'reference_date':'2026-01-06'})
        op=app.update_mark({'leg_id':legs[1]['id'],'reference_premium':0.4,'reference_date':'2026-01-06'})
        self.assertIsNotNone(op['unrealized'])
        self.assertEqual(op['realized'],0)
        self.assertAlmostEqual(op['unrealized'],50.0,places=2)
    
    def test_structure_unit_value_arredonda_meio_para_cima(self):
        legs = [
            {
                "side": "LONG",
                "entry_premium": "1.0000005",
                "quantity": 2,
            }
        ]

        result = app.structure_unit_value(legs)

        self.assertEqual(result["quantity"], 2)
        self.assertEqual(result["net_premium"], 2.000001)
        self.assertEqual(result["unit_value"], 1.000001)

if __name__=='__main__': unittest.main()
