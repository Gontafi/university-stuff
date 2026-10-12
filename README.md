# Assignment 4. Security, resilience and intrusion tolerance

Synthetic ransomware-style incident on a university research server. All data are fictional.

```sh
python3 scripts/analyze.py          # Parts A, B, E: stage mapping, risk matrix, anomaly rate
python3 scripts/recovery_drill.py   # Part D: immutable snapshots, simulated encryption, restore, SHA-256 check
```

`data/` holds the incident dataset and the anomaly scores from the task. `results/` holds the
generated tables (`analysis.txt`, `analysis.json`) and the drill log (`recovery_drill.log`,
`recovery_drill.json`). The drill works only on generated files in a temporary directory.
