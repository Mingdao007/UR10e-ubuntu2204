#!/usr/bin/env python3
"""Summarize retained sensor captures without opening any device."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np
from contact_benchmark_protocol import (
    FRESH_AGE_S,
    FRESHNESS_SENSITIVITY_CUTOFFS_S,
    STALE_AGE_S,
    classify_sensor_age,
)


def _gap_summary(gaps):
    bands = {"fresh": [], "held": [], "stale": []}
    for gap in gaps:
        bands[classify_sensor_age(float(gap))].append(float(gap))
    elapsed = float(np.sum(gaps))
    result = {}
    for name, values in bands.items():
        duration = float(np.sum(values)) if values else 0.0
        result[name] = {
            "count": len(values),
            "duration_s": duration,
            "fraction_of_gap_duration": duration / elapsed if elapsed else 0.0,
        }
    return result, elapsed


def _cutoff_summary(gaps):
    cutoffs = tuple(FRESHNESS_SENSITIVITY_CUTOFFS_S)
    edges = (0.0,) + cutoffs + (float("inf"),)
    names = ("lt_20ms", "20_to_40ms", "40_to_60ms", "60_to_80ms", "ge_80ms")
    result = {}
    for index, name in enumerate(names):
        lower, upper = edges[index], edges[index + 1]
        values = [float(gap) for gap in gaps if lower <= gap < upper]
        result[name] = {
            "count": len(values),
            "duration_s": float(np.sum(values)) if values else 0.0,
        }
    return result


def analyze(directory):
    directory=Path(directory)
    with (directory/'data.csv').open() as stream:
        rows=list(csv.DictReader(stream))
    if len(rows)<2:
        raise ValueError('at least two captured frames required')
    times=np.array([float(r['t_monotonic_s']) for r in rows])
    gaps=np.diff(times)
    if not np.isfinite(times).all() or np.any(gaps<=0):
        raise ValueError('invalid retained host frame timestamps')
    columns=('Fx_N','Fy_N','Fz_N','Mx_Nm','My_Nm','Mz_Nm')
    wrench=np.array([[float(r[c]) for c in columns] for r in rows])
    if not np.isfinite(wrench).all():raise ValueError('nonfinite retained wrench')
    metadata=json.loads((directory/'metadata.json').read_text())
    summary=json.loads((directory/'summary.json').read_text())
    gap_bands, gap_duration_s = _gap_summary(gaps)
    # These captures contain host receive timestamps only.  If a future
    # capture carries a sensor_observed_at_s column, report true observation
    # age separately; never infer sensor age from a host delivery gap.
    age_column = next((name for name in ('sensor_observed_at_s', 'observation_age_s')
                       if name in rows[0]), None)
    observation_age = None
    observation_age_source = 'host_delivery_gap_only'
    if age_column == 'sensor_observed_at_s':
        observed = np.array([float(r[age_column]) for r in rows])
        observation_age = times - observed
        if not np.isfinite(observation_age).all() or np.any(observation_age < 0):
            raise ValueError('invalid sensor observation timestamps')
        observation_age_source = 'sensor_observed_at_s'
    elif age_column == 'observation_age_s':
        observation_age = np.array([float(r[age_column]) for r in rows])
        if not np.isfinite(observation_age).all() or np.any(observation_age < 0):
            raise ValueError('invalid observation ages')
        observation_age_source = 'observation_age_s'
    age_bands = None
    age_summary = None
    if observation_age is not None:
        age_bands = {name: int(np.count_nonzero(
            [classify_sensor_age(value) == name for value in observation_age]))
                     for name in ('fresh', 'held', 'stale')}
        age_summary = {str(p): float(np.percentile(observation_age, p) * 1000)
                       for p in (50, 95, 99, 100)}
    max_gap = float(np.max(gaps))
    return {
        'source_directory':str(directory.resolve()),
        'raw_frames_sha256':hashlib.sha256((directory/'raw_frames.bin').read_bytes()).hexdigest(),
        'csv_sha256':hashlib.sha256((directory/'data.csv').read_bytes()).hexdigest(),
        'samples':len(rows),'elapsed_first_last_s':float(times[-1]-times[0]),
        'average_frame_rate_hz':float((len(rows)-1)/(times[-1]-times[0])),
        'host_frame_gap_ms':{str(p):float(np.percentile(gaps,p)*1000) for p in (50,90,95,99,100)},
        'gaps_over_20ms':int(np.count_nonzero(gaps>=FRESH_AGE_S)),
        'gaps_at_or_over_80ms':int(np.count_nonzero(gaps>=STALE_AGE_S)),
        'time_over_20ms_sample_age_s':float(np.maximum(gaps-FRESH_AGE_S,0).sum()),
        'fresh_gap_diagnostic_pass':bool(max_gap < FRESH_AGE_S),
        # Kept for consumers of the previous report; it is now explicitly a
        # diagnostic and must not be read as controller acceptance.
        'continuous_20ms_delivery_pass':bool(max_gap < FRESH_AGE_S),
        'delivery_gap_bands':gap_bands,
        'delivery_gap_cutoff_bands':_cutoff_summary(gaps),
        'delivery_gap_duration_s':gap_duration_s,
        'held_fraction':gap_bands['held']['duration_s'] / gap_duration_s if gap_duration_s else 0.0,
        'longest_hold_s':max((gap for gap in gaps if FRESH_AGE_S <= gap < STALE_AGE_S), default=0.0),
        'stale_stop_count':gap_bands['stale']['count'],
        'geometric_latency_reject_count':0,
        'observation_age_source':observation_age_source,
        'observation_age_ms':age_summary,
        'observation_age_bands':age_bands,
        'freshness_policy':{
            'fresh_age_s':FRESH_AGE_S,
            'stale_age_s':STALE_AGE_S,
            'held_policy':'latest_value_zero_order_hold_no_interpolation',
            'stale_policy':'fail_closed_stop_zero_and_censor_trial',
        },
        'raw_wrench_mean_si':dict(zip(columns,wrench.mean(axis=0).tolist())),
        'raw_wrench_std_si':dict(zip(columns,wrench.std(axis=0,ddof=1).tolist())),
        'tcp_quickack':metadata['args'].get('tcp_quickack',False),
        'parse_errors':summary['parse_errors'],'dropped_sync_bytes':summary['dropped_sync_bytes'],
        'claim_scope':'host parser timestamps and delivery gaps only unless observation_age_source says otherwise; not sensor-internal sampling timestamps or packet capture; raw values include bias/gravity; no controller comparison',
        'sensor_stop_claim':'logger sent the existing STOP command and closed; sensor conversion stop not independently proven',
    }


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('capture',type=Path,nargs='+');p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    result={'schema':'contact-static-delivery-v2','captures':[analyze(d) for d in args.capture],
            'motion_dispatched':False,'live_contact_qualified':False}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps(result,indent=2,allow_nan=False))

if __name__=='__main__':main()
