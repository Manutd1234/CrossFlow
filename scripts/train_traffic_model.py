"""Train and periodically apply a road-edge speed model with Modal.

This is a CPU LightGBM workload. It derives its target from observed traversal
cost rather than fitting random labels, and a synthetic demo artifact is kept
separate so it can never be applied to a production road table.
"""

import os
from pathlib import Path

import modal


ROAD_TYPE_CODES = {
    "motorway": 0,
    "trunk": 1,
    "primary": 2,
    "secondary": 3,
    "tertiary": 4,
}

training_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "lightgbm==4.6.0",
        "scikit-learn==1.9.0",
        "numpy==2.5.1",
        "pandas==3.0.5",
        "psycopg2-binary==2.9.12",
        "joblib==1.5.3",
    )
)

volume = modal.Volume.from_name("traffic-models", create_if_missing=True)
VOLUME_PATH = Path("/vol")

app = modal.App("traffic-model-trainer", image=training_image)


@app.function(
    timeout=3600,
    volumes={VOLUME_PATH: volume},
)
def train_traffic_predictor(supabase_db_url: str = ""):
    """Fit a CPU model to the observed/free-flow speed ratio."""
    import joblib
    import lightgbm as lgb
    import numpy as np
    import pandas as pd
    import psycopg2
    from sklearn.metrics import mean_squared_error, r2_score
    from sklearn.model_selection import train_test_split

    configured_url = supabase_db_url or os.environ.get("SUPABASE_DB_URL", "")
    dataset_source = "supabase"

    if configured_url:
        print("Connecting to the configured Supabase PostGIS database.")
        try:
            conn = psycopg2.connect(configured_url, connect_timeout=5)
            try:
                df = pd.read_sql(
                    """
                    SELECT id AS edge_id, length_m, max_speed_kmh, road_type,
                           cost_s AS current_cost_s
                    FROM public.road_network
                    """,
                    conn,
                )
            finally:
                conn.close()
        except Exception as err:  # noqa: BLE001
            # A bad production connection must not silently create and publish
            # a synthetic model that could later overwrite real edge costs.
            raise RuntimeError(
                f"Configured Supabase database is unavailable ({type(err).__name__})."
            ) from err
        if df.empty:
            raise ValueError("Supabase road_network contains no training rows.")
    else:
        dataset_source = "synthetic_demo"
        print("No SUPABASE_DB_URL configured; creating a synthetic demo dataset.")
        rng = np.random.default_rng(42)
        sample_count = 5000
        road_types = rng.choice(list(ROAD_TYPE_CODES), sample_count)
        lengths = rng.uniform(100, 8000, sample_count)
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
            - 0.10 * np.clip(lengths / 8000.0, 0.0, 1.0)
            + rng.normal(0.0, 0.04, sample_count),
            0.2,
            1.0,
        )
        free_flow_cost = lengths / (speeds / 3.6)
        df = pd.DataFrame({
            "edge_id": [f"edge_{index}" for index in range(sample_count)],
            "length_m": lengths,
            "max_speed_kmh": speeds,
            "road_type": road_types,
            "current_cost_s": free_flow_cost / speed_multiplier,
        })

    for column in ("length_m", "max_speed_kmh", "current_cost_s"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["road_type_code"] = (
        df["road_type"].astype(str).str.lower().map(ROAD_TYPE_CODES).fillna(5).astype(int)
    )
    valid = (
        np.isfinite(df["length_m"])
        & np.isfinite(df["max_speed_kmh"])
        & np.isfinite(df["current_cost_s"])
        & (df["length_m"] > 0)
        & (df["max_speed_kmh"] > 0)
        & (df["current_cost_s"] > 0)
    )
    df = df.loc[valid].copy()
    if len(df) < 100:
        raise ValueError("At least 100 valid road-edge observations are required.")

    free_flow_cost = df["length_m"] / (df["max_speed_kmh"] / 3.6)
    y = (free_flow_cost / df["current_cost_s"]).clip(0.2, 1.0)
    X = df[["length_m", "max_speed_kmh", "road_type_code"]]
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42,
    )

    print(f"Training LightGBM on {len(X_train)} CPU samples from {dataset_source} data.")
    model = lgb.LGBMRegressor(
        n_estimators=500,
        learning_rate=0.05,
        num_leaves=31,
        random_state=42,
        n_jobs=-1,
        verbosity=-1,
    )
    model.fit(X_train, y_train)
    predictions = model.predict(X_test)
    r2 = float(r2_score(y_test, predictions))
    rmse = float(np.sqrt(mean_squared_error(y_test, predictions)))

    # The scheduled synchronizer deliberately ignores demo artifacts.
    artifact_name = (
        "traffic_lightgbm.pkl" if dataset_source == "supabase"
        else "traffic_lightgbm_demo.pkl"
    )
    model_path = VOLUME_PATH / artifact_name
    joblib.dump(model, model_path)
    volume.commit()
    print(f"Saved {dataset_source} model to {model_path} (R²={r2:.4f}, RMSE={rmse:.5f}).")

    return {
        "status": "success",
        "dataset_source": dataset_source,
        "samples": len(df),
        "r2_score": r2,
        "rmse": rmse,
        "saved_path": str(model_path),
    }


@app.function(
    schedule=modal.Cron("0 * * * *"),
    volumes={VOLUME_PATH: volume},
)
def apply_hourly_traffic_weights(supabase_db_url: str = ""):
    """Synchronize the latest production model output to pgRouting costs."""
    import joblib
    import pandas as pd
    import psycopg2

    configured_url = supabase_db_url or os.environ.get("SUPABASE_DB_URL", "")
    if not configured_url:
        print("SUPABASE_DB_URL is not configured in the Modal environment.")
        return {"status": "skipped", "reason": "missing_database_url"}

    model_path = VOLUME_PATH / "traffic_lightgbm.pkl"
    if not model_path.exists():
        print("Production model not found; train against Supabase before synchronizing.")
        return {"status": "skipped", "reason": "missing_production_model"}

    model = joblib.load(model_path)
    conn = psycopg2.connect(configured_url, connect_timeout=5)
    try:
        df = pd.read_sql(
            "SELECT id, length_m, max_speed_kmh, road_type FROM public.road_network",
            conn,
        )
        if df.empty:
            return {"status": "skipped", "reason": "empty_road_network"}

        for column in ("length_m", "max_speed_kmh"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df = df[
            df["length_m"].notna()
            & df["max_speed_kmh"].notna()
            & (df["length_m"] > 0)
            & (df["max_speed_kmh"] > 0)
        ].reset_index(drop=True)
        if df.empty:
            return {"status": "skipped", "reason": "no_valid_road_edges"}

        df["road_type_code"] = (
            df["road_type"].astype(str).str.lower().map(ROAD_TYPE_CODES).fillna(5).astype(int)
        )
        features = df[["length_m", "max_speed_kmh", "road_type_code"]]
        predicted_multipliers = model.predict(features)
        updates = []
        for predicted, row in zip(predicted_multipliers, df.itertuples(index=False)):
            multiplier = min(1.0, max(0.2, float(predicted)))
            actual_speed_ms = row.max_speed_kmh * multiplier / 3.6
            updates.append((row.length_m / actual_speed_ms, row.id))

        with conn.cursor() as cursor:
            cursor.executemany(
                "UPDATE public.road_network SET cost_s = %s WHERE id = %s",
                updates,
            )
        conn.commit()
    finally:
        conn.close()

    print(f"Synchronized {len(updates)} pgRouting edge costs.")
    return {"status": "success", "updated_edges": len(updates)}


@app.local_entrypoint()
def main():
    supabase_db_url = os.environ.get("SUPABASE_DB_URL", "")
    if not supabase_db_url:
        print(
            "SUPABASE_DB_URL is absent: this run creates a demo artifact only; "
            "the scheduled database synchronizer will not use it."
        )
    result = train_traffic_predictor.remote(supabase_db_url)
    print("Training result:", result)
