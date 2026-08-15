"""Bounded LightGBM hyperparameter search on Modal CPU workers.

The target is the observed speed multiplier implied by edge length, free-flow
speed, and current traversal cost. A deterministic synthetic dataset is used
only when no Supabase database is configured.

Run explicitly with::

    modal run scripts/tune_hyperparams.py

Set ``CROSSFLOW_TUNING_TRIALS`` to 1-24 to change the default 12-trial sample.
"""

import itertools
import os
import random
from typing import Any, Dict, Tuple

import modal


MAX_TRIALS = 24
ROAD_TYPE_CODES = {
    "motorway": 0,
    "trunk": 1,
    "primary": 2,
    "secondary": 3,
    "tertiary": 4,
}

tuning_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "lightgbm==4.6.0",
        "scikit-learn==1.9.0",
        "numpy==2.5.1",
        "pandas==3.0.5",
        "psycopg2-binary==2.9.12",
    )
)

app = modal.App("crossflow-hyperparameter-tuning", image=tuning_image)


@app.function(
    image=tuning_image,
    timeout=900,
    max_containers=4,
)
def evaluate_config(item: Tuple[int, Dict[str, Any], str]) -> Dict[str, Any]:
    """Evaluate one configuration without allocating an unnecessary GPU."""
    trial_id, params, supabase_db_url = item

    import lightgbm as lgb
    import numpy as np
    import pandas as pd
    import psycopg2
    from sklearn.metrics import mean_squared_error
    from sklearn.model_selection import train_test_split

    print(f"[trial {trial_id}] params={params}")

    df = None
    if supabase_db_url:
        try:
            conn = psycopg2.connect(supabase_db_url, connect_timeout=5)
            try:
                df = pd.read_sql(
                    """
                    SELECT length_m, max_speed_kmh, road_type, cost_s
                    FROM public.road_network
                    LIMIT 50000
                    """,
                    conn,
                )
            finally:
                conn.close()
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(
                f"Configured Supabase database is unavailable ({type(err).__name__})."
            ) from err

    if df is None or df.empty:
        # Every trial must see the same dataset; changing the random seed per
        # trial makes hyperparameter scores incomparable.
        rng = np.random.default_rng(42)
        sample_count = 5000
        road_types = rng.choice(list(ROAD_TYPE_CODES), sample_count)
        lengths = rng.uniform(200, 5000, sample_count)
        speeds = rng.choice([30, 40, 50, 60, 80, 100], sample_count)
        road_penalty = pd.Series(road_types).map({
            "motorway": 0.02,
            "trunk": 0.05,
            "primary": 0.10,
            "secondary": 0.15,
            "tertiary": 0.20,
        }).to_numpy()
        speed_multiplier = np.clip(
            0.98
            - road_penalty
            - 0.08 * np.clip(lengths / 5000.0, 0.0, 1.0)
            + rng.normal(0.0, 0.04, sample_count),
            0.2,
            1.0,
        )
        free_flow_cost = lengths / (speeds / 3.6)
        df = pd.DataFrame({
            "length_m": lengths,
            "max_speed_kmh": speeds,
            "road_type": road_types,
            "cost_s": free_flow_cost / speed_multiplier,
        })

    for column in ("length_m", "max_speed_kmh", "cost_s"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["road_type_code"] = (
        df["road_type"].astype(str).str.lower().map(ROAD_TYPE_CODES).fillna(5).astype(int)
    )
    valid = (
        np.isfinite(df["length_m"])
        & np.isfinite(df["max_speed_kmh"])
        & np.isfinite(df["cost_s"])
        & (df["length_m"] > 0)
        & (df["max_speed_kmh"] > 0)
        & (df["cost_s"] > 0)
    )
    df = df.loc[valid].copy()
    if len(df) < 100:
        raise ValueError("At least 100 valid road-edge observations are required for tuning.")

    free_flow_cost = df["length_m"] / (df["max_speed_kmh"] / 3.6)
    y = (free_flow_cost / df["cost_s"]).clip(0.2, 1.0)
    X = df[["length_m", "max_speed_kmh", "road_type_code"]]
    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=0.2, random_state=42,
    )

    model = lgb.LGBMRegressor(
        **params,
        random_state=42,
        n_jobs=1,
        subsample_freq=1,
        verbosity=-1,
    )
    model.fit(X_train, y_train)
    predictions = model.predict(X_val)
    val_rmse = float(np.sqrt(mean_squared_error(y_val, predictions)))

    print(f"[trial {trial_id}] validation RMSE={val_rmse:.5f}")
    return {"trial_id": trial_id, "params": params, "val_rmse": val_rmse}


@app.local_entrypoint()
def main():
    supabase_db_url = os.environ.get("SUPABASE_DB_URL", "")
    search_space = {
        "learning_rate": [0.02, 0.05, 0.1],
        "num_leaves": [15, 31, 63],
        "max_depth": [-1, 8],
        "n_estimators": [200, 500],
        "subsample": [0.8, 1.0],
    }

    keys, values = zip(*search_space.items())
    combinations = [dict(zip(keys, values_)) for values_ in itertools.product(*values)]
    random.Random(42).shuffle(combinations)
    try:
        requested_trials = int(os.environ.get("CROSSFLOW_TUNING_TRIALS", "12"))
    except ValueError as err:
        raise ValueError("CROSSFLOW_TUNING_TRIALS must be an integer from 1 to 24.") from err
    trial_count = max(1, min(MAX_TRIALS, requested_trials))
    selected = combinations[:trial_count]

    print(
        f"Running {trial_count} bounded CPU trials "
        f"(sampled from {len(combinations)} configurations; max {MAX_TRIALS})."
    )
    work_items = [
        (index, params, supabase_db_url)
        for index, params in enumerate(selected, start=1)
    ]
    results = list(evaluate_config.map(work_items))
    best_trial = min(results, key=lambda result: result["val_rmse"])

    print("Hyperparameter tuning complete")
    print(f"Best trial: {best_trial['trial_id']}")
    print(f"Validation RMSE: {best_trial['val_rmse']:.5f}")
    print("Parameters:")
    for name, value in best_trial["params"].items():
        print(f"  {name}: {value}")
