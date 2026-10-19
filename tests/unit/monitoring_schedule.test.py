import importlib.util
import json
from pathlib import Path
import random
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('notification_schedules', ROOT/'master/notifications.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ScheduleParityTest(unittest.TestCase):
    def test_js_python_dst_overnight_and_invalid_policy_parity(self):
        rng = random.Random(20261018)
        vectors = []
        # Include both sides of US/European/Lord Howe clock transitions, plus
        # random instants. Expected DST boundaries also have explicit JS tests.
        anchors = [1772964000000, 1793523600000, 1774746000000, 1792890000000,
                   1775314800000, 1791041400000]
        for zone in ['UTC', 'America/Los_Angeles', 'Europe/London', 'Australia/Lord_Howe', 'Asia/Kolkata']:
            for mode in ['quiet', 'working']:
                for start, end in [(60, 120), (120, 180), (1320, 480), (540, 1020), (0, 1440)]:
                    schedule = dict(timezone=zone, mode=mode, days=[1, 2, 3, 4, 5, 7], start_minute=start, end_minute=end)
                    for anchor in anchors:
                        for delta in [-3600000, -60000, 0, 60000, 3600000, rng.randrange(-86400000, 86400000)]:
                            vectors.append([schedule, anchor+delta])
        vectors += [[None, 1700000000000], [{}, 1700000000000]]
        bad = dict(timezone='UTC', mode='quiet', days=[1], start_minute=0, end_minute=1440)
        for patch in [dict(days=[1, 1]), dict(timezone='+01:00'), dict(start_minute=True), dict(mode='send'), dict(extra=True)]:
            vectors.append([dict(bad, **patch), 1700000000000])
        script = "import fs from 'node:fs';import {monitoringAllows} from './shared/model/crm.js';const v=JSON.parse(fs.readFileSync(0,'utf8'));console.log(JSON.stringify(v.map(([s,t])=>monitoringAllows(s,t))));"
        result = subprocess.run(['node', '--input-type=module', '-e', script], input=json.dumps(vectors), text=True, capture_output=True, cwd=ROOT, check=True)
        js = json.loads(result.stdout)
        python = [module.monitoring_allows(s, t) for s, t in vectors]
        self.assertEqual(js, python)
        self.assertGreater(len(vectors), 1800)

    def test_repeat_slots_skip_missed_intervals(self):
        self.assertEqual(module.reminder_slot(100000, 60000, 310000), 280000)
        self.assertEqual(module.reminder_slot(100000, None, 310000), 100000)
        self.assertIsNone(module.reminder_slot(100000, 60000, 99999))
        self.assertIsNone(module.reminder_slot(100000, True, 310000))


if __name__ == '__main__':
    unittest.main()
