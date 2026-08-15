"""Phase 1: Import OpenStreetMap (OSM) Batam Graph into Supabase pgRouting via Modal.

Run with Modal:
    modal run scripts/ingest_osm.py
"""

import modal

image = modal.Image.debian_slim().pip_install("osmnx", "psycopg2-binary", "shapely")
app = modal.App("osm-ingester", image=image)

@app.function(timeout=900)
def import_city_graph(city_name: str = "Batam, Indonesia", supabase_db_url: str = ""):
    import osmnx as ox
    import psycopg2
    import os

    if not supabase_db_url:
        supabase_db_url = os.environ.get("SUPABASE_DB_URL", "")

    if not supabase_db_url:
        print("Error: SUPABASE_DB_URL is missing.")
        return

    print(f"Downloading street network for {city_name}...")
    G = ox.graph_from_place(city_name, network_type="drive")
    G = ox.routing.add_edge_speeds(G)
    G = ox.routing.add_edge_travel_times(G)

    conn = psycopg2.connect(supabase_db_url)
    cursor = conn.cursor()

    print("Ingesting edges into Supabase public.road_network...")
    inserted_count = 0
    for u, v, k, data in G.edges(keys=True, data=True):
        length = data.get("length", 10.0)
        speed = data.get("speed_kph", 50.0)
        travel_time = data.get("travel_time", length / (speed * (1000 / 3600)))
        one_way = data.get("oneway", False)
        name = str(data.get("name", "Unnamed Road"))
        highway = str(data.get("highway", "unclassified"))

        geom_wkt = (
            data["geometry"].wkt
            if "geometry" in data
            else f"LINESTRING({G.nodes[u]['x']} {G.nodes[u]['y']}, {G.nodes[v]['x']} {G.nodes[v]['y']})"
        )

        reverse_cost = -1.0 if one_way else travel_time

        cursor.execute(
            """
            INSERT INTO public.road_network 
            (source, target, cost_s, reverse_cost_s, length_m, max_speed_kmh, road_name, road_type, geom)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, ST_GeomFromText(%s, 4326))
            """,
            (u, v, travel_time, reverse_cost, length, speed, name, highway, geom_wkt),
        )
        inserted_count += 1

    conn.commit()
    cursor.close()
    conn.close()
    print(f"Import complete! Successfully ingested {inserted_count} road network edges into Supabase.")

@app.local_entrypoint()
def main():
    import_city_graph.remote()
