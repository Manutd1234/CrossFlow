"""Train the optional road-edge speed model locally on CPU."""

import os
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_squared_error, r2_score
from sklearn.model_selection import train_test_split


ROAD_TYPE_CODES = {
    "motorway": 0,
    "trunk": 1,
    "primary": 2,
    "secondary": 3,
    "tertiary": 4,
}


def main():
    print("CrossFlow local CPU traffic-model training")
    db_url = os.environ.get("SUPABASE_DB_URL", "")

    if db_url:
        # Never print even a prefix of a credential-bearing connection URL.
        print("Connecting to the configured Supabase PostGIS database.")
        import psycopg2

        conn = psycopg2.connect(db_url, connect_timeout=5)
        try:
            df = pd.read_sql(
                "SELECT id, length_m, max_speed_kmh, road_type, cost_s FROM public.road_network",
                conn,
            )
        finally:
            conn.close()
        if df.empty:
            raise ValueError("Supabase road_network contains no training rows.")
        cost_column = "cost_s"
        dataset_source = "supabase"
    else:
        print("SUPABASE_DB_URL is absent; generating a deterministic synthetic demo dataset.")
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
            "length_m": lengths,
            "max_speed_kmh": speeds,
            "road_type": road_types,
            "synthetic_cost_s": free_flow_cost / speed_multiplier,
        })
        cost_column = "synthetic_cost_s"
        dataset_source = "synthetic_demo"

    for column in ("length_m", "max_speed_kmh", cost_column):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["road_type_code"] = (
        df["road_type"].astype(str).str.lower().map(ROAD_TYPE_CODES).fillna(5).astype(int)
    )
    valid = (
        np.isfinite(df["length_m"])
        & np.isfinite(df["max_speed_kmh"])
        & np.isfinite(df[cost_column])
        & (df["length_m"] > 0)
        & (df["max_speed_kmh"] > 0)
        & (df[cost_column] > 0)
    )
    df = df.loc[valid].copy()
    if len(df) < 100:
        raise ValueError("At least 100 valid road-edge observations are required.")

    free_flow_cost = df["length_m"] / (df["max_speed_kmh"] / 3.6)
    y = (free_flow_cost / df[cost_column]).clip(0.2, 1.0)
    X = df[["length_m", "max_speed_kmh", "road_type_code"]]
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42,
    )

    model = HistGradientBoostingRegressor(
        max_iter=1000,
        learning_rate=0.03,
        max_leaf_nodes=31,
        random_state=42,
    )
    model.fit(X_train, y_train)
    predictions = model.predict(X_test)
    r2 = float(r2_score(y_test, predictions))
    rmse = float(np.sqrt(mean_squared_error(y_test, predictions)))

    output_dir = Path("models")
    output_dir.mkdir(exist_ok=True)
    model_path = output_dir / f"mac_traffic_model_{dataset_source}.pkl"
    joblib.dump(model, model_path)

    print(f"Training source: {dataset_source}")
    print(f"Samples: {len(df)}")
    print(f"Validation R²: {r2:.4f}")
    print(f"Validation RMSE: {rmse:.5f}")
    print(f"Saved model: {model_path}")


if __name__ == "__main__":
    main()
