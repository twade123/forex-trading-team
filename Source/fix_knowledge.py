"""Remove polluted live_performance data from knowledge.json files.

The learning pipeline ran 31 audits with incomplete data (0% scout accuracy,
0/31 thesis correct, 0/31 validator correct). This polluted the live_performance
sections with bad win rates and default entry_timing/exit_quality scores.

This script removes the live_performance section entirely so the learning
pipeline can rebuild it cleanly when trades have proper audit data.
"""
import json, glob, os

data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Data')
files = glob.glob(os.path.join(data_dir, '*/knowledge.json'))

cleaned = 0
for f in sorted(files):
    pair = os.path.basename(os.path.dirname(f))
    with open(f, 'r') as fh:
        knowledge = json.load(fh)

    if 'live_performance' in knowledge:
        lp = knowledge['live_performance']
        setups = list(lp.keys())
        if setups:
            print(f"{pair}: removing live_performance for setups: {setups}")
            for s in setups:
                perf = lp[s]
                print(f"  {s}: trades={perf.get('trades',0)} wins={perf.get('wins',0)} "
                      f"wr={perf.get('win_rate','?')} "
                      f"entry_timing={perf.get('avg_entry_timing','?')} "
                      f"exit_quality={perf.get('avg_exit_quality','?')}")
            del knowledge['live_performance']
            with open(f, 'w') as fh:
                json.dump(knowledge, fh, indent=2, default=str)
            cleaned += 1
        else:
            print(f"{pair}: live_performance empty, skipping")
    else:
        print(f"{pair}: no live_performance section")

print(f"\nCleaned {cleaned} knowledge.json files")
