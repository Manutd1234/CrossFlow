"""Manual rule-based pgRouting cost initializer on Modal.

This is intentionally not scheduled: ``train_traffic_model.py`` owns the one
hourly synchronization job. Scheduling both scripts made two jobs overwrite the
same ``cost_s`` values in an undefined order.

Run with Modal:
    modal run scripts/traffic_model.py
"""

import modal

image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "psycopg2-binary==2.9.12",
)
app = modal.App("traffic-static-baseline", image=image)

@app.function()
def apply_static_baseline_weights():
    import os
    import psycopg2

    db_url = os.environ.get("SUPABASE_DB_URL", "")
    if not db_url:
        print("SUPABASE_DB_URL not set in Modal environment.")
        return

    conn = psycopg2.connect(db_url)
    cursor = conn.cursor()

    cursor.execute("SELECT id, length_m, max_speed_kmh, road_type FROM public.road_network")
    rows = cursor.fetchall()

    updates = []
    for edge_id, length, max_speed, road_type in rows:
        congestion_factor = 0.4 if road_type == "motorway" else 0.8
        actual_speed = max_speed * congestion_factor
        new_cost_s = length / (actual_speed * (1000 / 3600))
        updates.append((new_cost_s, edge_id))

    cursor.executemany("UPDATE public.road_network SET cost_s = %s WHERE id = %s", updates)
    conn.commit()
    cursor.close()
    conn.close()
    print(f"Applied static baseline costs to {len(updates)} edges.")

@app.local_entrypoint()
def main():
    apply_static_baseline_weights.remote()
